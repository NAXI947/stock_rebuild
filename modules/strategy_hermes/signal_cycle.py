"""Deterministic automatic intents. No quote fetching or real order routing."""
from datetime import datetime, time, timedelta
from decimal import Decimal, localcontext

from .simulator import (LINES, PRECISION, Rules, affordable, buy, decimal, eligible_lots,
                        floor_lot, moment, new_book, sell, shares, valuation)

ACTIVE = ("WAIT_BUY", "OPEN", "WAIT_EXIT")
TIME_EXIT_FROM = time(14, 5)


def create_cycle(code, unit, rules, report, choice, candidates):
    at, sid = report["at"], choice["sector_id"]
    cycle = dict(etf=code, primary_sector=sid, unit=unit, status="WAIT_BUY",
                 rule=rules.snapshot(), book=new_book(rules), orders=[], reduced=False,
                 last_decision="UP", last_report=report["id"], closed_at=None,
                 entry_sources=candidates)
    add_order(cycle, "BUY", report)
    return cycle


def add_order(cycle, kind, report, target=None, theoretical=None, *, quote=None, reason=None):
    """An order comes from a report or, for price and time exits, from the quote that triggered it."""
    assert not pending(cycle)
    source = quote or report
    at = quote["observed_at"] if quote else report["at"]
    row = (dict(quote_id=quote["id"], price_cny=quote["price_cny"], observed_at=at) if quote
           else report["sectors"][cycle["primary_sector"]])
    order = dict(id=f"{source['id']}:{kind}", action=kind, report_id=None if quote else report["id"], signal_at=at,
                 requested_at=at, eligible_at=at if kind=="BUY" else None, remaining=target,
                 target=target, theoretical=None if theoretical is None else str(theoretical),
                 status="PENDING" if target != 0 else "BELOW_TRADING_UNIT",
                 sector_id=cycle["primary_sector"], evidence=row)
    if reason:
        seen = quote or cycle.get("last_quote")
        order.update(trigger_reason=reason, triggered_at=at,
                     trigger_price=(seen.get("price_cny") or seen.get("price")) if seen else None)
    cycle["orders"].append(order)
    return order


def pending(cycle):
    return next((o for o in reversed(cycle["orders"]) if o["status"]=="PENDING"), None)


def update_eligible(order, book, at):
    unlocked = [lot["sellable_from"] for lot in book["lots"] if lot["remaining"] and lot["sellable_from"]]
    if unlocked and order["eligible_at"] is None:
        start = datetime.combine(datetime.fromisoformat(min(unlocked)).date(), time(9,30), tzinfo=at.tzinfo)
        order["eligible_at"] = max(start, at).isoformat()


def _exit(cycle, reason, *, report=None, quote=None):
    """Replace an unfilled reduction with one order for every remaining share."""
    active = pending(cycle)
    if active:
        active["status"] = "SUPERSEDED"
    order = add_order(cycle, "EXIT", report, shares(cycle["book"]), quote=quote, reason=reason)
    cycle["status"] = "WAIT_EXIT"
    update_eligible(order, cycle["book"], moment(order["requested_at"]))


def _reduce(cycle, rules, reason, *, report=None, quote=None):
    cycle["reduced"] = True
    with localcontext(PRECISION):
        theoretical = shares(cycle["book"]) * decimal(rules.reduce_ratio)
        target = floor_lot(theoretical, cycle["unit"])
    order = add_order(cycle, "REDUCE", report, target, theoretical, quote=quote, reason=reason)
    update_eligible(order, cycle["book"], moment(order["requested_at"]))


def build_exit_plan(cycle, rules, inputs, fill):
    """Freeze the exit lines of one cycle when its first buy fills."""
    regime = inputs.get("market_regime") or "RANGE_ROTATION"
    flags = list(inputs["flags"]) if inputs.get("market_regime") else ["REGIME_FALLBACK_RANGE"]
    atr = inputs.get("atr20")
    if atr is None:
        flags.append("ATR_UNAVAILABLE")
    elif -Decimal(atr).as_tuple().exponent > 6:
        atr = str(Decimal(atr).quantize(Decimal("0.000001")))
    row = rules.exit_table[regime]
    plan = dict(market_regime=regime, regime_mode=rules.regime_mode, regime_as_of=inputs.get("regime_as_of"),
                atr20=atr, atr_as_of=inputs.get("atr_as_of"), flags=flags)
    with localcontext(PRECISION):
        for key in LINES:
            multiple, low, high = (decimal(value) for value in row[key])
            plan[key] = str(low if atr is None else min(max(multiple * decimal(atr), low), high))
    plan.update(stagnant_days=row["stagnant_days"], stagnant_return=row["stagnant_return"],
                max_days=row["max_days"], buy_fill_trade_date=moment(fill["filled_at"]).date().isoformat(),
                cost_price=fill["price"])
    cycle.update(exit_plan=plan, peak_price=fill["price"], breakeven_armed=False, weak_trade_days=[],
                 last_quote=None)


def evaluate_exit(cycle, rules, quote, held_days):
    """First matching exit line for one sampled quote: (action, reason) or None."""
    plan = cycle["exit_plan"]
    with localcontext(PRECISION):
        price, cost, peak = decimal(quote["price_cny"]), decimal(plan["cost_price"]), decimal(cycle["peak_price"])
        gain = price / cost - 1
        if gain <= -decimal(plan["stop_loss"]):
            return "EXIT", "STOP_LOSS_HIT"
        reduced = any(fill["action"] == "REDUCE" for fill in cycle["book"]["fills"])
        if reduced and cycle["breakeven_armed"] and price <= cost * decimal(rules.breakeven_buffer):
            return "EXIT", "BREAKEVEN_PROTECT"
        if peak / cost - 1 >= decimal(plan["trail_start"]) and 1 - price / peak >= decimal(plan["trail_drawdown"]):
            return "EXIT", "TRAILING_TAKE_PROFIT"
        if rules.time_exit_enabled and moment(quote["observed_at"]).time() >= TIME_EXIT_FROM:
            if held_days >= plan["max_days"]:
                return "EXIT", "TIME_EXIT_MAX_DAYS"
            if held_days >= plan["stagnant_days"] and gain < decimal(plan["stagnant_return"]):
                return "EXIT", "TIME_EXIT_STAGNANT"
        if not cycle["reduced"] and not pending(cycle) and gain >= decimal(plan["target_reduce"]):
            return "REDUCE", "TARGET_PROFIT_REDUCE"
    return None


def process_quote(cycle, quote, calendar, exit_inputs=None):
    """Match existing orders first, then judge the exit lines on what is still held."""
    before = cycle["status"]
    fill(cycle, quote, calendar)
    rules = Rules(**cycle["rule"])
    if rules.exit_table is None:
        return
    at = moment(quote["observed_at"])
    if before == "WAIT_BUY":
        # The quote that opens the position only sets the plan; later quotes are judged.
        if cycle["status"] == "OPEN":
            inputs = exit_inputs(at, rules) if exit_inputs else {}
            build_exit_plan(cycle, rules, inputs, cycle["book"]["fills"][0])
        return
    if (cycle["status"] != "OPEN" or "exit_plan" not in cycle or not shares(cycle["book"])
            or not quote["tradeable"] or not calendar.legal(at)):
        return
    plan = cycle["exit_plan"]
    bought = datetime.fromisoformat(plan["buy_fill_trade_date"]).date()
    held_days = sum(bought < day <= at.date() for day in calendar.days)
    with localcontext(PRECISION):
        price, cost = decimal(quote["price_cny"]), decimal(plan["cost_price"])
        if price > decimal(cycle["peak_price"]):
            cycle["peak_price"] = quote["price_cny"]
        if decimal(cycle["peak_price"]) / cost - 1 >= decimal(rules.breakeven_arm_return):
            cycle["breakeven_armed"] = True
    cycle["last_quote"] = dict(price=quote["price_cny"], observed_at=quote["observed_at"], held_days=held_days)
    hit = evaluate_exit(cycle, rules, quote, held_days)
    if not hit:
        return
    if hit[0] == "EXIT":
        _exit(cycle, hit[1], quote=quote)
    else:
        _reduce(cycle, rules, hit[1], quote=quote)
        cycle["breakeven_armed"] = True


def _apply_report_exits(cycle, rules, report, decision):
    """v2: weak days are counted per trading day and a second one clears the position."""
    if cycle["status"] != "OPEN":
        return
    cycle.setdefault("weak_trade_days", [])
    if decision in ("HOLD", "UP"):
        # A report that only repeats earlier evidence is shown as HOLD but is not a new judgement.
        if not report["sectors"][cycle["primary_sector"]].get("duplicate_evidence"):
            cycle["weak_trade_days"] = []
    elif decision == "EXIT":
        _exit(cycle, "AI_RETREAT", report=report)
    elif decision == "WEAK":
        day = moment(report["at"]).date().isoformat()
        if day not in cycle["weak_trade_days"]:
            cycle["weak_trade_days"].append(day)
        plan, seen = cycle.get("exit_plan") or {}, cycle.get("last_quote")
        if len(cycle["weak_trade_days"]) >= rules.weak_exit_trade_days:
            _exit(cycle, "AI_WEAK_CROSS_DAY_2", report=report)
        elif (rules.bear_early_weak_exit and plan.get("market_regime") == "BEAR_DEFENSE"
              and not cycle["breakeven_armed"] and day > plan["buy_fill_trade_date"] and seen
              and moment(seen["observed_at"]).date().isoformat() == day
              and decimal(seen["price"]) < decimal(plan["cost_price"])):
            _exit(cycle, "BEAR_EARLY_WEAK_EXIT", report=report)
        elif not cycle["reduced"]:
            _reduce(cycle, rules, "AI_FIRST_WEAK_REDUCE", report=report)


def apply_report(cycle, report):
    if cycle["status"] not in ACTIVE:
        return
    decision = report["sectors"].get(cycle["primary_sector"], {}).get("state", "UNKNOWN")
    cycle["last_decision"], cycle["last_report"] = decision, report["id"]
    if (cycle.get("rule") or {}).get("exit_table") and cycle["status"] != "WAIT_BUY":
        return _apply_report_exits(cycle, Rules(**cycle["rule"]), report, decision)
    if decision not in ("WEAK","EXIT"):
        return
    active = pending(cycle)
    if cycle["status"] == "WAIT_BUY":
        active["status"] = "CANCELLED"
        cycle["status"], cycle["closed_at"] = "CANCELLED", report["at"]
        return
    rules = Rules(**cycle["rule"])
    if decision == "EXIT" and cycle["status"] != "WAIT_EXIT":
        if active:
            active["status"] = "SUPERSEDED"
        order = add_order(cycle, "EXIT", report, shares(cycle["book"]))
        cycle["status"] = "WAIT_EXIT"
        update_eligible(order, cycle["book"], moment(report["at"]))
    if decision == "WEAK" and not cycle["reduced"] and cycle["status"] == "OPEN":
        cycle["reduced"] = True
        with localcontext(PRECISION):
            theoretical = shares(cycle["book"]) * decimal(rules.reduce_ratio)
            target = floor_lot(theoretical,cycle["unit"])
        order = add_order(cycle, "REDUCE", report, target, theoretical)
        update_eligible(order, cycle["book"], moment(report["at"]))


def fill(cycle, quote, calendar):
    order = pending(cycle)
    at = moment(quote["observed_at"])
    if (not order or at <= moment(order["requested_at"]) or
            not quote["tradeable"] or not calendar.legal(at)):
        return
    admission = order.get("entry_admission") if order["action"] == "BUY" else None
    if admission is not None:
        if admission.get("status") != "READY":
            order["last_block"] = admission.get("reason") or "ENTRY_ADMISSION_UNAVAILABLE"
            return
        if at <= moment(admission["eligible_after"]):
            return
    if any(f["quote_id"] == quote["id"] or f["filled_at"] == quote["observed_at"] for f in cycle["book"]["fills"]):
        return
    rule = Rules(**cycle["rule"])
    book = cycle["book"]
    raw = decimal(quote["price_cny"])
    if order["action"] == "BUY":
        quantity = affordable(decimal(book["cash"]),rule,raw,cycle["unit"])
        if not quantity:
            order["last_block"] = "INSUFFICIENT_CASH_FOR_ONE_LOT"
            return
        result = buy(book,rule,quantity,raw,at.date(),calendar.next(at.date()))
        order.update(target=quantity, remaining=0)
        cycle["status"] = "OPEN"
    else:
        unlocked = sum(l["remaining"] for l in eligible_lots(book,at.date()))
        quantity = min(order["remaining"],unlocked)
        if not quantity:
            return
        try:
            result = sell(book,rule,quantity,raw,at.date())
        except ValueError as exc:
            if str(exc)!="INSUFFICIENT_CASH_FOR_SELL_FEE":
                raise
            order["last_block"] = str(exc)
            return
        order["remaining"] -= quantity
        if not shares(book):
            cycle["status"],cycle["closed_at"] = "CLOSED",quote["observed_at"]
    order.pop("last_block",None)
    if order["remaining"] == 0:
        order["status"] = "FILLED"
    book["fills"].append(dict(result, action=order["action"], order_id=order["id"],
        filled_at=quote["observed_at"], quote_id=quote["id"], received_at=quote["received_at"],
        price_source=quote["source"], raw_price=quote["price_cny"], revision=quote["revision"]))


def view(cycle, latest, now):
    state = dict(cycle)
    rule = Rules(**cycle["rule"])
    quote = latest if shares(cycle["book"]) and latest and latest["tradeable"] and moment(latest["observed_at"]).date()==now.date() else None
    state["valuation"] = valuation(cycle["book"], rule, decimal(quote["price_cny"]) if quote else None)
    state["valuation_price_at"] = quote["observed_at"] if quote else None
    state["position_duration_seconds"] = None
    if cycle["book"]["fills"]:
        start = moment(cycle["book"]["fills"][0]["filled_at"])
        end = moment(cycle["closed_at"]) if cycle["closed_at"] else now
        state["position_duration_seconds"] = (end-start)//timedelta(seconds=1)
    order = pending(cycle)
    if order:
        t1 = order["action"] != "BUY" and not eligible_lots(cycle["book"], now.date())
        state["pending_reason"] = ("WAIT_CALENDAR" if order["eligible_at"] is None else "WAIT_T1") if t1 else order.get("last_block") or "WAIT_PRICE"
    else:
        state["pending_reason"] = None
    return state
