"""Index bars, market regime and ETF ATR20 inputs of hermes_signal_cycle_v2."""
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import etf_statistics as stats, macro_refresh, market_regime as regime
from modules.strategy_hermes.stage2.market_bootstrap import ProbeError, normalize_kaipanla
from modules.strategy_hermes.stage2.tushare_bootstrap import SourceFailure

DAYS = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(30)]


def bars(closes, amounts=None):
    amounts = amounts or [100] * len(closes)
    return [dict(trade_date=day, open=str(close), close=str(close), high=str(close), low=str(close),
                 amount_cny=str(amount)) for day, close, amount in zip(DAYS, closes, amounts)]


class IndexBars(TestCase):
    def body(self):
        return dict(errcode="0", StockID="SH000300", x=[20261119, 20261120],
                    y=[[10, 11, 12, 9], [11, 10.5, 11.5, 10]], bal=[5, 6], vol=[1, 2])

    def test_rows_keep_dates_prices_and_amount(self):
        rows = regime.normalize_index(self.body(), "SH000300")
        self.assertEqual(rows[-1], dict(trade_date="2026-11-20", open="11", close="10.5", high="11.5",
                                        low="10", amount_cny="6"))
        payload, count, source = normalize_kaipanla(self.body(), "107", dict(StockID="SH000300"), datetime.now(CHINA))
        self.assertEqual((payload["code"], count, source), ("SH000300", 2, None))

    def test_empty_misaligned_or_other_index_is_rejected(self):
        for change in (dict(StockID="SH000001"), dict(x=[]), dict(bal=[5]), dict(errcode="1"),
                       dict(x=[20261120, 20261119]), dict(y=[[10, 11, 12, 9], [11, 10.5, 10.4, 10]])):
            with self.subTest(change=change), self.assertRaises(ValueError):
                regime.normalize_index(self.body() | change, "SH000300")
        with self.assertRaises(ProbeError):
            normalize_kaipanla(self.body() | dict(x=[]), "107", dict(StockID="SH000300"), datetime.now(CHINA))


class Regime(TestCase):
    def classify(self, first, second, amounts=None, mode="DUAL", day=DAYS[-1]):
        series = dict(SH000300=bars(first, amounts), SH000852=bars(second, amounts), SH000001=bars(first, amounts))
        return regime.classify(series, day, mode)

    def test_three_regimes_follow_ma20_slope_and_volume_ratio(self):
        up, down = list(range(100, 130)), list(range(130, 100, -1))
        heavy, light = [100] * 25 + [150] * 5, [100] * 25 + [50] * 5
        self.assertEqual(self.classify(up, up, heavy)["market_regime"], regime.BULL)
        self.assertEqual(self.classify(up, up)["market_regime"], regime.RANGE)
        self.assertEqual(self.classify(up, down, heavy)["market_regime"], regime.RANGE)
        self.assertEqual(self.classify(down, down)["market_regime"], regime.BEAR)
        flat_below = [100] * 29 + [99]
        self.assertEqual(self.classify(flat_below, flat_below, light)["market_regime"], regime.BEAR)
        self.assertEqual(self.classify(up, down, heavy, mode="SINGLE")["market_regime"], regime.BULL)
        result = self.classify(up, up, heavy)
        self.assertEqual((result["flags"], result["regime_as_of"]), ([], DAYS[-1]))
        self.assertEqual(round(Decimal(result["volume_ratio"]), 6), Decimal("1.333333"))

    def test_missing_previous_session_or_short_history_falls_back_to_range(self):
        down = list(range(130, 100, -1))
        late = self.classify(down, down, day="2026-10-01")
        self.assertEqual((late["market_regime"], late["flags"]), (regime.RANGE, [regime.FALLBACK]))
        short = regime.classify(dict(SH000300=bars(down)[-24:], SH000852=bars(down)), DAYS[-1], "DUAL")
        self.assertEqual(short["flags"], [regime.FALLBACK])
        self.assertEqual(regime.classify({}, DAYS[-1], "SINGLE")["market_regime"], regime.RANGE)

    def test_recent_sessions_are_listed_oldest_first(self):
        down = list(range(130, 100, -1))
        result = regime.recent(dict(SH000300=bars(down), SH000852=bars(down)), "DUAL")
        self.assertEqual([row["regime_as_of"] for row in result], DAYS[-5:])
        self.assertEqual({row["market_regime"] for row in result}, {regime.BEAR})


class Atr(TestCase):
    AT = datetime(2026, 10, 1, 10, 35, tzinfo=CHINA)

    def rows(self, days, **change):
        return [dict(code="512800.SH", trade_date=day, high_cny="1.03", low_cny="0.99", pre_close_cny="1",
                     available_at=day + "T16:00:00+08:00") | change for day in days]

    def test_true_range_uses_gap_and_previous_close(self):
        result = stats.atr20("512800.SH", self.rows(DAYS[10:30]), DAYS + ["2026-10-01"], self.AT)
        self.assertEqual(result, dict(atr20="0.040000", atr_as_of=DAYS[-1]))
        gap = stats.atr20("512800.SH", self.rows(DAYS[10:30], high_cny="1.08", low_cny="1.06"),
                          DAYS + ["2026-10-01"], self.AT)
        self.assertEqual(gap["atr20"], "0.080000")
        thirds = stats.atr20("512800.SH", self.rows(DAYS[10:30], high_cny="1.01", low_cny="1", pre_close_cny="3"),
                             DAYS + ["2026-10-01"], self.AT)
        self.assertEqual(thirds["atr20"], "0.666667")

    def test_window_may_lag_three_sessions_but_not_more(self):
        calendar = DAYS + ["2026-10-01"]
        # DAYS[-1] is T-1: a window ending at T-2 or T-3 is used, one ending at T-4 is not.
        self.assertEqual(stats.atr20("512800.SH", self.rows(DAYS[9:29]), calendar, self.AT),
                         dict(atr20="0.040000", atr_as_of=DAYS[28]))
        lagging = stats.atr20("512800.SH", self.rows(DAYS[8:28]), calendar, self.AT)
        self.assertEqual(lagging["atr_as_of"], DAYS[27])
        self.assertEqual(stats.atr20("512800.SH", self.rows(DAYS[7:27]), calendar, self.AT),
                         dict(atr20=None, atr_as_of=None))

    def test_missing_range_future_or_other_product_rows_are_unavailable(self):
        calendar = DAYS + ["2026-10-01"]
        legacy = [dict(row, high_cny=None) for row in self.rows(DAYS[10:30])]
        future = self.rows(DAYS[10:30], available_at="2026-10-01T16:00:00+08:00")
        other = self.rows(DAYS[10:30], code="512480.SH")
        for rows in (legacy, future, other, self.rows(DAYS[11:30])):
            self.assertIsNone(stats.atr20("512800.SH", rows, calendar, self.AT)["atr20"])

    def test_daily_rows_keep_high_and_low(self):
        raw = [dict(ts_code="512800.SH", trade_date="2026-09-30", open="1", high="1.03", low="0.99",
                    close="1.01", pre_close="1", vol="10", amount="20")]
        row = stats.normalize_daily(raw, "512800.SH", {date(2026, 9, 30)})[0]
        self.assertEqual((row["high_cny"], row["low_cny"]), ("1.03", "0.99"))
        with self.assertRaises(SourceFailure):
            stats.normalize_daily([raw[0] | dict(low="1.02")], "512800.SH", {date(2026, 9, 30)})

    def test_fetch_window_is_25_sessions_and_statistics_stay_on_20(self):
        calendar = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(40)]
        at = datetime(2026, 9, 20, 10, tzinfo=CHINA)
        self.assertEqual(len(stats.history_window(calendar, at, size=stats.FETCH_DAYS)), 25)
        self.assertEqual(len(stats.history_window(calendar, at)), 20)
        self.assertEqual((stats.MAX_PRODUCTS, stats.FETCH_DAYS), (24, 25))


class IndexCollection(TestCase):
    def refresher(self, stored=()):
        found = lambda sql, params: SimpleNamespace(fetchone=lambda: (1,) if params[0] in stored else None)
        ledger = SimpleNamespace(_connect=lambda: nullcontext(SimpleNamespace(execute=found)))
        refresher = macro_refresh.MacroRefresh(ledger, "captures")
        refresher.due.update({"072": 10**9, "018": 10**9})
        return refresher

    def run_ticks(self, refresher, stamp, ticks, fail=()):
        calls = []
        def capture(root, endpoint, overrides=None):
            calls.append(overrides["StockID"])
            return SimpleNamespace(full_url="https://fixture", header_items=lambda: ()), {}
        def fetch(url, headers, timeout):
            if calls[-1] in fail:
                raise OSError("offline")
            return dict(errcode="0", StockID=calls[-1], x=[20261120], y=[[1, 1, 1, 1]], bal=[1])
        log = SimpleNamespace(call=lambda source, scope, operation, limit: operation())
        clock = SimpleNamespace(now=lambda zone=None: stamp)
        with patch.object(macro_refresh, "capture_request", capture), patch.object(macro_refresh, "get_json", fetch), \
             patch.object(macro_refresh, "save", return_value=dict(batch="b")), \
             patch.object(macro_refresh, "datetime", clock), \
             patch("modules.strategy_hermes.stage2.bulk_collect.RequestLog", return_value=log):
            states = [refresher.tick(now) for now in ticks]
        return calls, states

    def test_each_index_is_fetched_once_after_the_close(self):
        refresher = self.refresher(stored=regime.COLLECTED)
        before = datetime(2026, 11, 20, 14, 30, tzinfo=CHINA)
        self.assertEqual(self.run_ticks(refresher, before, [0, 60])[0], [])
        after = datetime(2026, 11, 20, 15, 6, tzinfo=CHINA)
        calls, states = self.run_ticks(refresher, after, [0, 30, 60, 120, 180, 240, 2000])
        self.assertEqual(calls, list(regime.COLLECTED))
        self.assertEqual(states[0]["latest"], "2026-11-20")
        weekend = datetime(2026, 11, 21, 16, 0, tzinfo=CHINA)
        self.assertEqual(self.run_ticks(refresher, weekend, [5000, 6000])[0], [])

    def test_first_start_fetches_history_at_once_and_failures_wait_for_the_next_slot(self):
        refresher = self.refresher()
        morning = datetime(2026, 11, 20, 9, 0, tzinfo=CHINA)
        calls, states = self.run_ticks(refresher, morning, [0, 60, 120, 180, 900], fail={"SH000300"})
        self.assertEqual(calls, ["SH000300", "SH000852", "SH000001", "SH000300"])
        self.assertEqual(states[0]["state"], "FAILED")

    def test_morning_seed_does_not_replace_the_closing_fetch(self):
        refresher = self.refresher()
        morning = datetime(2026, 11, 20, 9, 0, tzinfo=CHINA)
        self.assertEqual(self.run_ticks(refresher, morning, [0, 60, 120, 180, 2000])[0], list(regime.COLLECTED))
        after = datetime(2026, 11, 20, 15, 6, tzinfo=CHINA)
        self.assertEqual(self.run_ticks(refresher, after, [3000, 3060, 3120, 3180])[0], list(regime.COLLECTED))
