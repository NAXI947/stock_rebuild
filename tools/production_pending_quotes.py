"""Inspect pending simulation quotes and optionally publish one shared late-session sample."""
import argparse
from datetime import datetime, time
import json
import os

import psycopg
from redis.exceptions import RedisError

from modules.strategy_hermes.ledger import fingerprint
from modules.strategy_hermes.runtime import _enabled, _file, _ledger, _paused
from modules.strategy_hermes.signal_cycle import pending
from modules.strategy_hermes.simulator import Calendar, CHINA, eligible_lots, moment
from modules.strategy_hermes.stage2.build_sources import publish_snapshot
from modules.strategy_hermes.stage2.bulk_collect import RequestLog, quote_pool
from modules.strategy_hermes.stage2.inputs import SharedMarket, cache_client, document, mapping_file, trading_days
from modules.strategy_hermes.stage2.intraday import assemble
from modules.strategy_hermes.stage2.market_bootstrap import ProbeError, tickflow_config
from modules.strategy_hermes.stage2.penetration import instrument_units

WINDOW_START = time(14, 45)
WINDOW_END = time(14, 55)
SOURCE = "tickflow:quotes"


def within_window(now):
    return WINDOW_START <= now.time() < WINDOW_END


def eligible_orders(cycles, units, calendar, now):
    if now.date() not in calendar.days:
        return {}
    result = {}
    for cycle in cycles:
        code = cycle["etf"]
        order = pending(cycle)
        if code not in units or order is None:
            continue
        requested = moment(order["requested_at"])
        if requested >= now:
            continue
        if order["action"] == "BUY":
            if cycle["status"] != "WAIT_BUY" or calendar.next(now.date()) is None:
                continue
        elif (order["action"] not in ("REDUCE", "EXIT") or not order["remaining"]
              or not eligible_lots(cycle["book"], now.date())):
            continue
        result[code] = requested
    return result


def missing_prices(orders, observed, calendar, now):
    return sorted(code for code, requested in orders.items() if not any(
        requested < at <= now and at.date() == now.date() and calendar.legal(at)
        for at in observed.get(code, ())
    ))


def quote_times(market, units, now):
    if not market or market.body["trade_date"] != now.date().isoformat():
        return []
    quotes, _ = market.quote_events(units, now)
    return [(quote["etf"], moment(quote["observed_at"])) for quote in quotes if quote["tradeable"]]


def inspect(ledger, cache, key, calendar, units, now, run_key):
    with ledger._connect() as db, db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cycles = [document(raw) for raw, in db.execute(
            "SELECT body FROM cycles WHERE ns=%s AND status IN ('WAIT_BUY','OPEN','WAIT_EXIT')", (ledger.ns,)
        )]
        orders = eligible_orders(cycles, units, calendar, now)
        saved = db.execute("SELECT body FROM snapshot_state WHERE ns=%s AND dataset='market'", (ledger.ns,)).fetchone()
        recorded = db.execute("""SELECT status FROM collection_requests
            WHERE run_key=%s AND source=%s LIMIT 1""", (run_key, SOURCE)).fetchone()
        rows = db.execute("""
            SELECT payload->>'etf', max(payload->>'observed_at') FROM events
            WHERE ns=%s AND kind='quote' AND payload->>'etf'=ANY(%s::text[])
              AND payload->>'tradeable'='true' AND left(payload->>'observed_at',10)=%s
            GROUP BY 1
        """, (ledger.ns, list(orders), now.date().isoformat())).fetchall() if orders else []
    persisted = SharedMarket.read(document(saved[0])) if saved else None
    try:
        raw = cache.get(key)
    except RedisError:
        raw = None
    cached = SharedMarket.read(document(raw)) if raw else None
    previous = max((item for item in (persisted, cached) if item is not None),
                   key=lambda item: (moment(item.observed_at), item.revision), default=None)
    observed = {code: [moment(at)] for code, at in rows}
    for item in (persisted, cached):
        for code, at in quote_times(item, orders, now):
            observed.setdefault(code, []).append(at)
    needed = missing_prices(orders, observed, calendar, now)
    return previous, orders, needed, recorded[0] if recorded else None


def sample(ledger, cache, key, calendar, units, config, paused, *, apply, clock):
    now = clock()
    day = now.date().isoformat()
    scope = "prod:" + fingerprint(ledger.ns)[:24]
    run_key = f"{scope}:feed:{day}:pending-quotes"
    result = {"trade_date": day, "observed_at": now.isoformat(timespec="seconds"),
              "mode": "APPLY" if apply else "PREVIEW", "physical_requests": 0,
              "run_key": run_key}

    def check(at):
        return (at.date().isoformat() == day and at.date() in calendar.days
                and within_window(at) and not paused())

    with ledger._connect() as lock:
        if apply:
            acquired, = lock.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                                     (ledger.ns + ":intraday-collector",)).fetchone()
            if not acquired:
                return result | {"status": "COLLECTOR_BUSY"}
        previous, orders, needed, recorded = inspect(ledger, cache, key, calendar, units, now, run_key)
        result.update(eligible_orders=len(orders), needing_quote=needed, previous_attempt=recorded)
        if not check(clock()):
            return result | {"status": "CLOSED_OR_PAUSED"}
        if not needed:
            return result | {"status": "NO_PENDING_PRICE"}
        if recorded:
            return result | {"status": "ALREADY_ATTEMPTED"}
        if not apply:
            return result | {"status": "READY"}

        tickflow_config(config)
        budget = RequestLog(ledger, run_key)
        physical_requests = 0

        def collect():
            nonlocal physical_requests
            if not check(clock()):
                raise ProbeError("sampling window closed")
            physical_requests += 1
            return quote_pool(ledger, config)

        try:
            tick = budget.call(SOURCE, ["CN_ETF"], collect, limit=1)
        except ProbeError as exc:
            return result | {"status": "COLLECTION_FAILED", "error": str(exc), "physical_requests": physical_requests}

        # Keep the latest shared data if its publisher advanced while this request was in flight.
        current = clock()
        latest, orders, needed, _ = inspect(ledger, cache, key, calendar, units, current, run_key)
        market = assemble(ledger, day, latest or previous, tick, [])
        result.update(physical_requests=physical_requests, supplier_batch=tick["batch"])
        if paused() or current.date().isoformat() != day:
            return result | {"status": "PAUSED_AFTER_COLLECTION"}
        if market is None or not needed:
            return result | {"status": "NO_NEW_PENDING_PRICE"}
        quote_market = SharedMarket.read(market)
        fresh = {}
        for code, at in quote_times(quote_market, needed, current):
            fresh.setdefault(code, []).append(at)
        covered = sorted(set(needed) - set(missing_prices(
            {code: orders[code] for code in needed}, fresh, calendar, current
        )))
        if not covered:
            return result | {"status": "NO_NEW_PENDING_PRICE"}
        if paused():
            return result | {"status": "PAUSED_AFTER_COLLECTION"}
        if latest and latest.body["trade_date"] == day:
            market["quality"]["board_reported_total"] = latest.body.get("quality", {}).get("board_reported_total")
        try:
            _, cached, snapshot_id = publish_snapshot(ledger, cache, market, key=key)
        except RedisError:
            # The publication commits PostgreSQL before updating Redis.
            cached, snapshot_id = False, market["snapshot_id"]
        return result | {"status": "PUBLISHED", "cache_available": cached,
                         "snapshot_id": snapshot_id, "covered": covered}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="allow one bounded TickFlow request")
    args = parser.parse_args(argv)
    try:
        ledger = _ledger()
        if ledger.settings.env != "production" or not _enabled("ENABLE_HERMES_SCHEDULE"):
            raise ValueError("requires the production Hermes worker environment")
        key = os.environ.get("APP_SNAPSHOT_KEY", "")
        if not key.startswith(ledger.settings.redis_prefix) or _file("APP_MARKET_FILE"):
            raise ValueError("requires the worker's production shared Redis key")
        config = _file("APP_TICKFLOW_CONFIG")
        if args.apply and (config is None or not _enabled("ENABLE_OFFICIAL_COLLECTION")):
            raise ValueError("requires the worker's authorized TickFlow configuration")
        calendar = Calendar(trading_days(_file("APP_CALENDAR_FILE")))
        mapping = mapping_file(_file("APP_MAPPING_FILE"))
        clock = lambda: datetime.now(CHINA)
        units = instrument_units(mapping, clock())
        with cache_client(ledger.settings) as cache:
            result = sample(ledger, cache, key, calendar, units, config, _paused(), apply=args.apply, clock=clock)
        print(json.dumps(result, ensure_ascii=False))
        return int(result["status"] == "COLLECTION_FAILED")
    except (psycopg.Error, RedisError, OSError, ValueError, KeyError, TypeError, ProbeError) as exc:
        print(json.dumps({"status": "ERROR", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
