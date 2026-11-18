"""Waiting orders retain a visible reason across optional JSON null fields."""
from dataclasses import asdict
import unittest

from modules.strategy_hermes.signal_cycle import view
from modules.strategy_hermes.simulator import Rules, moment, new_book


class PendingReason(unittest.TestCase):
    def test_absent_or_null_block_keeps_wait_price_until_filled(self):
        rules = Rules()
        order = dict(action="BUY", status="PENDING", eligible_at="2026-11-17T13:05:00+08:00")
        cycle = dict(rule=asdict(rules), book=new_book(rules), closed_at=None, orders=[order])
        now = moment("2026-11-17T13:05:00+08:00")
        self.assertEqual(view(cycle, None, now)["pending_reason"], "WAIT_PRICE")
        order["last_block"] = None
        self.assertEqual(view(cycle, None, now)["pending_reason"], "WAIT_PRICE")
        order["last_block"] = "INSUFFICIENT_CASH_FOR_ONE_LOT"
        self.assertEqual(view(cycle, None, now)["pending_reason"], "INSUFFICIENT_CASH_FOR_ONE_LOT")
        order["status"] = "FILLED"
        self.assertIsNone(view(cycle, None, now)["pending_reason"])


if __name__ == "__main__":
    unittest.main()
