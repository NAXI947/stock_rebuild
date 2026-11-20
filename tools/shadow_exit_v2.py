"""Replay recorded reports and quotes through the v2 exit rules without touching the ledger.

Each automatic cycle that actually filled its buy is rebuilt with the same entry report and the same
buy quote, then every later saved report and quote is fed to the v2 engine. The output compares the
recorded outcome with the simulated one. The database is only read.

    python -B -m tools.shadow_exit_v2 --env-file /absolute/.env [--namespace NS] [--since 2026-11-13] [--inputs plans.json]
    docker exec -i stock-v4-backend python - < tools/shadow_exit_v2.py        # settings from the environment
"""
import argparse
from collections import Counter
from dataclasses import replace
from decimal import Decimal, localcontext
import json
import os
import sys

from modules.strategy_hermes.signal_cycle import apply_report, create_cycle, process_quote
from modules.strategy_hermes.simulator import PRECISION, Calendar, Rules, moment, shares, valuation


def _return(cycle, price):
    rules = Rules(**cycle["rule"])
    return valuation(cycle["book"], rules, Decimal(price) if price and shares(cycle["book"]) else None)["return_pct"]


def _orders(cycle):
    fills = {}
    for fill in cycle["book"]["fills"]:
        fills.setdefault(fill["order_id"], fill)
    return [dict(action=order["action"], status=order["status"], reason=order.get("trigger_reason"),
                 requested_at=order["requested_at"], trigger_price=order.get("trigger_price"),
                 fill_price=fills.get(order["id"], {}).get("price"), filled_at=fills.get(order["id"], {}).get("filled_at"))
            for order in cycle["orders"]]


def replay_cycle(actual, events, calendar, rules, inputs):
    """Simulated v2 cycle for one recorded cycle; ``events`` are (kind, payload) in ledger order."""
    reports = {event["id"]: event for kind, event in events if kind == "report"}
    quotes = {event["id"]: event for kind, event in events if kind == "quote"}
    entry, bought = reports.get(actual["orders"][0]["report_id"]), quotes.get(actual["book"]["fills"][0]["quote_id"])
    if entry is None or bought is None:
        return None
    cycle = create_cycle(actual["etf"], actual["unit"], rules, entry, dict(sector_id=actual["primary_sector"]),
                         actual.get("entry_sources", []))
    process_quote(cycle, bought, calendar, lambda at, frozen: inputs(actual["etf"], at, frozen))
    if cycle["status"] != "OPEN":
        return None
    seen, last = bought["observed_at"], bought
    start = events.index(("quote", bought)) + 1
    for kind, event in events[start:]:
        active = cycle["status"] in ("OPEN", "WAIT_EXIT")
        if kind == "report":
            if active:
                apply_report(cycle, event)
        elif event["etf"] == actual["etf"] and event["observed_at"] > seen and calendar.legal(moment(event["observed_at"])):
            # Both sides are valued at the newest quote, also after the simulated cycle has closed.
            seen, last = event["observed_at"], event
            if active:
                process_quote(cycle, event, calendar)
    return cycle, last


def compare(cycles, events, calendar, rules, inputs):
    rows, reasons, flags, gaps, held = [], Counter(), Counter(), [], []
    for actual in cycles:
        if not actual["book"]["fills"]:
            continue
        result = replay_cycle(actual, events, calendar, rules, inputs)
        if result is None:
            rows.append(dict(etf=actual["etf"], status="NOT_REPLAYABLE"))
            continue
        shadow, last = result
        plan = shadow["exit_plan"]
        flags.update(plan["flags"])
        orders = _orders(shadow)
        with localcontext(PRECISION):
            for order in orders:
                if order["reason"] and order["status"] == "FILLED":
                    reasons[order["reason"]] += 1
                    if order["trigger_price"] and order["fill_price"]:
                        gaps.append(Decimal(order["fill_price"]) / Decimal(order["trigger_price"]) - 1)
            before, after = _return(actual, last["price_cny"]), _return(shadow, last["price_cny"])
            difference = None if before is None or after is None else str(Decimal(after) - Decimal(before))
        if shadow["status"] == "CLOSED" and shadow.get("last_quote"):
            held.append(shadow["last_quote"]["held_days"])
        rows.append(dict(
            etf=actual["etf"], bought_at=actual["book"]["fills"][0]["filled_at"], valued_at=last["observed_at"],
            recorded=dict(rule=actual["rule"]["version"], status=actual["status"], return_pct=before, orders=_orders(actual)),
            shadow=dict(rule=rules.version, status=shadow["status"], return_pct=after, orders=orders,
                        plan={key: plan[key] for key in ("market_regime", "regime_as_of", "atr20", "atr_as_of", "flags",
                                                         "stop_loss", "trail_start", "trail_drawdown", "target_reduce", "max_days")}),
            return_pct_difference=difference))
    with localcontext(PRECISION):
        gap = str((sum(gaps) / len(gaps) * 100).quantize(Decimal("0.0001"))) if gaps else None
        differences = [Decimal(row["return_pct_difference"]) for row in rows if row.get("return_pct_difference")]
        summary = dict(cycles=len(rows), filled_exit_reasons=dict(reasons), fallback_flags=dict(flags),
                       average_fill_vs_trigger_pct=gap,
                       average_held_days_closed=str(sum(held) / Decimal(len(held))) if held else None,
                       average_return_pct_difference=(str((sum(differences) / len(differences)).quantize(Decimal("0.0001")))
                                                      if differences else None))
    return dict(schema_version="stock-v4-exit-shadow-v1", note="simulation only; no ledger row was written",
                summary=summary, cycles=rows)


def load(ledger, since):
    """Everything the replay needs, in one read-only snapshot."""
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        run = conn.execute("SELECT config FROM runs WHERE ns=%s", (ledger.ns,)).fetchone()
        if run is None:
            return None, [], []
        config, = run
        cycles = [json.loads(body) if isinstance(body, str) else body for body, in conn.execute(
            "SELECT body FROM cycles WHERE ns=%s AND opened_at>=%s ORDER BY opened_at,id", (ledger.ns, since)).fetchall()]
        events = [(kind, json.loads(payload) if isinstance(payload, str) else payload) for kind, payload in conn.execute(
            """SELECT kind,payload FROM events WHERE ns=%s AND at>=%s
               ORDER BY at,(kind<>'report'),id""", (ledger.ns, since)).fetchall()]
    return (json.loads(config) if isinstance(config, str) else config), cycles, events


def main(argv=None):
    from modules.strategy_hermes.stage2.postgres import BoundConnection, PostgresLedger
    from modules.strategy_hermes.stage2.settings import Settings
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file")
    parser.add_argument("--namespace", help="another run of the same environment; defaults to AUTO_RUN_NAMESPACE")
    parser.add_argument("--since", default="2000-01-01")
    parser.add_argument("--regime-mode", default="DUAL", choices=("DUAL", "SINGLE"))
    parser.add_argument("--inputs", help="JSON {etf: {market_regime, atr20, ...}} used instead of stored batches")
    args = parser.parse_args(argv)
    settings = Settings.read(args.env_file) if args.env_file else Settings.from_values(os.environ)
    if args.namespace:
        if not args.namespace.startswith(settings.redis_prefix) or len(args.namespace) > 120:
            raise ValueError("namespace outside configured environment")
        settings = replace(settings, namespace=args.namespace)
    ledger = PostgresLedger(settings, "LIVE")
    config, cycles, events = load(ledger, args.since)
    if config is None:
        print(json.dumps(dict(schema_version="stock-v4-exit-shadow-v1", note="no run recorded in this namespace",
                              summary=dict(cycles=0), cycles=[]), ensure_ascii=False, indent=2))
        return 0
    calendar, rules = Calendar(config["calendar"]), Rules.v2(args.regime_mode)
    fixed = json.loads(open(args.inputs, encoding="utf-8").read()) if args.inputs else None

    def inputs(code, at, frozen):
        if fixed is not None:
            return dict(flags=[], **fixed[code]) if code in fixed else {}
        with ledger._connect() as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            return ledger._exit_inputs(BoundConnection(conn), code, at, calendar, frozen)

    json.dump(compare(cycles, events, calendar, rules, inputs), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
