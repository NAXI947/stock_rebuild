"""Read-only study figures for the exit rules: entry facts, the gap after the 14:30 round, fallback and bar coverage,
and per week how often an UP judgement came with ETF choices and how often a judgement was downgraded to UNKNOWN.

Nothing is collected and nothing is written. Every figure is rebuilt from what the ledger already saved: cycles,
round quotes, the ETF daily batches and the 107 index bars. A fact that was not saved is reported as missing.

    python -B -m tools.exit_observation --env-file /absolute/.env [--since 2026-11-21] [--until 2026-12-11]
    docker exec -i stock-v4-backend python - [--since ...] < tools/exit_observation.py   # settings from the environment
"""
import argparse
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal, localcontext
import json
import os
import sys

from modules.strategy_hermes.signal_cycle import evaluate_exit
from modules.strategy_hermes.stage2.market_regime import FALLBACK
from modules.strategy_hermes.simulator import CHINA, PRECISION, Calendar, Rules, moment

LATE_ROUND = time(14, 30)
SIX = Decimal("0.000001")
PRICE_LINES = ("STOP_LOSS_HIT", "BREAKEVEN_PROTECT", "TRAILING_TAKE_PROFIT", "TARGET_PROFIT_REDUCE")


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def _ratio(value, base):
    return str((Decimal(value) / Decimal(base) - 1).quantize(SIX))


def held_state(cycle, quotes, calendar, day):
    """The cycle as the engine held it after its last sampled quote of ``day``; None when not held over the close."""
    fills = cycle["book"]["fills"]
    if not fills or "exit_plan" not in cycle:
        return None
    bought = moment(fills[0]["filled_at"])
    end = datetime.combine(day, time(15), CHINA)
    if bought >= end or (cycle["closed_at"] and moment(cycle["closed_at"]) <= end):
        return None
    if any(order["action"] == "EXIT" and moment(order["signal_at"]) <= end for order in cycle["orders"]):
        return None                                   # the position was already on its way out
    rules, plan = Rules(**cycle["rule"]), cycle["exit_plan"]
    peak, armed, last, seen = Decimal(fills[0]["price"]), False, None, None
    with localcontext(PRECISION):
        for quote in quotes:
            at = moment(quote["observed_at"])
            if at <= bought or at > end or not calendar.legal(at):
                continue
            mark = (quote["observed_at"], quote["revision"])
            if seen is not None and mark <= seen:
                continue
            seen = mark
            if not quote["tradeable"]:
                continue
            peak = max(peak, Decimal(quote["price_cny"]))
            armed = armed or peak / Decimal(plan["cost_price"]) - 1 >= Decimal(rules.breakeven_arm_return)
            if at.date() == day:
                last = quote
    book = dict(cycle["book"], fills=[fill for fill in fills if moment(fill["filled_at"]) <= end])
    reduced = any(order["action"] == "REDUCE" and moment(order["signal_at"]) <= end for order in cycle["orders"])
    return dict(cycle, book=book, peak_price=str(peak), breakeven_armed=armed, reduced=reduced, orders=[]), last


def late_gap(cycle, quotes, closes, calendar, day):
    """One held session: would the close have crossed a price line that the last round sample did not?"""
    state = held_state(cycle, quotes, calendar, day)
    if state is None:
        return None
    held, last = state
    rules = replace(Rules(**cycle["rule"]), time_exit_enabled=False)
    close = closes.get((cycle["etf"], day.isoformat()))
    row = dict(etf=cycle["etf"], trade_date=day.isoformat(), close_cny=close and close["close_cny"],
               close_available_at=close and close["available_at"],
               last_sample=last and dict(price_cny=last["price_cny"], observed_at=last["observed_at"]),
               late_round_sampled=bool(last and moment(last["observed_at"]).time() >= LATE_ROUND),
               crossed=None, line=None, next_open_sample=None, close_vs_last_sample=None, overnight_gap=None)
    if close is None:
        return dict(row, missing="CLOSE_NOT_STORED")
    probe = dict(price_cny=close["close_cny"], observed_at=datetime.combine(day, time(15), CHINA).isoformat())
    hit = evaluate_exit(held, rules, probe, 0)
    row.update(crossed=bool(hit and hit[1] in PRICE_LINES), line=hit[1] if hit and hit[1] in PRICE_LINES else None)
    later = [d for d in calendar.days if d > day]
    opened = next((quote for quote in quotes if later and quote["tradeable"]
                   and moment(quote["observed_at"]).date() == later[0] and calendar.legal(moment(quote["observed_at"]))), None)
    with localcontext(PRECISION):
        if last:
            row["close_vs_last_sample"] = _ratio(close["close_cny"], last["price_cny"])
        if opened:
            row["next_open_sample"] = dict(price_cny=opened["price_cny"], observed_at=opened["observed_at"])
            row["overnight_gap"] = _ratio(opened["price_cny"], close["close_cny"])
    return row


def bar_coverage(batches, calendar, days, now):
    """Per session: was each index bar stored before the next session's first round needed it?"""
    rows = []
    for day in days:
        later = [d for d in calendar.days if d > day]
        due = datetime.combine(later[0], time(9, 30), CHINA) if later else now
        if datetime.combine(day, time(15), CHINA) > now:
            continue
        first = {}
        for code, collected, dates in batches:
            if day.isoformat() in dates and collected <= due and code not in first:
                first[code] = collected.astimezone(CHINA).isoformat()
        rows.append(dict(trade_date=day.isoformat(), due_at=due.isoformat(), stored_at=first,
                         DUAL=all(code in first for code in ("SH000300", "SH000852")), SINGLE="SH000001" in first))
    return rows


def skill_effect(rounds, since, until):
    """Per week (its Monday): judged boards, UP ones with ETF choices from the AI, and UNKNOWN ones.

    ``rounds`` are (report time, {sector: (state, phase, repeats earlier evidence)}, {sector: number of ETFs chosen}).
    """
    weeks = {}
    for at, sectors, chosen in rounds:
        day = moment(at).date()
        if not since <= day <= until:
            continue
        week = weeks.setdefault((day - timedelta(days=day.weekday())).isoformat(), dict.fromkeys(
            ("reports", "judged_boards", "up_boards", "up_with_etf_choices", "unknown_boards"), 0))
        week["reports"] += 1
        for sid, (state, phase, repeated) in sectors.items():
            if phase == "NOT_COVERED":
                continue
            week["judged_boards"] += 1
            week["unknown_boards"] += state == "UNKNOWN"
            if state == "UP" and not repeated:
                week["up_boards"] += 1
                week["up_with_etf_choices"] += bool(chosen.get(sid))
    def share(part, whole):
        return str((Decimal(part) / whole).quantize(SIX)) if whole else None
    return [dict(week_of=key, **row, up_with_etf_choices_ratio=share(row["up_with_etf_choices"], row["up_boards"]),
                 unknown_ratio=share(row["unknown_boards"], row["judged_boards"])) for key, row in sorted(weeks.items())]


def report(cycles, quotes, closes, batches, calendar, since, until, now, rounds=()):
    days = [day for day in calendar.days if since <= day <= until]
    planned = [cycle for cycle in cycles if "exit_plan" in cycle and cycle["book"]["fills"]
               and since <= moment(cycle["book"]["fills"][0]["filled_at"]).date() <= until]
    entries = [dict(etf=cycle["etf"], bought_at=cycle["book"]["fills"][0]["filled_at"], rule=cycle["rule"]["version"],
                    migrated="migration" in cycle, flags=cycle["exit_plan"]["flags"],
                    entry_market_regime=(None if FALLBACK in cycle["exit_plan"]["flags"]
                                         else cycle["exit_plan"]["market_regime"]), entry=cycle.get("entry"))
               for cycle in planned]
    gaps = [row for cycle in cycles for day in days
            if datetime.combine(day, time(15), CHINA) <= now
            for row in [late_gap(cycle, quotes.get(cycle["etf"], []), closes, calendar, day)] if row]
    crossed = [row for row in gaps if row["crossed"]]
    bars = bar_coverage(batches, calendar, days, now)
    with localcontext(PRECISION):
        extra = [Decimal(row["overnight_gap"]) for row in crossed
                 if row["line"] != "TARGET_PROFIT_REDUCE" and row["overnight_gap"] is not None]
        summary = dict(
            cycles_opened=len(planned),
            atr_unavailable=sum("ATR_UNAVAILABLE" in row["flags"] for row in entries),
            regime_fallback_range=sum("REGIME_FALLBACK_RANGE" in row["flags"] for row in entries),
            held_sessions=len(gaps), close_not_stored=sum(row.get("missing") == "CLOSE_NOT_STORED" for row in gaps),
            close_crossed_a_line=len(crossed),
            crossed_lines={line: sum(row["line"] == line for row in crossed) for line in PRICE_LINES
                           if any(row["line"] == line for row in crossed)},
            average_overnight_gap_after_exit_line=str((sum(extra) / len(extra)).quantize(SIX)) if extra else None,
            index_bar_sessions=len(bars), dual_stored=sum(row["DUAL"] for row in bars),
            single_stored=sum(row["SINGLE"] for row in bars))
    return dict(schema_version="stock-v4-exit-observation-v2", note="read-only; no ledger row was written",
                since=since.isoformat(), until=until.isoformat(), summary=summary, entries=entries,
                late_round_gaps=gaps, index_bars=bars, skill_effect=skill_effect(rounds, since, until))


def load(ledger, since):
    from modules.strategy_hermes.stage2.etf_statistics import DAILY
    from modules.strategy_hermes.stage2.inputs import document
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        run = conn.execute("SELECT config FROM runs WHERE ns=%s", (ledger.ns,)).fetchone()
        if run is None:
            return None
        cycles = [_decode(body) for body, in conn.execute(
            """SELECT body FROM cycles WHERE ns=%s AND body ? 'exit_plan'
               AND (body->>'closed_at' IS NULL OR body->>'closed_at'>=%s) ORDER BY opened_at,id""",
            (ledger.ns, since.isoformat())).fetchall()]
        codes = sorted({cycle["etf"] for cycle in cycles})
        start = min([cycle["book"]["fills"][0]["filled_at"] for cycle in cycles if cycle["book"]["fills"]],
                    default=since.isoformat())
        quotes = {}
        for payload, in conn.execute("""SELECT payload FROM events WHERE ns=%s AND kind='quote' AND at>=%s
                AND payload->>'etf'=ANY(%s) ORDER BY at,id""", (ledger.ns, start, codes)).fetchall():
            quote = _decode(payload)
            quotes.setdefault(quote["etf"], []).append(quote)
        closes = {}
        for body, collected in conn.execute("""SELECT body,collected_at FROM supplier_market_batches
                WHERE provider='tushare' AND endpoint=%s AND scope->>'ts_code'=ANY(%s)
                ORDER BY collected_at,batch_id""", (DAILY, codes)).fetchall():
            for row in document(body)["rows"]:
                if row.get("close_cny"):
                    closes[(row["code"], row["trade_date"])] = dict(
                        close_cny=row["close_cny"], available_at=collected.astimezone(CHINA).isoformat())
        batches = [(code, collected, {row["trade_date"] for row in document(body)["rows"]})
                   for code, body, collected in conn.execute("""SELECT scope->>'StockID',body,collected_at
                       FROM supplier_market_batches WHERE provider='kaipanla' AND endpoint='107'
                       AND scope->>'purpose'='index-kline' ORDER BY collected_at,batch_id""").fetchall()]
        # A saved report carries its whole source document; three facts per board are read from it.
        rounds = [(at, {sid: (row.get("state"), row.get("phase"), bool(row.get("duplicate_evidence")))
                        for sid, row in (_decode(sectors) or {}).items()}, _decode(chosen) or {})
                  for at, sectors, chosen in conn.execute(
                      """SELECT payload->>'at',
                           (SELECT jsonb_object_agg(key,jsonb_build_object('state',value->'state','phase',value->'phase',
                               'duplicate_evidence',value->'duplicate_evidence')) FROM jsonb_each(payload->'sectors')),
                           (SELECT jsonb_object_agg(key,jsonb_array_length(value))
                              FROM jsonb_each(payload->'input_meta'->'octop'->'etf_choices')
                             WHERE jsonb_typeof(value)='array')
                         FROM events WHERE ns=%s AND kind='report' AND at>=%s
                           AND jsonb_typeof(payload->'sectors')='object' ORDER BY at,id""",
                      (ledger.ns, datetime.combine(since, time(), CHINA).isoformat())).fetchall()]
    return _decode(run[0]), cycles, quotes, closes, batches, rounds


def main(argv=None):
    from datetime import date
    from modules.strategy_hermes.stage2.postgres import PostgresLedger
    from modules.strategy_hermes.stage2.settings import Settings
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file")
    parser.add_argument("--since", type=date.fromisoformat, default=date(2026, 11, 21))
    parser.add_argument("--until", type=date.fromisoformat)
    args = parser.parse_args(argv)
    settings = Settings.read(args.env_file) if args.env_file else Settings.from_values(os.environ)
    now = datetime.now(CHINA)
    until = args.until or now.date()
    loaded = load(PostgresLedger(settings, "LIVE"), args.since)
    if loaded is None:
        result = dict(schema_version="stock-v4-exit-observation-v2", note="no run recorded in this namespace")
    else:
        config, cycles, quotes, closes, batches, rounds = loaded
        result = report(cycles, quotes, closes, batches, Calendar(config["calendar"]), args.since, until, now, rounds)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
