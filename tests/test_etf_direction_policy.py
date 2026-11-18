"""Direction choices share one fact result; preferences never grant execution rights."""
from copy import deepcopy
from datetime import timedelta
from unittest import TestCase

from test_etf_recommendations import AT, CODES, ETF, OTHER, SID, catalog_fixture, fixture
from test_stage1 import report
from modules.strategy_hermes.stage2.penetration import (
    DIRECTION_METHOD, evaluate, evaluate_recommendations, instrument_units,
    prepare_etf_facts, recommendation_candidates, research_products, verified_product,
)


class DirectionPolicy(TestCase):
    def setUp(self):
        self.mapping, self.market = fixture()
        self.event = report('direction-policy', '2026-11-13', '14:05', [(SID, '新增')], published='14:06')

    def result(self, choices=None):
        return evaluate_recommendations(self.event, self.mapping, self.market, etf_choices=choices)

    def item(self, result):
        return result['sectors'][SID]['products'][0]

    def test_ai_choice_can_enter_with_twenty_percent_rule_and_reference_checks_failing(self):
        product = self.mapping['products'][ETF]
        product['rule']['price_limit_ratio'] = '.2'
        product['recommendation_stats'] = {}
        for row in self.mapping['weights']['index']['rows']:
            row['mainboard'] = False
        self.market.body['quotes'][ETF]['price_cny'] = '9.9'
        original = deepcopy(self.mapping)
        result = self.result({SID:[ETF]})
        item = self.item(result)
        self.assertEqual(result['method'], DIRECTION_METHOD)
        self.assertEqual(item['recommendation_status'], 'CONFIRMED')
        self.assertEqual(item['execution_eligibility'], 'READY')
        self.assertEqual(item['checks']['mainboard']['result'], 'FAIL')
        self.assertEqual(item['checks']['mainboard']['effect'], 'REFERENCE')
        self.assertFalse(item['checks']['etf_return']['required'])
        self.assertEqual(item['checks']['liquidity']['result'], 'UNKNOWN')
        self.assertEqual(item['rejection_reasons'], [])
        self.assertEqual(recommendation_candidates(result), result['candidates'])
        self.assertEqual(result['candidates'], [dict(etf=ETF, sector_id=SID, rank=1, mapping_ref='fixture')])
        self.assertEqual(self.mapping, original)

    def test_missing_empty_and_invalid_choices_keep_direction_without_inventing_entries(self):
        for choices, status in ((None, 'UNCONFIRMED'), ({SID:[]}, 'CONFIRMED'),
                                ({SID:['159999.SZ']}, 'UNCONFIRMED'), ({SID:123}, 'UNCONFIRMED')):
            with self.subTest(choices=choices):
                result = self.result(choices)
                self.assertEqual(result['candidates'], [])
                self.assertEqual(result['sectors'][SID]['selection_status'], status)
                self.assertEqual(result['sectors'][SID]['direction'], 'NEW')
                self.assertEqual(self.item(result)['recommendation_status'], 'OBSERVE')

    def test_unverified_rule_stays_researchable_and_selected_pending(self):
        self.mapping['products'][ETF]['rule']['verified'] = False
        self.mapping['products'][ETF].pop('trade_unit')
        facts = prepare_etf_facts(self.mapping, self.market, AT, [SID])
        self.assertEqual(facts[SID]['products'][0]['etf'], ETF)
        self.assertIn(ETF, research_products(self.mapping, AT))
        self.assertEqual(instrument_units(self.mapping, AT), {})
        item = self.item(self.result({SID:[ETF]}))
        self.assertTrue(item['ai_selected'])
        self.assertEqual(item['recommendation_status'], 'PENDING')
        self.assertEqual(item['execution_reasons'], ['PRODUCT_RULE_UNVERIFIED', 'TRADE_UNIT_UNVERIFIED'])

    def test_actual_rules_and_units_remain_required(self):
        for change, reason in (({'scope':'cross_border'}, 'PRODUCT_OUTSIDE_SCOPE'),
                               ({'settlement_cycle':'T+0'}, 'PRODUCT_OUTSIDE_SCOPE'),
                               ({'index_code':'other-index'}, 'PRODUCT_OUTSIDE_SCOPE')):
            with self.subTest(change=change):
                self.mapping, self.market = fixture()
                self.mapping['products'][ETF]['rule'].update(change)
                item = self.item(self.result({SID:[ETF]}))
                self.assertEqual(item['recommendation_status'], 'REJECTED')
                self.assertIn(reason, item['execution_reasons'])
        for ratio in ('0', '-.1', 'NaN', None):
            self.mapping, self.market = fixture()
            self.mapping['products'][ETF]['rule']['price_limit_ratio'] = ratio
            self.assertFalse(verified_product(self.mapping['products'][ETF], AT))
        self.mapping, self.market = fixture()
        self.mapping['products'][ETF]['trade_unit'] = 0
        self.assertEqual(self.item(self.result({SID:[ETF]}))['execution_eligibility'], 'PENDING')

    def test_price_and_suspension_are_local_execution_conditions(self):
        for change, status, reason in (({'trading_status':'SUSPENDED'}, 'BLOCKED', 'TRADING_UNAVAILABLE'),
                                      ({'price_cny':'0'}, 'PENDING', 'USABLE_PRICE_UNAVAILABLE'),
                                      ({'source':''}, 'PENDING', 'USABLE_PRICE_UNAVAILABLE'),
                                      ({'source':'  '}, 'PENDING', 'USABLE_PRICE_UNAVAILABLE'),
                                      ({'source':123}, 'PENDING', 'USABLE_PRICE_UNAVAILABLE'),
                                      ({'observed_at':'2026-11-14T14:05:00+08:00'}, 'PENDING', 'USABLE_PRICE_UNAVAILABLE')):
            with self.subTest(change=change):
                self.mapping, self.market = fixture()
                self.market.body['quotes'][ETF].update(change)
                result = self.result({SID:[ETF]})
                self.assertEqual(result['candidates'], [])
                self.assertEqual(self.item(result)['execution_eligibility'], status)
                self.assertIn(reason, self.item(result)['execution_reasons'])

    def test_same_day_delayed_price_keeps_source_clock_and_does_not_create_age_gate(self):
        quote = self.market.body['quotes'][ETF]
        quote['observed_at'] = '2026-11-13T10:00:00+08:00'
        result = self.result({SID:[ETF]})
        self.assertEqual(self.item(result)['recommendation_status'], 'CONFIRMED')
        self.assertEqual(self.item(result)['checks']['quote']['result'], 'UNKNOWN')
        self.assertEqual(self.item(result)['checks']['entry_quote']['value'], quote['observed_at'])

    def test_eod_snapshot_is_reference_only_even_with_valid_same_day_quote(self):
        self.market.body['mode'] = 'EOD'
        result = self.result({SID:[ETF]})
        self.assertEqual(result['candidates'], [])
        self.assertEqual(self.item(result)['execution_eligibility'], 'BLOCKED')
        self.assertIn('EOD_OBSERVATION_ONLY', self.item(result)['execution_reasons'])
        facts = prepare_etf_facts(self.mapping, self.market, AT, [SID])
        self.assertEqual(facts[SID]['products'][0]['execution_eligibility'], 'BLOCKED')

    def test_negative_direction_keeps_association_and_creates_no_new_entry(self):
        self.event = report('weak-direction', '2026-11-13', '14:05', [(SID, '减弱')], published='14:06')
        result = self.result({SID:[ETF]})
        self.assertEqual(result['candidates'], [])
        self.assertEqual(self.item(result)['recommendation_status'], 'OBSERVE')
        self.assertTrue(self.item(result)['ai_selected'])

    def test_maintain_is_observation_for_new_entries_and_holds_existing_cycle(self):
        from modules.strategy_hermes.adapter import normalize
        from modules.strategy_hermes.signal_cycle import apply_report
        self.event = report('maintain', '2026-11-13', '14:05', [(SID, '维持')], published='14:06')
        result = self.result({SID:[ETF]})
        self.assertEqual(result['candidates'], [])
        self.assertEqual(self.item(result)['recommendation_status'], 'OBSERVE')
        cycle = dict(status='OPEN', primary_sector=SID, orders=[],
                     book=dict(lots=[dict(remaining=100, sellable_from='2026-11-13')]), reduced=False)
        previous = deepcopy(cycle)
        apply_report(cycle, normalize(self.event))
        self.assertEqual(cycle['last_decision'], 'HOLD')
        for key in previous:
            self.assertEqual(cycle[key], previous[key])

    def test_same_index_choices_use_first_ai_choice_with_explicit_duplicate_trace(self):
        self.mapping['products'][OTHER] = deepcopy(self.mapping['products'][ETF])
        self.market.body['quotes'][OTHER] = deepcopy(self.market.body['quotes'][ETF])
        result = self.result({SID:[OTHER, ETF]})
        self.assertEqual([row['etf'] for row in result['candidates']], [OTHER])
        self.assertEqual(result['sectors'][SID]['selection_duplicates'], [ETF])

    def test_same_index_does_not_let_pending_choice_hide_ready_ai_alternative(self):
        self.mapping['products'][OTHER] = deepcopy(self.mapping['products'][ETF])
        self.mapping['products'][OTHER]['rule']['verified'] = False
        result = self.result({SID:[OTHER, ETF]})
        self.assertEqual([row['etf'] for row in result['candidates']], [ETF])
        self.assertEqual(result['sectors'][SID]['selection_duplicates'], [OTHER])

    def test_same_index_cross_sector_has_one_executable_etf_without_automatic_substitution(self):
        second = 'kaipanla:801001'
        self.mapping['sectors'][second] = deepcopy(self.mapping['sectors'][SID])
        self.mapping['products'][OTHER] = deepcopy(self.mapping['products'][ETF])
        self.market.body['quotes'][OTHER] = deepcopy(self.market.body['quotes'][ETF])
        self.event = report('two-sectors', '2026-11-13', '14:05', [(SID,'新增'), (second,'新增')], published='14:06')
        result = self.result({SID:[ETF], second:[OTHER]})
        self.assertEqual(result['candidates'], [dict(etf=ETF, sector_id=SID, rank=1, mapping_ref='fixture')])
        loser = next(row for row in result['sectors'][second]['products'] if row['etf'] == OTHER)
        self.assertEqual(loser['recommendation_status'], 'OBSERVE')
        self.assertEqual(loser['same_index_alternative'], ETF)
        self.assertTrue(loser['ai_selected'])
        self.assertEqual(loser['rejection_reasons'], [])
        self.assertEqual(result['sectors'][second]['representatives'], [OTHER])

    def test_same_etf_selected_by_two_sectors_preserves_both_sources(self):
        second = 'kaipanla:801001'
        self.mapping['sectors'][second] = deepcopy(self.mapping['sectors'][SID])
        self.event = report('two-sources', '2026-11-13', '14:05', [(SID,'新增'), (second,'新增')], published='14:06')
        result = self.result({SID:[ETF], second:[ETF]})
        self.assertEqual({row['sector_id'] for row in result['candidates']}, {SID,second})
        self.assertEqual({row['etf'] for row in result['candidates']}, {ETF})
        self.assertEqual(set(self.item(result)['selected_source_sectors']), {SID,second})

    def test_known_partial_weights_support_association_without_claiming_complete_exposure(self):
        self.mapping['weights']['index']['complete'] = False
        result = self.result({SID:[ETF]})
        item = self.item(result)
        self.assertEqual(item['exposure_basis'], 'KNOWN_LOWER_BOUND')
        self.assertEqual(item['checks']['weights']['result'], 'UNKNOWN')
        self.assertEqual(item['recommendation_status'], 'CONFIRMED')

    def test_catalog_gaps_do_not_become_relations_or_execution_blocks(self):
        self.mapping, self.market = catalog_fixture()
        result = self.result({SID:[ETF]})
        sector = result['sectors'][SID]
        self.assertEqual(sector['coverage_gaps']['weights']['products'], 1351)
        self.assertEqual(len(sector['products']), 118)
        self.assertFalse(any(p['recommendation_status'] == 'REJECTED' for p in sector['products']))
        self.assertEqual([row['etf'] for row in result['candidates']], [ETF])

    def test_ai_fact_list_is_bounded_by_index_and_keeps_held_alternatives(self):
        self.mapping['products'][OTHER] = deepcopy(self.mapping['products'][ETF])
        for i in range(10):
            code, index = f'5129{i:02}.SH', f'index-{i}'
            self.mapping['products'][code] = deepcopy(self.mapping['products'][ETF])
            self.mapping['products'][code].update(index_code=index, rule=dict(verified=False))
            self.mapping['weights'][index] = deepcopy(self.mapping['weights']['index'])
        facts = prepare_etf_facts(self.mapping, self.market, AT, [SID], held_codes=[OTHER], limit=8)
        rows = facts[SID]['products']
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0]['etf'], OTHER)
        self.assertIn(ETF, [p['etf'] for p in rows])
        self.assertTrue(any(p['execution_eligibility'] == 'PENDING' for p in rows))

    def test_ready_fact_alternative_precedes_unknown_and_coexists_with_pending_holding(self):
        self.mapping['products'][OTHER] = deepcopy(self.mapping['products'][ETF])
        self.market.body['quotes'][OTHER] = deepcopy(self.market.body['quotes'][ETF])
        self.mapping['products'][ETF]['rule']['verified'] = False
        rows = prepare_etf_facts(self.mapping, self.market, AT, [SID])[SID]['products']
        self.assertEqual([row['etf'] for row in rows], [OTHER])
        rows = prepare_etf_facts(self.mapping, self.market, AT, [SID], held_codes=[ETF])[SID]['products']
        self.assertEqual([row['etf'] for row in rows], [ETF, OTHER])

    def test_older_mapping_notice_survives_policy_projection(self):
        self.mapping['weights']['index']['as_of_date'] = '2026-08-01'
        item = self.item(self.result({SID:[ETF]}))
        self.assertIn('OLDER_MAPPING', item['quality'])
        self.assertEqual(item['recommendation_status'], 'CONFIRMED')

    def test_future_weight_availability_never_supplies_current_association(self):
        self.mapping['weights']['index']['available_at'] = (AT + timedelta(days=1)).isoformat()
        facts = prepare_etf_facts(self.mapping, self.market, AT, [SID])
        self.assertEqual(facts[SID]['products'], [])
        self.assertEqual(self.result({SID:[ETF]})['candidates'], [])

    def test_explicit_index_relationship_supports_observation_without_inventing_weights(self):
        self.mapping['weights'] = {}
        self.mapping['sector_index_relations'] = [dict(sector_id=SID, index_code='index', verified=True,
            evidence_refs=['verified-sector-index-link'], available_at='2026-11-12T16:00:00+08:00')]
        facts = prepare_etf_facts(self.mapping, self.market, AT, [SID])
        item = facts[SID]['products'][0]
        self.assertEqual(item['association_basis'], 'EXPLICIT_INDEX_RELATION')
        self.assertIsNone(item['metrics']['E'])
        self.assertEqual(item['matched'], [])
        result = self.result({SID:[ETF]})
        self.assertEqual([r['etf'] for r in result['candidates']], [ETF])
        self.assertEqual(self.item(result)['exposure_basis'], 'UNAVAILABLE')
        self.assertIn('WEIGHTS_UNVERIFIED', self.item(result)['quality'])
        self.mapping['sector_index_relations'][0]['available_at'] = (AT+timedelta(days=1)).isoformat()
        self.assertEqual(prepare_etf_facts(self.mapping, self.market, AT, [SID])[SID]['products'], [])

    def test_research_identity_rejects_invalid_future_and_unlisted_catalog_rows(self):
        for change in ({'index_code':''}, {'catalog_available_at':(AT+timedelta(days=1)).isoformat()},
                       {'list_status':'P'}, {'list_date':'20261114'}):
            with self.subTest(change=change):
                self.mapping, self.market = fixture()
                self.mapping['products'][ETF].update(change)
                self.assertEqual(research_products(self.mapping, AT), {})

    def test_legacy_explanation_uses_actual_stock_quotes_without_changing_its_policy(self):
        _, legacy = evaluate(self.event, self.mapping, self.market, use_report_core=False)
        self.assertEqual(legacy['sectors'][SID]['products'][0]['metrics']['Q'], '1.0')
        self.assertEqual(self.item(self.result({SID:[ETF]}))['metrics']['Q'], '1.0')
