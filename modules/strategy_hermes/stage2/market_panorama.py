"""Read-only panorama assembled from persisted breadth, turnover and global data."""
from datetime import datetime
from decimal import Decimal
import json

from ..simulator import CHINA
from .market_bootstrap import instant


def _count(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    return value if type(value) is int and 0 <= value <= 100000 else None


def normalize_background(body, collected_at):
    if str(body.get("errcode")) != "0" or not isinstance(body.get("list"), dict):
        raise ValueError("invalid market breadth response")
    rows = body["list"]
    values = {target: _count(rows.get(key)) for key, target in (
        ("SZJS", "up_count"), ("XDJS", "down_count"),
        ("ZTJS", "limit_up_count"), ("DTJS", "limit_down_count"))}
    if all(value is None for value in values.values()):
        raise ValueError("market breadth has no usable counts")
    observed = None
    if body.get("Time") is not None:
        observed = instant(body["Time"], datetime.fromisoformat(collected_at), milliseconds=False)
    up, down = values["up_count"], values["down_count"]
    # The registered 018 contract has no flat-count field. Its turnover fields
    # are not used to infer stock counts, A-share amounts, or source time.
    ratio = str(Decimal(up) / Decimal(up + down)) if up is not None and down is not None and up + down else None
    return dict(schema_version="stock-v4-market-breadth-v1", source="开盘啦 018 · 市场涨跌家数",
                observed_at=observed, collected_at=collected_at,
                trade_date=observed[:10] if observed else None, flat_count=None,
                up_ratio=ratio, **values)


def load_background(ledger):
    with ledger._connect() as conn:
        row = conn.execute("""SELECT body,collected_at,source_clock FROM supplier_market_batches
            WHERE provider='kaipanla' AND endpoint='018'
            ORDER BY collected_at DESC,batch_id DESC LIMIT 1""").fetchone()
    if row is None:
        return None
    body = json.loads(row[0])
    if body.get("schema_version") == "stock-v4-market-breadth-v1":
        return body
    # Earlier probes saved the same registered fields under 'rows'.
    if not isinstance(body.get("rows"), dict):
        return None
    result = normalize_background(dict(errcode="0", list=body["rows"]), row[1].astimezone(CHINA).isoformat())
    if row[2]:
        result.update(observed_at=row[2].astimezone(CHINA).isoformat(), trade_date=row[2].astimezone(CHINA).date().isoformat())
    return result


INDEX_NAMES = {"SH000300": "沪深300", "SH000852": "中证1000", "SH000001": "上证指数"}


def load_regime(ledger):
    """Market regime after each of the latest five stored sessions; stored 107 bars only."""
    from .market_regime import FALLBACK, INDEXES, classify, stored_series
    mode = getattr(getattr(ledger, "settings", None), "regime_mode", None) or "DUAL"
    with ledger._connect() as conn:
        series = stored_series(conn)
        collected, = conn.execute("""SELECT max(collected_at) FROM supplier_market_batches
            WHERE provider='kaipanla' AND endpoint='107' AND scope->>'purpose'='index-kline'""").fetchone()
    codes = INDEXES[mode]
    sessions = sorted(set().union(*({row["trade_date"] for row in series.get(code, ())} for code in codes)))[-5:]
    if not sessions:
        return None
    days = []
    for day in sessions:
        result = classify(series, day, mode)
        missing = FALLBACK in result["flags"]
        indexes = []
        for code in codes:
            row = result["indexes"].get(code)
            indexes.append(dict(code=code, name=INDEX_NAMES[code], **(dict(close=None, ma20=None, above_ma20=None,
                ma20_rising=None) if missing or row is None else dict(close=row["close"], ma20=str(Decimal(row["ma20"]).quantize(Decimal("0.01"))),
                above_ma20=Decimal(row["close"]) > Decimal(row["ma20"]),
                ma20_rising=Decimal(row["ma20"]) > Decimal(row["ma20_prior5"])))))
        days.append(dict(trade_date=day, market_regime=None if missing else result["market_regime"],
                         status="INSUFFICIENT_DATA" if missing else "READY",
                         volume_ratio=(str(Decimal(result["volume_ratio"]).quantize(Decimal("0.0001")))
                                       if "volume_ratio" in result else None), indexes=indexes))
    return dict(schema_version="stock-v4-market-regime-v1", source="开盘啦 107 · 指数日K线", mode=mode,
                indexes=[dict(code=code, name=INDEX_NAMES[code]) for code in codes],
                observed_at=sessions[-1], collected_at=collected.astimezone(CHINA).isoformat(), days=days)


def read_panorama(ledger, macro_file=None):
    from .macro_background import read_background
    from .macro_refresh import load as load_global
    from .market_turnover import load as load_turnover
    return dict(background=load_background(ledger), regime=load_regime(ledger), turnover=load_turnover(ledger),
                global_indices=load_global(ledger) or read_background(macro_file))
