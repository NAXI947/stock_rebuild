"""Print a read-only audit of one trading day from the current worker's ledger."""

import argparse
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path

import psycopg
from redis.exceptions import RedisError

from modules.strategy_hermes.ledger import fingerprint
from modules.strategy_hermes.simulator import CHINA, moment
from modules.strategy_hermes.stage2.inputs import cache_client
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings
from modules.strategy_hermes.stage2.worker import SLOTS, planned_at


def cst(value):
    if value is None:
        return None
    return (value.astimezone(CHINA) if isinstance(value, datetime) else moment(value)).isoformat()


def receipt_fields(raw, allowed):
    receipt = json.loads(raw) if isinstance(raw, str) else raw
    return {key: receipt[key] for key in allowed if key in (receipt or {})}


def missing_state(planned, now, open_day):
    if open_day is False:
        return "MARKET_CLOSED"
    if now < planned:
        return "UPCOMING"
    return "MISSING" if now >= planned + timedelta(minutes=8) else "AWAITING_CLAIM"


def audit(ledger, day, now, *, open_day=None, qq_enabled=None):
    scope = "prod:" + fingerprint(ledger.ns)[:24] if ledger.settings.env == "production" else ledger.ns
    request_keys = {f"{scope}:feed:{day}:{slot.replace(':', '')}": slot for slot in SLOTS}
    start = datetime.combine(day, time.min, tzinfo=CHINA)
    end = start + timedelta(days=1)
    acquisitions, deliveries, report_orders = defaultdict(list), defaultdict(dict), defaultdict(dict)
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        tasks = {row[0]: row[1:] for row in conn.execute(
            """SELECT t.slot,t.state,t.error_code,t.attempts,t.calls,t.report_id,e.payload
               FROM round_tasks t LEFT JOIN events e ON e.ns=t.ns AND e.id=t.report_id AND e.kind='report'
               WHERE t.ns=%s AND t.trade_date=%s""", (ledger.ns, day),
        )}
        for key, source, status, started, finished, raw in conn.execute(
            """SELECT run_key,source,status,started_at,finished_at,result
               FROM collection_requests WHERE run_key=ANY(%s::text[])
               ORDER BY started_at,source,request_key""", (list(request_keys),),
        ):
            batch = json.loads(raw) if raw else {}
            acquisitions[request_keys[key]].append(dict(
                source=source, state=status, started_at=cst(started), finished_at=cst(finished),
                observed_at=cst(batch.get("source_clock")), rows=batch.get("rows"),
                batch_id=batch.get("batch"), error=batch.get("error"),
            ))
        for slot, target, status, attempts, delivered, error, receipt in conn.execute(
            """SELECT t.slot,o.target,o.state,o.attempts,o.delivered_at,o.error_code,o.delivery_receipt
               FROM round_tasks t JOIN outbox o ON o.ns=t.ns AND o.report_id=t.report_id
                    AND o.event_key='report:' || t.report_id
               WHERE t.ns=%s AND t.trade_date=%s""", (ledger.ns, day),
        ):
            name = "qq" if target == "octop" else target
            deliveries[slot][name] = dict(
                state=status, attempts=attempts, delivered_at=cst(delivered), error=error,
                receipt=receipt_fields(receipt, ("status", "http_status", "message_posts")),
            )
        for slot, action, count in conn.execute(
            """SELECT t.slot,o.action,count(*) FROM round_tasks t JOIN simulation_orders o
                    ON o.ns=t.ns AND o.evidence->>'report_id'=t.report_id
               WHERE t.ns=%s AND t.trade_date=%s GROUP BY t.slot,o.action""", (ledger.ns, day),
        ):
            report_orders[slot][action] = count
        orders, fills, actual = conn.execute(
            """SELECT (SELECT count(*) FROM simulation_orders WHERE ns=%s AND requested_at >= %s AND requested_at < %s),
                      (SELECT count(*) FROM simulation_fills WHERE ns=%s AND filled_at >= %s AND filled_at < %s),
                      (SELECT count(*) FROM actual_trades WHERE ns=%s AND executed_at >= %s AND executed_at < %s)""",
            (ledger.ns, start, end) * 3,
        ).fetchone()
        latest = conn.execute(
            """SELECT observed_at,revision,body->>'snapshot_id',body->>'source',body->'quality'
               FROM snapshot_state WHERE ns=%s AND dataset='market'""", (ledger.ns,),
        ).fetchone()
    rounds = []
    for slot in SLOTS:
        planned = planned_at(day.isoformat(), slot)
        default = missing_state(planned, now, open_day)
        status, error, attempts, calls, report_id, payload = tasks.get(slot, (default, None, 0, "{}", None, None))
        report = json.loads(payload) if payload else {}
        metadata = report.get("input_meta", {})
        details = metadata.get("octop", {})
        mx = details.get("mx") or {}
        records = acquisitions[slot]
        by_source = {}
        for row in records:
            counts = by_source.setdefault(row["source"], dict(attempts=0, completed=0, rows=0))
            counts["attempts"] += 1
            counts["completed"] += row["state"] == "SAVED"
            counts["rows"] += row["rows"] or 0
        qq_default = "DISABLED" if qq_enabled is False else (
            "NO_SIMULATION_ORDERS" if report_id and not report_orders[slot] else "NOT_QUEUED")
        rounds.append(dict(
            slot=slot, planned_at=cst(planned), state=status, error=error,
            attempts=attempts, request_budget=json.loads(calls), report_id=report_id,
            published_at=cst(report.get("at")), report_status=report.get("status"),
            data_as_of=cst(report.get("data_as_of")), source_observed_at=cst(metadata.get("source_observed_at")),
            snapshot_id=details.get("market_snapshot_id"), mapping_version=metadata.get("mapping_version"),
            phases={key: row.get("phase") for key, row in report.get("sectors", {}).items()},
            candidates=len(report.get("candidates", {})), simulation_orders=report_orders[slot],
            octop=receipt_fields(details.get("receipt"), ("status", "requests", "http_status", "configured_model")),
            mx=dict(status=mx.get("status"), requests=mx.get("requests"), http_status=mx.get("http_status"),
                    observed_at=cst(mx.get("observed_at")), rows=len(mx.get("items", []))),
            acquisition=by_source, acquisition_records=records,
            redis=deliveries[slot].get("redis", {"state": "NOT_QUEUED"}),
            qq=deliveries[slot].get("qq", {"state": qq_default}),
        ))
    prefix = ledger.settings.redis_prefix
    if ledger.origin == "FROZEN" or ledger.ns != prefix + "baseline":
        prefix = ledger.ns + ":"
    try:
        with cache_client(ledger.settings) as cache:
            shared_views = cache.exists(*(prefix + name for name in (
                "snap:hermes:watermark", "snap:hermes:penetration_board", "snap:hermes:cycle_positions",
                "snap:hermes:accuracy_stats", "snap:sector:chain_1_5d",
            )))
    except (OSError, RedisError):
        shared_views = None
    market = None
    if latest:
        at, revision, snapshot_id, source, quality = latest
        market = dict(observed_at=cst(at), revision=revision, snapshot_id=snapshot_id,
                      source=source, quality=json.loads(quality) if quality else {})
    return dict(
        schema_version="stock-v4-production-live-audit-v1", environment=ledger.settings.env,
        namespace=ledger.ns, trade_date=day.isoformat(), checked_at=cst(now), exchange_open=open_day,
        latest_shared_market=market, redis_views=shared_views, round_states=dict(Counter(row["state"] for row in rounds)),
        day_totals=dict(simulation_orders=orders, simulation_fills=fills, actual_trades=actual), rounds=rounds,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=date.fromisoformat, default=datetime.now(CHINA).date())
    parser.add_argument("--env-file", type=Path, help="isolated development/test fixture configuration")
    parser.add_argument("--namespace", help="isolated development/test audit namespace")
    args = parser.parse_args()
    try:
        settings = Settings.read(args.env_file) if args.env_file else Settings.from_values(os.environ)
        if args.namespace:
            if settings.env == "production" or not args.namespace.startswith(settings.redis_prefix):
                parser.error("namespace overrides require an isolated development or test environment")
            settings = replace(settings, namespace=args.namespace)
        ledger = PostgresLedger(settings, "FROZEN" if settings.env == "test" else "LIVE")
        calendar_path = os.environ.get("APP_CALENDAR_FILE")
        open_day = None
        if calendar_path:
            from modules.strategy_hermes.stage2.inputs import read_file, trading_days

            days = trading_days(calendar_path)
            calendar = read_file(calendar_path)
            if args.day.isoformat() in calendar["days"]:
                open_day = args.day.isoformat() in days
        qq = os.environ.get("ENABLE_QQ_PUSH", "").lower()
        data = audit(ledger, args.day, datetime.now(CHINA), open_day=open_day,
                     qq_enabled={"true": True, "false": False}.get(qq))
    except (OSError, ValueError, KeyError, TypeError, psycopg.Error) as exc:
        parser.exit(1, f"Audit failed ({type(exc).__name__}).\n")
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
