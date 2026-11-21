"""Bounded quote follow-up for OCTOP rounds, on the existing worker.

This is an execution stage of a round, not another analysis clock. The request
journal survives restart; every round has at most three provider calls. A
published round serves every executable pending order. A failed round has no
report, so its three calls serve only sell orders that already exist. The
14:30 round spreads its three calls to 14:50 while a sell is waiting.
"""
from copy import deepcopy
from datetime import datetime, time, timedelta
import os

from redis.exceptions import RedisError

from ..ledger import encode, fingerprint
from ..signal_cycle import pending
from ..simulator import Calendar, CHINA, eligible_lots, moment
from .build_sources import publish_snapshot
from .bulk_collect import RequestLog, quote_pool
from .inputs import SharedMarket, document, shared_market
from .intraday import assemble
from .market_bootstrap import ProbeError
from .entry_admission import read_states, update_waiting_buy

DELAYS = (30, 120, 300)
# The last round of the day has no later round to price a waiting sell, so its three calls reach towards the close.
LATE_SLOT, LATE_DELAYS, LATE_FINAL, LATE_GAP = "14:30", (60, 300), time(14, 50), 60
SOURCE = "tickflow:quotes"
OUTCOME = "pending-quotes:outcome"
SCOPE = ["CN_ETF"]


def provider_enabled():
    """Honor the same explicit HTTP kill switch as the production worker."""
    value = os.environ.get("DISABLE_EXTERNAL_PROVIDER_HTTP", "false").lower()
    if value not in ("true", "false"):
        raise ValueError("DISABLE_EXTERNAL_PROVIDER_HTTP requires true or false")
    return value == "false"


def eligible_orders(cycles, calendar, now):
    """Buy permission is current; existing sells retain frozen cycle/T+1 rules."""
    if not calendar.legal(now):
        return {}
    result = {}
    for cycle in cycles:
        order = pending(cycle)
        if not order or order.get("last_block") or moment(order["requested_at"]) >= now:
            continue
        if order["action"] == "BUY":
            if cycle["status"] != "WAIT_BUY" or calendar.next(now.date()) is None:
                continue
            admission = order.get('entry_admission')
            if admission and admission.get('status') != 'READY':
                continue
        elif (order["action"] not in ("REDUCE", "EXIT") or not order["remaining"]
              or not eligible_lots(cycle["book"], now.date())):
            continue
        after = moment(order["requested_at"])
        if order['action'] == 'BUY' and order.get('entry_admission', {}).get('eligible_after'):
            after = max(after, moment(order['entry_admission']['eligible_after']))
        result[cycle["etf"]] = after
    return result


def missing_prices(orders, market, calendar, now):
    if not market or market.body["trade_date"] != now.date().isoformat():
        return sorted(orders)
    quotes, _ = market.quote_events(orders, now)
    covered = {q["etf"] for q in quotes if q["tradeable"]
               and orders[q["etf"]] < moment(q["observed_at"]) <= now
               and calendar.legal(moment(q["observed_at"]))}
    return sorted(set(orders) - covered)


def run_keys(namespace, day, slot):
    # Use the round slot, not a retry/report ID: manual recovery cannot add budget.
    scope = "prod:" + fingerprint(namespace)[:24]
    return [f"{scope}:feed:{day}:{slot.replace(':', '')}:post:{i}" for i in range(1, 4)]


def schedule(at, late=False):
    """Earliest time of each attempt and the real seconds it must leave after the attempt before it."""
    if not late:
        return [(at + timedelta(seconds=delay), delay - (DELAYS[i - 1] if i else 0)) for i, delay in enumerate(DELAYS)]
    first, second = (at + timedelta(seconds=delay) for delay in LATE_DELAYS)
    final = datetime.combine(at.date(), LATE_FINAL, tzinfo=at.tzinfo)
    return [(first, 0), (second, LATE_DELAYS[1] - LATE_DELAYS[0]),
            (max(final, second + timedelta(seconds=LATE_GAP)), LATE_GAP)]


def choose_attempt(published_at, records, keys, now, late=False):
    """Resume saved input; otherwise respect real time between physical attempts."""
    plan = schedule(published_at, late)
    for i, key in enumerate(keys):
        record = records.get(key, {})
        request = record.get(SOURCE)
        if OUTCOME in record:
            continue
        if request:
            if request["status"] == "SAVED":
                return key  # Crash after collection: reuse this exact supplier batch.
            continue  # STARTED is uncertain, FAILED consumed its one request.
        due, gap = plan[i]
        previous = records.get(keys[i - 1], {}).get(SOURCE) if i else None
        if i and previous is None:
            return None
        if previous:
            due = max(due, moment(previous["started_at"]) + timedelta(seconds=gap))
        return key if now >= due else None
    return None


def read_state(ledger, day):
    from .worker import SLOTS
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        rounds = conn.execute("""SELECT t.slot,t.state,t.planned_at,e.at,t.report_id
            FROM round_tasks t LEFT JOIN events e ON e.ns=t.ns AND e.kind='report' AND e.id=t.report_id
            WHERE t.ns=%s AND t.trade_date=%s AND t.slot=ANY(%s::text[])
            ORDER BY t.planned_at DESC""", (ledger.ns, day, list(SLOTS))).fetchall()
        anchors = []
        if rounds and rounds[0][1] == 'FAILED':
            # The newest round failed: its own attempts, timed from its planned start, price sells only.
            # A failed round behind a newer one, even one still running, no longer counts.
            slot, _, planned, _, _ = rounds[0]
            anchors.append(dict(slot=slot, at=planned.astimezone(CHINA).isoformat(), report_id=None,
                                keys=run_keys(ledger.ns, day, slot), sells_only=True))
        published = next((row for row in rounds if row[1] == 'PUBLISHED' and row[3] is not None), None)
        if published:
            anchors.append(dict(slot=published[0], at=published[3], report_id=published[4],
                                keys=run_keys(ledger.ns, day, published[0])))
        if not anchors:
            return [], [], {}
        keys = [key for anchor in anchors for key in anchor["keys"]]
        cycles = [document(row[0]) for row in conn.execute("""SELECT body FROM cycles
            WHERE ns=%s AND status IN ('WAIT_BUY','OPEN','WAIT_EXIT')""", (ledger.ns,))]
        states = read_states(conn, ledger.ns, [cycle['etf'] for cycle in cycles if cycle['status'] == 'WAIT_BUY'])
        for cycle in cycles:
            if cycle['status'] == 'WAIT_BUY':
                update_waiting_buy(cycle, states[cycle['etf']])
        records = {}
        for key, source, status, started, result in conn.execute("""SELECT run_key,source,status,started_at,result
            FROM collection_requests WHERE run_key=ANY(%s::text[]) AND source=ANY(%s::text[])""",
                (keys, [SOURCE, OUTCOME])):
            records.setdefault(key, {})[source] = dict(status=status,
                started_at=started.isoformat(), result=document(result) if result else None)
    return anchors, cycles, records


def save_outcome(ledger, key, result):
    # No STARTED checkpoint: if publication commits then crashes, it is safe to
    # replay the saved source batch and publish the same snapshot idempotently.
    with ledger._connect() as conn:
        conn.execute("""INSERT INTO collection_requests
            (run_key,source,request_key,status,result,finished_at)
            VALUES (%s,%s,%s,'SAVED',%s,clock_timestamp()) ON CONFLICT DO NOTHING""",
            (key, OUTCOME, fingerprint(SCOPE), encode(result)))


def merge_quotes(ledger, day, previous, tick):
    refreshed = assemble(ledger, day, previous, tick, [])
    if refreshed is None or previous is None or previous.body["trade_date"] != day:
        return refreshed
    # A quote-only sample must preserve the original board, stock and IOPV facts,
    # including their own source clocks. It must not relabel them NOT_COLLECTED.
    merged = deepcopy(previous.body)
    for name in ("quotes", "observed_at", "snapshot_id", "mode"):
        merged[name] = refreshed[name]
    merged.setdefault("quality", {}).update({key: refreshed["quality"][key]
        for key in ("etf_quote_count", "fresh_etf_count")})
    merged.setdefault("provenance", {}).update(previous_snapshot=previous.snapshot_id,
                                               tickflow=tick["batch"], pending_quotes=tick["batch"])
    return merged


def tick(ledger, cache, key, days, config, *, paused=lambda: False, clock=lambda: datetime.now(CHINA)):
    """Collect only for executable pending orders after a published round, or sells after a failed one."""
    now = clock()
    calendar = Calendar(days)
    if ledger.origin != "LIVE" or not config or not provider_enabled() or paused() or not calendar.legal(now):
        return None
    day = now.date().isoformat()
    with ledger._connect() as lock:
        acquired, = lock.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                                 (ledger.ns + ":intraday-collector",)).fetchone()
        if not acquired:
            return None
        anchors, cycles, records = read_state(ledger, day)
        every = eligible_orders(cycles, calendar, now)
        if not anchors or not every:
            return None
        previous = shared_market(ledger, cache, key=key)
        sells = {cycle["etf"] for cycle in cycles if (pending(cycle) or {}).get("action") in ("REDUCE", "EXIT")}
        for anchor in anchors:
            orders = {code: after for code, after in every.items() if code in sells} if anchor.get("sells_only") else every
            needed = missing_prices(orders, previous, calendar, now) if orders else []
            # The caller consumes the shared sample on every tick, so a covered order needs no request.
            late = anchor["slot"] == LATE_SLOT and not sells.isdisjoint(orders)
            run_key = choose_attempt(moment(anchor["at"]), records, anchor["keys"], now, late) if needed else None
            if run_key is not None:
                break
        else:
            return None
        result = dict(run_key=run_key, report_id=anchor["report_id"], slot=anchor["slot"],
                      needing_quote=needed, physical_requests=0)
        if anchor.get("sells_only"):
            result["failed_round"] = True
        budget = RequestLog(ledger, run_key)

        def collect():
            at = clock()
            if not provider_enabled() or paused() or at.date().isoformat() != day or not calendar.legal(at):
                raise ProbeError("post-signal quote collection closed or paused")
            result["physical_requests"] += 1
            return quote_pool(ledger, config)

        try:
            batch = budget.call(SOURCE, SCOPE, collect, limit=1)
        except (ProbeError, OSError, ValueError, KeyError, TypeError) as exc:
            return result | dict(status="COLLECTION_FAILED", error_type=type(exc).__name__)
        current = clock()
        if paused() or current.date().isoformat() != day or not calendar.legal(current):
            return result | dict(status="DEFERRED_AFTER_COLLECTION")
        latest = shared_market(ledger, cache, key=key) or previous
        market = merge_quotes(ledger, day, latest, batch)
        if market and anchor.get("sells_only"):
            # Every other quote stays as it was, so this sample cannot fill a buy or raise a new order.
            kept = dict(latest.body["quotes"]) if latest and latest.body["trade_date"] == day else {}
            kept.update({code: quote for code, quote in market["quotes"].items() if code in orders})
            market["quotes"] = kept
            # A partial sample is not the full one: a later full sample with the same source data must still publish.
            market["snapshot_id"] = "intraday-" + fingerprint([market["snapshot_id"], "sells", sorted(orders)])[:24]
        fresh = SharedMarket.read(market) if market else None
        covered = sorted(set(needed) - set(missing_prices(orders, fresh, calendar, current)))
        result.update(supplier_batch=batch["batch"], covered=covered)
        if not covered:
            result["status"] = "NO_NEW_PENDING_PRICE"
        else:
            try:
                _, cached, snapshot_id = publish_snapshot(ledger, cache, market, key=key)
            except RedisError:
                cached, snapshot_id = False, market["snapshot_id"]
            result.update(status="PUBLISHED", cache_available=cached, snapshot_id=snapshot_id)
        save_outcome(ledger, run_key, result)
        return result
