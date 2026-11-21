"""The one-off migration hands an open v2 position to v2.0.1 and changes only the rule and the weak days."""
from copy import deepcopy
from unittest import TestCase

from modules.strategy_hermes.signal_cycle import apply_report, create_cycle, process_quote
from modules.strategy_hermes.simulator import Calendar, Rules
from tools.migrate_cycles_to_exit_v2_0_1 import migrate

DAYS = ["2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24"]
CALENDAR = Calendar(DAYS)
ETF, SECTOR = "512800.SH", "kaipanla:A"
AT = "2026-11-21T18:30:00+08:00"


def report(day, hour, state, **row):
    return dict(id=f"r{day}-{hour}", at=f"{DAYS[day]}T{hour}:00+08:00", sectors={SECTOR: dict(row, state=state)})


def quote(day, hour, price):
    at = f"{DAYS[day]}T{hour}:00+08:00"
    return dict(id=f"q{day}-{hour}", etf=ETF, at=at, observed_at=at, received_at=at, price_cny=price,
                source="frozen:test", tradeable=True, revision=0)


def recorded(events, rules):
    cycle = None
    for kind, event in events:
        if kind == "report" and cycle is None:
            cycle = create_cycle(ETF, 100, rules, event, dict(sector_id=SECTOR), [])
        elif kind == "report":
            apply_report(cycle, event)
        else:
            process_quote(cycle, event, CALENDAR, lambda at, frozen: dict(market_regime="BULL_OFFENSE", flags=[]))
    return cycle


# Weak on three trading days, each cleared by a HOLD in between: v2 still holds the reduced position.
EVENTS = [("report", report(0, "09:35", "UP")), ("quote", quote(0, "09:36", "1.000")),
          ("report", report(1, "10:35", "WEAK")), ("quote", quote(1, "10:36", "1.000")),
          ("report", report(1, "13:05", "HOLD")), ("report", report(2, "09:35", "WEAK")),
          ("report", report(2, "10:35", "HOLD", duplicate_evidence=True)), ("report", report(3, "09:35", "UNKNOWN")),
          ("report", report(3, "10:35", "HOLD")), ("quote", quote(3, "10:36", "1.001"))]


class Migration(TestCase):
    def setUp(self):
        self.actual = recorded(EVENTS, Rules.v2("DUAL"))
        self.assertEqual((self.actual["status"], self.actual["weak_trade_days"], self.actual["reduced"]), ("OPEN", [], True))

    def test_only_the_rule_and_the_weak_days_change(self):
        frozen = deepcopy((self.actual, EVENTS))
        cycle = migrate(self.actual, EVENTS, AT)
        self.assertEqual((self.actual, EVENTS), frozen)
        self.assertEqual(cycle["rule"], Rules.v2_0_1("DUAL").snapshot())
        self.assertEqual(cycle["weak_trade_days"], [DAYS[1], DAYS[2]])
        self.assertEqual(cycle["rule_change"],
                         dict(at=AT, from_version="hermes_signal_cycle_v2", weak_trade_days_before=[]))
        for key in ("rule", "weak_trade_days", "rule_change"):
            cycle.pop(key), frozen[0].pop(key, None)
        self.assertEqual(cycle, frozen[0])

    def test_the_state_equals_a_cycle_that_ran_under_v2_0_1_until_its_second_weak_day(self):
        cycle = migrate(self.actual, EVENTS, AT)
        native = recorded(EVENTS[:5], Rules.v2_0_1("DUAL"))
        self.assertEqual((cycle["weak_trade_days"][:1], cycle["rule"]), (native["weak_trade_days"], native["rule"]))
        apply_report(cycle, report(5, "09:35", "HOLD"))
        self.assertEqual(cycle["status"], "OPEN")
        apply_report(cycle, report(6, "09:35", "WEAK"))
        self.assertEqual((cycle["status"], cycle["orders"][-1]["trigger_reason"]), ("WAIT_EXIT", "AI_WEAK_CROSS_DAY_2"))

    def test_an_up_after_the_last_weak_day_leaves_nothing_counted(self):
        cycle = migrate(self.actual, EVENTS + [("report", report(4, "09:35", "UP"))], AT)
        self.assertEqual(cycle["weak_trade_days"], [])

    def test_other_cycles_are_refused(self):
        for change in (dict(status="WAIT_EXIT"), dict(rule=Rules().snapshot()),
                       dict(rule=Rules.v2_0_1("DUAL").snapshot())):
            with self.subTest(change=change), self.assertRaises(ValueError):
                migrate(self.actual | change, EVENTS, AT)
        with self.assertRaises(ValueError):
            migrate(self.actual, EVENTS[2:], AT)
