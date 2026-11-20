"""Market regime from stored Kaipanla 107 index bars; no stock or ETF data."""
from datetime import date
from decimal import localcontext
import re

from ..simulator import PRECISION, decimal
from .inputs import document

BULL, RANGE, BEAR = "BULL_OFFENSE", "RANGE_ROTATION", "BEAR_DEFENSE"
FALLBACK = "REGIME_FALLBACK_RANGE"
INDEXES = {"DUAL": ("SH000300", "SH000852"), "SINGLE": ("SH000001",)}
COLLECTED = ("SH000300", "SH000852", "SH000001")
KEEP_ROWS = 120


def normalize_index(body, code):
    """Rows of one index, ascending by trade date; prices and amount stay decimal strings."""
    if not isinstance(body, dict) or str(body.get("errcode")) != "0" or body.get("StockID") != code:
        raise ValueError("index bars rejected or identity mismatch")
    days, prices, amounts = body.get("x"), body.get("y"), body.get("bal")
    if (not all(isinstance(part, list) for part in (days, prices, amounts)) or not days
            or not len(days) == len(prices) == len(amounts)):
        raise ValueError("index bars missing or misaligned")
    rows, previous = [], None
    for day, bar, amount in zip(days, prices, amounts):
        text = str(day)
        if not re.fullmatch(r"[0-9]{8}", text) or not isinstance(bar, list) or len(bar) != 4:
            raise ValueError("invalid index bar")
        when = date(int(text[:4]), int(text[4:6]), int(text[6:]))
        opened, close, high, low, turnover = (
            decimal(str(value) if isinstance(value, float) else value) for value in (*bar, amount))
        if (previous is not None and when <= previous) or min(opened, close, high, low) <= 0 \
                or low > min(opened, close) or high < max(opened, close) or turnover < 0:
            raise ValueError("invalid index bar")
        previous = when
        rows.append(dict(trade_date=when.isoformat(), open=str(opened), close=str(close),
                         high=str(high), low=str(low), amount_cny=str(turnover)))
    return rows[-KEEP_ROWS:]


def load_series(ledger, at=None):
    """Latest stored bars per index, optionally as known at one instant."""
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute("""SELECT DISTINCT ON (scope->>'StockID') scope->>'StockID',body
            FROM supplier_market_batches WHERE provider='kaipanla' AND endpoint='107'
            AND scope->>'purpose'='index-kline' AND (%s::timestamptz IS NULL OR collected_at<=%s)
            ORDER BY scope->>'StockID',collected_at DESC,batch_id DESC""", (at, at)).fetchall()
    return {code: document(body)["rows"] for code, body in rows}


def classify(series, as_of, mode, *, bull="1.10", bear="0.90"):
    """Regime for a buy filled on the session after ``as_of`` (T-1, ISO date)."""
    codes = INDEXES[mode]
    result = dict(market_regime=RANGE, regime_mode=mode, regime_as_of=as_of, flags=[FALLBACK], indexes={})
    windows = []
    for code in codes:
        rows = [row for row in series.get(code, ()) if row["trade_date"] <= as_of]
        if len(rows) < 25 or rows[-1]["trade_date"] != as_of:
            return result
        windows.append(rows[-25:])
    with localcontext(PRECISION):
        above, below, rising, falling = [], [], [], []
        for code, rows in zip(codes, windows):
            closes = [decimal(row["close"]) for row in rows]
            ma20, earlier = sum(closes[-20:]) / 20, sum(closes[-25:-5]) / 20
            above.append(closes[-1] > ma20)
            below.append(closes[-1] < ma20)
            rising.append(ma20 > earlier)
            falling.append(ma20 < earlier)
            result["indexes"][code] = dict(close=str(closes[-1]), ma20=str(ma20), ma20_prior5=str(earlier))
        amounts = [sum(decimal(rows[i]["amount_cny"]) for rows in windows) for i in range(5, 25)]
        average20 = sum(amounts) / 20
        if average20 <= 0:
            return result
        ratio = sum(amounts[-5:]) / 5 / average20
        if all(above) and all(rising) and ratio >= decimal(bull):
            regime = BULL
        elif all(below) and (any(falling) or ratio <= decimal(bear)):
            regime = BEAR
        else:
            regime = RANGE
    return dict(result, market_regime=regime, flags=[], volume_ratio=str(ratio))


def recent(series, mode, count=5):
    """Regime that applies after each of the latest sessions, oldest first."""
    days = sorted(set.intersection(*({row["trade_date"] for row in series.get(code, ())}
                                     for code in INDEXES[mode])))[-count:]
    return [classify(series, day, mode) for day in days]
