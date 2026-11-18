"""Exercise bounded stock-evidence selection and collection without egress."""
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import stock_research as stocks
from modules.strategy_hermes.stage2 import intraday
from modules.strategy_hermes.stage2.inputs import SharedMarket

AT = datetime(2026, 11, 13, 14, 5, 30, tzinfo=CHINA)
SID = "kaipanla:801027"
INDEX = "399986.SZ"
ETF = "512800.SH"
FIRST, SECOND, THIRD = "600001.SH", "600002.SH", "600003.SH"


def fixture():
    mapping = {"version": "bank-fixture", "sectors": {SID: {
        "members": [FIRST, SECOND, THIRD, "600004.SH", "600005.SH", "600006.SH", "600007.SH"],
        "unresolved_count": 0, "reported_count": 7, "as_of_date": "2026-11-12", "source_batches": ["fixture"],
    }}, "products": {ETF: {"index_code": INDEX, "trade_unit": 100, "rule": {
        "verified": True, "verified_at": "2026-11-13T13:40:00+08:00",
        "scope": "domestic_passive_sector", "index_code": INDEX, "price_limit_ratio": "0.10",
    }}}, "weights": {INDEX: {"complete": True, "as_of_date": "2026-10-30", "rows": [
        {"code": "600007.SH", "weight": ".20", "mainboard": False},
        {"code": SECOND, "weight": ".20", "mainboard": True},
        {"code": FIRST, "weight": ".20", "mainboard": True},
        {"code": THIRD, "weight": ".15", "mainboard": True},
        {"code": "600004.SH", "weight": ".10", "mainboard": True},
        {"code": "600005.SH", "weight": ".05", "mainboard": True},
        {"code": "600006.SH", "weight": ".05", "mainboard": True},
        {"code": "600008.SH", "weight": ".05", "mainboard": True},
    ]}}}
    market = SimpleNamespace(body={"trade_date": "2026-11-13", "mode": "PARTIAL_REALTIME", "sectors": [
        {"sector_id": SID, "observed_at": "2026-11-13T14:05:00+08:00", "return_ratio": ".02"}
    ]})
    return mapping, market


class StockResearch(TestCase):
    def test_scope_uses_complete_members_independent_of_etf_weights(self):
        mapping, market = fixture()
        result = stocks.shortlist(mapping, market, AT)
        self.assertEqual([item["code"] for item in result[SID]], mapping["sectors"][SID]["members"])
        self.assertEqual(result[SID][0], {"code": FIRST, "membership_as_of": "2026-11-12"})
        no_etf = deepcopy(mapping)
        no_etf["products"], no_etf["weights"] = {}, {}
        self.assertEqual(stocks.shortlist(no_etf, market, AT), result)
        for key in ("unresolved_count", "reported_count", "as_of_date"):
            invalid = deepcopy(mapping)
            invalid["sectors"][SID][key] = 1 if key != "as_of_date" else "2026-11-16"
            self.assertEqual(stocks.shortlist(invalid, market, AT), {})
        mapping["products"][ETF]["rule"]["verified"] = False
        self.assertEqual(stocks.shortlist(mapping, market, AT), result)

    def test_stock_payload_keeps_the_etf_clock_and_is_bound_to_the_round(self):
        mapping, market = fixture()
        scope = stocks.shortlist(mapping, market, AT)
        etf_clock = "2026-11-13T14:05:00+08:00"
        stock_clock = "2026-11-13T14:05:27+08:00"
        body = dict(schema_version="hotspot_market_snapshot_v1", snapshot_id="combined",
                    mode="PARTIAL_REALTIME", trade_date="2026-11-13", observed_at=etf_clock,
                    quality={"etf_quote_count": 1}, quotes={ETF: {"price_cny": "1.10", "observed_at": etf_clock}})
        stocks.attach(body, {"quotes": {FIRST: {"price_cny": "1.50", "observed_at": stock_clock,
                      "source": "tickflow:stock_quotes"}}, "research_scope": scope}, "batch-1", "14:05")
        snapshot = SharedMarket.read(body)
        self.assertEqual(snapshot.observed_at, etf_clock)
        self.assertEqual(list(snapshot.body["quotes"]), [ETF])
        self.assertEqual(stocks.for_sector(snapshot, SID, "14:30", AT), [])
        self.assertEqual(stocks.for_sector(snapshot, SID, "14:05", AT.replace(minute=8)), [])
        self.assertEqual(stocks.for_sector(snapshot, SID, "14:05", AT.replace(second=10)), [])
        row, = stocks.for_sector(snapshot, SID, "14:05", AT)
        self.assertEqual((row["code"], row["observed_at"], row["batch"]), (FIRST, stock_clock, "batch-1"))

    def test_unavailable_stock_provider_does_not_drop_the_shared_market(self):
        mapping, _ = fixture()
        ledger = SimpleNamespace(ns="stock-v4:prod:baseline", settings=SimpleNamespace(env="production"))
        cache = object()
        body = dict(schema_version="hotspot_market_snapshot_v1", snapshot_id="base",
                    trade_date="2026-11-13", observed_at=AT.isoformat(), quotes={}, quality={})
        with patch("modules.strategy_hermes.stage2.inputs.shared_market", return_value=None),              patch.object(intraday, "RequestLog") as budget,              patch.object(intraday, "collect_boards", return_value=[]),              patch.object(intraday, "assemble", return_value=body),              patch.object(intraday, "publish_snapshot") as publish,              patch.object(stocks, "collect", side_effect=intraday.ProbeError("provider failure")) as collect:
            budget.return_value.call.return_value = {"batch": "etfs"}
            budget.return_value.run_key = "fixture"
            result = intraday._refresh(ledger, cache, "stock-v4:prod:source:market",
                                      ("2026-11-13", "14:05", AT), None, "private", "captures",
                                      stock_mapping=mapping)
        self.assertEqual(result["stock_collection"]["status"], "UNAVAILABLE")
        self.assertEqual(result["snapshot_id"], "base")
        self.assertEqual(body["quality"]["stock_quotes"], "UNAVAILABLE")
        self.assertEqual(collect.call_args.args[5], "fixture")
        publish.assert_called_once_with(ledger, cache, body, key="stock-v4:prod:source:market")

    def test_default_and_outside_session_do_not_allocate_request_budget(self):
        with patch.object(stocks, "RequestLog") as budget:
            self.assertEqual(stocks.collect(None, None, None, None, AT, "fixture"),
                             {"status": "DISABLED", "new_requests": 0})
            self.assertEqual(stocks.collect(None, None, None, None, AT.replace(hour=16), "fixture", enabled=True),
                             {"status": "OUTSIDE_SESSION", "new_requests": 0})
            budget.assert_not_called()

    def test_one_scoped_batch_persists_evidence_without_etf_rows(self):
        mapping, market = fixture()
        stored = {"batch": "fixture-batch", "status": "STORED"}
        stamp = lambda clock: int(datetime.fromisoformat("2026-11-13T"+clock+"+08:00").timestamp()*1000)
        body = {"data": [
            {"symbol": FIRST, "timestamp": stamp("14:05:32"), "last_price": "10.25", "prev_close": "10"},
            {"symbol": SECOND, "timestamp": stamp("14:05:20"), "last_price": "12", "trading_status": "suspended"},
            {"symbol": THIRD, "timestamp": stamp("12:00:01"), "last_price": "11"},
        ]}
        ledger = object()
        with patch.object(stocks, "RequestLog") as budget, patch.object(stocks, "fetch", return_value=body) as fetch, \
             patch.object(stocks, "tickflow_config", return_value="fixture-key"), \
             patch.object(stocks, "save", return_value=stored) as save, \
             patch.object(stocks, "datetime", wraps=datetime) as clock:
            clock.now.return_value = AT.replace(second=35)
            budget.return_value.call.side_effect = lambda source, scope, operation, *, limit: operation()
            budget.return_value.new_calls = 1
            self.assertEqual(stocks.collect(ledger, "/private/config.json", mapping, market, AT, "fixture", enabled=True),
                             stored | {"new_requests": 1})
        budget.assert_called_once_with(ledger, "fixture")
        source, scope, _ = budget.return_value.call.call_args.args
        self.assertEqual(source, "tickflow:stock_quotes")
        self.assertEqual(budget.return_value.call.call_args.kwargs, {"limit": 1})
        fetch.assert_called_once()
        request, = fetch.call_args.args
        self.assertEqual(json.loads(request.data), {"universes": ["CN_Equity_A"]})
        self.assertEqual(fetch.call_args.kwargs, {"timeout": 15})
        self.assertEqual(scope["mapping_version"], "bank-fixture")
        _, provider, endpoint, _, payload, *_ = save.call_args.args
        self.assertEqual((provider, endpoint), ("tickflow", "stock_quotes"))
        self.assertEqual(save.call_args.kwargs, {})
        self.assertEqual(list(payload["quotes"]), [FIRST])
        self.assertEqual(payload["quotes"][FIRST]["observed_at"], "2026-11-13T14:05:32+08:00")
        self.assertEqual(payload["quotes"][FIRST]["return_ratio"], "0.025000000000")
        self.assertEqual(payload["issues"], {SECOND: "NOT_TRADING", THIRD: "NOT_CURRENT_SESSION"})

    def test_breadth_keeps_population_and_does_not_extrapolate_research_shortlist(self):
        mapping, market = fixture()
        scope = stocks.shortlist(mapping, market, AT)
        quotes = {
            FIRST: {"price_cny": "10", "return_ratio": "0.02", "observed_at": AT.isoformat(),
                    "source": "tickflow:stock_quotes"},
            SECOND: {"price_cny": "10", "return_ratio": "-0.01", "observed_at": AT.isoformat(),
                     "source": "tickflow:stock_quotes"},
            THIRD: {"price_cny": "10", "return_ratio": "0.03", "observed_at": AT.replace(hour=10).isoformat(),
                    "source": "tickflow:stock_quotes"},
        }
        market.body["stock_research"] = dict(round_slot="14:05", batch="batch",
                                             quotes=quotes, research_scope=scope)
        result = stocks.breadth(market, mapping, SID, "14:05", AT)
        self.assertEqual((result["status"], result["member_count"], result["observed_count"]),
                         ("PARTIAL", 7, 2))
        self.assertEqual((result["up"], result["down"], result["up_ratio"]), (1, 1, "0.5"))
        self.assertEqual(result["median_return"], "0.005")
        self.assertEqual(stocks.breadth(market, mapping, SID, "14:30", AT)["status"], "UNKNOWN")
        mapping["sectors"][SID]["available_at"] = AT.replace(hour=15).isoformat()
        self.assertEqual(stocks.breadth(market, mapping, SID, "14:05", AT)["status"], "UNKNOWN")

    def test_real_return_is_consumable_without_decimal_precision_rejection(self):
        from decimal import Decimal
        from modules.strategy_hermes.stage2.penetration import number
        quote = {"symbol": FIRST, "timestamp": int(AT.timestamp()*1000),
                 "last_price": "10.23", "prev_close": "9.7"}
        with patch.object(stocks, "fetch", return_value={"data": [quote]}), \
             patch.object(stocks, "tickflow_config", return_value="fixture-key"), \
             patch.object(stocks, "save", return_value={}) as saved, \
             patch.object(stocks, "datetime", wraps=datetime) as clock:
            clock.now.return_value = AT
            stocks._fetch_batch(None, "/private/key", {"universes": ["CN_Equity_A"]}, {}, AT)
        row = saved.call_args.args[4]["quotes"][FIRST]
        self.assertEqual(number(row["return_ratio"]), Decimal("0.054639175258"))

    def test_stock_leaders_are_selected_by_saved_market_evidence(self):
        mapping, market = fixture()
        codes = [f"600{i:03}.SH" for i in range(1,21)]
        market.body["stock_research"] = dict(batch="sample", round_slot="14:05",
            research_scope={SID: [{"code": code} for code in codes]},
            quotes={code: {"price_cny": "10", "observed_at": AT.isoformat(),
                    "return_ratio": str(i/1000), "amount_cny": str(1000000*(20-i)),
                    "source": "tickflow:stock_quotes"} for i,code in enumerate(codes)})
        rows = stocks.for_sector(market, SID, "14:05", AT)
        self.assertEqual(len(rows), 12)
        self.assertIn(codes[-1], {row["code"] for row in rows})
        self.assertIn(codes[0], {row["code"] for row in rows})
        market.body["stock_research"]["quotes"][codes[-1]]["trading_status"] = "suspended"
        self.assertNotIn(codes[-1], {row["code"] for row in stocks.for_sector(market, SID, "14:05", AT)})


    def test_repeating_breadth_ratios_reach_qq_as_partial_coverage(self):
        from decimal import Decimal
        from modules.strategy_hermes.stage2.notifications import _breadth_summary
        mapping, market = fixture()
        market.body["stock_research"] = dict(round_slot="14:05", batch="thirds", quotes={
            code: dict(price_cny="10", return_ratio=value, observed_at=AT.isoformat(),
                       source="tickflow:stock_quotes")
            for code, value in ((FIRST, ".02"), (SECOND, "-.01"), (THIRD, "0"))
        })
        result = stocks.breadth(market, mapping, SID, "14:05", AT)
        self.assertEqual((result["status"], result["observed_count"], result["member_count"]), ("PARTIAL", 3, 7))
        self.assertEqual(stocks.number(result["up_ratio"]), Decimal("0.333333333333"))
        self.assertEqual(stocks.number(result["coverage"]), Decimal("0.428571428571"))
        line, = _breadth_summary({"source_facts": {SID: {"breadth": result}}}, [SID], {SID: "银行"})
        self.assertIn("覆盖43%", line)
        self.assertIn("上涨占比33%", line)
        self.assertNotIn("待核验", line)

    def test_even_member_median_is_rounded_to_supported_precision(self):
        from decimal import Decimal
        mapping, market = fixture()
        market.body["stock_research"] = dict(round_slot="14:05", batch="half-precision", quotes={
            code: dict(price_cny="10", return_ratio=value, observed_at=AT.isoformat(),
                       source="tickflow:stock_quotes")
            for code, value in ((FIRST, ".000000000001"), (SECOND, ".000000000002"))
        })
        result = stocks.breadth(market, mapping, SID, "14:05", AT)
        self.assertEqual(stocks.number(result["median_return"]), Decimal(".000000000002"))
        self.assertEqual(stocks.number(result["coverage"]), Decimal(".285714285714"))
