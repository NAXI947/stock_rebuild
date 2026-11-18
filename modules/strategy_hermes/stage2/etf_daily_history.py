"""Collect unadjusted ETF daily bars for offline touch evaluation in the test database."""
import argparse
from datetime import date, datetime, time
from pathlib import Path
import re

from ..ledger import encode
from ..simulator import CHINA, CODE, decimal
from .bulk_collect import RequestLog
from .inputs import document, trading_days
from .market_bootstrap import ProbeError, save
from .postgres import PostgresLedger
from .settings import Settings
from .tushare_bootstrap import SourceFailure, Tushare

ENDPOINT = "fund_daily"
SOURCE = f"tushare:{ENDPOINT}"


def scope_for(symbol, first, last, calendar, now):
    if not CODE.fullmatch(symbol):
        raise ValueError("invalid ETF symbol")
    if first not in calendar or last not in calendar or not 0 <= (last - first).days < 93:
        raise ValueError("ETF daily range must use trading-day endpoints within 93 days")
    if now < datetime.combine(last, time(15), CHINA):
        raise ValueError("ETF daily range includes an unfinished trading session")
    return {
        "symbols": [symbol], "from": first.isoformat(), "to": last.isoformat(),
        "period": "1d", "adjust": "none",
    }


def normalize_bars(raw, symbol, first, last, calendar):
    bars = {}
    for row in raw:
        try:
            day = date.fromisoformat(row["trade_date"])
            opening, high, low, close = (decimal(row[key]) for key in ("open", "high", "low", "close"))
        except (ValueError, TypeError, ArithmeticError):
            raise SourceFailure("invalid ETF daily date or price") from None
        if row["ts_code"] != symbol or not first <= day <= last or day not in calendar or day in bars:
            raise SourceFailure("ETF daily response identity, date or uniqueness mismatch")
        if not 0 < low <= min(opening, close) <= max(opening, close) <= high:
            raise SourceFailure("inconsistent ETF daily OHLC")
        bars[day] = {
            "etf": symbol, "trade_date": day.isoformat(), "price_basis": "UNADJUSTED",
            "open_cny": str(opening), "high_cny": str(high), "low_cny": str(low), "close_cny": str(close),
        }
    return [bars[day] for day in sorted(bars)]


def collect(store, config, symbol, first, last, calendar, run_key):
    scope = scope_for(symbol, first, last, calendar, datetime.now(CHINA))
    budget = RequestLog(store, run_key)

    def fetch_batch():
        params = {"ts_code": symbol, "start_date": first.strftime("%Y%m%d"), "end_date": last.strftime("%Y%m%d")}
        raw = Tushare(config).fetch(ENDPOINT, params)
        received = datetime.now(CHINA)
        bars = normalize_bars(raw, symbol, first, last, calendar)
        return save(store, "tushare", ENDPOINT, scope, {"bars": bars}, len(bars), None, received)

    result = budget.call(SOURCE, scope, fetch_batch, limit=1)
    return result | {"new_requests": budget.new_calls}


def read_prices(store, batch):
    """Return evaluator-ready price evidence from the isolated database."""
    if not re.fullmatch(r"[0-9a-f]{64}", batch):
        raise ValueError("invalid ETF daily batch ID")
    with store._connect() as conn:
        row = conn.execute(
            "SELECT collected_at,body FROM supplier_market_batches "
            "WHERE batch_id=%s AND provider='tushare' AND endpoint=%s",
            (batch, ENDPOINT),
        ).fetchone()
    if not row:
        raise ValueError("unknown ETF daily batch")
    collected, body = row
    return [
        bar | {"available_at": collected.astimezone(CHINA).isoformat(), "source": f"{SOURCE}:{batch}"}
        for bar in document(body)["bars"]
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("collect")
    ingest.add_argument("--config", required=True, type=Path)
    ingest.add_argument("--calendar", required=True, type=Path)
    ingest.add_argument("--run-key", required=True)
    ingest.add_argument("--symbol", required=True)
    ingest.add_argument("--from", dest="first", required=True, type=date.fromisoformat)
    ingest.add_argument("--to", dest="last", required=True, type=date.fromisoformat)
    commands.add_parser("show").add_argument("batch")
    args = parser.parse_args(argv)
    try:
        settings = Settings.read(args.env_file)
        if settings.env != "test":
            raise ValueError("ETF daily history requires the isolated test database")
        store = PostgresLedger(settings, "FROZEN")
        if args.command == "show":
            result = {"prices": read_prices(store, args.batch)}
        else:
            calendar = set(map(date.fromisoformat, trading_days(args.calendar)))
            result = collect(store, args.config, args.symbol, args.first, args.last, calendar, args.run_key)
        print(encode(result))
        return 0
    except (ProbeError, SourceFailure, ValueError, OSError, TypeError, KeyError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
