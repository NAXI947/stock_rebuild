"""Exit lines of hermes_signal_cycle_v2: price, time and AI exits on sampled quotes (V2-T01..T27)."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless

from modules.strategy_hermes.ledger import Ledger, encode
from modules.strategy_hermes.signal_cycle import apply_report, create_cycle, pending, process_quote, view
from modules.strategy_hermes.simulator import Calendar, Rules, moment, shares

DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
        "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
CALENDAR = Calendar(DAYS)
ETF, SECTOR = "512480.SH", "kaipanla:A"
BEAR, RANGE, BULL = "BEAR_DEFENSE", "RANGE_ROTATION", "BULL_OFFENSE"
LEGACY_RULE = dict(version="hermes_signal_cycle_v1", initial_cash="10000.00", reduce_ratio="0.50",
                   fee_rate=None, minimum_fee=None, slippage_bps=None, cash_rounding="CNY_CENT_HALF_UP",
                   matching="FIFO_T1_FIRST_VALID_POST_SIGNAL", pending_sell="CARRY_UNTIL_EXIT")


def stamp(day, hour):
    return f"{DAYS[day]}T{hour}:00+08:00"


def report(day, hour, state):
    return dict(id=f"r{day}-{hour}", at=stamp(day, hour), sectors={SECTOR: dict(state=state)})


def quote(day, hour, price):
    return dict(id=f"q{day}-{hour}", etf=ETF, observed_at=stamp(day, hour), received_at=stamp(day, hour),
                price_cny=price, source="frozen:test", tradeable=True, revision=0)


def opened(regime=RANGE, atr="0.025", *, rules=None, day=0, hour="10:35", price="1.000"):
    """A cycle whose buy of 10000 shares filled at ``price`` on ``day``."""
    rules = rules or Rules.v2("DUAL")
    cycle = create_cycle(ETF, 100, rules, report(day, "09:35", "UP"), dict(sector_id=SECTOR), [])
    inputs = dict(market_regime=regime, flags=[], regime_as_of="2026-10-30", atr20=atr, atr_as_of="2026-10-30")
    process_quote(cycle, quote(day, hour, price), CALENDAR, lambda at, frozen: inputs)
    return cycle


def feed(cycle, *steps):
    for day, hour, value in steps:
        if value in ("UP", "HOLD", "WEAK", "EXIT", "UNKNOWN"):
            apply_report(cycle, report(day, hour, value))
        else:
            process_quote(cycle, quote(day, hour, value), CALENDAR)
    return cycle


def trail(cycle):
    return [(order["action"], order["status"], order.get("trigger_reason")) for order in cycle["orders"][1:]]


class ExitPlan(TestCase):
    def test_t11_t14_lines_follow_atr_between_floor_and_cap(self):
        plan = opened(RANGE, "0.055")["exit_plan"]
        self.assertEqual([plan[key] for key in ("stop_loss", "trail_start", "trail_drawdown", "target_reduce")],
                         ["0.050", "0.02750", "0.02200", "0.04400"])
        self.assertEqual((plan["stagnant_days"], plan["max_days"], plan["cost_price"], plan["buy_fill_trade_date"]),
                         (3, 5, "1.000", DAYS[0]))
        self.assertEqual([opened(regime, "0.075")["exit_plan"]["stop_loss"] for regime in (BEAR, RANGE, BULL)],
                         ["0.040", "0.050", "0.055"])
        missing = opened(BEAR, None)["exit_plan"]
        self.assertEqual((missing["flags"], missing["atr20"], missing["stop_loss"], missing["trail_start"],
                          missing["trail_drawdown"], missing["target_reduce"]),
                         (["ATR_UNAVAILABLE"], None, "0.025", "0.020", "0.012", "0.025"))

    def test_unrounded_atr_is_frozen_at_six_places(self):
        plan = opened(BEAR, "0.05492131204224070015186513121684315684")["exit_plan"]
        self.assertEqual((plan["atr20"], plan["stop_loss"], plan["trail_start"], plan["target_reduce"]),
                         ("0.054921", "0.040", "0.02196840", "0.035"))
        feed_plan = opened(RANGE, "0.019088760527575725428494922175718780778")
        self.assertEqual(trail(feed(feed_plan, (1, "10:35", "0.969"))), [("EXIT", "PENDING", "STOP_LOSS_HIT")])

    def test_t15_missing_inputs_fall_back_to_range_and_are_flagged(self):
        cycle = create_cycle(ETF, 100, Rules.v2("SINGLE"), report(0, "09:35", "UP"), dict(sector_id=SECTOR), [])
        process_quote(cycle, quote(0, "10:35", "1.000"), CALENDAR)
        plan = cycle["exit_plan"]
        self.assertEqual((plan["market_regime"], plan["regime_mode"], plan["flags"], plan["stop_loss"]),
                         (RANGE, "SINGLE", ["REGIME_FALLBACK_RANGE", "ATR_UNAVAILABLE"], "0.030"))

    def test_t15_classified_regime_is_frozen_into_the_plan(self):
        from modules.strategy_hermes.stage2.market_regime import classify
        rows = [dict(trade_date=f"2026-10-{day:02d}", close=str(130 - day), amount_cny="100") for day in range(1, 31)]
        regime = classify(dict(SH000001=rows), "2026-10-30", "SINGLE")
        cycle = create_cycle(ETF, 100, Rules.v2("SINGLE"), report(0, "09:35", "UP"), dict(sector_id=SECTOR), [])
        process_quote(cycle, quote(0, "10:35", "1.000"), CALENDAR, lambda at, frozen: regime)
        self.assertEqual((cycle["exit_plan"]["market_regime"], cycle["exit_plan"]["regime_as_of"],
                          cycle["exit_plan"]["flags"]), (BEAR, "2026-10-30", ["ATR_UNAVAILABLE"]))

    def test_t18c_opening_quote_only_sets_the_plan(self):
        cycle = create_cycle(ETF, 100, Rules.v2("DUAL"), report(0, "09:35", "UP"), dict(sector_id=SECTOR), [])
        process_quote(cycle, quote(0, "09:34", "1.080"), CALENDAR)
        self.assertNotIn("exit_plan", cycle)
        process_quote(cycle, quote(0, "10:35", "1.020"), CALENDAR)
        self.assertEqual((cycle["status"], cycle["peak_price"], cycle["last_quote"], cycle["breakeven_armed"]),
                         ("OPEN", "1.020", None, False))
        # T05: only quotes sampled after the fill move the peak.
        feed(cycle, (0, "13:05", "1.030"), (0, "14:05", "1.025"))
        self.assertEqual((cycle["peak_price"], trail(cycle)), ("1.030", []))


class ExitView(TestCase):
    def test_view_reports_sampled_return_peak_days_and_breakeven_state(self):
        cycle = opened(RANGE, "0.055")
        now = moment(stamp(1, "14:30"))
        self.assertEqual(view(cycle, None, now)["exit_view"], dict(
            return_ratio=None, peak_return_ratio="0.000000", sampled_at=None, held_days=None, max_days=5,
            breakeven_armed=False, remainder=False))
        feed(cycle, (1, "10:35", "1.016"), (1, "10:36", "WEAK"), (1, "13:05", "1.010"))
        self.assertEqual(view(cycle, None, now)["exit_view"], dict(
            return_ratio="0.010000", peak_return_ratio="0.016000", sampled_at=stamp(1, "13:05"), held_days=1,
            max_days=5, breakeven_armed=True, remainder=True))
        self.assertNotIn("exit_view", view(opened(rules=Rules()), None, now))


class PriceExits(TestCase):
    def test_t01_t19_stop_loss_orders_on_one_quote_and_fills_on_the_next(self):
        cycle = feed(opened(RANGE, "0.025"), (1, "10:35", "0.971"))
        self.assertEqual((cycle["status"], trail(cycle)), ("OPEN", []))
        feed(cycle, (1, "13:05", "0.970"))
        order = cycle["orders"][-1]
        self.assertEqual((cycle["status"], order["id"], order["target"], order["report_id"], len(cycle["book"]["fills"])),
                         ("WAIT_EXIT", "q1-13:05:EXIT", 10000, None, 1))
        self.assertEqual((order["trigger_reason"], order["trigger_price"], order["triggered_at"]),
                         ("STOP_LOSS_HIT", "0.970", stamp(1, "13:05")))
        feed(cycle, (1, "14:05", "0.968"))
        self.assertEqual((cycle["status"], cycle["book"]["fills"][-1]["price"], order["trigger_price"]),
                         ("CLOSED", "0.968", "0.970"))
        # T18-c: nothing is judged once the position is gone.
        feed(cycle, (1, "14:30", "0.900"))
        self.assertEqual(trail(cycle), [("EXIT", "FILLED", "STOP_LOSS_HIT")])

    def test_t02_same_day_stop_waits_for_t1_and_cannot_be_withdrawn(self):
        cycle = feed(opened(RANGE, "0.025"), (0, "14:05", "0.960"), (0, "14:30", "0.990"), (0, "14:31", "UP"))
        self.assertEqual((cycle["status"], pending(cycle)["action"], shares(cycle["book"])), ("WAIT_EXIT", "EXIT", 10000))
        self.assertTrue(pending(cycle)["eligible_at"].startswith(DAYS[1] + "T09:30"))
        feed(cycle, (1, "09:35", "0.985"))
        self.assertEqual((cycle["status"], cycle["book"]["fills"][-1]["price"]), ("CLOSED", "0.985"))

    def test_t03a_t24_armed_remainder_leaves_at_breakeven_in_any_round(self):
        for hour in ("09:35", "10:35"):
            cycle = feed(opened(RANGE, "0.055"), (1, "10:35", "1.016"), (1, "10:36", "WEAK"), (1, "13:05", "1.010"))
            self.assertEqual((cycle["breakeven_armed"], shares(cycle["book"]), trail(cycle)),
                             (True, 5000, [("REDUCE", "FILLED", "AI_FIRST_WEAK_REDUCE")]))
            feed(cycle, (2, hour, "1.003"))
            self.assertEqual(trail(cycle)[-1], ("EXIT", "PENDING", "BREAKEVEN_PROTECT"))
            self.assertEqual(pending(cycle)["target"], 5000)
        # T24: the reduction fills near cost and the same quote clears the armed remainder.
        cycle = feed(opened(RANGE, "0.055"), (1, "10:35", "1.016"), (1, "13:05", "1.004"), (1, "13:06", "WEAK"),
                     (2, "09:35", "1.002"))
        self.assertEqual(trail(cycle), [("REDUCE", "FILLED", "AI_FIRST_WEAK_REDUCE"), ("EXIT", "PENDING", "BREAKEVEN_PROTECT")])

    def test_t03b_unarmed_loss_reduction_keeps_the_remainder(self):
        cycle = feed(opened(RANGE, "0.055"), (1, "10:35", "1.005"), (1, "13:05", "0.990"), (1, "13:06", "WEAK"),
                     (1, "14:05", "0.985"), (2, "09:35", "0.990"))
        self.assertEqual((cycle["status"], cycle["breakeven_armed"], shares(cycle["book"]), trail(cycle)),
                         ("OPEN", False, 5000, [("REDUCE", "FILLED", "AI_FIRST_WEAK_REDUCE")]))

    def test_t22_unreduced_position_is_not_washed_out_at_breakeven(self):
        cycle = feed(opened(RANGE, "0.055"), (1, "09:35", "1.016"), (1, "14:05", "1.002"))
        self.assertEqual((cycle["status"], cycle["breakeven_armed"], trail(cycle)), ("OPEN", True, []))

    def test_t04_trailing_exit_needs_the_start_line(self):
        below = feed(opened(RANGE, "0.055"), (1, "10:35", "1.026"), (1, "13:05", "1.0034"))
        self.assertEqual((below["status"], trail(below)), ("OPEN", []))
        above = feed(opened(RANGE, "0.055"), (1, "10:35", "1.028"), (1, "13:05", "1.0054"))
        self.assertEqual(trail(above), [])
        feed(above, (1, "14:05", "1.0053"))
        self.assertEqual((above["status"], trail(above)), ("WAIT_EXIT", [("EXIT", "PENDING", "TRAILING_TAKE_PROFIT")]))

    def test_t16_t21_target_reduction_arms_breakeven_which_outranks_trailing(self):
        cycle = feed(opened(RANGE, "0.055"), (1, "10:35", "1.043"))
        self.assertEqual(trail(cycle), [])
        feed(cycle, (1, "13:05", "1.045"))
        order = pending(cycle)
        self.assertEqual((order["action"], order["target"], order["trigger_reason"], order["trigger_price"],
                          cycle["reduced"], cycle["breakeven_armed"], cycle["status"]),
                         ("REDUCE", 5000, "TARGET_PROFIT_REDUCE", "1.045", True, True, "OPEN"))
        feed(cycle, (1, "14:05", "1.046"))
        self.assertEqual((shares(cycle["book"]), trail(cycle)), (5000, [("REDUCE", "FILLED", "TARGET_PROFIT_REDUCE")]))
        feed(cycle, (2, "09:35", "1.002"))
        self.assertEqual(trail(cycle)[-1], ("EXIT", "PENDING", "BREAKEVEN_PROTECT"))

    def test_t18a_fillable_reduction_fills_before_the_exit_is_sized(self):
        cycle = feed(opened(RANGE, "0.025"), (1, "10:35", "WEAK"), (1, "13:05", "0.960"))
        self.assertEqual(trail(cycle), [("REDUCE", "FILLED", "AI_FIRST_WEAK_REDUCE"), ("EXIT", "PENDING", "STOP_LOSS_HIT")])
        self.assertEqual((pending(cycle)["target"], cycle["book"]["fills"][-1]["quantity"]), (5000, 5000))
        feed(cycle, (1, "14:05", "0.958"))
        self.assertEqual((cycle["status"], [fill["quantity"] for fill in cycle["book"]["fills"]]),
                         ("CLOSED", [10000, 5000, 5000]))

    def test_t18b_locked_reduction_is_superseded_by_a_full_exit(self):
        cycle = feed(opened(RANGE, "0.025"), (0, "13:05", "WEAK"), (0, "14:05", "0.960"))
        self.assertEqual(trail(cycle), [("REDUCE", "SUPERSEDED", "AI_FIRST_WEAK_REDUCE"), ("EXIT", "PENDING", "STOP_LOSS_HIT")])
        self.assertEqual(pending(cycle)["target"], 10000)
        feed(cycle, (1, "09:35", "0.955"))
        self.assertEqual((cycle["status"], [fill["quantity"] for fill in cycle["book"]["fills"]]), ("CLOSED", [10000, 10000]))


class TimeExits(TestCase):
    def test_t06_stagnant_exit_follows_the_frozen_switch(self):
        cycle = feed(opened(RANGE, "0.025"), (2, "14:05", "1.008"), (3, "13:05", "1.008"))
        self.assertEqual(trail(cycle), [])
        feed(cycle, (3, "14:05", "1.008"))
        self.assertEqual(trail(cycle), [("EXIT", "PENDING", "TIME_EXIT_STAGNANT")])
        self.assertEqual(cycle["last_quote"]["held_days"], 3)
        off = Rules.v2("DUAL", version="hermes_signal_cycle_v2_no_time_exit", time_exit_enabled=False)
        self.assertEqual(trail(feed(opened(RANGE, "0.025", rules=off), (3, "14:05", "1.008"), (6, "14:30", "1.008"))), [])
        self.assertEqual(trail(feed(opened(RANGE, "0.025"), (3, "14:05", "1.010"))), [])

    def test_t07_maximum_holding_days_exit_whatever_the_return(self):
        for regime, day, price in ((BEAR, 3, "1.015"), (RANGE, 5, "0.990"), (BULL, 7, "1.020")):
            with self.subTest(regime=regime):
                cycle = feed(opened(regime, "0.030"), (day - 1, "14:30", "1.020"), (day, "13:05", price))
                self.assertEqual(trail(cycle), [])
                feed(cycle, (day, "14:30", price))
                self.assertEqual(trail(cycle), [("EXIT", "PENDING", "TIME_EXIT_MAX_DAYS")])

    def test_t10_stop_loss_outranks_time_exit(self):
        self.assertEqual(trail(feed(opened(RANGE, "0.025"), (5, "14:05", "0.960"))), [("EXIT", "PENDING", "STOP_LOSS_HIT")])

    def test_t23_holding_days_count_from_the_fill_day(self):
        for rules in (None, Rules.v2("DUAL", version="hermes_signal_cycle_v2_early_weak", bear_early_weak_exit=True)):
            cycle = create_cycle(ETF, 100, rules or Rules.v2("DUAL"), report(0, "14:30", "UP"), dict(sector_id=SECTOR), [])
            process_quote(cycle, quote(1, "09:35", "1.000"), CALENDAR, lambda at, frozen: dict(market_regime=BEAR, flags=[]))
            self.assertEqual(cycle["exit_plan"]["buy_fill_trade_date"], DAYS[1])
            feed(cycle, (1, "10:34", "0.990"), (1, "10:35", "WEAK"))
            self.assertEqual((cycle["status"], trail(cycle)), ("OPEN", [("REDUCE", "PENDING", "AI_FIRST_WEAK_REDUCE")]))
            feed(cycle, (3, "13:05", "0.995"))
            self.assertEqual(cycle["last_quote"]["held_days"], 2)


class ReportExits(TestCase):
    def test_t08_second_weak_day_clears_and_same_day_repeats_do_not(self):
        cycle = feed(opened(RANGE, "0.025"), (1, "10:35", "WEAK"), (1, "13:05", "WEAK"))
        self.assertEqual((cycle["weak_trade_days"], trail(cycle)), ([DAYS[1]], [("REDUCE", "PENDING", "AI_FIRST_WEAK_REDUCE")]))
        feed(cycle, (2, "09:35", "WEAK"))
        self.assertEqual((cycle["status"], trail(cycle)),
                         ("WAIT_EXIT", [("REDUCE", "SUPERSEDED", "AI_FIRST_WEAK_REDUCE"), ("EXIT", "PENDING", "AI_WEAK_CROSS_DAY_2")]))
        feed(cycle, (2, "10:35", "UP"), (2, "13:05", "EXIT"))
        self.assertEqual(len(cycle["orders"]), 3)
        held = feed(opened(RANGE, "0.025"), (1, "10:35", "WEAK"), (1, "13:05", "HOLD"), (2, "09:35", "WEAK"))
        self.assertEqual((held["status"], held["weak_trade_days"], len(held["orders"])), ("OPEN", [DAYS[2]], 2))
        unknown = feed(opened(RANGE, "0.025"), (1, "10:35", "WEAK"), (1, "13:05", "UNKNOWN"), (2, "09:35", "WEAK"))
        self.assertEqual(trail(unknown)[-1], ("EXIT", "PENDING", "AI_WEAK_CROSS_DAY_2"))
        # Weak evidence repeated in a later report is stored as HOLD and must not reset the count.
        repeated = feed(opened(RANGE, "0.025"), (1, "10:35", "WEAK"))
        echo = report(1, "13:05", "HOLD")
        echo["sectors"][SECTOR].update(phase="WEAKEN", duplicate_evidence=True)
        apply_report(repeated, echo)
        self.assertEqual(repeated["weak_trade_days"], [DAYS[1]])
        self.assertEqual(trail(feed(repeated, (2, "09:35", "WEAK")))[-1], ("EXIT", "PENDING", "AI_WEAK_CROSS_DAY_2"))

    def test_v2_1_hold_keeps_the_weak_days_and_only_up_clears_them(self):
        rules = Rules.v2_1("DUAL")
        held = feed(opened(RANGE, "0.025", rules=rules), (1, "10:35", "WEAK"), (2, "10:35", "HOLD"))
        self.assertEqual((held["status"], held["weak_trade_days"]), ("OPEN", [DAYS[1]]))
        feed(held, (3, "09:35", "WEAK"))
        self.assertEqual((held["status"], held["weak_trade_days"], trail(held)), ("WAIT_EXIT", [DAYS[1], DAYS[3]],
                         [("REDUCE", "SUPERSEDED", "AI_FIRST_WEAK_REDUCE"), ("EXIT", "PENDING", "AI_WEAK_CROSS_DAY_2")]))
        up = feed(opened(RANGE, "0.025", rules=rules), (1, "10:35", "WEAK"), (2, "10:35", "UP"))
        self.assertEqual(up["weak_trade_days"], [])
        feed(up, (3, "09:35", "WEAK"))
        self.assertEqual((up["status"], up["weak_trade_days"], trail(up)),
                         ("OPEN", [DAYS[3]], [("REDUCE", "PENDING", "AI_FIRST_WEAK_REDUCE")]))
        unknown = feed(opened(RANGE, "0.025", rules=rules), (1, "10:35", "WEAK"), (2, "10:35", "UNKNOWN"))
        self.assertEqual(unknown["weak_trade_days"], [DAYS[1]])
        # An UP that only repeats earlier evidence is not a new judgement either.
        echo = report(2, "13:05", "UP")
        echo["sectors"][SECTOR].update(duplicate_evidence=True)
        apply_report(unknown, echo)
        self.assertEqual(unknown["weak_trade_days"], [DAYS[1]])
        # The same reports under v2: the HOLD clears the first weak day and the position stays open.
        old = feed(opened(RANGE, "0.025"), (1, "10:35", "WEAK"), (2, "10:35", "HOLD"), (3, "09:35", "WEAK"))
        self.assertEqual((old["status"], old["weak_trade_days"]), ("OPEN", [DAYS[3]]))

    def test_retreat_clears_and_waiting_buy_is_cancelled(self):
        cycle = feed(opened(RANGE, "0.025"), (1, "09:35", "1.010"), (1, "10:35", "EXIT"))
        order = pending(cycle)
        self.assertEqual((order["id"], order["trigger_reason"], order["trigger_price"], order["report_id"]),
                         ("r1-10:35:EXIT", "AI_RETREAT", "1.010", "r1-10:35"))
        waiting = create_cycle(ETF, 100, Rules.v2("DUAL"), report(0, "09:35", "UP"), dict(sector_id=SECTOR), [])
        apply_report(waiting, report(0, "10:35", "WEAK"))
        self.assertEqual((waiting["status"], waiting["orders"][0]["status"]), ("CANCELLED", "CANCELLED"))

    def test_t25_early_weak_exit_in_a_bear_market_is_opt_in(self):
        early = Rules.v2("DUAL", version="hermes_signal_cycle_v2_early_weak", bear_early_weak_exit=True)
        loss = feed(opened(BEAR, "0.030", rules=early), (1, "10:34", "0.990"), (1, "10:35", "WEAK"))
        self.assertEqual(trail(loss), [("EXIT", "PENDING", "BEAR_EARLY_WEAK_EXIT")])
        for steps in ([(1, "10:35", "WEAK")], [(1, "10:34", "1.005"), (1, "10:35", "WEAK")],
                      [(0, "14:05", "0.990"), (1, "10:35", "WEAK")]):
            self.assertEqual(trail(feed(opened(BEAR, "0.030", rules=early), *steps)),
                             [("REDUCE", "PENDING", "AI_FIRST_WEAK_REDUCE")])
        self.assertEqual(trail(feed(opened(RANGE, "0.030", rules=early), (1, "10:34", "0.990"), (1, "10:35", "WEAK"))),
                         [("REDUCE", "PENDING", "AI_FIRST_WEAK_REDUCE")])
        self.assertEqual(trail(feed(opened(BEAR, "0.030"), (1, "10:34", "0.990"), (1, "10:35", "WEAK"))),
                         [("REDUCE", "PENDING", "AI_FIRST_WEAK_REDUCE")])


class Versions(TestCase):
    def test_t09_t13_v1_cycles_and_snapshots_are_unchanged(self):
        self.assertEqual(encode(Rules().snapshot()), encode(LEGACY_RULE))
        self.assertEqual(Rules(**LEGACY_RULE), Rules())
        cycle = opened(rules=Rules(**LEGACY_RULE))
        self.assertEqual(cycle["rule"], LEGACY_RULE)
        feed(cycle, (5, "14:05", "0.955"), (8, "14:30", "0.955"), (8, "14:31", "HOLD"))
        self.assertEqual((cycle["status"], len(cycle["orders"])), ("OPEN", 1))
        self.assertFalse({"exit_plan", "peak_price", "last_quote", "weak_trade_days"} & set(cycle))
        feed(cycle, (9, "09:35", "WEAK"), (9, "10:35", "WEAK"))
        self.assertEqual([(order["action"], "trigger_reason" in order) for order in cycle["orders"][1:]], [("REDUCE", False)])

    def test_v2_rules_survive_storage_and_reject_inconsistent_values(self):
        rules = Rules.v2("DUAL")
        self.assertEqual(Rules(**json.loads(encode(rules.snapshot()))), rules)
        with self.assertRaises(ValueError):
            Rules.v2("AUTO")
        for change in (dict(version="hermes_signal_cycle_v1"), dict(weak_exit_trade_days=1),
                       dict(breakeven_arm_return="1.5"), dict(regime_bear_volume_ratio="1.20")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Rules.v2("DUAL", **change)
        table = deepcopy(rules.exit_table)
        table["RANGE_ROTATION"]["stagnant_days"] = 5
        with self.assertRaises(ValueError):
            Rules.v2("DUAL", exit_table=table)
        with self.assertRaises(ValueError):
            Rules(time_exit_enabled=True)

    def test_v2_1_is_v2_with_the_hold_rule_and_v2_keeps_its_stored_snapshot(self):
        old, new = Rules.v2("DUAL").snapshot(), Rules.v2_1("DUAL").snapshot()
        self.assertNotIn("hold_clears_weak_days", old)
        self.assertEqual(new, old | dict(version="hermes_signal_cycle_v2_1", hold_clears_weak_days=False))
        self.assertEqual(Rules(**json.loads(encode(new))), Rules.v2_1("DUAL"))
        # A stored v2 snapshot has no such key and reads back as the rule that still clears on HOLD.
        stored = Rules(**json.loads(encode(old)))
        self.assertEqual((stored, stored.hold_clears_weak_days), (Rules.v2("DUAL"), True))
        kept = feed(opened(RANGE, "0.025", rules=stored), (1, "10:35", "WEAK"), (2, "10:35", "HOLD"))
        self.assertEqual(kept["weak_trade_days"], [])
        for values in (dict(hold_clears_weak_days=False), dict(version="hermes_signal_cycle_v2_1"),
                       dict(version="hermes_signal_cycle_v2_1", hold_clears_weak_days=0)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Rules.v2("DUAL", **values)
        with self.assertRaises(ValueError):
            Rules(hold_clears_weak_days=False)

    def test_t20_rule_switch_accepts_only_known_versions(self):
        from modules.strategy_hermes.stage2.settings import Settings
        base = dict(APP_ENV="test", DATABASE_URL="postgresql://u:p@127.0.0.1:54320/stock_v4_test",
                    REDIS_URL="redis://127.0.0.1:63794/1", REDIS_KEY_PREFIX="stock-v4:test:",
                    AUTO_RUN_NAMESPACE="stock-v4:test:baseline")
        self.assertEqual(Settings.from_values(base).rules(), Rules())
        self.assertEqual(Settings.from_values(base | dict(MODEL_EVALUATION_RULE="hermes_signal_cycle_v1",
                                                          REGIME_MODE="DUAL")).rules(), Rules())
        for name in ("v2", "hermes_signal_cycle_v2"):
            chosen = Settings.from_values(base | dict(MODEL_EVALUATION_RULE=name, REGIME_MODE="SINGLE")).rules()
            self.assertEqual(chosen, Rules.v2("SINGLE"))
        for name in ("v2.1", "hermes_signal_cycle_v2_1"):
            chosen = Settings.from_values(base | dict(MODEL_EVALUATION_RULE=name, REGIME_MODE="DUAL")).rules()
            self.assertEqual(chosen, Rules.v2_1("DUAL"))
        for values in (dict(MODEL_EVALUATION_RULE="hermes_signal_cycle_v3"), dict(MODEL_EVALUATION_RULE="v2"),
                       dict(MODEL_EVALUATION_RULE="v2.1"), dict(MODEL_EVALUATION_RULE="v2.0.1", REGIME_MODE="DUAL"),
                       dict(MODEL_EVALUATION_RULE="v2", REGIME_MODE="AUTO"), dict(REGIME_MODE="dual")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Settings.from_values(base | values)


class SqliteLedger(TestCase):
    def test_t27_frozen_replay_runs_v2_with_both_fallbacks(self):
        import test_stage1 as stage1
        runtime = TemporaryDirectory(dir=Path(os.environ["STAGE1_TEST_ROOT"]))
        self.addCleanup(runtime.cleanup)
        state = Path(runtime.name, "ledger")
        state.mkdir(mode=0o700)
        events = stage1.start() + [stage1.quote("drop", stage1.D2, "09:36", "9.70"),
                                   stage1.quote("sold", stage1.D2, "10:36", "9.65")]
        result = Ledger(state).replay(stage1.bundle(events, ns="stage1:v2", rules=Rules.v2("DUAL").snapshot()))
        cycle, = result["cycles"]
        self.assertEqual((cycle["status"], cycle["rule"]["version"], cycle["exit_plan"]["flags"]),
                         ("CLOSED", "hermes_signal_cycle_v2", ["REGIME_FALLBACK_RANGE", "ATR_UNAVAILABLE"]))
        self.assertEqual([(order["action"], order.get("trigger_reason")) for order in cycle["orders"]],
                         [("BUY", None), ("EXIT", "STOP_LOSS_HIT")])
        self.assertEqual([fill["price"] for fill in cycle["book"]["fills"]], ["10.00", "9.65"])


@skipUnless(os.environ.get("STAGE2_TEST_ENV_FILE"), "set STAGE2_TEST_ENV_FILE to run PostgreSQL integration")
class PostgresVersions(TestCase):
    def setUp(self):
        from dataclasses import replace
        from uuid import uuid4
        from modules.strategy_hermes.stage2.postgres import PostgresLedger
        from modules.strategy_hermes.stage2.settings import Settings
        settings = Settings.read(os.environ["STAGE2_TEST_ENV_FILE"])
        self.settings = replace(settings, namespace=settings.redis_prefix + "exit-v2:" + uuid4().hex)
        self.store = PostgresLedger(self.settings, "FROZEN")
        self.store.migrate()
        self.addCleanup(self.clean)

    def clean(self):
        with self.store._connect() as conn, conn.transaction():
            for table in ("simulation_fills", "simulation_orders", "latest", "rotation", "cycles", "events",
                          "rule_versions", "runs"):
                conn.execute(f"DELETE FROM {table} WHERE ns=%s", (self.settings.namespace,))

    def store_events(self, events, rules):
        import test_stage1 as stage1
        from modules.strategy_hermes.stage2.postgres import BoundConnection
        with self.store._connect() as conn, conn.transaction():
            self.store._store(BoundConnection(conn), events, stage1.DAYS, {stage1.ETF: 100, stage1.ETF2: 100}, rules)

    def test_t17_cycles_with_different_plans_share_one_rule_version(self):
        import test_stage1 as stage1
        plans = {stage1.ETF: dict(market_regime=BEAR, flags=[], regime_as_of=stage1.D1, atr20="0.055", atr_as_of=stage1.D1),
                 stage1.ETF2: dict(market_regime=BULL, flags=[], regime_as_of=stage1.D1, atr20="0.019", atr_as_of=stage1.D1)}
        self.store._exit_inputs = lambda db, code, at, calendar, rules: plans[code]
        rules = Rules.v2("DUAL")
        self.store_events([stage1.report("entry", stage1.D1, "09:35", [(stage1.A, "新增")], [(stage1.ETF, stage1.A, 1)]),
                           stage1.quote("p1", stage1.D1, "09:36", "10.00")], rules)
        self.store_events([stage1.report("entry2", stage1.D1, "10:35", [(stage1.A, "加强"), (stage1.B, "新增")],
                                         [(stage1.ETF2, stage1.B, 1)]),
                           stage1.quote("p2", stage1.D1, "10:36", "5.00", code=stage1.ETF2),
                           stage1.quote("p3", stage1.D2, "09:36", "9.60")], rules)
        cycles = {cycle["etf"]: cycle for cycle in self.store.inspect()["cycles"]}
        self.assertEqual({code: (cycle["exit_plan"]["market_regime"], cycle["exit_plan"]["stop_loss"])
                          for code, cycle in cycles.items()},
                         {stage1.ETF: (BEAR, "0.040"), stage1.ETF2: (BULL, "0.030")})
        self.assertEqual(cycles[stage1.ETF]["orders"][-1]["trigger_reason"], "STOP_LOSS_HIT")
        with self.store._connect() as conn:
            saved = conn.execute("SELECT evidence->>'trigger_reason',evidence->>'trigger_price' FROM simulation_orders "
                                 "WHERE ns=%s AND action='EXIT'", (self.settings.namespace,)).fetchall()
            versions = conn.execute("SELECT version FROM rule_versions WHERE ns=%s", (self.settings.namespace,)).fetchall()
        self.assertEqual((saved, versions), ([("STOP_LOSS_HIT", "9.60")], [("hermes_signal_cycle_v2",)]))
        with self.assertRaisesRegex(ValueError, "changed rules require a new version"):
            self.store_events([], Rules.v2("DUAL", time_exit_enabled=False))

    def test_entry_observation_is_read_from_saved_reports_and_never_changes_orders(self):
        import test_stage1 as stage1
        plan = dict(market_regime=BEAR, flags=[], regime_as_of=stage1.D1, atr20="0.055", atr_as_of=stage1.D1)
        self.store._exit_inputs = lambda db, code, at, calendar, rules: plan
        rules = Rules.v2_1("DUAL")
        events = [stage1.report("r1", stage1.D1, "09:35", [(stage1.A, "减弱")]),
                  stage1.report("r2", stage1.D1, "10:35", [(stage1.A, "新增")]),
                  stage1.report("entry", stage1.D1, "13:05", [(stage1.A, "加强")], [(stage1.ETF, stage1.A, 1)])]
        self.store_events(events, rules)
        with self.store._connect() as conn:
            # The publisher adds the slot and the input facts to the saved report after the ledger has applied it.
            conn.execute("""UPDATE events SET payload=payload||%s::jsonb WHERE ns=%s AND kind='report' AND id='entry'""",
                         (encode(dict(round_slot="13:05", input_meta=dict(source_observed_at=stage1.D1 + "T13:05:11+08:00",
                             market_scope=dict(board_counts=dict(up=30, down=160, flat=10, unknown=3))))),
                          self.settings.namespace))
        self.store_events([stage1.quote("p1", stage1.D1, "13:06", "10.00")], rules)
        plain = self.store.inspect()["cycles"][0]
        self.assertNotIn("entry", plain)          # only the live ledger records it
        from modules.strategy_hermes.stage2.entry_observation import read
        with self.store._connect() as conn:
            seen = read(conn, self.settings.namespace, plain)
            self.assertEqual(seen, dict(
                entry_market_regime=BEAR, entry_round_slot="13:05", entry_up_streak=2,
                entry_market_breadth_ratio="0.150000", report_id="entry", breadth_boards=dict(up=30, down=160, flat=10),
                breadth_observed_at=stage1.D1 + "T13:05:11+08:00"))
            # A fallback regime and a report without the facts stay empty rather than guessed.
            other = dict(plain, exit_plan=dict(plain["exit_plan"], flags=["REGIME_FALLBACK_RANGE"]),
                         orders=[dict(plain["orders"][0], report_id="r2")])
            self.assertEqual(read(conn, self.settings.namespace, other), dict(
                entry_market_regime=None, entry_round_slot=None, entry_up_streak=1, entry_market_breadth_ratio=None,
                report_id="r2", breadth_boards=None, breadth_observed_at=None))
            missing = dict(plain, orders=[dict(plain["orders"][0], report_id="absent")])
            self.assertIsNone(read(conn, self.settings.namespace, missing)["entry_up_streak"])
        # The live ledger stores the same facts on the cycle when its buy fills, and nothing else differs.
        self.store._entry_observation = lambda db, ns, cycle: read(db.connection, ns, cycle)
        self.store_events([stage1.report("entry2", stage1.D1, "14:05", [(stage1.B, "新增")], [(stage1.ETF2, stage1.B, 1)]),
                           stage1.quote("p2", stage1.D1, "14:06", "5.00", code=stage1.ETF2)], rules)
        cycles = {cycle["etf"]: cycle for cycle in self.store.inspect()["cycles"]}
        self.assertEqual([cycles[stage1.ETF].get(key) for key in ("entry", "orders", "book", "exit_plan")],
                         [None, plain["orders"], plain["book"], plain["exit_plan"]])
        entry = cycles[stage1.ETF2].pop("entry")
        self.assertEqual((entry["entry_up_streak"], entry["entry_round_slot"], entry["report_id"]), (1, None, "entry2"))
        self.assertEqual(sorted(cycles[stage1.ETF2]), sorted(plain))

    def test_default_rule_keeps_the_stored_v1_snapshot(self):
        import test_stage1 as stage1
        with self.store._connect() as conn:
            conn.execute("INSERT INTO runs VALUES (%s,%s,NULL,'FROZEN')", (self.settings.namespace, encode(
                dict(calendar=stage1.DAYS, instruments={stage1.ETF: 100, stage1.ETF2: 100}))))
            conn.execute("INSERT INTO rule_versions VALUES (%s,%s,%s)",
                         (self.settings.namespace, "hermes_signal_cycle_v1", encode(LEGACY_RULE)))
        self.store_events(stage1.start(), self.store._rules())
        cycle, = self.store.inspect()["cycles"]
        self.assertEqual(cycle["rule"], LEGACY_RULE)
