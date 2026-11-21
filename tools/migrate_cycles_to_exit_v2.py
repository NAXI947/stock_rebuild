"""One-off: put named open v1 cycles under the v2 exit rules (owner authorisation 2026-11-21, CLAUDE.md rule 32).

Nothing is sold here. Each named cycle gets the v2 rule snapshot and the exit plan it would have received when
its buy filled, plus the peak, breakeven and weak-day state implied by the quotes and reports already stored.
Fills, orders, cash and lots are untouched; the engine judges the next quote and report as for any v2 cycle.
The v1 snapshot is kept in the cycle body. Without --apply the database is only read.

    python -B -m tools.migrate_cycles_to_exit_v2 --env-file /absolute/.env --etf 512800.SH --atr 512800.SH=0.018764@2026-11-12 [--apply]
    docker exec -i stock-v4-backend python - --etf 512800.SH ... < tools/migrate_cycles_to_exit_v2.py
"""
import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, localcontext
import json
import os
import sys

from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.signal_cycle import build_exit_plan, pending
from modules.strategy_hermes.simulator import CHINA, PRECISION, V1, Calendar, Rules, decimal, moment, shares

MIGRATED = "MIGRATED_FROM_V1"


def migrate(cycle, events, calendar, rules, inputs, at):
    """The v2 body of one open v1 cycle; ``events`` are (kind, payload) in ledger order."""
    fills = cycle["book"]["fills"]
    if (cycle["status"] != "OPEN" or cycle["rule"]["version"] != V1 or pending(cycle) or not fills
            or not shares(cycle["book"])):
        raise ValueError(f"{cycle['etf']}: only an open v1 position without a waiting order can be migrated")
    result, bought = deepcopy(cycle), fills[0]
    result["rule"] = rules.snapshot()
    build_exit_plan(result, rules, inputs, bought)
    plan = result["exit_plan"]
    plan["flags"].append(MIGRATED)
    result["migration"] = dict(at=at, from_rule=cycle["rule"])
    start = datetime.fromisoformat(plan["buy_fill_trade_date"]).date()
    seen, held = (bought["filled_at"], bought["revision"]), False
    # Quotes that arrive together: the ledger applies only the newest observation of the batch.
    newest = {}
    for kind, event in events:
        if kind == "quote" and event["etf"] == cycle["etf"]:
            newest[event["at"]] = max(newest.get(event["at"], event), event,
                                      key=lambda quote: (quote["observed_at"], quote["revision"], quote["id"]))
    with localcontext(PRECISION):
        cost = decimal(plan["cost_price"])
        for kind, event in events:
            if not held:
                held = kind == "quote" and event["id"] == bought["quote_id"]
            elif kind == "report":
                row = event["sectors"].get(cycle["primary_sector"], {})
                if row.get("state") == "WEAK":
                    day = moment(event["at"]).date().isoformat()
                    if day not in result["weak_trade_days"]:
                        result["weak_trade_days"].append(day)
                elif row.get("state") in ("HOLD", "UP") and not row.get("duplicate_evidence"):
                    result["weak_trade_days"] = []
            elif event["etf"] == cycle["etf"] and newest[event["at"]] is event:
                observed = moment(event["observed_at"])
                mark = (event["observed_at"], event["revision"])
                # Same acceptance as the ledger: a later observation, or a higher revision of the same one.
                if mark <= seen or not calendar.legal(observed):
                    continue
                seen = mark
                if not event["tradeable"]:
                    continue
                if decimal(event["price_cny"]) > decimal(result["peak_price"]):
                    result["peak_price"] = event["price_cny"]
                result["last_quote"] = dict(price=event["price_cny"], observed_at=event["observed_at"],
                                            held_days=sum(start < day <= observed.date() for day in calendar.days))
        if not held:
            raise ValueError(f"{cycle['etf']}: the buy quote is not among the stored events")
        result["breakeven_armed"] = decimal(result["peak_price"]) / cost - 1 >= decimal(rules.breakeven_arm_return)
    return result


def lines(cycle, rules, calendar, today):
    """Prices at which the next tradeable quote sells the rest, for the session after ``today``."""
    plan = cycle["exit_plan"]
    session = next((day for day in calendar.days if day > today), None)
    start = datetime.fromisoformat(plan["buy_fill_trade_date"]).date()
    held = None if session is None else sum(start < day <= session for day in calendar.days)
    cent = Decimal("0.0001")
    with localcontext(PRECISION):
        cost, peak = decimal(plan["cost_price"]), decimal(cycle["peak_price"])
        reduced = any(fill["action"] == "REDUCE" for fill in cycle["book"]["fills"])
        trailing = peak / cost - 1 >= decimal(plan["trail_start"])
        return dict(
            session=session and session.isoformat(), held_days=held, cost_price=plan["cost_price"],
            peak_price=cycle["peak_price"], peak_return_pct=str(((peak / cost - 1) * 100).quantize(cent)),
            stop_loss_at_or_below=str((cost * (1 - decimal(plan["stop_loss"]))).quantize(cent)),
            breakeven_at_or_below=(str((cost * decimal(rules.breakeven_buffer)).quantize(cent))
                                   if reduced and cycle["breakeven_armed"] else None),
            trailing_at_or_below=str((peak * (1 - decimal(plan["trail_drawdown"]))).quantize(cent)) if trailing else None,
            time_exit_from_1405=(None if held is None else "TIME_EXIT_MAX_DAYS" if held >= plan["max_days"]
                                 else "TIME_EXIT_STAGNANT_BELOW_%s" % plan["stagnant_return"]
                                 if held >= plan["stagnant_days"] else None),
            weak_trade_days=cycle["weak_trade_days"], next_weak_report_exits=len(cycle["weak_trade_days"]) + 1
            >= rules.weak_exit_trade_days)


def run(ledger, codes, atr, rules, now, apply):
    from modules.strategy_hermes.stage2.market_regime import classify, stored_series
    stamp, rows = now.isoformat(), []
    with ledger._connect() as conn, conn.transaction():
        if apply:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (ledger.ns,))
        else:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        config, = conn.execute("SELECT config FROM runs WHERE ns=%s", (ledger.ns,)).fetchone()
        calendar = Calendar(json.loads(config)["calendar"])
        found = {etf: (cid, json.loads(body)) for cid, etf, body in conn.execute(
            "SELECT id,etf,body FROM cycles WHERE ns=%s AND etf=ANY(%s) AND status IN ('WAIT_BUY','OPEN','WAIT_EXIT')"
            + (" FOR UPDATE" if apply else ""), (ledger.ns, codes)).fetchall()}
        series = stored_series(conn)
        for code in codes:
            if code not in found:
                raise ValueError(f"{code}: no active cycle")
            cid, cycle = found[code]
            if cycle["rule"]["version"] == rules.version:
                rows.append(dict(etf=code, cycle_id=cid, status="ALREADY_V2"))
                continue
            bought = cycle["book"]["fills"][0] if cycle["book"]["fills"] else None
            if bought is None:
                raise ValueError(f"{code}: the buy has not filled")
            # A saved report carries its whole source document; three fields of it are needed.
            events = [(kind, json.loads(payload)) for kind, payload in conn.execute(
                """SELECT kind,CASE WHEN kind='report' THEN jsonb_build_object(
                       'id',payload->'id','at',payload->'at','sectors',payload->'sectors') ELSE payload END
                   FROM events WHERE ns=%s AND at>=%s AND (kind='report' OR payload->>'etf'=%s)
                   ORDER BY at,(kind<>'report'),id""", (ledger.ns, bought["filled_at"], code)).fetchall()]
            # The index bars were collected after this buy; only sessions before the buy day are read.
            previous = [day for day in calendar.days if day < moment(bought["filled_at"]).date()]
            inputs = classify(series, previous[-1].isoformat(), rules.regime_mode,
                              bull=rules.regime_bull_volume_ratio, bear=rules.regime_bear_volume_ratio)
            value, _, as_of = atr.get(code, "").partition("@")
            inputs = inputs | dict(atr20=value or None, atr_as_of=as_of or None)
            migrated = migrate(cycle, events, calendar, rules, inputs, stamp)
            rows.append(dict(etf=code, cycle_id=cid, status="MIGRATED" if apply else "DRY_RUN",
                             from_rule=cycle["rule"]["version"], to_rule=rules.version, shares=shares(cycle["book"]),
                             exit_plan=migrated["exit_plan"], breakeven_armed=migrated["breakeven_armed"],
                             last_quote=migrated["last_quote"], next_session=lines(migrated, rules, calendar, now.date())))
            if apply:
                conn.execute("UPDATE cycles SET body=%s WHERE ns=%s AND id=%s", (encode(migrated), ledger.ns, cid))
        if apply and any(row["status"] == "MIGRATED" for row in rows):
            frozen = encode(rules.snapshot())
            conn.execute("INSERT INTO rule_versions VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                         (ledger.ns, rules.version, frozen))
            saved, = conn.execute("SELECT snapshot FROM rule_versions WHERE ns=%s AND version=%s",
                                  (ledger.ns, rules.version)).fetchone()
            if saved != frozen:
                raise ValueError("changed rules require a new version")
    return dict(schema_version="stock-v4-exit-migration-v1", namespace=ledger.ns, applied=apply, cycles=rows)


def main(argv=None):
    from modules.strategy_hermes.stage2.postgres import PostgresLedger
    from modules.strategy_hermes.stage2.settings import Settings
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file")
    parser.add_argument("--namespace", help="another run of the same environment; defaults to AUTO_RUN_NAMESPACE")
    parser.add_argument("--etf", action="append", required=True, help="ETF of an open v1 cycle; repeat per cycle")
    parser.add_argument("--atr", action="append", default=[], metavar="CODE=ATR20@AS_OF",
                        help="ATR20 known before the buy; without it the plan uses the floor of every line")
    parser.add_argument("--regime-mode", choices=("DUAL", "SINGLE"),
                        help="only where MODEL_EVALUATION_RULE is still v1")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings.read(args.env_file) if args.env_file else Settings.from_values(os.environ)
    if args.namespace:
        if not args.namespace.startswith(settings.redis_prefix) or len(args.namespace) > 120:
            raise ValueError("namespace outside configured environment")
        settings = replace(settings, namespace=args.namespace)
    ledger = PostgresLedger(settings, "LIVE")
    rules = ledger._rules()
    if rules.exit_table is None:
        if not args.regime_mode:
            raise ValueError("MODEL_EVALUATION_RULE is v1 here; pass --regime-mode")
        rules = Rules.v2(args.regime_mode)
    atr = dict(item.split("=", 1) for item in args.atr)
    json.dump(run(ledger, args.etf, atr, rules, datetime.now(CHINA), args.apply), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
