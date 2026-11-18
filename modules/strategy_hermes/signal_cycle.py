"""Deterministic automatic intents. No quote fetching or real order routing."""
from dataclasses import asdict
from datetime import datetime, time, timedelta
from decimal import localcontext

from .simulator import (PRECISION, Rules, affordable, buy, decimal, eligible_lots,
                        floor_lot, moment, new_book, sell, shares, valuation)

ACTIVE = ("WAIT_BUY", "OPEN", "WAIT_EXIT")


def create_cycle(code, unit, rules, report, choice, candidates):
    at, sid = report["at"], choice["sector_id"]
    cycle = dict(etf=code, primary_sector=sid, unit=unit, status="WAIT_BUY",
                 rule=asdict(rules), book=new_book(rules), orders=[], reduced=False,
                 last_decision="UP", last_report=report["id"], closed_at=None,
                 entry_sources=candidates)
    add_order(cycle, "BUY", report)
    return cycle


def add_order(cycle, kind, report, target=None, theoretical=None):
    assert not pending(cycle)
    at = report["at"]
    row = report["sectors"][cycle["primary_sector"]]
    order = dict(id=f"{report['id']}:{kind}", action=kind, report_id=report["id"], signal_at=at,
                 requested_at=at, eligible_at=at if kind=="BUY" else None, remaining=target,
                 target=target, theoretical=None if theoretical is None else str(theoretical),
                 status="PENDING" if target != 0 else "BELOW_TRADING_UNIT",
                 sector_id=cycle["primary_sector"], evidence=row)
    cycle["orders"].append(order)
    return order


def pending(cycle):
    return next((o for o in reversed(cycle["orders"]) if o["status"]=="PENDING"), None)


def update_eligible(order, book, at):
    unlocked = [lot["sellable_from"] for lot in book["lots"] if lot["remaining"] and lot["sellable_from"]]
    if unlocked and order["eligible_at"] is None:
        start = datetime.combine(datetime.fromisoformat(min(unlocked)).date(), time(9,30), tzinfo=at.tzinfo)
        order["eligible_at"] = max(start, at).isoformat()


def apply_report(cycle, report):
    if cycle["status"] not in ACTIVE:
        return
    decision = report["sectors"].get(cycle["primary_sector"], {}).get("state", "UNKNOWN")
    cycle["last_decision"], cycle["last_report"] = decision, report["id"]
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
