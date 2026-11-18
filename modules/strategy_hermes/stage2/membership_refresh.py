"""Bounded low-frequency verified board-member refresh; no ETF product mutation."""
import argparse
from copy import deepcopy
from dataclasses import replace
from collections import defaultdict
import re
from datetime import date, datetime
import json
import os
from pathlib import Path

from shared_kernel.temp_manager import TempManager, store_file
from ..ledger import encode, fingerprint
from ..simulator import CHINA, moment
from .build_sources import membership, stock_identity
from .bulk_collect import RequestLog
from .discovery import select_directions
from .inputs import mapping_file
from .market_bootstrap import ProbeError, capture_request, fetch, normalize_kaipanla, save
from .postgres import PostgresLedger
from .settings import Settings

MAX_NEW_SECTORS = 3
MAX_PAGES = 8


def _daily_targets(store, proposed, now):
    """Freeze the daily discovery plan so retries/fallback dates cannot expand it."""
    day = now.astimezone(CHINA).date().isoformat()
    plan = RequestLog(store, "members-plan:" + fingerprint(store.ns)[:16] + ":" + day)
    return plan.call("membership-plan", [day],
                     lambda: {"targets": proposed[:MAX_NEW_SECTORS]}, limit=1)["targets"]


def _page(store, captures, sid, day, offset):
    # A daytime repair may fetch a completed historical session, never an
    # unfinished same-day snapshot from the configured historical endpoint.
    now = datetime.now(CHINA)
    source_day = date.fromisoformat(day)
    if source_day > now.date() or (source_day == now.date() and now.hour < 15):
        raise ProbeError('membership requires a completed source day', kind='DATE_NOT_COMPLETED',
                         details={'expected_date': day})
    request, scope = capture_request(captures, "046",
                                    {"Date": day, "PlateID": sid.split(":")[1], "Index": str(offset)})
    raw = fetch(request)
    received = datetime.now(CHINA)
    body, count, source = normalize_kaipanla(raw, "046", scope, received)
    total = body.get("reported_total")
    if type(total) is not int or total < offset + count or count > int(scope["st"]):
        raise ProbeError("membership population or page size changed", kind='PAGE_MISMATCH',
                         details={'offset': offset, 'row_count': count,
                                  'reported_total': total, 'page_size': int(scope['st'])})
    return save(store, "kaipanla", "046", scope, body, count, source, received) | {
        "total": total, "count": count, "page_size": int(scope["st"]),
    }


def refresh(store, mapping, market, day, captures, *, now=None):
    now = now or datetime.now(CHINA)
    if (day > market["trade_date"] or day > now.date().isoformat()
            or (day == now.date().isoformat() and now.hour < 15)):
        raise ValueError("membership refresh requires a completed source trading day")
    boards = {row["sector_id"]: row for row in market.get("sectors", [])}
    selected, discovery = select_directions(boards, {}, [], now)
    # select_directions requires same-day observations. For a preceding completed day
    # use its actual last stored market timestamp, never a fabricated quote timestamp.
    if not selected:
        selected, discovery = select_directions(boards, {}, [], moment(market["observed_at"]))
    ranked = sorted(selected, key=lambda sid: (-float(boards[sid]["return_ratio"]), sid))
    proposed = [sid for sid in ranked if sid not in mapping["sectors"]][:MAX_NEW_SECTORS]
    targets = _daily_targets(store, proposed, now)
    targets = [sid for sid in targets if sid not in mapping["sectors"] and sid in boards]
    result = deepcopy(mapping)
    statuses, additions = {}, {}
    with store._connect() as conn:
        row = conn.execute("SELECT batch_id FROM supplier_batches WHERE api='stock_basic' "
                           "ORDER BY collected_at DESC LIMIT 1").fetchone()
        if row is not None:
            identities, _ = stock_identity(conn, row[0])
            identity_ref = row[0]
            identity_at = conn.execute("SELECT collected_at FROM supplier_batches WHERE batch_id=%s",
                                       (identity_ref,)).fetchone()[0]
        else:
            source = conn.execute("SELECT batch_id,body,collected_at FROM supplier_market_batches "
                                  "WHERE provider='tickflow' AND endpoint='stock_universe_raw' "
                                  "ORDER BY collected_at DESC LIMIT 1").fetchone()
            if source is None:
                return result, {"status": "IDENTITIES_UNAVAILABLE", "sectors": {}, "requests": 0}
            identities = defaultdict(list)
            identity_ref, raw, identity_at = source
            for row in json.loads(raw).get("data", []):
                code = row.get("symbol")
                if isinstance(code, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", code):
                    if code not in identities[code[:6]]:
                        identities[code[:6]].append(code)
    budget = RequestLog(store, "members:" + fingerprint(store.ns)[:16] + ":" + day)
    for sid in targets:
        offset, total = 0, None
        try:
            for _ in range(MAX_PAGES):
                page = budget.call("kaipanla:046", [day, sid, offset],
                    lambda: _page(store, captures, sid, day, offset),
                    limit=MAX_NEW_SECTORS * MAX_PAGES)
                if total is not None and page["total"] != total:
                    raise ProbeError("membership total changed")
                total = page["total"]
                offset += page["count"]
                if offset >= total or page["count"] < page["page_size"]:
                    break
            with store._connect() as conn:
                members = membership(conn, day, sid.split(":")[1], identities)
            if members["unresolved_count"]:
                statuses[sid] = {"status": "IDENTITIES_UNRESOLVED", "count": members["unresolved_count"]}
                continue
            with store._connect() as conn:
                last_received = conn.execute("SELECT max(collected_at) FROM supplier_market_batches "
                                             "WHERE batch_id=ANY(%s)", (members["source_batches"],)).fetchone()[0]
            available_at = max(last_received, identity_at).astimezone(CHINA)
            members.update(sector_name=boards[sid].get("sector_name") or sid,
                           available_at=available_at.isoformat(), identity_source_batch=identity_ref)
            additions[sid] = members
            statuses[sid] = {"status": "READY", "members": len(members["members"])}
        except (ProbeError, OSError, ValueError, KeyError, TypeError) as exc:
            statuses[sid] = {"status": "UNAVAILABLE", "error_type": type(exc).__name__}
    if additions:
        result["sectors"].update(additions)
        result["parent_mapping_version"] = mapping["version"]
        result["version"] = "hybrid-members-" + fingerprint([mapping["version"], additions])[:24]
    return result, dict(status="UPDATED" if additions else "UNCHANGED", sectors=statuses,
                        requests=budget.new_calls, daily_targets=targets, discovery=discovery)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--namespace")
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--day", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings.read(args.env_file)
    if args.namespace:
        if not args.namespace.startswith(settings.redis_prefix):
            parser.error("namespace must remain in the selected environment")
        settings = replace(settings, namespace=args.namespace)
    output = args.output_dir
    if (not output.is_absolute() or output.resolve() != output or not output.is_dir()
            or output.stat().st_mode & 0o077):
        parser.error("output must be an existing private absolute directory")
    store = PostgresLedger(settings, "FROZEN" if settings.env == "test" else "LIVE")
    with store._connect() as conn:
        row = conn.execute("SELECT body FROM snapshot_state WHERE ns=%s AND dataset='market'",
                           (store.ns,)).fetchone()
    if row is None:
        raise ValueError("shared market unavailable")
    mapping, status = refresh(store, mapping_file(args.mapping), json.loads(row[0]),
                              args.day.isoformat(), args.captures)
    manager = TempManager(os.environ["APP_TMP_ROOT"], persistent=output)
    path = store_file(manager, output, mapping["version"] + ".json", encode(mapping).encode())
    print(encode(dict(path=path, result=status)))


if __name__ == "__main__":
    main()
