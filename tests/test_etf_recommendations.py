"""Recommendation rules exercise real pure code and never send, collect or write a ledger."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from modules.strategy_hermes.stage2.penetration import (
    _recommend_sector, evaluate_recommendations_legacy as evaluate_recommendations,
    evaluate, explain_sector, maintained_stats,
)
from modules.strategy_hermes.stage2.momentum import alpha, evaluate as momentum
from test_stage1 import report

AT = datetime.fromisoformat("2026-11-13T14:05:30+08:00")
SID, ETF, OTHER = "kaipanla:801027", "512800.SH", "512801.SH"
CODES = ["600001.SH", "600002.SH", "600003.SH", "600004.SH"]


def fixture():
    stats = dict(adv20="100000000",adv5="110000000",history_days=20,
                 history_as_of="2026-11-12",history_available_at="2026-11-13T09:00:00+08:00",
                 ma5_raw_prev5="10",prior_close_cny="10",ma5_basis_status="VERIFIED")
    product = dict(index_code="index",trade_unit=100,stats={},recommendation_stats=stats,
                   rule=dict(verified=True,scope="domestic_passive_sector",index_code="index",
                             price_limit_ratio=".1",verified_at="2026-11-12T16:00:00+08:00"))
    mapping = dict(version="fixture",sectors={SID:dict(members=CODES[:3],reported_count=3,unresolved_count=0,source_batches=["fixture"],as_of_date="2026-11-12")},products={ETF:product},
                   weights={"index":dict(complete=True,as_of_date="2026-11-12",rows=[
                       dict(code=c,weight=w,mainboard=True) for c,w in zip(CODES,[".4",".3",".2",".1"])
                   ])})
    quote = dict(price_cny="10.2",pre_close_cny="10",observed_at=AT.isoformat(),
                 amount_cny="101500000",volume_shares="10000000",
                 volume_unit_verified=True,amount_unit_verified=True,source="fixture")
    stocks = {c:dict(price_cny="10.2",pre_close_cny="10",return_ratio=".02",observed_at=AT.isoformat(),source="fixture")
              for c in CODES}
    market = SimpleNamespace(snapshot_id="fixture",body=dict(quotes={ETF:quote},stock_research={"quotes":stocks},
                           iopv={"readings":{}}))
    return mapping,market


def catalog_fixture():
    """Production shape: 1,469 catalog products, 118 weighted, one verified."""
    mapping, market = fixture()
    for i in range(117):
        product = deepcopy(mapping['products'][ETF])
        product['rule'] = dict(verified=False, reason='PRODUCT_REFERENCE_REQUIRES_VERIFICATION')
        mapping['products'][f'{510000+i:06}.SH'] = product
    for i in range(1351):
        mapping['products'][f'{158000+i:06}.SZ'] = dict(index_code='missing-index', rule=dict(verified=False))
    mapping['sectors']['kaipanla:801001'] = dict(mapping['sectors'][SID], members=[CODES[3]], reported_count=1)
    return mapping, market


class Recommendations(unittest.TestCase):
    def setUp(self):
        self.mapping,self.market=fixture()

    def rows(self,core=None,status="VERIFIED",events=()):
        return _recommend_sector(self.mapping,SID,self.market,AT,CODES[:2] if core is None else core,status,set(events))

    def test_legacy_frozen_statistics_and_future_maintenance_remain_unchanged(self):
        mapping=deepcopy(self.mapping)
        product=mapping["products"][ETF]
        current=product.pop("recommendation_stats")
        product["stats"]={"adv20":"40000000","adv5":"45000000"}
        original=deepcopy(mapping)
        baseline=explain_sector(mapping,SID,self.market.body["quotes"],AT)
        self.assertEqual(mapping,original)
        self.assertEqual(explain_sector(mapping,SID,self.market.body["quotes"],AT),baseline)
        for fields in ({"history_available_at":"2026-11-14T09:00:00+08:00"},
                       {"history_as_of":"2026-11-13"}):
            product["recommendation_stats"]=dict(current,adv20="1",**fields)
            self.assertEqual(explain_sector(mapping,SID,self.market.body["quotes"],AT),baseline)

    def test_legacy_candidate_uses_available_maintained_liquidity_without_mutation(self):
        mapping=deepcopy(self.mapping)
        product=mapping["products"][ETF]
        product["stats"]={"adv20":"10000000","adv5":"15000000"}
        current=product.pop("recommendation_stats")
        old=explain_sector(mapping,SID,self.market.body["quotes"],AT)
        self.assertIn("LIQUIDITY_BELOW_MINIMUM",old["products"][0]["rejection_reasons"])
        product["recommendation_stats"]=current
        archived=deepcopy(mapping)
        updated=explain_sector(mapping,SID,self.market.body["quotes"],AT)
        self.assertNotIn("LIQUIDITY_BELOW_MINIMUM",updated["products"][0]["rejection_reasons"])
        self.assertEqual(updated["representatives"],[ETF])
        self.assertEqual(mapping,archived)
        self.assertEqual(product["stats"]["adv20"],"10000000")

    def test_dated_maintained_aum_respects_validity_without_rewriting_legacy_archives(self):
        old={"aum_yuan":"100000000","aum_verified":True,"aum_as_of":"2026-03-31"}
        self.assertEqual(maintained_stats({"stats":old},AT),old)
        dated=dict(old,aum_available_at="2026-11-12T09:00:00+08:00",aum_valid_until="2026-11-12")
        self.assertFalse(maintained_stats({"stats":dated},AT)["aum_verified"])
        dated["aum_valid_until"]="2026-11-13"
        self.assertTrue(maintained_stats({"stats":dated},AT)["aum_verified"])
        self.assertTrue(dated["aum_verified"])

    def test_full_evidence_confirms_with_optional_missing(self):
        item=self.rows()["products"][0]
        self.assertEqual(item["static_status"],"PASS")
        self.assertEqual(item["momentum_status"],"PASS")
        self.assertEqual(item["execution_status"],"PARTIAL")
        self.assertEqual(item["recommendation_status"],"CONFIRMED")
        self.assertEqual(item["checks"]["volume_ratio"]["result"],"OBSERVE_ONLY")
        self.assertTrue(item["representative_current"])

    def test_low_constituent_coverage_keeps_static_result(self):
        self.market.body["stock_research"]["quotes"]={CODES[0]:self.market.body["stock_research"]["quotes"][CODES[0]]}
        item=self.rows()["products"][0]
        self.assertEqual(item["static_status"],"PASS")
        self.assertEqual(item["momentum_status"],"UNKNOWN")
        self.assertEqual(item["recommendation_status"],"PENDING")
        self.assertEqual(item["checks"]["resonance"]["result"],"UNKNOWN")

    def test_missing_core_differs_from_verified_empty(self):
        self.assertEqual(self.rows(core=[],status="UNKNOWN")["products"][0]["recommendation_status"],"PENDING")
        empty=self.rows(core=[])["products"][0]
        self.assertEqual(empty["checks"]["core"]["result"],"NOT_APPLICABLE")
        self.assertEqual(empty["recommendation_status"],"CONFIRMED")

    def test_fifty_percent_recommendation_does_not_change_legacy(self):
        self.mapping["sectors"][SID].update(members=[CODES[0],CODES[3]],reported_count=2)
        self.assertEqual(self.rows(core=[])["products"][0]["static_status"],"PASS")
        event=report("legacy","2026-11-13","14:05",[(SID,"新增")])
        candidates,_=evaluate(event,self.mapping,self.market,use_report_core=False)
        self.assertEqual(candidates,[])

    def test_quote_age_and_cross_source_alignment_are_local_unknowns(self):
        self.market.body["stock_research"]["quotes"][CODES[0]]["observed_at"]="2026-11-13T10:00:00+08:00"
        item=self.rows()["products"][0]
        self.assertEqual(item["checks"]["resonance"]["result"],"UNKNOWN")
        self.assertEqual(item["static_status"],"PASS")
        self.market.body["quotes"][ETF]["observed_at"]="2026-11-13T10:00:00+08:00"
        item=self.rows()["products"][0]
        self.assertEqual(item["checks"]["quote"]["result"],"UNKNOWN")
        self.assertFalse(item["representative_current"])

    def test_valid_premium_failure_is_different_from_optional_missing(self):
        self.market.body["iopv"]["readings"][ETF]=dict(iopv="10",observed_at=AT.isoformat(),source="fixture")
        item=self.rows()["products"][0]
        self.assertEqual(item["momentum_status"],"PASS")
        self.assertEqual(item["execution_status"],"FAILED")
        self.assertEqual(item["recommendation_status"],"STATIC_ONLY")
        self.assertFalse(item["representative_current"])
        self.market.body["iopv"]["readings"][ETF]["iopv"]="10.3"
        self.assertEqual(self.rows()["products"][0]["checks"]["premium"]["result"],"PASS")

    def test_book_spread_selects_other_usable_representative(self):
        self.mapping["products"][OTHER]=deepcopy(self.mapping["products"][ETF])
        self.market.body["quotes"][OTHER]=deepcopy(self.market.body["quotes"][ETF])
        self.market.body["quotes"][ETF].update(bid1_cny="10",ask1_cny="10.2")
        self.market.body["quotes"][OTHER].update(bid1_cny="10.19",ask1_cny="10.20")
        result=self.rows()
        self.assertEqual(result["representatives"],[OTHER])
        self.assertEqual(result["products"][0]["execution_status"],"FAILED")

    def test_liquidity_is_representative_priority_even_if_momentum_fails(self):
        self.mapping["products"][OTHER]=deepcopy(self.mapping["products"][ETF])
        self.mapping["products"][OTHER]["recommendation_stats"]["adv20"]="90000000"
        self.market.body["quotes"][OTHER]=deepcopy(self.market.body["quotes"][ETF])
        self.market.body["quotes"][ETF].update(price_cny="10.01")
        result=self.rows()
        self.assertEqual(result["representatives"],[ETF])
        self.assertEqual(result["products"][0]["momentum_status"],"FAIL")
        alternative=next(p for p in result["products"] if p["etf"]==OTHER)
        self.assertEqual(alternative["recommendation_status"],"CONFIRMED")
        self.assertFalse(alternative["representative_current"])
        self.assertIn(OTHER,result["products"][0]["alternatives"])

    def test_final_top_three_are_the_only_current_representatives(self):
        for index,code in zip(("a","b","c","d"),("512802.SH","512803.SH","512804.SH","512805.SH")):
            product=deepcopy(self.mapping["products"][ETF])
            product["index_code"]=index
            product["rule"]["index_code"]=index
            self.mapping["products"][code]=product
            self.mapping["weights"][index]=deepcopy(self.mapping["weights"]["index"])
            self.market.body["quotes"][code]=deepcopy(self.market.body["quotes"][ETF])
        result=self.rows()
        self.assertEqual(len(result["products"]),5)
        self.assertEqual(result["representatives"],["512802.SH","512803.SH","512804.SH"])
        flagged={p["etf"] for p in result["products"] if p["representative_current"]}
        self.assertEqual(flagged,set(result["representatives"]))
        self.assertTrue(all(p["recommendation_status"]=="CONFIRMED" for p in result["products"]))

    def test_ma5_basis_failure_and_future_stats_remain_unknown(self):
        self.market.body["quotes"][ETF]["pre_close_cny"]="9"
        self.assertEqual(self.rows()["products"][0]["checks"]["ma5"]["result"],"UNKNOWN")
        self.mapping["products"][ETF]["recommendation_stats"]["history_available_at"]="2026-11-14T09:00:00+08:00"
        self.assertEqual(self.rows()["products"][0]["checks"]["liquidity"]["result"],"UNKNOWN")

    def test_vwap_requires_verified_units(self):
        self.market.body["quotes"][ETF].pop("amount_unit_verified")
        self.assertEqual(self.rows()["products"][0]["checks"]["vwap"]["result"],"UNKNOWN")

    def test_follow_three_applicability_cases_and_observe_volume(self):
        quote=self.market.body["quotes"][ETF]
        stats=self.mapping["products"][ETF]["recommendation_stats"]
        for value,expected in (("-.01","FAIL"),(".007","NOT_APPLICABLE"),(".04","FAIL"),(".02","PASS")):
            metrics=dict(Q="1",Qt="1",R_top3=".02",U_active="1",R_est=value)
            result=momentum(quote,metrics,stats,AT)
            self.assertEqual(result["checks"]["follow"]["result"],expected)
        quote["amount_cny"]="1000000"
        result=momentum(quote,dict(Q="1",Qt="1",R_top3=".02",U_active="1",R_est=".02"),stats,AT)
        self.assertEqual(result["checks"]["volume_ratio"]["result"],"OBSERVE_ONLY")

    def test_event_modes_preserve_sector_gate_for_small_event_sets(self):
        self.mapping["sectors"][SID].update(members=[CODES[0]],reported_count=1)
        small=self.rows(core=[],events=[CODES[1]])
        self.assertEqual(small["scope_mode"],"sector_plus_event_members")
        self.assertEqual(small["products"][0]["static_status"],"FAIL")
        events=CODES+["600005.SH"]
        larger=self.rows(core=[],events=events)
        self.assertEqual(larger["scope_mode"],"event_members")
        self.assertEqual(larger["products"][0]["static_status"],"PASS")

    def test_nonpositive_direction_never_promoted_and_core_not_watch_picks(self):
        evidence=[dict(code=c,ref="ref:"+c) for c in CODES[:2]]
        cores={SID:dict(status="VERIFIED",codes=CODES[:2],evidence=evidence)}
        event=report("direction","2026-11-13","14:05",[(SID,"减弱")],published="14:06")
        result=evaluate_recommendations(event,self.mapping,self.market,core_leaders=cores)
        item=result["sectors"][SID]["products"][0]
        self.assertEqual(item["momentum_status"],"PASS")
        self.assertEqual(item["recommendation_status"],"PENDING")
        event=report("new","2026-11-13","14:05",[(SID,"新增")],published="14:06")
        raw=json.loads(event["report_json"])
        raw["sectors"][0]["core_stock_codes"]=CODES[:2]
        event["report_json"]=json.dumps(raw)
        item=evaluate_recommendations(event,self.mapping,self.market)["sectors"][SID]["products"][0]
        self.assertEqual(item["checks"]["core"]["result"],"UNKNOWN")

    def test_alpha_uses_trading_minutes(self):
        from decimal import Decimal
        for clock,value in (("09:30:00","0"),("09:35:00",".06"),("11:30:00",".55"),("12:00:00",".55"),("13:05:00",".58"),("15:00:00","1")):
            self.assertEqual(alpha(datetime.fromisoformat("2026-11-13T"+clock+"+08:00")),Decimal(value))


    def test_event_scope_works_without_sector_members_and_rejects_unsourced_events(self):
        self.mapping["sectors"]={}
        result=self.rows(core=[],events=CODES+["600005.SH"])
        self.assertEqual(result["scope_mode"],"event_members")
        self.assertIsNone(result["products"][0]["metrics"]["E_sector"])
        self.assertEqual(result["products"][0]["static_status"],"PASS")
        event=report("event","2026-11-13","14:05",[(SID,"新增")],published="14:06")
        invalid={SID:dict(status="VERIFIED",codes=CODES+["600005.SH"],evidence_refs=["fabricated"],observed_at=AT.isoformat())}
        result=evaluate_recommendations(event,self.mapping,self.market,event_members=invalid)
        self.assertEqual(result["sectors"][SID]["reason"],"SECTOR_MEMBERS_UNAVAILABLE")

    def test_expired_scale_is_unknown_and_effective_low_scale_rejected(self):
        stats=self.mapping["products"][ETF]["recommendation_stats"]
        stats.update(aum_verified=True,aum_yuan="100000000",aum_as_of="2026-03-31")
        self.assertEqual(self.rows()["products"][0]["checks"]["aum"]["result"],"UNKNOWN")
        stats.update(aum_as_of="2026-11-12",aum_valid_until="2026-11-13",aum_available_at="2026-11-13T09:00:00+08:00")
        self.assertEqual(self.rows()["products"][0]["recommendation_status"],"REJECTED")


    def test_verified_wrong_product_rule_fails_but_future_rule_unknown(self):
        rule=self.mapping["products"][ETF]["rule"]
        rule["price_limit_ratio"]=".2"
        self.assertEqual(self.rows()["products"][0]["static_status"],"FAIL")
        rule["verified_at"]="2026-11-14T09:00:00+08:00"
        self.assertEqual(self.rows()["products"][0]["static_status"],"UNKNOWN")


    def test_future_or_incomplete_members_cannot_confirm_exact_exposure(self):
        for change in ({"as_of_date":"2026-11-14"},{"reported_count":4},{"unresolved_count":1}):
            with self.subTest(change=change):
                self.mapping,self.market=fixture()
                self.mapping["sectors"][SID].update(change)
                self.assertEqual(self.rows()["reason"],"SECTOR_MEMBERS_UNAVAILABLE")

    def test_beijing_core_is_retained_as_nonindex_member(self):
        self.mapping["sectors"][SID]["members"].append("920001.BJ")
        self.mapping["sectors"][SID]["reported_count"]=4
        result=self.rows(core=[CODES[0],"920001.BJ"])
        self.assertEqual(result["core_codes"],[CODES[0],"920001.BJ"])
        self.assertEqual(result["products"][0]["checks"]["core"]["result"],"PASS")


    def test_invalid_session_status_or_trade_date_is_not_current(self):
        for change in ({"trading_status":"auction"},{"trade_date":"2026-11-12"}):
            with self.subTest(change=change):
                self.mapping,self.market=fixture()
                self.market.body["quotes"][ETF].update(change)
                item=self.rows()["products"][0]
                self.assertEqual(item["checks"]["quote"]["result"],"UNKNOWN")
                self.assertFalse(item["representative_current"])

    def test_catalog_weight_gaps_do_not_become_bank_or_chip_relationships(self):
        mapping, market = catalog_fixture()
        original = deepcopy(mapping)
        bank = _recommend_sector(mapping, SID, market, AT, CODES[:2], 'VERIFIED', set())
        chip = _recommend_sector(mapping, 'kaipanla:801001', market, AT, [], 'UNKNOWN', set())
        self.assertEqual(len(mapping['products']), 1469)
        for result in (bank, chip):
            self.assertEqual(len(result['products']), 118)
            self.assertTrue(all(product['matched'] for product in result['products']))
            self.assertEqual(result['coverage_gaps']['weights'],
                             dict(reason='WEIGHTS_UNVERIFIED', products=1351, indexes=1))
        self.assertEqual(bank['representatives'], [ETF])
        self.assertEqual(bank['static_representatives'], [ETF])
        self.assertEqual(chip['representatives'], [])
        self.assertEqual(mapping, original)
        legacy = explain_sector(mapping, SID, market.body['quotes'], AT)
        self.assertEqual(len(legacy['products']), 118)
        self.assertEqual(legacy['representatives'], [ETF])
        self.assertEqual(legacy['coverage_gaps'], bank['coverage_gaps'])

    def test_static_bank_representative_stays_visible_with_large_incomplete_catalog(self):
        from modules.strategy_hermes.stage2.notifications import _recommendation_sections
        mapping, market = catalog_fixture()
        market.body['quotes'] = {}
        groups = {sid:_recommend_sector(mapping, sid, market, AT, [], 'UNKNOWN', set())
                  for sid in ('kaipanla:801001', SID)}
        entry = dict(input_meta=dict(etf_recommendations=dict(sectors=groups)))
        candidates, observations, gaps = _recommendation_sections(entry, list(groups),
            {SID:'银行', 'kaipanla:801001':'芯片'})
        self.assertEqual(candidates, [])
        self.assertIn('银行 '+ETF, observations[0])
        self.assertFalse(any('158000.SZ' in text for text in observations))
        self.assertFalse(any('目录1351' in text for text in gaps))
        self.assertEqual(groups[SID]['coverage_gaps']['weights']['products'], 1351)

    def test_partial_known_weights_keep_relationship_and_unknown_evidence(self):
        self.mapping['weights']['index']['complete'] = False
        result = self.rows()
        self.assertEqual(len(result['products']), 1)
        self.assertTrue(result['products'][0]['matched'])
        self.assertEqual(result['products'][0]['checks']['weights']['result'], 'UNKNOWN')
        self.assertEqual(result['products'][0]['recommendation_status'], 'PENDING')
        self.assertEqual(result['coverage_gaps']['weights']['products'], 0)

    def test_quote_alignment_cache_is_equivalent_for_shared_and_distinct_source_clocks(self):
        from modules.strategy_hermes.stage2 import momentum, penetration
        mapping, market = fixture()
        for code in (OTHER, '512802.SH'):
            mapping['products'][code] = deepcopy(mapping['products'][ETF])
            market.body['quotes'][code] = deepcopy(market.body['quotes'][ETF])
        market.body['quotes']['512802.SH']['observed_at'] = '2026-11-13T14:03:20+08:00'
        market.body['stock_research']['quotes'][CODES[0]]['observed_at'] = '2026-11-13T14:03:25+08:00'
        original = deepcopy(market.body)
        aligned = penetration._usable_stock_quotes
        valid = momentum.valid_quote
        with patch.object(momentum, 'valid_quote', wraps=valid) as cached_calls:
            cached = _recommend_sector(mapping, SID, market, AT, CODES[:2], 'VERIFIED', set())
        def uncached(stocks, at, reference, cache):
            return aligned(stocks, at, reference, {})
        with patch.object(penetration, '_usable_stock_quotes', side_effect=uncached), \
             patch.object(momentum, 'valid_quote', wraps=valid) as uncached_calls:
            repeated = _recommend_sector(mapping, SID, market, AT, CODES[:2], 'VERIFIED', set())
        self.assertEqual(cached, repeated)
        self.assertLess(cached_calls.call_count, uncached_calls.call_count)
        self.assertEqual(market.body, original)

    def test_shared_quote_cache_is_bounded_and_does_not_mix_snapshot_objects(self):
        from datetime import timedelta
        from modules.strategy_hermes.stage2.penetration import _usable_stock_quotes
        stocks = self.market.body['stock_research']['quotes']
        cache = {}
        for offset in range(25):
            _usable_stock_quotes(stocks, AT, AT-timedelta(seconds=offset), cache)
        self.assertLessEqual(len(cache), 16)
        changed = deepcopy(stocks)
        changed[CODES[0]]['trading_status'] = 'SUSPENDED'
        current = _usable_stock_quotes(changed, AT, AT, cache)
        self.assertNotIn(CODES[0], current)
        self.assertIn(CODES[0], _usable_stock_quotes(stocks, AT, AT, cache))
