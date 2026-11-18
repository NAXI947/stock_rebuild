"""Check the operator's late-session quote gates without contacting providers."""
from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import Mock, patch

from modules.strategy_hermes.simulator import Calendar, moment
from modules.strategy_hermes.stage2.inputs import SharedMarket
from tools import production_pending_quotes as tool


DAY = "2026-11-13"
CODE = "512800.SH"
CALENDAR = Calendar(["2026-11-12", DAY, "2026-11-16"])
UNITS = {CODE: 100}
KEY = "stock-v4:prod:source:market"


def at(clock):
    return moment(f"{DAY}T{clock}+08:00")


def market(clock):
    observed = at(clock).isoformat()
    return SharedMarket.read(dict(schema_version="hotspot_market_snapshot_v1", snapshot_id="quote-fixture",
        trade_date=DAY, observed_at=observed, mode="PARTIAL_REALTIME", source="frozen:test",
        quotes={CODE: dict(observed_at=observed, price_cny="0.898")}, sectors=[]))


class PendingQuoteSample(TestCase):
    def test_strictly_later_quote_and_same_day_sell_eligibility(self):
        now = at("14:50:00")
        requested = at("14:30:35")
        cycle = dict(etf=CODE, status="WAIT_EXIT", book={"lots": [
            {"remaining": 100, "sellable_from": "2026-11-16"},
        ]}, orders=[dict(action="EXIT", status="PENDING", remaining=100, requested_at=requested.isoformat())])
        self.assertEqual(tool.eligible_orders([cycle], UNITS, CALENDAR, now), {})
        cycle["book"]["lots"][0]["sellable_from"] = DAY
        orders = tool.eligible_orders([cycle], UNITS, CALENDAR, now)
        self.assertEqual(orders, {CODE: requested})
        self.assertEqual(tool.missing_prices(orders, {CODE: [requested]}, CALENDAR, now), [CODE])
        self.assertEqual(tool.missing_prices(orders, {CODE: [at("14:45:05")]}, CALENDAR, now), [])

    def test_preview_and_recorded_attempt_do_not_reach_vendor(self):
        ledger = Mock(ns="stock-v4:prod:baseline")
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (True,)
        ledger._connect.side_effect = lambda: nullcontext(connection)
        orders = {CODE: at("14:30:35")}
        with patch.object(tool, "inspect", return_value=(None, orders, [CODE], None)) as inspect, \
             patch.object(tool, "quote_pool") as quote_pool, patch.object(tool, "publish_snapshot") as publish:
            result = tool.sample(ledger, Mock(), KEY, CALENDAR, UNITS, None, lambda: False,
                                 apply=False, clock=lambda: at("14:50:00"))
            self.assertEqual(result["status"], "READY")
            connection.execute.assert_not_called()
            inspect.return_value = (None, orders, [CODE], "FAILED")
            result = tool.sample(ledger, Mock(), KEY, CALENDAR, UNITS, None, lambda: False,
                                 apply=True, clock=lambda: at("14:50:00"))
            self.assertEqual(result["status"], "ALREADY_ATTEMPTED")
            quote_pool.assert_not_called()
            publish.assert_not_called()

    def test_one_limited_batch_only_publishes_a_later_quote(self):
        ledger = Mock(ns="stock-v4:prod:baseline")
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (True,)
        ledger._connect.side_effect = lambda: nullcontext(connection)
        orders = {CODE: at("14:30:35")}
        cache = Mock()
        previous, fresh = market("14:30:00"), market("14:45:05")
        fresh.body["quality"] = {}
        with patch.object(tool, "inspect", side_effect=[
            (previous, orders, [CODE], None), (previous, orders, [CODE], "SAVED"),
        ]), patch.object(tool, "tickflow_config"), \
             patch.object(tool, "RequestLog") as request_log, \
             patch.object(tool, "quote_pool", return_value={"batch": "fixture-batch"}) as quote_pool, \
             patch.object(tool, "assemble", return_value=fresh.body) as assemble, \
             patch.object(tool, "publish_snapshot", return_value=(KEY, True, fresh.snapshot_id)) as publish:
            request_log.return_value.call.side_effect = lambda source, scope, fn, *, limit: fn()
            result = tool.sample(ledger, cache, KEY, CALENDAR, UNITS, "fixture", lambda: False,
                                 apply=True, clock=lambda: at("14:50:00"))
            self.assertEqual(result["status"], "PUBLISHED")
            self.assertEqual((result["physical_requests"], result["covered"]), (1, [CODE]))
            self.assertTrue(result["run_key"].endswith(":2026-11-13:pending-quotes"))
            self.assertEqual(request_log.return_value.call.call_args.kwargs, {"limit": 1})
            self.assertEqual(request_log.return_value.call.call_args.args[:2], ("tickflow:quotes", ["CN_ETF"]))
            quote_pool.assert_called_once_with(ledger, "fixture")
            assemble.assert_called_once_with(ledger, DAY, previous, {"batch": "fixture-batch"}, [])
            publish.assert_called_once_with(ledger, cache, fresh.body, key=KEY)
            ledger.apply_quotes.assert_not_called()
