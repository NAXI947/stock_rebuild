"""Idempotent manual recording of actual fills, isolated from simulation."""
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
import json
import re

from .ledger import encode
from .simulator import CHINA, CODE, PRECISION, Calendar, decimal, integer, moment, money

IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{8,96}\Z")
LIMIT = 5000


class FillConflict(ValueError):
    pass


def record(ledger, row):
    request_id, code, side = row["request_id"], row["etf"], row["side"]
    if (not isinstance(request_id,str) or not IDENTIFIER.fullmatch(request_id)
            or not CODE.fullmatch(code) or side not in ("BUY","SELL")):
        raise ValueError("invalid actual fill identity")
    at = moment(row["executed_at"])
    if at > datetime.now(CHINA)+timedelta(minutes=1):
        raise ValueError("actual fill time is in the future")
    price = decimal(row["price_cny"])
    fees = decimal(row["fees_cny"]) if row.get("fees_cny") not in ("",None) else None
    quantity = integer(row["quantity"]) if row.get("quantity") is not None else None
    if price <= 0 or (fees is not None and fees < 0):
        raise ValueError("invalid actual price or fee")
    body = dict(request_id=request_id,etf=code,side=side,executed_at=at.isoformat(),
                price_cny=str(price.normalize()),quantity=quantity,
                fees_cny=str(fees.normalize()) if fees is not None else None)
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(ledger.ns+":actual-trades",))
        previous = conn.execute("SELECT body FROM actual_trades WHERE ns=%s AND request_id=%s",
                                (ledger.ns,request_id)).fetchone()
        if previous:
            if json.loads(previous[0]) != body:
                raise FillConflict("request ID reused for a different fill")
            return body, False
        count, = conn.execute("SELECT count(*) FROM actual_trades WHERE ns=%s",(ledger.ns,)).fetchone()
        if count >= LIMIT:
            raise ValueError("actual ledger exceeds current display bound")
        conn.execute("INSERT INTO actual_trades(ns,request_id,executed_at,body) VALUES (%s,%s,%s,%s)",
                     (ledger.ns,request_id,at,encode(body)))
    return body, True


def read(ledger, days, now):
    with ledger._connect() as conn:
        rows = conn.execute(
            "SELECT body FROM actual_trades WHERE ns=%s AND executed_at<=%s ORDER BY executed_at,created_at,request_id",
            (ledger.ns,now),
        ).fetchall()
    fills = [json.loads(row[0]) for row in rows]
    calendar = Calendar(days) if days else None
    lots, incomplete = defaultdict(list), set()
    with localcontext(PRECISION):
        for fill in fills:
            code, quantity = fill["etf"], fill["quantity"]
            if code in incomplete:
                continue
            day = moment(fill["executed_at"]).date()
            if quantity is None or not calendar or day not in calendar.days:
                incomplete.add(code)
                continue
            if fill["side"] == "BUY":
                unlock = calendar.next(day)
                if unlock is None:
                    incomplete.add(code)
                    continue
                fees = fill["fees_cny"]
                basis = (money(decimal(fill["price_cny"])*quantity+decimal(fees))
                         if fees is not None else None)
                lots[code].append([unlock,quantity,basis])
            else:
                eligible = [lot for lot in lots[code] if lot[0] <= day and lot[1]]
                if sum(lot[1] for lot in eligible) < quantity:
                    incomplete.add(code)
                    continue
                for lot in eligible:
                    take = min(lot[1],quantity)
                    if lot[2] is not None:
                        lot[2] -= lot[2] if take == lot[1] else money(lot[2]*take/lot[1])
                    lot[1] -= take
                    quantity -= take
                    if not quantity:
                        break
        positions = []
        for code in sorted(set(lots)|incomplete):
            rows = [lot for lot in lots[code] if lot[1]]
            known = code not in incomplete
            total = sum(lot[1] for lot in rows) if known else None
            sellable = sum(lot[1] for lot in rows if lot[0] <= now.date()) if known else None
            basis = (str(sum((lot[2] for lot in rows),Decimal("0.00")))
                     if known and all(lot[2] is not None for lot in rows) else None)
            if total or not known:
                positions.append(dict(etf=code,quantity=total,sellable=sellable,
                                      locked=total-sellable if known else None,basis_cny=basis))
    return dict(fills=list(reversed(fills)),positions=positions,observed_at=now.isoformat())
