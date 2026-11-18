"""Validate explicit historical ETF distributions and unit conversions."""
from datetime import date, datetime, time

from .simulator import CHINA, decimal, integer, moment


def action_schedule(rows, instruments, calendar):
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("scenario supports up to 500 corporate actions")
    scheduled, seen, dates = [], set(), set()
    for raw in rows:
        code, kind, source = raw["etf"], raw["kind"], raw["source"]
        if code not in instruments or not isinstance(source, str) or not source.strip() or len(source) > 300:
            raise ValueError("corporate action requires a known ETF and explicit source")
        identifier = raw["id"]
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 80 or identifier in seen:
            raise ValueError("invalid or duplicate corporate action identifier")
        seen.add(identifier)
        publication = moment(raw["published_at"])
        event = dict(id=identifier, etf=code, source=source.strip(), kind=kind, published_at=publication.isoformat())
        if kind == "CASH_DIVIDEND":
            record, ex, pay = (date.fromisoformat(raw[field]) for field in ("record_date", "ex_date", "pay_date"))
            if record not in calendar.days or ex not in calendar.days or not record < ex <= pay:
                raise ValueError("invalid dividend record, ex-dividend, or payment date")
            record_at = datetime.combine(record, time(15), CHINA)
            amount = decimal(raw["cash_per_share_cny"])
            if publication > record_at or not 0 < amount <= 10000:
                raise ValueError("dividend must have a known record date and positive cash per share")
            event.update(record_date=record.isoformat(), ex_date=ex.isoformat(), pay_date=pay.isoformat(),
                         cash_per_share_cny=str(amount))
            schedule = ((record_at, "RECORD"), (datetime.combine(ex, time.min, CHINA), "EX"),
                        (datetime.combine(pay, time.min, CHINA), "PAY"))
        elif kind == "SHARE_SPLIT":
            effective = date.fromisoformat(raw["effective_date"])
            at = datetime.combine(effective, time.min, CHINA)
            if effective not in calendar.days or publication > at or (code, effective) in dates:
                raise ValueError("invalid, late, or ambiguous unit conversion")
            dates.add((code, effective))
            event.update(effective_date=effective.isoformat(), numerator=integer(raw["numerator"]),
                         denominator=integer(raw["denominator"]))
            schedule = ((at, "SPLIT"),)
        else:
            raise ValueError("unsupported corporate action")
        for at, step in schedule:
            # The record snapshot follows quotes at the session close. On the same ex/pay
            # date, invalidate the cum-dividend quote before releasing the cash.
            priority = {"RECORD": 2, "SPLIT": -3, "EX": -2, "PAY": -1}[step]
            scheduled.append((at, priority, identifier, (step, event)))
    return scheduled
