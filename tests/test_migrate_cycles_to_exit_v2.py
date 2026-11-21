"""The one-off migration hands an open v1 position to the v2 exit rules without touching its book."""
from copy import deepcopy
from datetime import date
from unittest import TestCase

from modules.strategy_hermes.signal_cycle import apply_report, create_cycle, process_quote
from modules.strategy_hermes.simulator import Calendar, Rules
from tools.migrate_cycles_to_exit_v2 import MIGRATED, lines, migrate

DAYS = ["2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24"]
CALENDAR = Calendar(DAYS)
ETF, SECTOR = "512480.SH", "kaipanla:A"
V2 = Rules.v2("DUAL")
BEAR = dict(market_regime="BEAR_DEFENSE", flags=[], regime_as_of="2026-11-12", atr20="0.055128", atr_as_of="2026-11-12")


def report(day, hour, state, **row):
    return dict(id=f"r{day}-{hour}", at=f"{DAYS[day]}T{hour}:00+08:00", sectors={SECTOR: dict(row, state=state)})


def quote(day, hour, price, code=ETF, *, tradeable=True, revision=0):
    at = f"{DAYS[day]}T{hour}:00+08:00"
    return dict(id=f"q{day}-{hour}-{code}-{revision}", etf=code, observed_at=at, received_at=at, price_cny=price,
                source="frozen:test", tradeable=tradeable, revision=revision)


def recorded(events):
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


# Bought at 1.000, reduced by half on the first weak report, peak 1.030, now below cost and still held by v1.
EVENTS = [("report", report(0, "09:35", "UP")), ("quote", quote(0, "09:36", "1.000")),
          ("quote", quote(0, "09:36", "9.000", "159001.SZ")),
          ("report", report(1, "10:35", "WEAK")), ("quote", quote(1, "10:36", "1.030")),
          ("quote", quote(1, "13:05", "9.999", tradeable=False)), ("quote", quote(1, "13:05", "1.020", revision=1)),
          ("report", report(2, "09:35", "HOLD")), ("report", report(3, "09:35", "WEAK")),
          ("report", report(3, "10:35", "HOLD", duplicate_evidence=True)), ("quote", quote(5, "14:30", "0.990"))]


class Migration(TestCase):
    def setUp(self):
        self.actual = recorded(EVENTS)
        self.assertEqual((self.actual["status"], self.actual["rule"]["version"], self.actual["reduced"]),
                         ("OPEN", "hermes_signal_cycle_v1", True))

    def test_plan_and_tracked_state_come_from_stored_events_and_the_book_is_untouched(self):
        frozen = deepcopy((self.actual, EVENTS))
        cycle = migrate(self.actual, EVENTS, CALENDAR, V2, BEAR, "2026-11-21T10:30:00+08:00")
        self.assertEqual((self.actual, EVENTS), frozen)
        self.assertEqual({key: cycle[key] for key in ("book", "orders", "status", "reduced", "unit", "last_decision")},
                         {key: self.actual[key] for key in ("book", "orders", "status", "reduced", "unit", "last_decision")})
        self.assertEqual(cycle["rule"], V2.snapshot())
        self.assertEqual(cycle["migration"], dict(at="2026-11-21T10:30:00+08:00", from_rule=Rules().snapshot()))
        plan = cycle["exit_plan"]
        self.assertEqual((plan["market_regime"], plan["atr20"], plan["flags"], plan["cost_price"], plan["buy_fill_trade_date"],
                          plan["stop_loss"], plan["trail_start"], plan["trail_drawdown"], plan["max_days"]),
                         ("BEAR_DEFENSE", "0.055128", [MIGRATED], "1.000", "2026-11-13",
                          "0.040", "0.02205120", "0.01929480", 3))
        # The untradeable 9.999 is not a peak; the duplicate HOLD does not clear the second weak day.
        self.assertEqual((cycle["peak_price"], cycle["breakeven_armed"], cycle["weak_trade_days"], cycle["last_quote"]),
                         ("1.030", True, ["2026-11-18"],
                          dict(price="0.990", observed_at="2026-11-20T14:30:00+08:00", held_days=5)))
        self.assertEqual(lines(cycle, V2, CALENDAR, date(2026, 11, 21)), dict(
            session="2026-11-23", held_days=6, cost_price="1.000", peak_price="1.030", peak_return_pct="3.0000",
            stop_loss_at_or_below="0.9600", breakeven_at_or_below="1.0030", trailing_at_or_below="1.0101",
            time_exit_from_1405="TIME_EXIT_MAX_DAYS", weak_trade_days=["2026-11-18"], next_weak_report_exits=True))

    def test_the_engine_then_sells_the_rest_on_the_next_quotes(self):
        for price, hour, reason in (("0.995", "09:35", "BREAKEVEN_PROTECT"), ("1.008", "09:35", "TRAILING_TAKE_PROFIT"),
                                    ("1.025", "14:05", "TIME_EXIT_MAX_DAYS")):
            cycle = migrate(self.actual, EVENTS, CALENDAR, V2, BEAR, "2026-11-21T10:30:00+08:00")
            if hour == "14:05":
                process_quote(cycle, quote(6, "09:35", price), CALENDAR)
                self.assertEqual(cycle["status"], "OPEN")
            process_quote(cycle, quote(6, hour, price), CALENDAR)
            order = cycle["orders"][-1]
            self.assertEqual((cycle["status"], order["action"], order["trigger_reason"], order["target"]),
                             ("WAIT_EXIT", "EXIT", reason, 5000))
            process_quote(cycle, quote(6, "14:30", price), CALENDAR)
            self.assertEqual((cycle["status"], order["status"], cycle["book"]["fills"][-1]["price"]),
                             ("CLOSED", "FILLED", price))

    def test_a_second_weak_day_after_migration_clears_the_position(self):
        cycle = migrate(self.actual, EVENTS, CALENDAR, V2, BEAR, "2026-11-21T10:30:00+08:00")
        apply_report(cycle, report(6, "09:35", "WEAK"))
        self.assertEqual((cycle["status"], cycle["orders"][-1]["trigger_reason"]), ("WAIT_EXIT", "AI_WEAK_CROSS_DAY_2"))

    def test_missing_inputs_fall_back_and_are_flagged(self):
        cycle = migrate(self.actual, EVENTS, CALENDAR, V2, dict(atr20=None, atr_as_of=None), "2026-11-21T10:30:00+08:00")
        plan = cycle["exit_plan"]
        self.assertEqual((plan["market_regime"], plan["flags"], plan["stop_loss"]),
                         ("RANGE_ROTATION", ["REGIME_FALLBACK_RANGE", "ATR_UNAVAILABLE", MIGRATED], "0.030"))

    def test_only_an_open_v1_position_without_a_waiting_order_is_accepted(self):
        waiting = recorded(EVENTS + [("report", report(6, "09:35", "EXIT"))])
        already = migrate(self.actual, EVENTS, CALENDAR, V2, BEAR, "2026-11-21T10:30:00+08:00")
        for cycle in (waiting, already):
            with self.assertRaises(ValueError):
                migrate(cycle, EVENTS, CALENDAR, V2, BEAR, "2026-11-21T10:30:00+08:00")
        with self.assertRaises(ValueError):
            migrate(self.actual, EVENTS[2:], CALENDAR, V2, BEAR, "2026-11-21T10:30:00+08:00")
