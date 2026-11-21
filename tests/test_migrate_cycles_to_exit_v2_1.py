"""The one-off migration hands an open v2 position to v2.1 and changes only the rule; stored weak days stay."""
from copy import deepcopy
from unittest import TestCase

from modules.strategy_hermes.signal_cycle import apply_report, create_cycle, process_quote
from modules.strategy_hermes.simulator import Calendar, Rules
from tools.migrate_cycles_to_exit_v2_1 import migrate

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


# Weak on two trading days, the first cleared by a HOLD under v2: one weak day is stored when the migration runs.
EVENTS = [("report", report(0, "09:35", "UP")), ("quote", quote(0, "09:36", "1.000")),
          ("report", report(1, "10:35", "WEAK")), ("quote", quote(1, "10:36", "1.000")),
          ("report", report(1, "13:05", "HOLD")), ("report", report(2, "09:35", "WEAK")),
          ("report", report(2, "10:35", "HOLD", duplicate_evidence=True)), ("report", report(3, "09:35", "UNKNOWN"))]


class Migration(TestCase):
    def setUp(self):
        self.actual = recorded(EVENTS, Rules.v2("DUAL"))
        self.assertEqual((self.actual["status"], self.actual["weak_trade_days"], self.actual["reduced"]),
                         ("OPEN", [DAYS[2]], True))

    def test_only_the_rule_changes_and_the_stored_weak_days_stay(self):
        frozen = deepcopy(self.actual)
        cycle = migrate(self.actual, AT)
        self.assertEqual(self.actual, frozen)
        self.assertEqual(cycle["rule"], Rules.v2_1("DUAL").snapshot())
        self.assertEqual(cycle["rule_change"], dict(at=AT, from_version="hermes_signal_cycle_v2"))
        for key in ("rule", "rule_change"):
            cycle.pop(key), frozen.pop(key, None)
        self.assertEqual(cycle, frozen)

    def test_a_day_cleared_before_the_migration_is_not_restored(self):
        cleared = recorded(EVENTS[:5], Rules.v2("DUAL"))
        self.assertEqual(cleared["weak_trade_days"], [])
        cycle = migrate(cleared, AT)
        self.assertEqual(cycle["weak_trade_days"], [])
        apply_report(cycle, report(5, "09:35", "WEAK"))
        self.assertEqual((cycle["status"], cycle["weak_trade_days"]), ("OPEN", [DAYS[5]]))

    def test_after_the_migration_a_hold_keeps_the_stored_day_and_the_next_weak_day_exits(self):
        cycle = migrate(self.actual, AT)
        apply_report(cycle, report(5, "09:35", "HOLD"))
        self.assertEqual((cycle["status"], cycle["weak_trade_days"]), ("OPEN", [DAYS[2]]))
        apply_report(cycle, report(6, "09:35", "WEAK"))
        self.assertEqual((cycle["status"], cycle["orders"][-1]["trigger_reason"]), ("WAIT_EXIT", "AI_WEAK_CROSS_DAY_2"))

    def test_an_up_after_the_migration_clears_the_stored_day(self):
        cycle = migrate(self.actual, AT)
        apply_report(cycle, report(5, "09:35", "UP"))
        self.assertEqual(cycle["weak_trade_days"], [])

    def test_other_cycles_are_refused(self):
        for change in (dict(status="WAIT_EXIT"), dict(rule=Rules().snapshot()),
                       dict(rule=Rules.v2_1("DUAL").snapshot())):
            with self.subTest(change=change), self.assertRaises(ValueError):
                migrate(self.actual | change, AT)
