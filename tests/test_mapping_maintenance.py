from copy import deepcopy
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import mapping_maintenance as maintenance
from modules.strategy_hermes.stage2.tushare_bootstrap import SourceFailure

NOW = datetime(2026, 11, 16, 18, tzinfo=CHINA)
ETF = '512800.SH'
INDEX = '399986.SZ'
SID = 'kaipanla:801027'


def mapping():
    return dict(version='frozen-original', products={ETF:dict(name='银行ETF',index_code=INDEX,trade_unit=100,
                rule=dict(verified=True,index_code=INDEX,scope='domestic_passive_sector',price_limit_ratio='.1',
                          verified_at='2026-11-13T12:00:00+08:00'))},
                sectors={SID:dict(sector_name='银行',members=['600000.SH'],reported_count=1,
                                  unresolved_count=0,as_of_date='2026-11-13',source_batches=['old'])},
                weights={INDEX:dict(as_of_date='2026-10-30',complete=True,completeness_basis='CSI_SET_MATCH',
                                    rows=[dict(code='600000.SH',weight='1',mainboard=True)])})


class CatalogAndWeights(TestCase):
    def test_catalog_keeps_verified_rule_but_changes_invalidate_only_the_product(self):
        data = mapping()
        original = deepcopy(data['products'][ETF]['rule'])
        row = dict(ts_code=ETF,index_code=INDEX,csname='银行ETF',list_status='L',etf_type='股票型',list_date='20170101')
        maintenance.catalog_updates(data,[row],NOW)
        self.assertEqual(data['products'][ETF]['rule'], original)
        before = deepcopy(data)
        self.assertEqual(maintenance.catalog_updates(data,[row],NOW+timedelta(days=1)), (0,0))
        self.assertEqual(data,before)
        maintenance.catalog_updates(data,[dict(row,index_code='000300.SH')],NOW)
        self.assertFalse(data['products'][ETF]['rule']['verified'])
        self.assertEqual(data['weights'],before['weights'])
        maintenance.catalog_updates(data,[dict(row,ts_code='159999.SZ')],NOW)
        self.assertFalse(data['products']['159999.SZ']['rule']['verified'])
        self.assertIsNone(data['products']['159999.SZ']['rule']['scope'])
        self.assertNotIn('trade_unit',data['products']['159999.SZ'])

    def test_delisting_without_index_retires_known_product_and_bad_rows_are_local(self):
        data=mapping()
        count,rejected=maintenance.catalog_updates(data,[dict(ts_code=ETF,index_code=None,list_status='D'),
                                                        dict(ts_code='bad',list_status='L')],NOW)
        self.assertEqual((count,rejected),(1,1))
        self.assertEqual(data['products'][ETF]['index_code'],INDEX)
        self.assertFalse(data['products'][ETF]['rule']['verified'])

    def test_weights_keep_original_partial_denominator_and_unknown_identity(self):
        rows=[dict(index_code=INDEX,con_code='600000.SH',trade_date='20261030',weight='40'),
              dict(index_code=INDEX,con_code='000001.SZ',trade_date='20261030',weight='30')]
        result=maintenance.weight_basis(rows,INDEX,date(2026,10,1),NOW.date(),{'600000.SH':'主板'})
        self.assertFalse(result['complete'])
        self.assertEqual(sum(float(r['weight']) for r in result['rows']),.7)
        self.assertIsNone(next(r for r in result['rows'] if r['code']=='000001.SZ')['mainboard'])

    def test_unchanged_weight_observation_keeps_existing_completeness_proof(self):
        old=mapping()['weights'][INDEX]
        rows=[dict(index_code=INDEX,con_code='600000.SH',trade_date='20261030',weight='100')]
        result=maintenance.weight_basis(rows,INDEX,date(2026,10,1),NOW.date(),{},old)
        self.assertTrue(result['complete'])
        self.assertEqual(result['completeness_basis'],'CSI_SET_MATCH')
        self.assertTrue(result['rows'][0]['mainboard'])
        result=maintenance.weight_basis([dict(rows[0],trade_date='20261113')],INDEX,date(2026,10,1),NOW.date(),{},old)
        self.assertFalse(result['complete'])

    def test_aum_converts_source_unit_once_and_preserves_source_date(self):
        data=mapping()
        rows=[dict(ts_code=ETF,trade_date='20261113',total_size='12345.67',total_share='123')]
        count,invalid=maintenance.aum_updates(data,rows,date(2026,11,13),NOW,date(2026,11,16))
        self.assertEqual((count,invalid),(1,0))
        stats=data['products'][ETF]['stats']
        self.assertEqual(stats['aum_yuan'],'123456700.00')
        self.assertEqual(stats['aum_as_of'],'2026-11-13')
        self.assertEqual(stats['aum_available_at'],NOW.isoformat())
        old=deepcopy(stats)
        count,invalid=maintenance.aum_updates(data,[dict(rows[0],trade_date='20261117')],date(2026,11,13),NOW,NOW.date())
        self.assertEqual((count,invalid),(0,1))
        self.assertEqual(data['products'][ETF]['stats'],old)

    def test_existing_members_are_in_maintenance_scope(self):
        data=mapping()
        targets,_=maintenance.sector_targets(data,None,[],NOW)
        self.assertIn(SID,targets)
        self.assertLessEqual(len(targets),maintenance.MAX_SECTORS)


class FakeCursor:
    def __init__(self,rows): self.rows=rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


class FakeConnection:
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self,sql,args=None):
        if 'SELECT batch_id FROM supplier_batches' in sql:
            return FakeCursor([('identity',)])
        raise AssertionError(sql)


class PartialMaintenance(TestCase):
    def test_empty_delisted_catalog_is_durable_receipt_without_zero_row_batch(self):
        client=SimpleNamespace(config={},fetch=lambda *args,**kwargs: [])
        def request(source,scope,operation):
            return dict(status='SAVED',receipt=operation())
        with patch.object(maintenance,'_cached',return_value=None),\
             patch.object(maintenance,'Tushare',return_value=client),\
             patch.object(maintenance,'save_market') as save:
            body,available=maintenance._obtain(None,'etf_basic',{'list_status':'D'},'private',NOW,request,lambda:None)
        self.assertEqual(body,{'rows':[]})
        self.assertIsInstance(available,datetime)
        save.assert_not_called()

    def test_source_failure_retains_other_data_and_does_not_need_full_accuracy(self):
        original=mapping()
        ledger=SimpleNamespace(ns='test',_connect=FakeConnection)
        def obtain(store,api,params,*args,**kwargs):
            if api=='etf_basic':
                if params['list_status']=='L':
                    return {'rows':[dict(ts_code='159999.SZ',index_code='000300.SH',csname='新产品',list_status='L',etf_type=None)]},NOW
                return {'rows':[]},NOW
            raise SourceFailure('partial test source')
        with patch.object(maintenance,'_priorities',return_value=([ETF],[SID])),\
             patch.object(maintenance,'stock_identity',return_value=({},{})),\
             patch.object(maintenance,'_obtain',side_effect=obtain),\
             patch.object(maintenance,'save_reference',return_value={'batch':'new-catalog'}):
            result,status=maintenance.maintain(ledger,original,None,['2026-11-13','2026-11-16'],
                                                'private',None,NOW,lambda *a:None,lambda:None)
        self.assertEqual(status['status'],'PARTIAL')
        self.assertIn('159999.SZ',result['products'])
        self.assertEqual(result['weights'],original['weights'])
        self.assertEqual(result['sectors'],original['sectors'])
        self.assertTrue(result['products'][ETF]['rule']['verified'])
        self.assertNotEqual(result['version'],original['version'])
        self.assertEqual(original,mapping())

    def test_all_failed_updates_leave_original_mapping_version_usable(self):
        original=mapping()
        ledger=SimpleNamespace(ns='test',_connect=FakeConnection)
        with patch.object(maintenance,'_priorities',return_value=([ETF],[SID])),\
             patch.object(maintenance,'stock_identity',return_value=({},{})),\
             patch.object(maintenance,'_obtain',side_effect=SourceFailure('unavailable')):
            result,status=maintenance.maintain(ledger,original,None,['2026-11-13'],
                                                'private',None,NOW,lambda *a:None,lambda:None)
        for key in ('sectors', 'products', 'weights'):
            self.assertEqual(result[key], original[key])
        self.assertTrue(status['changed'])
        self.assertEqual(result['member_maintenance']['priority_after'], SID)
        self.assertEqual(status['status'],'PARTIAL')

    def test_preopen_only_requests_size_and_preserves_direction_related_mapping(self):
        original=mapping()
        ledger=SimpleNamespace(ns='test',_connect=FakeConnection)
        with patch.object(maintenance,'_priorities',return_value=([ETF],[SID])),\
             patch.object(maintenance,'stock_identity',return_value=({},{})),\
             patch.object(maintenance,'_obtain',return_value=({'rows':[dict(ts_code=ETF,trade_date='20261113',total_size='100000',total_share='500')]},NOW)) as obtain:
            result,status=maintenance.maintain(ledger,original,None,['2026-11-13','2026-11-16','2026-11-17'],
                    'private',None,NOW.replace(hour=8,minute=45),lambda *a:None,lambda:None,mode='preopen')
        self.assertEqual(obtain.call_count,1)
        self.assertEqual(obtain.call_args.args[1],'etf_share_size')
        self.assertEqual(result['weights'],original['weights'])
        self.assertEqual(result['sectors'],original['sectors'])
        self.assertEqual(result['products'][ETF]['rule'],original['products'][ETF]['rule'])

    def test_empty_calendar_returns_partial_without_source_or_phase_mutation(self):
        original=mapping()
        with patch.object(maintenance,'_priorities') as priorities:
            result,status=maintenance.maintain(None,original,None,[],'private',None,NOW,None,None)
        priorities.assert_not_called()
        self.assertEqual(result,original)
        self.assertEqual(status['reason'],'COMPLETED_CALENDAR_UNAVAILABLE')

    def test_budget_expiry_keeps_completed_catalog_change(self):
        original=mapping()
        ledger=SimpleNamespace(ns='test',_connect=FakeConnection)
        def obtain(store,api,params,*args,**kwargs):
            if params.get('list_status')=='L':
                return {'rows':[dict(ts_code='159999.SZ',index_code='000300.SH',csname='新产品',list_status='L')]},NOW
            raise maintenance.Interrupted('REQUEST_BUDGET_EXHAUSTED')
        with patch.object(maintenance,'_priorities',return_value=([],[])),\
             patch.object(maintenance,'stock_identity',return_value=({},{})),\
             patch.object(maintenance,'_obtain',side_effect=obtain),\
             patch.object(maintenance,'save_reference',return_value={'batch':'catalog'}):
            result,status=maintenance.maintain(ledger,original,None,['2026-11-13'],
                                                'private',None,NOW,None,lambda:None)
        self.assertIn('159999.SZ',result['products'])
        self.assertEqual(status['status'],'PARTIAL')
        self.assertEqual(status['interrupted'],'REQUEST_BUDGET_EXHAUSTED')
        self.assertIn('资料更新1项',status['message'])

    def test_new_partial_weights_do_not_downgrade_existing_verified_basis(self):
        original=mapping()
        ledger=SimpleNamespace(ns='test',_connect=FakeConnection)
        def obtain(store,api,params,*args,**kwargs):
            if api=='etf_basic':
                return {'rows':[]},NOW
            return {'rows':[dict(index_code=INDEX,con_code='600000.SH',trade_date='20261113',weight='99')]},NOW
        with patch.object(maintenance,'_priorities',return_value=([ETF],[SID])),\
             patch.object(maintenance,'stock_identity',return_value=({},{})),\
             patch.object(maintenance,'_obtain',side_effect=obtain),\
             patch.object(maintenance,'_cached',return_value=None),\
             patch.object(maintenance,'save_reference',return_value={'batch':'new-weights'}):
            result,status=maintenance.maintain(ledger,original,None,['2026-11-13'],
                    'private',None,NOW,lambda *a:dict(status='FAILED'),lambda:None)
        self.assertEqual(result['weights'],original['weights'])
        self.assertEqual(result['pending_weights'][INDEX]['as_of_date'],'2026-11-13')
        self.assertFalse(result['pending_weights'][INDEX]['complete'])
        self.assertEqual(status['weights'][INDEX]['status'],'RETAINED')
        self.assertEqual(status['status'],'PARTIAL')
