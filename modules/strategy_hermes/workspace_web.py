"""Authenticated self-hosted workspace using committed local data."""
import argparse
from dataclasses import replace
from datetime import datetime
from http import HTTPStatus
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs

import psycopg
from shared_kernel.diagnostics import record as record_error

from . import actual_trades
from .ledger import encode
from .manual_api import (
    APIError, BASE, TOKEN, _body, application as manual_application,
    authorized, read_token,
)
from .manual_cases import copy_auto
from .manual_queue import ManualQueue
from .market_stream import make_workspace_server, market_application, read_market
from .rotation_chain import COUNTED_PHASES, archive_phase
from .simulator import CHINA, CODE, moment
from .temp_maintenance import TempMaintenance
from .workspace_readiness import eod_body, eod_reference, qq_brief, recent_evaluation, runtime_status
from .stage2.inputs import read_file, trading_days
from .stage2.left_right_refresh import load_archive
from .stage2.macro_background import read_background
from .stage2.penetration import number
from .stage2.postgres import PostgresLedger
from .stage2.settings import Settings
from .stage2.worker import SLOTS

API = "/api/v2/workspace"
ASSETS = {"":("index.html","text/html"), "/":("index.html","text/html"),
          "/app.js":("app.js","text/javascript"), "/style.css":("style.css","text/css"),
          "/data-updates.js":("data-updates.js","text/javascript"),
          "/data-updates.css":("data-updates.css","text/css"),
          "/template-layout.js":("template-layout.js","text/javascript"),
          "/template-layout.css":("template-layout.css","text/css")}
WEB = Path(__file__).resolve().parents[2] / "web"
SECURITY = [
    ("X-Content-Type-Options","nosniff"), ("Referrer-Policy","no-referrer"),
    ("Content-Security-Policy","default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"),
    ("Cache-Control","no-store"),
]


def _background(market):
    if not market:
        return None
    counts = dict(up=0,down=0,flat=0,unknown=0)
    leaders = []
    for row in market.get("sectors",[]):
        change = number(row.get("return_ratio"))
        bucket = "unknown" if change is None else "up" if change>0 else "down" if change<0 else "flat"
        counts[bucket] += 1
        if change is not None:
            leaders.append((change,dict(name=row.get("sector_name"),id=row.get("sector_id"),
                                        return_ratio=str(change),observed_at=row.get("observed_at"))))
    leaders.sort(key=lambda item:item[0],reverse=True)
    observed_amounts = [number(row.get("amount_cny")) for row in market.get("quotes", {}).values()
                        if isinstance(row, dict)]
    amounts = [amount for amount in observed_amounts if amount is not None and amount >= 0]
    return dict(snapshot_id=market.get("snapshot_id"),trade_date=market.get("trade_date"),
                observed_at=market.get("observed_at"),mode=market.get("mode"),source=market.get("source"),
                quality=market.get("quality") or {},counts=counts,
                etf_amount_cny=str(sum(amounts)) if amounts else None, etf_amount_count=len(amounts),
                leaders=[item[1] for item in leaders[:8]])


def _sector_names(row, source):
    """Map the sector ids of one stored report to the names its raw rows carry."""
    named, names = source.get("sectors"), {}
    for sid, sector in row.get("sectors", {}).items():
        index = sector.get("row")
        raw = named[index] if isinstance(named, list) and type(index) is int and 0 <= index < len(named) else None
        names[sid] = raw.get("sector_name") if isinstance(raw, dict) else None
    return names


def _named_rotation(conn, ns, rotation, loaded):
    """Add each active direction's name, from the report window or the report that last moved it."""
    names = {}

    def learn(row, source):
        for sid, name in _sector_names(row, source).items():
            names[sid] = names.get(sid) or name

    for _, row, source in loaded:
        learn(row, source)
    wanted = sorted({state["report_id"] for sid, state in rotation.items()
                     if not names.get(sid) and state.get("report_id")})
    if wanted:
        for (raw,) in conn.execute(
            "SELECT payload FROM events WHERE ns=%s AND kind='report' AND id=ANY(%s)", (ns, wanted),
        ).fetchall():
            row = json.loads(raw)
            learn(row, json.loads(row["raw"]))
    return {sid: dict(state, name=names.get(sid)) for sid, state in rotation.items()}


def _round_sectors(loaded, rotation):
    """Follow each sector of the latest report day through that day's rounds.

    `loaded` is the overview's report window, newest first. Phases and streaks are the
    ones the ledger committed with each report; a round that did not advance a direction
    keeps the state it had, and `active` is None where the window cannot tell.
    """
    if not loaded:
        return []
    outside = dict(active=False, phase=None, streak=0)

    def member(row):
        return dict(active=True, phase=archive_phase(row) or row["phase"], streak=row.get("streak", 0))

    def carried(row):
        return bool(row.get("duplicate_evidence")) and row.get("state") == "HOLD"

    day, states, sectors = loaded[0][0]["at"][:10], {}, {}
    for entry, row, source in loaded:
        if entry["at"][:10] != day:
            continue
        for sid, name in _sector_names(row, source).items():
            sectors.setdefault(sid, dict(id=sid, name=name, rounds={}))
    for _, row, _ in reversed(loaded):
        for sid, sector in row.get("sectors", {}).items():
            if sid in states:
                continue
            phase = archive_phase(sector)
            if carried(sector):
                states[sid] = member(sector)
            elif phase == "NEW":
                states[sid] = outside
            elif phase in COUNTED_PHASES and sector.get("streak", 0) > 1:
                states[sid] = dict(member(sector), streak=sector["streak"] - 1)
            elif phase in COUNTED_PHASES:
                states[sid] = dict(active=True, phase=None, streak=0)
            elif phase:
                states[sid] = None
    for sid in sectors:
        if sid not in states:
            held = rotation.get(sid)
            states[sid] = dict(active=True, phase=held["phase"], streak=held["streak"]) if held else outside
    for entry, row, source in reversed(loaded):
        before = dict(states)
        for sid, sector in row.get("sectors", {}).items():
            phase = archive_phase(sector)
            if phase == "RETREAT":
                states[sid] = outside
            elif phase or carried(sector):
                states[sid] = member(sector)
        if entry["at"][:10] != day or not entry["slot"]:
            continue
        facts = source.get("source_facts")
        for sid, shown in sectors.items():
            sector, fact = row.get("sectors", {}).get(sid), facts.get(sid) if isinstance(facts, dict) else None
            was, now = before.get(sid), states.get(sid)
            cell = dict(now or dict(active=None, phase=None, streak=0), covered=sector is not None,
                        was_active=was["active"] if was else None)
            if sector is not None:
                cell.update(judged=sector.get("declared_phase", sector["phase"]), advanced=archive_phase(sector),
                            skipped="DUPLICATE_EVIDENCE" if sector.get("duplicate_evidence") else sector.get("reason"),
                            reasons=sector.get("reasons", []),
                            change=fact.get("board_return_ratio") if isinstance(fact, dict) else None)
            shown["rounds"][entry["slot"]] = cell
    return list(sectors.values())


def _overview(ledger, fallback, calendar):
    from .workspace_archive import snapshot
    try:
        view = snapshot(ledger, calendar)
    except ValueError as exc:
        if str(exc) != "unknown replay namespace":
            raise
        view = {}
    with ledger._connect() as conn:
        rows = conn.execute(
            """SELECT id,at,payload FROM events WHERE ns=%s AND kind='report'
               ORDER BY at DESC,id DESC LIMIT 8""", (ledger.ns,),
        ).fetchall()
        reports,window,loaded = [],[],[]
        for rid,at,raw in rows:
            row = json.loads(raw)
            source = json.loads(row["raw"])
            if not reports:
                window = row.get("input_meta",{}).get("rotation_window",[])
            reports.append(dict(id=rid,at=at,status=row["status"],summary=source.get("summary"),
                                data_as_of=row.get("data_as_of"),slot=row.get("round_slot")))
            loaded.append((reports[-1],row,source))
        rotation = _named_rotation(conn,ledger.ns,view.get("rotation",{}),loaded)
    return dict(environment=ledger.settings.env,input_kind=ledger.origin,slots=SLOTS,
                reports=reports,round_sectors=_round_sectors(loaded,rotation),
                rotation_window=window,rotation=rotation,cycles=view.get("cycles",[]),
                evaluation=view.get("evaluation",{}), evaluation_recent=view.get("evaluation_recent"),
                new_cycle_rule=ledger._rules().snapshot(),
                market=_background(read_market(ledger,fallback)))


def _report_detail(ledger, report_id):
    with ledger._connect() as conn:
        row = conn.execute(
            """SELECT e.payload,o.state,o.attempts,o.delivered_at,o.error_code,o.delivery_receipt,o.payload
               FROM events e LEFT JOIN outbox o ON o.ns=e.ns AND o.report_id=e.id
                 AND o.event_key=%s AND o.target='octop'
               WHERE e.ns=%s AND e.kind='report' AND e.id=%s""",
            ("report:" + report_id, ledger.ns, report_id),
        ).fetchone()
    if row is None:
        raise APIError(404, "REPORT_NOT_FOUND")
    payload, state, *_ = row
    return json.loads(payload) | {"qq_brief": qq_brief(row[1:] if state is not None else None)}


def _drafts(ledger):
    with ledger._connect() as conn:
        rows=conn.execute(
            """SELECT d.id,d.parent_id,d.created_at,j.status
               FROM manual_drafts d LEFT JOIN manual_jobs j ON (j.ns,j.draft_id)=(d.ns,d.id)
               WHERE d.ns=%s ORDER BY d.created_at DESC LIMIT 50""", (ledger.ns,),
        ).fetchall()
    return [dict(id=rid,parent_id=parent,created_at=at.isoformat(),status=status or "DRAFT")
            for rid,parent,at,status in rows]


def _history(ledger, body):
    code = body["etf"]
    first, cutoff = moment(body["from"]),moment(body["cutoff"])
    if not CODE.fullmatch(code) or first >= cutoff:
        raise ValueError("invalid saved quote range")
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        row = conn.execute("SELECT config FROM runs WHERE ns=%s",(ledger.ns,)).fetchone()
        if row is None:
            raise APIError(404,"NO_LOCAL_HISTORY")
        config = json.loads(row[0])
        unit = config["instruments"].get(code)
        if unit is None:
            raise APIError(404,"NO_LOCAL_HISTORY")
        quotes = conn.execute(
            """SELECT payload FROM events WHERE ns=%s AND kind='quote' AND payload->>'etf'=%s
               AND at BETWEEN %s AND %s ORDER BY at,id LIMIT 4001""",
            (ledger.ns,code,first.isoformat(),cutoff.isoformat()),
        ).fetchall()
    if not quotes:
        raise APIError(404,"NO_LOCAL_HISTORY")
    if len(quotes) > 4000:
        raise APIError(413,"HISTORY_RANGE_TOO_LARGE")
    observed = [json.loads(row[0]) for row in quotes]
    price_mode = "HISTORICAL" if ledger.origin == "LIVE" else "ASSUMPTION"
    if price_mode == "ASSUMPTION":
        for quote in observed:
            quote["price_kind"] = "ASSUMED"
    return dict(schema_version="stock-v4-manual-v1",calendar=config["calendar"],cutoff=cutoff.isoformat(),
                instruments={code:unit},price_mode=price_mode,initial_cash="10000.00",nodes=[],quotes=observed,
                source=dict(kind="STAGE2_ARCHIVED_QUOTES",origin=ledger.origin,namespace=ledger.ns))


def _backfill_route(env, ledger, calendar):
    from .left_right_backfill import BackfillQueue, JobNotFound, QueueFull, RequestConflict
    queue = BackfillQueue(ledger, calendar)
    method, path = env["REQUEST_METHOD"], env["PATH_INFO"]
    if env.get("QUERY_STRING"):
        raise ValueError("backfill routes do not accept query parameters")
    try:
        if path == API + "/left-right-backfill":
            if method == "GET":
                return 200, queue.listing()
            if method == "POST":
                return 202, queue.submit(_body(env, allowed={"request_id", "mode", "date_from", "date_to", "scope", "codes"}))
        if path == API + "/left-right-backfill/cancel" and method == "POST":
            return 200, queue.cancel(_body(env, allowed={"job_id"})["job_id"])
    except RequestConflict:
        raise APIError(409, "BACKFILL_REQUEST_CONFLICT") from None
    except QueueFull:
        raise APIError(429, "BACKFILL_QUEUE_FULL") from None
    except JobNotFound:
        raise APIError(404, "BACKFILL_JOB_NOT_FOUND") from None
    raise APIError(404, "NOT_FOUND")


def _route(env, ledger, queue, calendar, fallback, touch_file, eod_file, paused, maintenance, macro_file, mapping_file=None,
           lr_history_file=None, hot_history_file=None):
    method,path=env["REQUEST_METHOD"],env["PATH_INFO"]
    if path == API+"/data-coverage":
        if method != "GET":
            raise APIError(404, "NOT_FOUND")
        from .data_coverage import read_calendar
        try:
            return 200, read_calendar(ledger, calendar, env.get("QUERY_STRING", ""))
        except ValueError as exc:
            if str(exc) == "INVALID_COVERAGE_QUERY":
                raise APIError(400, "INVALID_COVERAGE_QUERY") from None
            raise
    if path in (API+"/data-updates", API+"/data-update-jobs"):
        from . import data_updates
        from .left_right_backfill import QueueFull, RequestConflict
        try:
            if method == "GET" and path.endswith("/data-update-jobs"):
                return 200, data_updates.jobs(ledger, env.get("QUERY_STRING", ""))
            if env.get("QUERY_STRING"):
                raise data_updates.UpdateError("INVALID_UPDATE_REQUEST")
            if path.endswith("/data-updates"):
                if method == "GET":
                    return 200, data_updates.snapshot(ledger, calendar)
                if method == "POST":
                    return 202, data_updates.submit(ledger, calendar, _body(env,
                        allowed={"request_id", "mode", "date_from", "date_to", "month", "year"}))
        except data_updates.UpdateError as exc:
            raise APIError(400, str(exc)) from None
        except RequestConflict:
            raise APIError(409, "BACKFILL_REQUEST_CONFLICT") from None
        except QueueFull:
            raise APIError(429, "BACKFILL_QUEUE_FULL") from None
        raise APIError(404, "NOT_FOUND")
    if path in (API+"/left-right-backfill", API+"/left-right-backfill/cancel"):
        return _backfill_route(env, ledger, calendar)
    if method=="GET" and path==API+"/overview":
        return 200,_overview(ledger,fallback,calendar)
    if method=="GET" and path==API+"/status":
        return 200,runtime_status(ledger,paused(),calendar,maintenance=maintenance)
    if method=="GET" and path==API+"/macro-background":
        from .stage2.macro_refresh import load
        return 200,{"background":load(ledger) or read_background(macro_file)}
    if method=="GET" and path==API+"/market-panorama":
        from .stage2.market_panorama import read_panorama
        return 200,read_panorama(ledger,macro_file)
    if method=="GET" and path==API+"/sector-etf-info":
        from .sector_etf_info import read_info
        return 200,read_info(ledger,env.get("QUERY_STRING",""),fallback,mapping_file)
    if method=="GET" and path==API+"/left-right-info":
        from .left_right_info import read_info
        return 200,read_info(ledger,env.get("QUERY_STRING",""),eod_file,lr_history_file)
    if method=="GET" and path==API+"/left-right-history":
        from .left_right_views import read_score_history
        return 200,read_score_history(ledger,env.get("QUERY_STRING",""),eod_file,lr_history_file)
    if method=="GET" and path==API+"/hot-sectors-info":
        from .hot_sectors_info import read_info
        return 200,read_info(ledger,env.get("QUERY_STRING",""),eod_file=eod_file,
                             lr_history_file=lr_history_file,hot_history_file=hot_history_file)
    if method=="GET" and path==API+"/left-right":
        saved=load_archive(ledger)
        current=eod_body(saved,calendar) if saved else None
        archived=eod_reference(eod_file,calendar)
        if current and (not archived or current["trade_date"]>=archived["trade_date"]):
            return 200,{"reference":current}
        return 200,{"reference":archived}
    if method=="GET" and path==API+"/report":
        report_id,=parse_qs(env.get("QUERY_STRING",""),max_num_fields=4)["id"]
        return 200,_report_detail(ledger,report_id)
    if method=="GET" and path==API+"/drafts":
        return 200,{"drafts":_drafts(ledger)}
    if method=="GET" and path==API+"/archive":
        from .workspace_archive import archive
        return 200,archive(ledger,env.get("QUERY_STRING",""))
    if method=="GET" and path==API+"/cycle":
        from .workspace_archive import cycle_detail
        identifier,=parse_qs(env.get("QUERY_STRING",""),max_num_fields=1)["id"]
        return 200,cycle_detail(ledger,identifier)
    if method=="GET" and path==API+"/touch":
        if not touch_file:
            return 200,{"result":None}
        try:
            artifact=read_file(touch_file)
        except FileNotFoundError:
            return 200,{"result":None}
        prices=artifact["input"].get("prices",[])
        frozen=any(str(row.get("source","")).startswith("frozen:") for row in prices)
        return 200,{"result":artifact["result"],"frozen":frozen}
    if method=="POST" and path==API+"/history":
        return 200,_history(ledger,_body(env,allowed={"etf","from","cutoff"}))
    if method=="POST" and path==API+"/preview":
        from .manual_executor import preview
        return 200,preview(_body(env,allowed={"bundle"})["bundle"])
    if method=="POST" and path==API+"/copy-auto":
        body=_body(env,allowed={"cycle_id"})
        return 201,{"id":queue.create(copy_auto(ledger,body["cycle_id"]))}
    if path==API+"/trades":
        if method=="GET":
            return 200,actual_trades.read(ledger,calendar,datetime.now(CHINA))
        if method=="POST":
            body=_body(env,allowed={"request_id","etf","side","price_cny","quantity","fees_cny","executed_at"})
            record,created=actual_trades.record(ledger,body)
            return (201 if created else 200),{"record":record}
    raise APIError(404,"NOT_FOUND")


def application(ledger,token,*,calendar=(),fallback=None,touch_file=None,eod_file=None,paused=lambda: False,
                maintenance=None,macro_file=None,mapping_file=None,lr_history_file=None,hot_history_file=None):
    if not TOKEN.fullmatch(token):
        raise ValueError("invalid API token")
    expected=("Bearer "+token).encode("ascii")
    queue=ManualQueue(ledger)
    backtests=manual_application(queue,token,paused=paused,export_manager=maintenance.manager if maintenance else None)

    def handle(env,start_response):
        path,method=env["PATH_INFO"],env["REQUEST_METHOD"]
        if path.startswith(BASE+"/"):
            return backtests(env,start_response)
        kind="application/json"
        try:
            if method=="GET" and path in ASSETS:
                file,kind=ASSETS[path]
                status,raw=200,(WEB/file).read_bytes()
            elif method=="GET" and path=="/favicon.ico":
                status,raw=204,b""
            elif method=="GET" and path=="/health":
                with ledger._connect() as conn:
                    conn.execute("SELECT 1")
                status,raw=200,b'{"status":"OK"}'
            else:
                if not authorized(env,expected):
                    raise APIError(401,"AUTH_REQUIRED")
                if method=="POST" and path!=API+"/history" and paused():
                    raise APIError(503,"RELEASE_PAUSED")
                status,data=_route(env,ledger,queue,calendar,fallback,touch_file,eod_file,paused,maintenance,macro_file,mapping_file,
                                   lr_history_file,hot_history_file)
                raw=encode(data).encode()
        except APIError as exc:
            status,raw=exc.status,encode({"error":exc.code}).encode()
        except actual_trades.FillConflict:
            status,raw=409,b'{"error":"FILL_REQUEST_CONFLICT"}'
        except (ValueError,KeyError,TypeError,ArithmeticError,RecursionError):
            status,raw=400,b'{"error":"INVALID_REQUEST"}'
        except (OSError,psycopg.Error) as exc:
            status,raw=503,encode(record_error("workspace", "WORKSPACE_UNAVAILABLE", exc)).encode()
        headers=SECURITY+[("Content-Type",kind+"; charset=utf-8"),("Content-Length",str(len(raw)))]
        if status==401:
            headers=headers+[("WWW-Authenticate","Bearer")]
        start_response(f"{status} {HTTPStatus(status).phrase}",headers)
        return [raw]
    return market_application(handle,ledger,fallback,expected)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",required=True,type=Path)
    parser.add_argument("--token-file",required=True,type=Path)
    parser.add_argument("--namespace")
    parser.add_argument("--calendar",type=Path)
    parser.add_argument("--market-file",type=Path)
    parser.add_argument("--touch-file",type=Path)
    parser.add_argument("--eod-file",type=Path)
    parser.add_argument("--macro-file",type=Path)
    parser.add_argument("--mapping-file",type=Path)
    parser.add_argument("--lr-history-file",type=Path)
    parser.add_argument("--hot-history-file",type=Path)
    parser.add_argument("--port",type=int,default=18084)
    args=parser.parse_args(argv)
    try:
        settings=Settings.read(args.env_file)
        if args.namespace:
            if not args.namespace.startswith(settings.redis_prefix) or len(args.namespace)>120:
                raise ValueError("namespace outside configured environment")
            settings=replace(settings,namespace=args.namespace)
        ledger=PostgresLedger(settings,"FROZEN" if settings.env=="test" else "LIVE")
        maintenance=TempMaintenance.from_environment()
        app=application(ledger,read_token(args.token_file),calendar=trading_days(args.calendar) if args.calendar else (),
                        fallback=args.market_file,touch_file=args.touch_file,eod_file=args.eod_file,
                        maintenance=maintenance,macro_file=args.macro_file,mapping_file=args.mapping_file,
                        lr_history_file=args.lr_history_file,hot_history_file=args.hot_history_file)
        with make_workspace_server("127.0.0.1",args.port,app) as server:
            server.timeout=0.5
            while True:
                if maintenance:
                    maintenance.tick()
                server.handle_request()
    except KeyboardInterrupt:
        return 0
    except (ValueError,KeyError,OSError) as exc:
        print(f"workspace web: {type(exc).__name__}",file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
