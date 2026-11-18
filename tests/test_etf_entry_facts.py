"""Entry rule consistency and bounded advisory statistics at the research cutoff."""
from copy import deepcopy
from datetime import timedelta
import unittest

from modules.strategy_hermes.stage2.penetration import (
    instrument_units, prepare_etf_facts, product_entry_reason, verified_product,
)
from test_etf_recommendations import AT, ETF, SID, fixture


class EntryFacts(unittest.TestCase):
    def setUp(self):
        self.mapping, self.market = fixture()
        self.product = self.mapping['products'][ETF]

    def facts(self):
        return prepare_etf_facts(self.mapping, self.market, AT, [SID])[SID]['products'][0]

    def test_review_required_keeps_research_and_rule_record_but_prevents_new_entry(self):
        self.product['rule']['review_required'] = True
        original = deepcopy(self.mapping)
        self.assertEqual(product_entry_reason(self.product, AT), 'RULE_REVIEW_REQUIRED')
        self.assertFalse(verified_product(self.product, AT))
        self.assertEqual(instrument_units(self.mapping, AT), {})
        row = self.facts()
        self.assertEqual(row['execution_eligibility'], 'PENDING')
        self.assertEqual(row['execution_reasons'], ['RULE_REVIEW_REQUIRED'])
        self.assertEqual(row['execution_checks']['product']['reason'], 'RULE_REVIEW_REQUIRED')
        self.assertTrue(row['rule']['review_required'])
        self.assertEqual(self.mapping, original)

    def test_settlement_aliases_legacy_default_and_actual_twenty_percent_rule(self):
        original = deepcopy(self.product['rule'])
        for fields in ({}, {'settlement':'T+1'}, {'settlement_cycle':'T+1'},
                       {'settlement':'T+1', 'settlement_cycle':'T+1'}):
            with self.subTest(fields=fields):
                self.product['rule'] = dict(original, price_limit_ratio='.2', **fields)
                self.assertIsNone(product_entry_reason(self.product, AT))
                self.assertEqual(self.facts()['execution_eligibility'], 'READY')
        for fields, reason in (({'settlement':'T+0'}, 'PRODUCT_OUTSIDE_SCOPE'),
                               ({'settlement':'T+0', 'settlement_cycle':'T+1'}, 'SETTLEMENT_RULE_CONFLICT')):
            with self.subTest(fields=fields):
                self.product['rule'] = dict(original, **fields)
                self.assertEqual(product_entry_reason(self.product, AT), reason)
                self.assertFalse(verified_product(self.product, AT))
                self.assertEqual(self.facts()['execution_eligibility'], 'BLOCKED')
                self.assertIn(reason, self.facts()['execution_reasons'])

    def test_future_rule_and_catalog_or_invalid_unit_never_grant_entry(self):
        future = (AT + timedelta(days=1)).isoformat()
        for field in ('verified_at', 'available_at', 'effective_at'):
            product = deepcopy(self.product)
            product['rule'][field] = future
            self.assertEqual(product_entry_reason(product, AT), 'PRODUCT_RULE_UNVERIFIED')
        product = dict(self.product, catalog_available_at=future)
        self.assertEqual(product_entry_reason(product, AT), 'PRODUCT_IDENTITY_UNVERIFIED')
        for unit in (0, -1, 1.5, '100', True, None):
            self.assertEqual(product_entry_reason(dict(self.product, trade_unit=unit), AT), 'TRADE_UNIT_UNVERIFIED')

    def test_small_aum_and_adv_are_dated_reference_values_without_entry_thresholds(self):
        self.product['stats'] = dict(aum_yuan='1000000', aum_verified=True,
            aum_as_of='2026-11-12', aum_available_at='2026-11-13T09:00:00+08:00',
            aum_valid_until='2026-11-13', aum_source='recorded-aum-source')
        self.product['recommendation_stats'].update(adv20='3000', adv5='5000', history_source='recorded-daily-source',
                                                   history_batches=['daily-source-batch'])
        row = self.facts()
        self.assertEqual(row['execution_eligibility'], 'READY')
        stats = row['reference_stats']
        self.assertEqual(stats['effect'], 'REFERENCE')
        self.assertEqual(stats['aum_yuan']['value'], '1000000')
        self.assertEqual(stats['aum_yuan']['unit'], 'CNY')
        self.assertEqual(stats['aum_yuan']['source'], 'recorded-aum-source')
        self.assertFalse(stats['aum_yuan']['expired'])
        self.assertEqual(stats['adv20']['value'], '3000')
        self.assertEqual(stats['adv20']['unit'], 'CNY/trading_day')
        self.assertEqual(stats['adv20']['covered_days'], 20)
        self.assertEqual(stats['adv20']['source_batches'], ['daily-source-batch'])
        self.assertEqual(stats['adv20']['age_days'], 1)
        self.assertIsNone(stats['adv20']['expired'])
        self.assertFalse(any('result' in item for item in stats.values() if isinstance(item, dict)))

    def test_expired_aum_is_labelled_and_partial_adv_is_missing_without_rejecting_product(self):
        self.product['stats'] = dict(aum_yuan='1000000', aum_verified=True,
            aum_as_of='2026-11-10', aum_available_at='2026-11-11T09:00:00+08:00', aum_valid_until='2026-11-12')
        self.product['recommendation_stats'].update(history_days=5, adv20='99999999', adv5='5000')
        row = self.facts()
        stats = row['reference_stats']
        self.assertEqual(stats['aum_yuan']['status'], 'EXPIRED')
        self.assertEqual(stats['aum_yuan']['value'], '1000000')
        self.assertTrue(stats['aum_yuan']['expired'])
        self.assertEqual(stats['adv20']['status'], 'INSUFFICIENT_HISTORY')
        self.assertIsNone(stats['adv20']['value'])
        self.assertTrue(stats['adv20']['missing'])
        self.assertEqual(stats['adv5']['value'], '5000')
        self.assertEqual(row['execution_eligibility'], 'READY')

    def test_future_statistics_never_leak_values_and_keep_available_older_group(self):
        self.product['stats'] = dict(self.product['recommendation_stats'], adv20='6000', history_source='older-source')
        self.product['recommendation_stats'].update(adv20='99999999', history_available_at=(AT+timedelta(hours=1)).isoformat())
        self.assertEqual(self.facts()['reference_stats']['adv20']['value'], '6000')
        self.assertEqual(self.facts()['reference_stats']['adv20']['source'], 'older-source')
        self.product['stats'] = {}
        stats = self.facts()['reference_stats']
        self.assertIsNone(stats['adv20']['value'])
        self.assertEqual(stats['adv20']['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(stats['aum_yuan']['status'], 'MISSING')

    def test_no_timestamp_is_not_a_current_statistic_and_projection_is_bounded(self):
        self.product['stats'] = dict(aum_verified=True, aum_yuan='1234', aum_as_of='2026-11-12')
        self.product['recommendation_stats']['history_batches'] = [f'batch-{i}' for i in range(20)]
        self.product['recommendation_stats']['daily_rows'] = [{'large':'unprojected'}] * 100
        stats = self.facts()['reference_stats']
        self.assertEqual(stats['aum_yuan']['status'], 'TIMING_UNVERIFIED')
        self.assertIsNone(stats['aum_yuan']['value'])
        self.assertEqual(len(stats['adv20']['source_batches']), 4)
        self.assertEqual(stats['adv20']['source_batch_count'], 20)
        self.assertNotIn('daily_rows', stats)

    def test_unverified_rule_does_not_invent_settlement_and_invalid_validity_is_unknown(self):
        self.product['rule'] = dict(verified=False)
        self.product['stats'] = dict(aum_verified=True, aum_yuan='1234', aum_as_of='2026-11-12',
                                    aum_available_at='2026-11-13T09:00:00+08:00', aum_valid_until='2026-11-11')
        row = self.facts()
        self.assertIsNone(row['rule']['settlement_cycle'])
        self.assertEqual(row['reference_stats']['aum_yuan']['status'], 'VALIDITY_UNVERIFIED')
        self.assertIsNone(row['reference_stats']['aum_yuan']['value'])


if __name__ == '__main__':
    unittest.main()
