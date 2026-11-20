"""The shadow replay rebuilds recorded cycles under v2 and never mutates what it was given."""
from copy import deepcopy
from unittest import TestCase

from modules.strategy_hermes.signal_cycle import apply_report, create_cycle, process_quote
from modules.strategy_hermes.simulator import Calendar, Rules
from tools.shadow_exit_v2 import compare

DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
CALENDAR = Calendar(DAYS)
ETF, SECTOR = "512480.SH", "kaipanla:A"


def report(day, hour, state):
    return dict(id=f"r{day}-{hour}", at=f"{DAYS[day]}T{hour}:00+08:00", sectors={SECTOR: dict(state=state)})


def quote(day, hour, price, code=ETF, *, tradeable=True, revision=0):
    at = f"{DAYS[day]}T{hour}:00+08:00"
    return dict(id=f"q{day}-{hour}-{code}-{revision}", etf=code, observed_at=at, received_at=at, price_cny=price,
                source="frozen:test", tradeable=tradeable, revision=revision)


class ShadowReplay(TestCase):
    def recorded(self, events):
        """What v1 did with the same events."""
        cycle = None
        for kind, event in events:
            if kind == "report" and cycle is None:
                cycle = create_cycle(ETF, 100, Rules(), event, dict(sector_id=SECTOR), [])
            elif kind == "report":
                apply_report(cycle, event)
            elif event["etf"] == ETF:
                process_quote(cycle, event, CALENDAR)
        return cycle

    def test_v2_exits_where_v1_kept_holding_and_inputs_are_untouched(self):
        events = [("report", report(0, "09:35", "UP")), ("quote", quote(0, "09:36", "1.000")),
                  ("quote", quote(0, "09:36", "9.000", "159001.SZ")),
                  ("quote", quote(1, "09:35", "1.045")), ("report", report(1, "10:35", "HOLD")),
                  ("quote", quote(2, "09:35", "1.046")), ("quote", quote(2, "10:35", "1.002")),
                  ("quote", quote(2, "13:05", "1.001")), ("quote", quote(3, "14:05", "0.960"))]
        actual = self.recorded(events)
        self.assertEqual((actual["status"], len(actual["orders"])), ("OPEN", 1))
        frozen = deepcopy((actual, events))
        result = compare([actual], events, CALENDAR, Rules.v2("DUAL"),
                         lambda code, at, rules: dict(market_regime="RANGE_ROTATION", flags=[], atr20="0.055"))
        self.assertEqual((actual, events), frozen)
        row, = result["cycles"]
        self.assertEqual((row["recorded"]["rule"], row["recorded"]["status"], row["recorded"]["return_pct"]),
                         ("hermes_signal_cycle_v1", "OPEN", "-4.0000"))
        self.assertEqual([(order["action"], order["reason"], order["trigger_price"], order["fill_price"])
                          for order in row["shadow"]["orders"][1:]],
                         [("REDUCE", "TARGET_PROFIT_REDUCE", "1.045", "1.046"),
                          ("EXIT", "BREAKEVEN_PROTECT", "1.002", "1.001")])
        self.assertEqual((row["shadow"]["status"], row["shadow"]["return_pct"], row["return_pct_difference"],
                          row["valued_at"]), ("CLOSED", "2.3500", "6.3500", events[-1][1]["observed_at"]))
        self.assertEqual(result["summary"], dict(
            cycles=1, filled_exit_reasons=dict(TARGET_PROFIT_REDUCE=1, BREAKEVEN_PROTECT=1), fallback_flags={},
            average_fill_vs_trigger_pct="-0.0021", average_held_days_closed="2",
            average_return_pct_difference="6.3500"))

    def test_corrections_untradeable_quotes_and_holding_days_follow_the_ledger(self):
        events = [("report", report(0, "09:35", "UP")), ("quote", quote(0, "09:36", "1.000")),
                  ("quote", quote(1, "09:35", "1.000")),
                  # A higher revision of the same observation carries the stop-loss price.
                  ("quote", quote(1, "09:35", "0.960", revision=1)),
                  ("quote", quote(1, "09:35", "1.200", revision=0)),
                  ("quote", quote(2, "09:35", "0.955")),
                  ("quote", quote(3, "09:35", "0.500", tradeable=False))]
        actual = self.recorded(events[:3])
        result = compare([actual], events, CALENDAR, Rules.v2("DUAL"),
                         lambda code, at, rules: dict(market_regime="RANGE_ROTATION", flags=[], atr20="0.025"))
        row, = result["cycles"]
        self.assertEqual([(order["reason"], order["trigger_price"], order["fill_price"])
                          for order in row["shadow"]["orders"][1:]], [("STOP_LOSS_HIT", "0.960", "0.955")])
        # The untradeable 0.500 is not a valuation price, and the exit filled two sessions after the buy.
        self.assertEqual((row["valued_at"], row["recorded"]["return_pct"]),
                         (events[5][1]["observed_at"], "-4.5000"))
        self.assertEqual(result["summary"]["average_held_days_closed"], "2")

    def test_missing_inputs_are_counted_and_unfilled_or_incomplete_cycles_are_reported(self):
        events = [("report", report(0, "09:35", "UP")), ("quote", quote(0, "09:36", "1.000")),
                  ("quote", quote(1, "09:35", "0.990"))]
        actual = self.recorded(events)
        waiting = create_cycle("159001.SZ", 100, Rules(), report(0, "09:35", "UP"), dict(sector_id=SECTOR), [])
        result = compare([waiting, actual, actual], events[:1] + events[2:] + events[1:2], CALENDAR, Rules.v2("DUAL"),
                         lambda code, at, rules: {})
        self.assertEqual(result["summary"]["fallback_flags"], dict(REGIME_FALLBACK_RANGE=2, ATR_UNAVAILABLE=2))
        self.assertEqual(result["summary"]["cycles"], 2)
        lost = compare([actual], events[2:], CALENDAR, Rules.v2("DUAL"), lambda code, at, rules: {})
        self.assertEqual(lost["cycles"], [dict(etf=ETF, status="NOT_REPLAYABLE")])
