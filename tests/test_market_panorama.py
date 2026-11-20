"""Provider breadth semantics and read-only panorama composition."""
from contextlib import contextmanager
from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.stage2.market_panorama import (
    load_background, load_regime, normalize_background, read_panorama)

STAMP = "2026-11-14T16:00:00+08:00"


class BreadthTest(TestCase):
    def test_breadth_keeps_018_count_meaning_and_unknown_source_clock(self):
        body = normalize_background(dict(errcode="0", list=dict(SZJS=765, XDJS=4748, ZTJS=37, DTJS=33,
                                    qscln=166722579, q_zrcs=194603817)), STAMP)
        self.assertEqual((body["up_count"], body["down_count"]), (765, 4748))
        self.assertAlmostEqual(float(body["up_ratio"]), 765 / 5513)
        self.assertIsNone(body["flat_count"])
        self.assertIsNone(body["observed_at"])
        self.assertIsNone(body["trade_date"])
        self.assertEqual(body["collected_at"], STAMP)

    def test_missing_and_invalid_counts_do_not_turn_into_zero(self):
        body = normalize_background(dict(errcode=0, list=dict(SZJS="0", XDJS=-1, ZTJS=True, DTJS="NaN")), STAMP)
        self.assertEqual(body["up_count"], 0)
        self.assertIsNone(body["down_count"])
        self.assertIsNone(body["limit_up_count"])
        self.assertIsNone(body["limit_down_count"])
        self.assertIsNone(body["up_ratio"])
        with self.assertRaises(ValueError):
            normalize_background(dict(errcode=0, list={}), STAMP)

    def test_old_saved_probe_is_read_without_claiming_collection_clock_as_market_clock(self):
        class Store:
            @contextmanager
            def _connect(self):
                yield self
            def execute(self, sql):
                self.sql = sql
                return self
            def fetchone(self):
                return (encode(dict(rows=dict(SZJS=100, XDJS=0, ZTJS=5, DTJS=0))),
                        datetime.fromisoformat(STAMP), None)
        ledger = Store()
        body = load_background(ledger)
        self.assertTrue(ledger.sql.strip().startswith("SELECT"))
        self.assertEqual(body["up_ratio"], "1")
        self.assertIsNone(body["observed_at"])

    def test_composition_reads_saved_data_even_when_all_network_is_forbidden(self):
        with patch("modules.strategy_hermes.stage2.market_panorama.load_background", return_value={"up_count":0}), \
             patch("modules.strategy_hermes.stage2.market_panorama.load_regime", return_value=None), \
             patch("modules.strategy_hermes.stage2.market_turnover.load", return_value=None), \
             patch("modules.strategy_hermes.stage2.macro_refresh.load", return_value=None), \
             patch("modules.strategy_hermes.stage2.market_bootstrap.fetch", side_effect=AssertionError("GET cannot collect")), \
             patch("modules.strategy_hermes.stage2.market_turnover.get_json", side_effect=AssertionError("GET cannot collect")):
            self.assertEqual(read_panorama(object()),
                             dict(background={"up_count":0}, regime=None, turnover=None, global_indices=None))


class RegimeTest(TestCase):
    COLLECTED = datetime.fromisoformat("2026-11-20T15:06:00+08:00")

    def store(self, batches, mode="DUAL"):
        class Store:
            settings = type("Settings", (), dict(regime_mode=mode))()
            statements = []
            @contextmanager
            def _connect(self):
                yield self
            def execute(self, sql, params=None):
                self.statements.append(sql)
                self.rows = batches if "DISTINCT ON" in sql else [(max((b[2] for b in batches), default=None),)]
                return self
            def fetchall(self):
                return self.rows
            def fetchone(self):
                return self.rows[0]
        return Store()

    def bars(self, closes, amounts=None, first=1):
        return encode(dict(rows=[dict(trade_date=f"2026-10-{day:02d}", close=str(close), amount_cny=str(amount))
                                 for day, close, amount in zip(range(first, 32), closes, amounts or [100] * 31)]))

    def test_five_sessions_keep_each_day_and_its_reasons(self):
        down, turn = list(range(130, 100, -1)), [100] * 25 + [101, 103, 106, 110, 115]
        rising = [100] * 25 + [150] * 5
        ledger = self.store([("SH000300", self.bars(turn, rising), self.COLLECTED),
                             ("SH000852", self.bars(turn, rising), self.COLLECTED),
                             ("SH000001", self.bars(down), self.COLLECTED)])
        body = load_regime(ledger)
        self.assertTrue(all(sql.lstrip().startswith("SELECT") for sql in ledger.statements))
        self.assertEqual([day["trade_date"][-2:] for day in body["days"]], ["26", "27", "28", "29", "30"])
        self.assertEqual({day["market_regime"] for day in body["days"]}, {"RANGE_ROTATION", "BULL_OFFENSE"})
        last = body["days"][-1]
        self.assertEqual((last["market_regime"], last["status"], last["volume_ratio"]), ("BULL_OFFENSE", "READY", "1.3333"))
        self.assertEqual(last["indexes"][0], dict(code="SH000300", name="沪深300", close="115", ma20="101.75",
                                                  above_ma20=True, ma20_rising=True))
        self.assertEqual((body["mode"], body["observed_at"], body["collected_at"], [i["name"] for i in body["indexes"]]),
                         ("DUAL", "2026-10-30", "2026-11-20T15:06:00+08:00", ["沪深300", "中证1000"]))
        single = load_regime(self.store([("SH000001", self.bars(down), self.COLLECTED)], "SINGLE"))
        self.assertEqual([day["market_regime"] for day in single["days"]], ["BEAR_DEFENSE"] * 5)

    def test_missing_history_is_shown_as_missing_not_as_a_regime(self):
        self.assertIsNone(load_regime(self.store([])))
        down = list(range(130, 100, -1))
        partial = load_regime(self.store([("SH000300", self.bars(down), self.COLLECTED),
                                          ("SH000852", self.bars(down[:-1]), self.COLLECTED)]))
        self.assertEqual([(day["market_regime"], day["status"]) for day in partial["days"]],
                         [("BEAR_DEFENSE", "READY")] * 4 + [(None, "INSUFFICIENT_DATA")])
        self.assertIsNone(partial["days"][-1]["indexes"][0]["above_ma20"])
        short = load_regime(self.store([("SH000300", self.bars(down[:10]), self.COLLECTED),
                                        ("SH000852", self.bars(down[:10]), self.COLLECTED)]))
        self.assertEqual({day["status"] for day in short["days"]}, {"INSUFFICIENT_DATA"})

    def test_a_session_still_trading_at_collection_is_not_judged(self):
        intraday = datetime.fromisoformat("2026-10-31T10:00:00+08:00")
        down = list(range(131, 100, -1))
        body = load_regime(self.store([("SH000001", self.bars(down), intraday)], "SINGLE"))
        self.assertEqual(body["days"][-1]["trade_date"], "2026-10-30")
