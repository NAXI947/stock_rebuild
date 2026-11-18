"""Pure quantity, cash and FIFO calculations for frozen A-share ETF replay."""
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Context, Decimal, ROUND_DOWN, ROUND_HALF_UP, localcontext
import re
from zoneinfo import ZoneInfo

CHINA = ZoneInfo("Asia/Shanghai")
CENT = Decimal("0.01")
PRECISION = Context(prec=38, rounding=ROUND_HALF_UP)
CODE = re.compile(r"[0-9]{6}\.(?:SH|SZ)\Z")
SESSIONS = ((time(9, 30), time(11, 30)), (time(13), time(15)))


def decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("expected exact decimal input")
    try:
        result = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite() or len(result.as_tuple().digits) > 18 or abs(result.as_tuple().exponent) > 12:
        raise ValueError("decimal outside supported precision")
    return result


def money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def integer(value, *, minimum=1):
    if type(value) is not int or not minimum <= value <= 10**12:
        raise ValueError("invalid share count or trading unit")
    return value


def floor_lot(value, unit):
    return int((value / unit).to_integral_value(rounding=ROUND_DOWN)) * unit


def moment(value):
    dt = datetime.fromisoformat(value)
    if dt.utcoffset() is None:
        raise ValueError("timestamp requires a timezone")
    return dt.astimezone(CHINA)


def session(dt):
    return any(start <= dt.time() <= end for start, end in SESSIONS)


class Calendar:
    def __init__(self, values):
        self.days = tuple(date.fromisoformat(day) for day in values)
        if not self.days or self.days != tuple(sorted(set(self.days))):
            raise ValueError("calendar must contain ordered unique trading days")

    def next(self, day):
        if day not in self.days:
            raise ValueError("date outside frozen trading calendar")
        return next((other for other in self.days if other > day), None)

    def legal(self, dt):
        return dt.date() in self.days and session(dt)


@dataclass(frozen=True)
class Rules:
    version: str = "hermes_signal_cycle_v1"
    initial_cash: str = "10000.00"
    reduce_ratio: str = "0.50"
    fee_rate: str | None = None
    minimum_fee: str | None = None
    slippage_bps: str | None = None
    cash_rounding: str = "CNY_CENT_HALF_UP"
    matching: str = "FIFO_T1_FIRST_VALID_POST_SIGNAL"
    pending_sell: str = "CARRY_UNTIL_EXIT"

    def __post_init__(self):
        if not self.version or money(decimal(self.initial_cash)) <= 0 or not 0 < decimal(self.reduce_ratio) < 1:
            raise ValueError("invalid rules")
        values = (self.fee_rate, self.minimum_fee, self.slippage_bps)
        if any(v is not None for v in values) and any(v is None for v in values):
            raise ValueError("fee, minimum and slippage must be configured together")
        if self.fee_rate is not None:
            if not 0 <= decimal(self.fee_rate) < 1 or decimal(self.minimum_fee) < 0 or not 0 <= decimal(self.slippage_bps) < 10000:
                raise ValueError("invalid transaction costs")
        if (self.cash_rounding, self.matching, self.pending_sell) != (
            "CNY_CENT_HALF_UP", "FIFO_T1_FIRST_VALID_POST_SIGNAL", "CARRY_UNTIL_EXIT"
        ):
            raise ValueError("unsupported execution policy")

    @property
    def basis(self):
        return "GROSS" if self.fee_rate is None else "NET"

    def execution_price(self, raw, *, sell):
        bps = decimal(self.slippage_bps or "0") / 10000
        return raw * (1 - bps if sell else 1 + bps)

    def fee(self, amount):
        if self.fee_rate is None:
            return Decimal(0)
        return money(max(amount * decimal(self.fee_rate), decimal(self.minimum_fee)))


def shares(book):
    return sum(x["remaining"] for x in book["lots"])


def eligible_lots(book, day):
    return [x for x in book["lots"] if x["remaining"] and x["sellable_from"] and x["sellable_from"] <= day.isoformat()]


def new_book(rules):
    return dict(cash=str(money(decimal(rules.initial_cash))), realized="0.00", lots=[], fills=[])


def buy(book, rules, quantity, price, day, next_day):
    """Apply only executable shares; all stored amounts are exact decimal strings."""
    with localcontext(PRECISION):
        quantity = integer(quantity)
        p = rules.execution_price(price, sell=False)
        amount = money(quantity * p)
        fee = rules.fee(amount)
        cash = decimal(book["cash"]) - amount - fee
        if cash < 0:
            raise ValueError("INSUFFICIENT_CASH")
        book["lots"].append(dict(bought_on=day.isoformat(),
            sellable_from=next_day.isoformat() if next_day else None, remaining=quantity,
            basis=str(amount + fee)))
        book["cash"] = str(cash)
        return dict(quantity=quantity, price=str(p), amount=str(amount), fee=str(fee))


def affordable(cash, rules, raw, unit):
    with localcontext(PRECISION):
        p = rules.execution_price(raw, sell=False)
        # Binary search pays the same rounding/fees as the actual buy.
        hi = floor_lot((cash + CENT / 2) / p, unit) // unit
        lo = 0
        while lo < hi:
            n = (lo + hi + 1) // 2
            amount = money(n * unit * p)
            if amount + rules.fee(amount) <= cash:
                lo = n
            else:
                hi = n - 1
        return lo * unit


def sell(book, rules, quantity, price, day):
    with localcontext(PRECISION):
        quantity = integer(quantity)
        lots = eligible_lots(book, day)
        if quantity > sum(lot["remaining"] for lot in lots):
            raise ValueError("INSUFFICIENT_T1_SHARES")
        p = rules.execution_price(price, sell=True)
        amount = money(quantity * p)
        fee = rules.fee(amount)
        cash = decimal(book["cash"]) + amount - fee
        if cash < 0:
            raise ValueError("INSUFFICIENT_CASH_FOR_SELL_FEE")
        basis, left = Decimal(0), quantity
        for lot in lots:
            if not left:
                break
            take = min(left, lot["remaining"])
            previous = decimal(lot["basis"])
            used = previous if take == lot["remaining"] else money(previous * take / lot["remaining"])
            lot["remaining"] -= take
            lot["basis"] = str(previous - used)
            basis += used
            left -= take
        book["cash"] = str(cash)
        book["realized"] = str(decimal(book["realized"]) + amount - fee - basis)
        return dict(quantity=quantity, price=str(p), amount=str(amount), fee=str(fee), basis=str(basis))


def valuation(book, rules, price=None):
    with localcontext(PRECISION):
        cash = decimal(book["cash"])
        realized = decimal(book["realized"])
        initial = money(decimal(rules.initial_cash))
        n = shares(book)
        market = Decimal(0) if not n else None if price is None else money(n * price)
        equity = None if market is None else cash + market
        profit = None if equity is None else equity - initial
        ratio = None if profit is None else (profit / initial * 100).quantize(Decimal("0.0001"))
        unrealized = None if market is None else market - sum((decimal(l["basis"]) for l in book["lots"]), Decimal(0))
        return dict(shares=n, cash=str(cash), realized=str(realized), basis=rules.basis,
                    unrealized=None if unrealized is None else str(unrealized),
                    equity=None if equity is None else str(equity),
                    profit=None if profit is None else str(profit),
                    return_pct=None if ratio is None else str(ratio))
