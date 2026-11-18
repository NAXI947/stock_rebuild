"""Discovery cannot be gated by the existing ETF mapping."""
from datetime import datetime
from contextlib import nullcontext
from unittest import TestCase
from modules.strategy_hermes.stage2.discovery import select_directions, MAX_NEW_DIRECTIONS
from modules.strategy_hermes.stage2.octop_analyst import OctopAnalyst
from types import SimpleNamespace

AT = datetime.fromisoformat("2026-11-13T14:05:30+08:00")


class HybridDiscovery(TestCase):
    def boards(self):
        return {f"kaipanla:{800000+i}": dict(sector_id=f"kaipanla:{800000+i}",
                  sector_name=f"board{i}", observed_at=AT.isoformat(),
                  source_batch="batch", return_ratio=str(i/1000), strength=str(100-i))
                for i in range(30)}

    def test_bounded_new_market_scope_preserves_held_and_active_directions(self):
        boards = self.boards()
        held, active = "kaipanla:700001", "kaipanla:700002"
        selected, scope = select_directions(boards, {active: {}}, [held], AT)
        self.assertEqual(len(set(selected)-{active,held}), MAX_NEW_DIRECTIONS)
        self.assertTrue({held, active, "kaipanla:800029", "kaipanla:800000"} <= set(selected))
        self.assertEqual(scope["observed_count"], 30)
        self.assertFalse(scope["evaluation_complete"])
        self.assertEqual(scope["unevaluated_count"], 22)

    def test_unmapped_board_gets_direction_input_and_unknown_membership(self):
        boards = self.boards()
        market = SimpleNamespace(observed_at=AT.isoformat(), snapshot_id="market",
                                 body=dict(sectors=list(boards.values()), quality={}))
        empty_db = SimpleNamespace(execute=lambda sql, args: [])
        ledger = SimpleNamespace(ns="test:discovery", _connect=lambda: nullcontext(empty_db))
        analyst = OctopAnalyst(ledger, {"sectors": {}, "version": "empty"}, market, None, AT)
        request = dict(active_directions={}, tracked_sectors=[], round_slot="14:05",
                       trade_date="2026-11-13")
        output = analyst.prepare(request, None)
        self.assertEqual(len(output["evidence"]), MAX_NEW_DIRECTIONS)
        row = output["evidence"]["kaipanla:800029"]
        self.assertTrue(row["current"])
        self.assertIn("SECTOR_MEMBERS_UNAVAILABLE", row["facts"]["quality"])
        self.assertIsNone(row["facts"]["breadth"]["member_count"])



class MembershipBudget(TestCase):
    def test_daily_plan_survives_mapping_and_source_day_changes(self):
        from unittest.mock import patch
        from modules.strategy_hermes.stage2 import membership_refresh as refresh
        plans = {}

        class SavedPlan:
            def __init__(self, store, run_key):
                self.run_key = run_key

            def call(self, source, scope, operation, *, limit):
                self.assert_limit = limit
                key = (self.run_key, source, tuple(scope))
                if key not in plans:
                    plans[key] = operation()
                return plans[key]

        store = SimpleNamespace(ns="stock-v4:dev:membership-test")
        first = [f"kaipanla:{800000+i}" for i in range(5)]
        later = [f"kaipanla:{900000+i}" for i in range(5)]
        with patch.object(refresh, "RequestLog", SavedPlan):
            self.assertEqual(refresh._daily_targets(store, first, AT), first[:3])
            self.assertEqual(refresh._daily_targets(store, later, AT.replace(hour=16)), first[:3])
            self.assertEqual(refresh._daily_targets(store, later, AT.replace(day=14)), later[:3])

    def test_member_refresh_rejects_current_uncompleted_source_day_before_requests(self):
        from unittest.mock import patch
        from modules.strategy_hermes.stage2 import membership_refresh as refresh
        with patch.object(refresh, "RequestLog") as request:
            with self.assertRaises(ValueError):
                refresh.refresh(None, {}, {"trade_date": "2026-11-13"},
                                "2026-11-13", None, now=AT)
            request.assert_not_called()
