from datetime import date, datetime
from unittest import TestCase

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2.etf_daily_history import normalize_bars, scope_for
from modules.strategy_hermes.stage2.tushare_bootstrap import SourceFailure

ETF = "512480.SH"
DAYS = tuple(date(2026, 11, day) for day in (10, 11, 12))


def bar(day, **changes):
    return {
        "ts_code": ETF, "trade_date": day.strftime("%Y%m%d"),
        "open": "0.795", "high": "0.798", "low": "0.782", "close": "0.785",
    } | changes


class EtfDailyHistory(TestCase):
    def test_completed_range_and_exact_unadjusted_prices(self):
        close = datetime(2026, 11, 12, 15, tzinfo=CHINA)
        scope = scope_for(ETF, DAYS[0], DAYS[-1], set(DAYS), close)
        self.assertEqual((scope["from"], scope["to"], scope["adjust"]), ("2026-11-10", "2026-11-12", "none"))
        result = normalize_bars([bar(day) for day in reversed(DAYS)], ETF, DAYS[0], DAYS[-1], set(DAYS))
        self.assertEqual([item["trade_date"] for item in result], [day.isoformat() for day in DAYS])
        self.assertEqual((result[0]["open_cny"], result[0]["high_cny"], result[0]["price_basis"]),
                         ("0.795", "0.798", "UNADJUSTED"))

    def test_incomplete_session_and_invalid_observations(self):
        before_close = datetime(2026, 11, 12, 14, 59, tzinfo=CHINA)
        with self.assertRaisesRegex(ValueError, "unfinished trading session"):
            scope_for(ETF, DAYS[0], DAYS[-1], set(DAYS), before_close)
        for invalid in (
            [bar(DAYS[0]), bar(DAYS[0])], [bar(DAYS[0], ts_code="510300.SH")],
            [bar(date(2026, 11, 9))], [bar(DAYS[0], high="0.794")],
            [bar(DAYS[0], open=0.795)],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(SourceFailure):
                normalize_bars(invalid, ETF, DAYS[0], DAYS[-1], set(DAYS))
