"""The exit study figures are rebuilt from saved rows only and never change what they were given."""
from copy import deepcopy
from datetime import date, datetime
from unittest import TestCase

from modules.strategy_hermes.signal_cycle import create_cycle, process_quote
from modules.strategy_hermes.simulator import CHINA, Calendar, Rules
from tools.exit_observation import report

DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
CALENDAR = Calendar(DAYS)
ETF, SECTOR = "512480.SH", "kaipanla:A"
NOW = datetime(2026, 11, 5, 18, tzinfo=CHINA)


def quote(day, hour, price, *, tradeable=True):
    at = f"{DAYS[day]}T{hour}:00+08:00"
    return dict(id=f"q{day}-{hour}", etf=ETF, observed_at=at, received_at=at, price_cny=price, source="frozen:test",
                tradeable=tradeable, revision=0)


def close(day, price):
    return {(ETF, DAYS[day]): dict(close_cny=price, available_at=f"{DAYS[day]}T16:10:00+08:00")}


class ExitObservation(TestCase):
    def held(self, quotes, inputs):
        entry = dict(id="entry", at=f"{DAYS[0]}T09:35:00+08:00", sectors={SECTOR: dict(state="UP")})
        cycle = create_cycle(ETF, 100, Rules.v2_0_1("DUAL"), entry, dict(sector_id=SECTOR), [])
        for item in quotes:
            process_quote(cycle, item, CALENDAR, lambda at, rules: inputs)
        return cycle

    def test_close_below_a_line_after_a_quiet_late_round_is_counted_with_the_next_open(self):
        quotes = [quote(0, "09:36", "1.000"), quote(1, "14:31", "0.990"), quote(2, "09:36", "0.940"),
                  quote(2, "09:37", "0.941")]
        cycle = self.held(quotes[:2], dict(market_regime="RANGE_ROTATION", flags=[], atr20="0.030"))
        cycle["entry"] = dict(entry_market_regime="RANGE_ROTATION", entry_round_slot="09:35", entry_up_streak=1,
                              entry_market_breadth_ratio="0.688000")
        self.assertEqual((cycle["status"], cycle["exit_plan"]["stop_loss"]), ("OPEN", "0.0300"))
        closes = close(0, "1.001") | close(1, "0.965")
        batches = [("SH000300", datetime(2026, 11, 2, 15, 40, tzinfo=CHINA), {DAYS[0]}),
                   ("SH000852", datetime(2026, 11, 3, 9, 40, tzinfo=CHINA), {DAYS[0]}),      # too late for the next round
                   ("SH000001", datetime(2026, 11, 2, 15, 41, tzinfo=CHINA), {DAYS[0]}),
                   ("SH000300", datetime(2026, 11, 3, 15, 40, tzinfo=CHINA), {DAYS[0], DAYS[1]}),
                   ("SH000852", datetime(2026, 11, 3, 15, 40, tzinfo=CHINA), {DAYS[0], DAYS[1]})]
        frozen = deepcopy((cycle, quotes, closes, batches))
        result = report([cycle], {ETF: quotes}, closes, batches, CALENDAR, date(2026, 11, 2), date(2026, 11, 3), NOW)
        self.assertEqual((cycle, quotes, closes, batches), frozen)
        first, second = result["late_round_gaps"]
        self.assertEqual((first["crossed"], first["late_round_sampled"], first["last_sample"]), (False, False, None))
        self.assertEqual({key: second[key] for key in ("crossed", "line", "late_round_sampled", "close_cny",
                                                       "close_vs_last_sample", "overnight_gap")},
                         dict(crossed=True, line="STOP_LOSS_HIT", late_round_sampled=True, close_cny="0.965",
                              close_vs_last_sample="-0.025253", overnight_gap="-0.025907"))
        self.assertEqual(second["next_open_sample"]["observed_at"], quotes[2]["observed_at"])
        self.assertEqual(result["summary"], dict(
            cycles_opened=1, atr_unavailable=0, regime_fallback_range=0, held_sessions=2, close_not_stored=0,
            close_crossed_a_line=1, crossed_lines=dict(STOP_LOSS_HIT=1),
            average_overnight_gap_after_exit_line="-0.025907",
            index_bar_sessions=2, dual_stored=1, single_stored=1))
        self.assertEqual(result["entries"][0]["entry"]["entry_round_slot"], "09:35")
        self.assertEqual([(row["trade_date"], row["DUAL"], row["SINGLE"]) for row in result["index_bars"]],
                         [(DAYS[0], False, True), (DAYS[1], True, False)])

    def test_fallbacks_missing_closes_and_sessions_with_an_exit_are_reported_not_guessed(self):
        quotes = [quote(0, "09:36", "1.000"), quote(1, "10:36", "0.960"), quote(1, "13:06", "0.958")]
        cycle = self.held(quotes, {})
        self.assertEqual((cycle["status"], cycle["orders"][-1]["trigger_reason"]), ("CLOSED", "STOP_LOSS_HIT"))
        result = report([cycle], {ETF: quotes}, {}, [], CALENDAR, date(2026, 11, 2), date(2026, 11, 5), NOW)
        row, = result["late_round_gaps"]            # held over the first close only; the exit day is not a gap day
        self.assertEqual((row["trade_date"], row["missing"], row["crossed"]), (DAYS[0], "CLOSE_NOT_STORED", None))
        self.assertEqual({key: result["summary"][key] for key in (
            "atr_unavailable", "regime_fallback_range", "close_not_stored", "close_crossed_a_line",
            "crossed_lines", "average_overnight_gap_after_exit_line", "dual_stored")}, dict(
            atr_unavailable=1, regime_fallback_range=1, close_not_stored=1, close_crossed_a_line=0,
            crossed_lines={}, average_overnight_gap_after_exit_line=None, dual_stored=0))
        self.assertIsNone(result["entries"][0]["entry"])
        # A session that has not closed yet is left out.
        early = report([cycle], {ETF: quotes}, {}, [], CALENDAR, date(2026, 11, 2), date(2026, 11, 5),
                       datetime(2026, 11, 2, 14, 40, tzinfo=CHINA))
        self.assertEqual((early["late_round_gaps"], early["index_bars"]), ([], []))

    def test_close_above_the_target_of_an_unreduced_holding_is_a_crossing_and_a_reduced_one_is_not(self):
        inputs = dict(market_regime="RANGE_ROTATION", flags=[], atr20="0.030")
        quotes = [quote(0, "09:36", "1.000"), quote(1, "14:31", "1.010")]
        cycle = self.held(quotes, inputs)
        target = cycle["exit_plan"]["target_reduce"]
        closes = close(1, str(1 + float(target) + 0.005))
        result = report([cycle], {ETF: quotes}, closes, [], CALENDAR, date(2026, 11, 3), date(2026, 11, 3), NOW)
        row, = result["late_round_gaps"]
        self.assertEqual((row["crossed"], row["line"]), (True, "TARGET_PROFIT_REDUCE"))
        self.assertEqual((result["summary"]["crossed_lines"], result["summary"]["average_overnight_gap_after_exit_line"]),
                         (dict(TARGET_PROFIT_REDUCE=1), None))
        # The same close after the target reduction was already raised that day is not a missed line.
        above = str(1 + float(target) + 0.001)
        quotes = [quotes[0], quote(1, "10:36", above), quote(1, "14:31", above)]
        reduced = self.held(quotes, inputs)
        self.assertEqual([order["action"] for order in reduced["orders"]], ["BUY", "REDUCE"])
        row, = report([reduced], {ETF: quotes}, closes, [], CALENDAR, date(2026, 11, 3), date(2026, 11, 3),
                      NOW)["late_round_gaps"]
        self.assertEqual((row["crossed"], row["line"]), (False, None))
