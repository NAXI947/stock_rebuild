"""Pure, versioned recommendation checks; never creates trading orders."""
from datetime import date
from decimal import Decimal, localcontext

from ..simulator import PRECISION, decimal, moment, session
from .inputs import CLOSED_STATUS

METHOD = "etf-v16-recommendation-v1"
MAX_SKEW_SECONDS = 120
CURVE = ((0, "0"), (5, ".06"), (65, ".38"), (120, ".55"),
         (125, ".58"), (185, ".78"), (240, "1"))


def number(value):
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        return decimal(value)
    except ValueError:
        return None


def check(result, reason, value=None, *, required=True):
    return dict(result=result, reason=reason,
                value=str(value) if isinstance(value, Decimal) else value, required=required)


def quote_time(quote, at, reference=None):
    if not isinstance(quote, dict):
        return None
    try:
        seen = moment(quote["observed_at"])
    except (ValueError, TypeError, KeyError):
        return None
    if (seen.date() != at.date() or seen > at or not session(seen)
            or quote.get("trade_date", at.date().isoformat()) != at.date().isoformat()):
        return None
    if reference is not None and abs((seen - reference).total_seconds()) > MAX_SKEW_SECONDS:
        return None
    return seen


def valid_quote(quote, at, reference=None):
    seen = quote_time(quote, at, reference)
    price = number(quote.get("price_cny")) if isinstance(quote, dict) else None
    status = str(quote.get("trading_status", "")).upper() if isinstance(quote, dict) else ""
    if price is None or price <= 0 or status in CLOSED_STATUS | {"HALTED", "DELISTED"} or quote.get("tradeable") is False:
        return None
    return seen


def history_ready(stats, at):
    try:
        return (date.fromisoformat(stats["history_as_of"]) < at.date()
                and moment(stats["history_available_at"]) <= at)
    except (ValueError, TypeError, KeyError):
        return False


def history_count(stats):
    count = stats.get("history_days")
    return count if type(count) is int and count >= 0 else 0


def aggregate(checks):
    if any(row["result"] == "FAIL" for row in checks):
        return "FAIL"
    if any(row["result"] == "UNKNOWN" for row in checks):
        return "UNKNOWN"
    return "PASS"


def alpha(at):
    minute = at.hour * 60 + at.minute + Decimal(at.second) / 60
    elapsed = min(120, max(0, minute - 570)) + min(120, max(0, minute - 780))
    for (left, a), (right, b) in zip(CURVE, CURVE[1:]):
        if left <= elapsed <= right:
            return Decimal(a) + (Decimal(b) - Decimal(a)) * (elapsed - left) / (right - left)
    return None


def evaluate(quote, metrics, stats, at, *, iopv=None):
    """Return independent evidence checks; same-day stale values remain visible elsewhere."""
    with localcontext(PRECISION):
        return _evaluate(quote or {}, metrics, stats or {}, at, iopv=iopv)


def _evaluate(quote, metrics, stats, at, *, iopv=None):
    checks = {}
    clock = valid_quote(quote, at)
    # Local eligibility of this recommendation, not a global snapshot freshness gate.
    current = clock is not None and session(at) and abs((at-clock).total_seconds()) <= MAX_SKEW_SECONDS
    checks["quote"] = check("PASS" if current else "UNKNOWN",
                            "CURRENT_QUOTE" if current else "QUOTE_NOT_CURRENT")
    last, previous = number(quote.get("price_cny")), number(quote.get("pre_close_cny"))
    change = last / previous - 1 if current and previous is not None and previous > 0 else None
    checks["etf_return"] = check("UNKNOWN" if change is None else "PASS" if change >= Decimal(".005") else "FAIL",
                                "ETF_RETURN_MISSING" if change is None else "MIN_RETURN_0_5_PERCENT", change)
    q, qt = number(metrics.get("Q")), number(metrics.get("Qt"))
    r3, active, est = (number(metrics.get(key)) for key in ("R_top3", "U_active", "R_est"))
    enough = current and q is not None and q >= Decimal(".85") and qt is not None and qt >= Decimal(".80")
    resonance = None if not enough or r3 is None or active is None else r3 > 0 and active >= Decimal(".30")
    checks["resonance"] = check("UNKNOWN" if resonance is None else "PASS" if resonance else "FAIL",
                               "CONSTITUENT_EVIDENCE_INSUFFICIENT" if resonance is None else "HEAVYWEIGHT_RESONANCE",
                               {"R_top3": str(r3) if r3 is not None else None,
                                "U_active": str(active) if active is not None else None,
                                "Q": str(q) if q is not None else None, "Qt": str(qt) if qt is not None else None})
    amount, volume = number(quote.get("amount_cny")), number(quote.get("volume_shares"))
    vwap = None
    if (current and quote.get("volume_unit_verified") is True and quote.get("amount_unit_verified") is True and amount is not None and amount > 0
            and volume is not None and volume > 0):
        proposed = amount / volume
        low, high, tick = (number(quote.get(key)) for key in ("low_cny", "high_cny", "tick_size"))
        valid = (low - tick <= proposed <= high + tick) if (
            low is not None and high is not None and tick is not None and 0 < low <= high and tick > 0
        ) else Decimal(".85") <= proposed / last <= Decimal("1.15")
        if valid:
            vwap = proposed
    checks["vwap"] = check("UNKNOWN" if vwap is None else "PASS" if last >= vwap * Decimal(".998") else "FAIL",
                           "VWAP_BASIS_UNVERIFIED" if vwap is None else "VWAP_SUPPORT", vwap)
    ready = history_ready(stats, at)
    ma5, prior_close = number(stats.get("ma5_raw_prev5")), number(stats.get("prior_close_cny"))
    basis = (ready and stats.get("ma5_basis_status") == "VERIFIED" and ma5 is not None and ma5 > 0
             and prior_close is not None and prior_close > 0 and previous is not None and previous > 0
             and abs(previous/prior_close-1) <= Decimal(".001"))
    checks["ma5"] = check("UNKNOWN" if not current or not basis else "PASS" if last >= ma5 else "FAIL",
                          "PRICE_BASIS_UNVERIFIED" if not basis else "MA5_PREVIOUS_5_DAYS", ma5 if basis else None)
    follow = None
    if not current or est is None:
        checks["follow"] = check("UNKNOWN", "INDEX_RETURN_UNAVAILABLE")
    elif est < 0:
        checks["follow"] = check("FAIL", "INDEX_RETURN_NEGATIVE", est)
    elif est < Decimal(".008"):
        checks["follow"] = check("NOT_APPLICABLE", "INDEX_RETURN_BELOW_0_8_PERCENT", est)
    elif change is None:
        checks["follow"] = check("UNKNOWN", "ETF_RETURN_MISSING")
    else:
        follow = change / est
        checks["follow"] = check("PASS" if follow >= Decimal(".75") else "FAIL", "MIN_FOLLOW_0_75", follow)
    adv = number(stats.get("adv20")) if ready and history_count(stats) >= 20 else None
    portion = alpha(clock) if current else None
    vr = amount / (adv * portion) if (
        adv is not None and adv > 0 and amount is not None and amount >= 0 and portion is not None and portion > 0
    ) else None
    checks["volume_ratio"] = check("OBSERVE_ONLY", "VR_OBSERVATION" if vr is not None else "VR_DATA_MISSING",
                                   vr, required=False)
    divergence = change-est if current and change is not None and est is not None else None
    checks["divergence"] = check("OBSERVE_ONLY",
        "DIVERGENCE_WARNING" if divergence is not None and divergence > Decimal(".008") else
        "DIVERGENCE_OBSERVATION" if divergence is not None else "DIVERGENCE_DATA_MISSING",
        divergence, required=False)
    bid, ask = number(quote.get("bid1_cny")), number(quote.get("ask1_cny"))
    book_current = current
    if "book_observed_at" in quote:
        book_clock = quote_time(dict(observed_at=quote["book_observed_at"]), at, clock)
        book_current = current and book_clock is not None and (at-book_clock).total_seconds() <= MAX_SKEW_SECONDS
    spread = (ask-bid)/((ask+bid)/2) if book_current and bid is not None and ask is not None and 0 < bid <= ask else None
    checks["spread"] = check("UNKNOWN" if spread is None else "PASS" if spread <= Decimal(".0025") else "FAIL",
                             "BOOK_UNAVAILABLE" if spread is None else "MAX_SPREAD_0_25_PERCENT", spread, required=False)
    premium = None
    if current and isinstance(iopv, dict) and iopv.get("source"):
        value = number(iopv.get("iopv"))
        if value is not None and value > 0 and quote_time(iopv, at, clock) is not None:
            premium = last/value-1
    checks["premium"] = check("UNKNOWN" if premium is None else "PASS" if premium <= Decimal(".008") else "FAIL",
                              "IOPV_UNAVAILABLE" if premium is None else "MAX_PREMIUM_0_8_PERCENT", premium, required=False)
    momentum = aggregate([checks[key] for key in ("quote", "etf_return", "resonance", "vwap", "ma5", "follow")])
    execution_checks = [checks[key] for key in ("premium", "spread")]
    execution = "FAILED" if aggregate(execution_checks) == "FAIL" else "PARTIAL" if aggregate(execution_checks) == "UNKNOWN" else "COMPLETE"
    return dict(checks=checks, momentum_status=momentum, execution_status=execution,
                quote_current=current, spread=str(spread) if spread is not None else None,
                max_cross_dataset_skew_seconds=MAX_SKEW_SECONDS)
