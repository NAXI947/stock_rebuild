"""Optional order-book transport and time alignment, without provider requests."""
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import intraday, market_bootstrap as adapter
from test_etf_recommendations import AT, ETF, fixture
from modules.strategy_hermes.stage2.penetration import _recommend_sector
from test_etf_recommendations import SID, CODES


class EtfDepth(TestCase):
    def row(self, code=ETF, *, time=None, bid=10.19, ask=10.20):
        clock = time or AT
        # fetch() parses JSON fractional numbers as exact decimal strings.
        # Exercise the same wire decoding, without introducing binary floats into money code.
        return adapter.document(json.dumps(dict(symbol=code, region="CN", timestamp=int(clock.timestamp()*1000),
                    bid_prices=[bid], ask_prices=[ask], bid_volumes=[100], ask_volumes=[200])).encode())

    def test_official_depth_shape_and_absent_sides_remain_unknown(self):
        rows, issues = adapter.normalize_tickflow_depth({"data": {ETF: self.row()}}, [ETF], AT)
        self.assertEqual(rows, [dict(code=ETF, book_observed_at=AT.isoformat(), bid1_cny="10.19", ask1_cny="10.2")])
        self.assertEqual(issues, {})
        rows, issues = adapter.normalize_tickflow_depth({"data": {ETF:self.row(ask=0)}}, [ETF,"512480.SH"], AT)
        self.assertNotIn("ask1_cny", rows[0])
        self.assertEqual(issues, {ETF:"BOOK_ONE_SIDE_UNAVAILABLE","512480.SH":"BOOK_UNAVAILABLE"})
        rows, issues = adapter.normalize_tickflow_depth({"data": {ETF:self.row(bid=11)}}, [ETF], AT)
        self.assertFalse(rows)
        self.assertEqual(issues[ETF], "BOOK_UNVERIFIED")

    def test_depth_batch_uses_one_get_and_retains_source_timestamp(self):
        with patch.object(adapter, "tickflow_config", return_value="fixture-key"), \
             patch.object(adapter, "fetch", return_value={"data":{ETF:self.row()}}) as fetch, \
             patch.object(adapter, "save", return_value={"batch":"book","status":"STORED"}) as save, \
             patch.object(adapter, "datetime", wraps=datetime) as clock:
            clock.now.return_value = AT
            result = adapter.depth_batch(object(), "private", [ETF])
        request = fetch.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://api.tickflow.org/v1/depth/batch?symbols=512800.SH")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(save.call_args.args[6], AT.isoformat())
        self.assertEqual(result["books"][0]["book_observed_at"], AT.isoformat())

    def test_multiple_symbols_use_literal_comma_supported_by_live_endpoint(self):
        codes = ["512480.SH", ETF]

        def endpoint(request, *, timeout):
            # The live endpoint resets multi-symbol requests containing %2C.
            if "%2C" in request.full_url:
                raise adapter.ProbeError("provider transport failure")
            self.assertEqual(request.full_url,
                             "https://api.tickflow.org/v1/depth/batch?symbols=512480.SH,512800.SH")
            return {"data": {code: self.row(code) for code in codes}}

        with patch.object(adapter, "tickflow_config", return_value="fixture-key"), \
             patch.object(adapter, "fetch", side_effect=endpoint) as fetch, \
             patch.object(adapter, "save", return_value={"batch": "book", "status": "STORED"}), \
             patch.object(adapter, "datetime", wraps=datetime) as clock:
            clock.now.return_value = AT
            result = adapter.depth_batch(object(), "private", [ETF, codes[0], ETF])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual({book["code"] for book in result["books"]}, set(codes))
        self.assertEqual(result["issues"], {})

    def test_optional_book_attachment_keeps_quote_price_and_clock(self):
        mapping, market = fixture()
        market.body.update(snapshot_id="before", quality={})
        quote = market.body["quotes"][ETF]
        saved_clock, price = quote["observed_at"], quote["price_cny"]
        budget = Mock()
        budget.call.side_effect = lambda source, scope, operation, limit: operation()
        book = dict(code=ETF, book_observed_at=(AT-timedelta(seconds=10)).isoformat(),bid1_cny="10.19",ask1_cny="10.2")
        with patch.object(adapter, "depth_batch", return_value=dict(batch="book",books=[book],issues={})):
            result=intraday.collect_books(object(),budget,"private",mapping,market.body,AT)
        self.assertEqual(result["valid"],1)
        budget.call.assert_called_once()
        self.assertEqual(budget.call.call_args.kwargs,{"limit":1})
        quote=market.body["quotes"][ETF]
        self.assertEqual((quote["observed_at"],quote["price_cny"]),(saved_clock,price))
        self.assertEqual(quote["book_observed_at"],book["book_observed_at"])
        self.assertNotEqual(market.body["snapshot_id"],"before")

    def test_no_shared_etf_quotes_does_not_request_book_or_change_snapshot(self):
        mapping, market = fixture()
        market.body.update(snapshot_id="before", quotes={})
        before = json.loads(json.dumps(market.body))
        budget = Mock()
        result = intraday.collect_books(object(), budget, "private", mapping, market.body, AT)
        self.assertEqual(result, dict(status="EMPTY_SCOPE", requested=0, valid=0))
        budget.call.assert_not_called()
        self.assertEqual(market.body, before)

    def test_old_future_and_prior_day_book_is_not_used_and_quotes_survive(self):
        for delta in (-121,1,-86400):
            mapping, market=fixture()
            market.body.update(snapshot_id="before",quality={})
            budget=Mock()
            budget.call.return_value=dict(batch="book",books=[dict(code=ETF,
                book_observed_at=(AT+timedelta(seconds=delta)).isoformat(),bid1_cny="10",ask1_cny="10.2")])
            result=intraday.collect_books(object(),budget,"private",mapping,market.body,AT)
            self.assertEqual(result["valid"],0)
            self.assertNotIn("bid1_cny",market.body["quotes"][ETF])
            self.assertEqual(market.body["quotes"][ETF]["price_cny"],"10.2")

    def test_stale_book_only_changes_optional_execution_check(self):
        mapping,market=fixture()
        quote=market.body["quotes"][ETF]
        quote.update(bid1_cny="10.19",ask1_cny="10.20",book_observed_at=(AT-timedelta(seconds=121)).isoformat())
        result=_recommend_sector(mapping,SID,market,AT,CODES[:2],"VERIFIED",set())["products"][0]
        self.assertEqual(result["checks"]["spread"]["result"],"UNKNOWN")
        self.assertEqual(result["momentum_status"],"PASS")
        self.assertEqual(result["recommendation_status"],"CONFIRMED")

    def test_book_failure_still_publishes_snapshot(self):
        mapping,shared=fixture()
        market=shared.body|dict(snapshot_id="fixture",quality={})
        ledger=SimpleNamespace(ns="stock-v4:prod:fixture",settings=SimpleNamespace(env="production"))
        with patch("modules.strategy_hermes.stage2.inputs.shared_market",return_value=None), \
             patch.object(intraday,"RequestLog") as log, \
             patch.object(intraday,"collect_boards",return_value=[]), \
             patch.object(intraday,"assemble",return_value=market), \
             patch.object(intraday,"collect_books",side_effect=adapter.ProbeError("denied")), \
             patch("modules.strategy_hermes.stage2.findesk_iopv.collect",return_value=dict(status="UNAVAILABLE")), \
             patch("modules.strategy_hermes.stage2.findesk_iopv.mark_quality"), \
             patch.object(intraday,"publish_snapshot") as publish:
            log.return_value.new_calls=2
            result=intraday._refresh(ledger,None,"fixture",(AT.date().isoformat(),"14:05",AT),
                                     None,None,None,findesk=("private",mapping))
        self.assertEqual(result["order_book"]["status"],"UNAVAILABLE")
        self.assertEqual(market["quality"]["book_count"],0)
        publish.assert_called_once()
