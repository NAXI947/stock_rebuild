"""Provider breadth semantics and read-only panorama composition."""
from contextlib import contextmanager
from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.stage2.market_panorama import normalize_background, load_background, read_panorama

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
             patch("modules.strategy_hermes.stage2.market_turnover.load", return_value=None), \
             patch("modules.strategy_hermes.stage2.macro_refresh.load", return_value=None), \
             patch("modules.strategy_hermes.stage2.market_bootstrap.fetch", side_effect=AssertionError("GET cannot collect")), \
             patch("modules.strategy_hermes.stage2.market_turnover.get_json", side_effect=AssertionError("GET cannot collect")):
            self.assertEqual(read_panorama(object()), dict(background={"up_count":0}, turnover=None, global_indices=None))
