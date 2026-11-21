from copy import deepcopy
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import mapping_maintenance as maintenance
from modules.strategy_hermes.stage2.reference_policy import ReferencePolicy, MAX_PENDING
from modules.strategy_hermes.stage2.tushare_bootstrap import SourceFailure
from test_mapping_maintenance import mapping, ETF, INDEX, SID, FakeCursor

NOW = datetime(2026, 11, 17, 10, tzinfo=CHINA)
DAYS = ['2026-11-13', '2026-11-16', '2026-11-17', '2026-11-18', '2026-11-19']


class Cadence(TestCase):
    def test_weekly_success_is_reused_but_daily_new_source_date_is_due(self):
        data = {}
        policy = ReferencePolicy(data, NOW, 'market')
        policy.succeeded('catalog:L', '2026-11-16')
        policy.succeeded('aum:2026-11-16', '2026-11-16')
        self.assertFalse(ReferencePolicy(data, NOW+timedelta(days=6), 'market').due('catalog:L'))
        self.assertTrue(ReferencePolicy(data, NOW+timedelta(days=7), 'market').due('catalog:L'))
        self.assertFalse(policy.due('aum:2026-11-16', days=0, target='2026-11-16'))
        self.assertTrue(policy.due('aum:2026-11-17', days=0, target='2026-11-17'))

    def test_retry_only_missing_items_and_preserves_failed_date_across_days(self):
        data = {}
        first = ReferencePolicy(data, NOW, 'market')
        self.assertTrue(first.begin('aum:2026-11-16', '2026-11-16'))
        first.failed('aum:2026-11-16', dict(error_kind='NOT_PUBLISHED'))
        retry = ReferencePolicy(data, NOW.replace(hour=14, minute=15), 'retry')
        self.assertTrue(retry.due('aum:2026-11-16'))
        self.assertFalse(retry.due('catalog:L'))
        later = ReferencePolicy(data, NOW+timedelta(days=1), 'market')
        self.assertEqual(later.target('aum:2026-11-16', '2026-11-17'), '2026-11-16')

    def test_four_attempts_remain_visible_for_review_and_same_slot_cannot_repeat(self):
        data = {}
        for offset, mode in [(0,'market'),(0,'retry'),(1,'market'),(1,'retry')]:
            policy = ReferencePolicy(data, NOW+timedelta(days=offset), mode)
            self.assertTrue(policy.due('members:x'))
            self.assertTrue(policy.begin('members:x','2026-11-16'))
            self.assertFalse(policy.due('members:x'))
        final = ReferencePolicy(data, NOW+timedelta(days=2), 'market')
        self.assertFalse(final.due('members:x'))
        self.assertEqual(final.report()['review_required'], 1)
        self.assertEqual(final.pending['members:x']['target'], '2026-11-16')

    def test_pending_capacity_reports_overflow_without_dropping_existing_targets(self):
        data = {}
        policy = ReferencePolicy(data, NOW, 'market')
        for i in range(MAX_PENDING):
            self.assertTrue(policy.begin('item:'+str(i), '2026-11-16'))
        self.assertFalse(policy.begin('overflow', '2026-11-16'))
        self.assertEqual(len(policy.pending), MAX_PENDING)
        self.assertEqual(policy.report()['overflow_count'], 1)


class Connection:
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def execute(self, sql, args=()):
        if 'SELECT batch_id FROM supplier_batches' in sql:
            return FakeCursor([('identity',)])
        if 'SELECT collected_at FROM supplier_batches' in sql:
            return FakeCursor([(NOW-timedelta(hours=1),)])
        if 'FROM supplier_market_batches' in sql or 'FROM rotation r JOIN events' in sql:
            return FakeCursor([])
        raise AssertionError(sql)


class CatalogFlow(TestCase):
    def run_catalog(self, data, now=NOW, mode='market', ledger=None):
        summary = {}
        maintenance._catalog_maintenance(ledger or SimpleNamespace(_connect=Connection), data, 'config',
            now, date(2026, 11, 16), None, lambda:None, summary, ReferencePolicy(data, now, mode))
        return summary

    def test_catalog_scope_review_is_weekly_success_without_retry_or_trading_approval(self):
        data = mapping()
        rule = deepcopy(data['products'][ETF]['rule'])
        rows = [dict(ts_code='158001.SZ', index_code='980081.CNI', list_status='L'),
                dict(ts_code='158008.OF', index_code='980027.SZ', list_status='L'),
                dict(ts_code='159999.SZ', index_code=None, list_status='L'),
                dict(ts_code='159998.SZ', index_code='000300.SH', list_status='L')]
        def obtain(store, api, params, *args, **kwargs):
            state = params['list_status']
            return dict(rows=rows if state == 'L' else [], source_batch='raw-'+state), NOW
        with patch.object(maintenance, '_obtain', side_effect=obtain) as source, \
             patch.object(maintenance, 'save_reference', return_value={'batch':'catalog'}):
            summary = self.run_catalog(data)
            self.assertEqual(source.call_count, 3)
            self.assertFalse(data['reference_maintenance']['pending'])
            self.assertEqual(summary['catalog']['L']['manual_review_count'], 3)
            self.assertEqual(summary['catalog_review']['L']['reasons'],
                dict(unsupported_index=1, product_scope=1, missing_relationship=1))
            self.assertEqual(summary['catalog_review']['L']['source_batch'], 'raw-L')
            retry = self.run_catalog(data, NOW.replace(hour=14, minute=15), 'retry')
            self.run_catalog(data, NOW+timedelta(days=6))
            self.assertEqual(source.call_count, 3)
        self.assertEqual(retry['catalog_review'], summary['catalog_review'])
        self.assertTrue(ReferencePolicy(data, NOW+timedelta(days=7), 'market').due('catalog:L'))
        self.assertEqual(set(data['products']), {ETF, '159998.SZ'})
        self.assertEqual(data['products'][ETF]['rule'], rule)
        self.assertFalse(data['products']['159998.SZ']['rule']['verified'])
        self.assertIsNone(data['products']['159998.SZ']['rule']['scope'])
        _, report = maintenance._finish(data, deepcopy(data), retry)
        self.assertEqual(report['status'], 'READY')
        self.assertIn('3条产品范围或指数关联待人工整理', report['message'])

    def test_previous_field_failure_is_reclassified_from_saved_batch_even_when_exhausted(self):
        data = mapping()
        policy = ReferencePolicy(data, NOW, 'market')
        policy.pending['catalog:L'] = dict(target='2026-11-16', attempts=4,
            attempted_at=(NOW-timedelta(minutes=1)).isoformat(), error_kind='FIELD_MISMATCH')
        rows = [dict(ts_code='158001.SZ', index_code='980081.CNI', list_status='L')]
        class Cached(Connection):
            def execute(self, sql, args=()):
                if 'SELECT batch_id,body,collected_at' in sql:
                    self_outer.assertEqual(args, ('L', NOW-timedelta(minutes=1), NOW))
                    return FakeCursor([('raw-before-release', {'rows':rows}, NOW-timedelta(seconds=30))])
                return super().execute(sql, args)
        self_outer = self
        with patch.object(maintenance, '_obtain') as source, patch.object(maintenance, 'save_reference') as save:
            result = self.run_catalog(data, mode='retry', ledger=SimpleNamespace(_connect=Cached))
        source.assert_not_called()
        save.assert_not_called()
        self.assertFalse(policy.pending)
        self.assertEqual(result['catalog_review']['L']['source_batch'], 'raw-before-release')
        self.assertEqual(result['catalog_review']['L']['source_available_at'], (NOW-timedelta(seconds=30)).isoformat())
        self.assertEqual(set(data['products']), {ETF})

    def test_wrong_status_or_malformed_rows_remain_retryable_without_partial_product_changes(self):
        good = dict(ts_code='159998.SZ', index_code='000300.SH', list_status='L')
        invalid = [dict(rows=[good, dict(good, list_status='D')]), dict(rows=[good, None]),
                   dict(rows=[good, {'ts_code':'159999.SZ', 'list_status':'L'}]),
                   dict(rows=[good, dict(good, ts_code='bad')]), dict(rows='invalid')]
        for body in invalid:
            with self.subTest(body=body):
                data = mapping()
                policy = ReferencePolicy(data, NOW, 'market')
                for state in ('D', 'P'):
                    policy.succeeded('catalog:'+state, '2026-11-16')
                with patch.object(maintenance, '_obtain', return_value=(body, NOW)) as source, \
                     patch.object(maintenance, 'save_reference') as save:
                    result = self.run_catalog(data)
                    self.run_catalog(data, NOW.replace(hour=14, minute=15), 'retry')
                save.assert_not_called()
                self.assertEqual(source.call_count, 2)
                self.assertTrue(source.call_args.kwargs['refresh'])
                self.assertEqual(result['catalog']['L']['error_kind'], 'FIELD_MISMATCH')
                self.assertEqual(policy.pending['catalog:L']['attempts'], 2)
                self.assertEqual(data['products'], mapping()['products'])

    def test_failed_http_catalog_stays_pending_with_safe_diagnostic(self):
        data = mapping()
        policy = ReferencePolicy(data, NOW, 'market')
        for state in ('D', 'P'):
            policy.succeeded('catalog:'+state, '2026-11-16')
        with patch.object(maintenance, '_obtain', side_effect=SourceFailure(
                'private source message', kind='HTTP_STATUS', details={'http_status':503})):
            result = self.run_catalog(data)
        self.assertEqual(policy.pending['catalog:L']['error_kind'], 'HTTP_STATUS')
        self.assertEqual(policy.pending['catalog:L']['error_details'], {'http_status':503})
        self.assertNotIn('private source message', str(result))

    def test_review_samples_are_bounded_and_unknown_source_text_is_not_copied(self):
        rows = [dict(ts_code=f'{158001+i:06}.SZ', index_code='unknown/private?token=x',
                     list_status='L', csname='private source text') for i in range(8)]
        review = maintenance._catalog_review(mapping(), {'rows':rows}, 'L')
        self.assertEqual(review['count'], 8)
        self.assertEqual(len(review['samples']), 5)
        self.assertTrue(all(row['index_code'] is None for row in review['samples']))
        self.assertNotIn('private', str(review))

    def test_catalog_obtain_retains_raw_batch_reference_for_review_traceability(self):
        class Raw(Connection):
            def execute(self, sql, args=()):
                if 'SELECT body,collected_at' in sql:
                    return FakeCursor([({'rows':[dict(ts_code='158001.SZ', index_code='980081.CNI', list_status='L')]}, NOW)])
                return super().execute(sql, args)
        with patch.object(maintenance, '_cached', return_value=('source-batch',)):
            body, available = maintenance._obtain(SimpleNamespace(_connect=Raw), 'etf_basic',
                {'list_status':'L'}, 'config', NOW, None, lambda:None)
        self.assertEqual(body['source_batch'], 'source-batch')
        self.assertEqual(available, NOW)


class ReferenceFlow(TestCase):
    def test_exhausted_member_pending_is_cleared_when_valid_cache_arrives(self):
        data = mapping()
        policy = ReferencePolicy(data,NOW,'market')
        policy.pending['members:'+SID] = dict(target='2026-11-16',attempts=4)
        newer = dict(data['sectors'][SID],as_of_date='2026-11-16',
            available_at='2026-11-17T06:00:00+08:00')
        with patch.object(maintenance,'cached_members',return_value=newer), \
             patch.object(maintenance,'_page') as page:
            maintenance._hydrate_members(SimpleNamespace(_connect=Connection),data,{},
                {'600000':['600000.SH']},'identity',date(2026,11,16),NOW,lambda:None,{},policy=policy)
        page.assert_not_called()
        self.assertFalse(policy.pending)
        self.assertFalse(policy.due('members:'+SID))

    def test_cache_parse_error_is_reported_without_secondary_failure(self):
        summary = {}
        with patch.object(maintenance, 'cached_members', side_effect=ValueError('bad cache')):
            maintenance._hydrate_members(SimpleNamespace(_connect=Connection), mapping(), {},
                {'600000':['600000.SH']}, 'identity', date(2026,11,16), NOW, lambda:None, summary)
        self.assertEqual(summary['cache_members']['error'], 'SOURCE_FAILURE')
        original = mapping()
        _, result = maintenance._finish(original, deepcopy(original), summary)
        self.assertEqual(result['status'], 'PARTIAL')

    def test_older_pending_members_are_satisfied_by_newer_verified_mapping(self):
        data = mapping()
        data['sectors'][SID].update(as_of_date='2026-11-16',
            available_at='2026-11-17T06:00:00+08:00')
        before = deepcopy(data['sectors'])
        ReferencePolicy(data, NOW-timedelta(days=1), 'market').begin('members:'+SID, '2026-11-13')
        policy = ReferencePolicy(data, NOW, 'market')
        summary = {}
        with patch.object(maintenance, 'cached_members') as cache, patch.object(maintenance, '_page') as page:
            maintenance._members(SimpleNamespace(_connect=Connection, ns='test'), data, {},
                {'600000':['600000.SH']}, 'identity', [], date(2026,11,16), None, NOW,
                lambda *a:None, lambda:None, summary, policy=policy)
        cache.assert_not_called()
        page.assert_not_called()
        self.assertEqual(data['sectors'], before)
        self.assertFalse(policy.pending)
        self.assertEqual(summary['members'][SID]['status'], 'CURRENT')

    def test_valid_cached_size_clears_pending_without_http_or_repeated_check_stamp(self):
        data = mapping()
        ReferencePolicy(data, NOW-timedelta(days=1), 'market').begin('aum:2026-11-16', '2026-11-16')
        class Cached(Connection):
            def execute(self, sql, args=()):
                if 'SELECT scope,body,collected_at' in sql:
                    return FakeCursor([({'params':{'trade_date':'20261116'}},
                        {'rows':[dict(ts_code=ETF,trade_date='20261116',total_size='10',total_share='1')]},
                        NOW-timedelta(minutes=1))])
                return super().execute(sql, args)
        ledger = SimpleNamespace(_connect=Cached)
        with patch.object(maintenance, '_obtain') as fetch:
            maintenance._aum_maintenance(ledger,data,DAYS,'config',NOW,date(2026,11,16),None,
                lambda:None,{},ReferencePolicy(data,NOW,'market'))
            stamp = deepcopy(data['reference_maintenance'])
            later = NOW.replace(hour=14,minute=15)
            maintenance._aum_maintenance(ledger,data,DAYS,'config',later,date(2026,11,16),None,
                lambda:None,{},ReferencePolicy(data,later,'retry'))
        fetch.assert_not_called()
        self.assertFalse(data['reference_maintenance']['pending'])
        self.assertEqual(data['reference_maintenance'], stamp)

    def test_saved_members_are_absorbed_without_http_even_when_weekly_check_is_not_due(self):
        data = mapping()
        data['sectors'][SID]['as_of_date'] = '2026-11-12'
        data['sectors'][SID]['available_at'] = '2026-11-12T18:00:00+08:00'
        newer = dict(data['sectors'][SID], as_of_date='2026-11-13',
                     available_at='2026-11-17T06:01:00+08:00', members=['600000.SH','600001.SH'],
                     reported_count=2)
        policy = ReferencePolicy(data,NOW,'market')
        policy.succeeded('members:'+SID,'2026-11-12')
        ledger = SimpleNamespace(_connect=Connection)
        summary = {}
        with patch.object(maintenance,'cached_members',return_value=newer), \
             patch.object(maintenance,'_page') as page:
            maintenance._hydrate_members(ledger,data,{}, {'600000':['600000.SH']},'identity',
                date(2026,11,16),NOW,lambda:None,summary)
        page.assert_not_called()
        self.assertEqual(data['sectors'][SID]['reported_count'],2)
        self.assertEqual(data['sectors'][SID]['available_at'],'2026-11-17T06:01:00+08:00')
        self.assertFalse(policy.due('members:'+SID))
        self.assertEqual(summary['cache_members']['updated'],1)

    def test_late_size_backfill_never_overwrites_newer_size(self):
        data = mapping()
        data['products'][ETF]['stats'] = dict(aum_as_of='2026-11-16', aum_yuan='123', aum_verified=True)
        before = deepcopy(data)
        maintenance.aum_updates(data,[dict(ts_code=ETF,trade_date='20261113',total_size='999',total_share='8')],
            date(2026,11,13),NOW,date(2026,11,16))
        self.assertEqual(data,before)
        self.assertEqual(maintenance._aum_valid_until(DAYS,date(2026,11,13)),date(2026,11,16))

    def test_earlier_size_batch_cannot_replace_later_batch_for_same_source_date(self):
        data = mapping()
        data['products'][ETF]['stats'] = dict(aum_as_of='2026-11-16',aum_yuan='123',aum_verified=True,
                                             aum_available_at=NOW.isoformat())
        before = deepcopy(data)
        maintenance.aum_updates(data,[dict(ts_code=ETF,trade_date='20261116',total_size='999',total_share='8')],
            date(2026,11,16),NOW-timedelta(hours=1),date(2026,11,17))
        self.assertEqual(data,before)

    def test_missing_size_date_is_retried_after_new_day_and_success_clears_only_that_item(self):
        data = mapping()
        ledger = SimpleNamespace(_connect=Connection)
        policy = ReferencePolicy(data,NOW,'market')
        with patch.object(maintenance,'_obtain',side_effect=SourceFailure('not yet',kind='NOT_PUBLISHED')):
            summary = {}
            maintenance._aum_maintenance(ledger,data,DAYS,'config',NOW,date(2026,11,16),None,
                lambda:None,summary,policy)
        self.assertEqual(summary['aum']['2026-11-16']['error_kind'],'NOT_PUBLISHED')
        later = NOW+timedelta(days=1)
        calls = []
        def obtain(store, api, params, *args, **kwargs):
            calls.append(params['trade_date'])
            return {'rows':[dict(ts_code=ETF,trade_date=params['trade_date'],total_size='100',total_share='10')]},later
        with patch.object(maintenance,'_obtain',side_effect=obtain):
            maintenance._aum_maintenance(ledger,data,DAYS,'config',later,date(2026,11,17),None,
                lambda:None,{},ReferencePolicy(data,later,'market'))
        self.assertEqual(calls,['20261117','20261116'])
        self.assertEqual(data['products'][ETF]['stats']['aum_as_of'],'2026-11-17')
        self.assertFalse(data['reference_maintenance']['pending'])

    def test_partial_cached_size_is_fetched_again_in_next_slot(self):
        data = mapping()
        ledger = SimpleNamespace(_connect=Connection)
        def obtain(*args,**kwargs):
            return {'rows':[dict(ts_code=ETF,trade_date='20261116',total_size='10',total_share='1'),
                dict(ts_code=ETF,trade_date='20261116',total_size=None,total_share='1')]},NOW
        with patch.object(maintenance,'_obtain',side_effect=obtain) as source:
            maintenance._aum_maintenance(ledger,data,DAYS,'config',NOW,date(2026,11,16),None,
                lambda:None,{},ReferencePolicy(data,NOW,'market'))
            later = NOW.replace(hour=14,minute=15)
            maintenance._aum_maintenance(ledger,data,DAYS,'config',later,date(2026,11,16),None,
                lambda:None,{},ReferencePolicy(data,later,'retry'))
        self.assertFalse(source.call_args_list[0].kwargs['refresh'])
        self.assertTrue(source.call_args_list[1].kwargs['refresh'])

    def test_legacy_size_is_marked_historical_only_in_new_mapping(self):
        original = mapping()
        original['products'][ETF]['stats'] = dict(aum_as_of='2026-03-31',aum_yuan='123',aum_verified=True)
        ledger = SimpleNamespace(_connect=Connection)
        with patch.object(maintenance,'_priorities',return_value=([],[])), \
             patch.object(maintenance,'stock_identity',return_value=({},{})), \
             patch.object(maintenance,'_members'), patch.object(maintenance,'_hydrate_members'), \
             patch.object(maintenance,'_aum_maintenance'), \
             patch.object(maintenance,'_obtain',side_effect=SourceFailure('test')):
            updated, _ = maintenance.maintain(ledger,original,{},DAYS,'config',None,NOW,
                lambda *a:None,lambda:None,mode='market')
        self.assertTrue(original['products'][ETF]['stats']['aum_verified'])
        self.assertFalse(updated['products'][ETF]['stats']['aum_verified'])
        self.assertEqual(updated['products'][ETF]['stats']['aum_status'],'HISTORICAL_REFERENCE')
