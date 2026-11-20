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


    def test_index_weight_reserved_rotation_advances_ordinary_targets_under_priority_pressure(self):
        """T26-a for index weights: more held indexes than slots never starve the due ordinary ones."""
        from modules.strategy_hermes.stage2.reference_policy import ReferencePolicy
        held = ['5100%02d.SH' % n for n in range(12)]
        ordinary = ['000%03d.SH' % n for n in range(900, 906)]
        data = dict(version='rotation', sectors={}, weights={}, products={
            **{code: dict(index_code='000%03d.SH' % n, list_status='L') for n, code in enumerate(held)},
            **{'5120%02d.SH' % n: dict(index_code=index, list_status='L') for n, index in enumerate(ordinary)}})
        priority = {data['products'][code]['index_code'] for code in held}
        advanced = set()
        with patch.dict('os.environ', {'ETF_REPRESENTATIVENESS_MODE': 'V2'}):
            for offset in range(len(priority) + len(ordinary)):
                day = (NOW + timedelta(days=offset)).date()
                selected = maintenance._weight_targets(data, held, [], NOW, day, None)
                self.assertEqual(len(selected), maintenance.MAX_INDEXES)
                self.assertEqual(len(set(selected)), maintenance.MAX_INDEXES)
                self.assertEqual(len(priority.intersection(selected)), maintenance.MAX_INDEXES - 2)
                self.assertEqual(len(set(ordinary).intersection(selected)), 2)
                advanced.update(set(ordinary).intersection(selected))
            self.assertEqual(advanced, set(ordinary))
            policy = ReferencePolicy(data, NOW, 'market')
            selected = maintenance._weight_targets(data, held, [], NOW, NOW.date(), policy)
        deferred = {key for key, value in policy.pending.items() if value['error_kind'] == 'DEFERRED'}
        self.assertEqual(len(deferred), maintenance.MAX_INDEXES)
        self.assertTrue(deferred.isdisjoint('weights:' + index for index in selected))
        self.assertTrue(deferred.intersection('weights:' + index for index in ordinary))


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

    def test_repeated_empty_weight_window_becomes_known_missing_with_same_day_cooldown(self):
        ledger=SimpleNamespace(ns='test',_connect=FakeConnection)
        calendar=['2026-11-13','2026-11-16','2026-11-17','2026-11-18','2026-11-19']
        requested=[]
        def obtain(store,api,params,*args,**kwargs):
            requested.append(api)
            raise SourceFailure('source data not published yet',kind='NOT_PUBLISHED')
        def run(current,day,hour,minute,mode):
            requested.clear()
            now=NOW.replace(day=day,hour=hour,minute=minute)
            with patch.object(maintenance,'_priorities',return_value=([ETF],[SID])),\
                 patch.object(maintenance,'stock_identity',return_value=({},{})),\
                 patch.object(maintenance,'_hydrate_members'),patch.object(maintenance,'_catalog_maintenance'),\
                 patch.object(maintenance,'_members'),patch.object(maintenance,'_aum_maintenance'),\
                 patch.object(maintenance,'_obtain',side_effect=obtain):
                result,status=maintenance.maintain(ledger,current,None,calendar,'private',None,now,
                                                    lambda *a:None,lambda:None,mode=mode)
            return result,status,result['reference_maintenance']['pending'].get('weights:'+INDEX),list(requested)
        current=mapping();current['weights'][INDEX]['complete']=False
        current,status,pending,calls=run(current,17,10,0,'market')
        self.assertEqual((pending['error_kind'],calls),('NOT_PUBLISHED',['index_weight']))
        self.assertEqual(status['weights'][INDEX]['error_kind'],'NOT_PUBLISHED')
        # A single empty reply keeps its same-day retry slot.
        current,status,pending,calls=run(current,17,14,15,'retry')
        self.assertEqual((pending['error_kind'],pending['attempts'],calls),('NOT_PUBLISHED',2,['index_weight']))
        self.assertNotIn('source_missing_since',pending)
        current,status,pending,calls=run(current,18,10,0,'market')
        self.assertEqual((pending['error_kind'],pending['source_missing_since'],calls),
                         ('CONFIRMED_SOURCE_MISSING','2026-11-17',['index_weight']))
        self.assertEqual(status['weights'][INDEX]['error_kind'],'CONFIRMED_SOURCE_MISSING')
        # Known missing: the same day's retry slot spends no request on it.
        current,status,pending,calls=run(current,18,14,15,'retry')
        self.assertEqual((pending['error_kind'],pending['attempts'],calls),('CONFIRMED_SOURCE_MISSING',3,[]))
        # The next trade date checks the source again.
        current,status,pending,calls=run(current,19,10,0,'market')
        self.assertEqual((pending['error_kind'],pending['source_missing_since'],pending['attempts'],calls),
                         ('CONFIRMED_SOURCE_MISSING','2026-11-17',4,['index_weight']))

    def test_connection_failures_never_become_known_missing(self):
        prior=dict(error_kind='CONNECTION_FAILED',last_failed_trade_date='2026-11-13')
        self.assertIsNone(maintenance._source_missing_since(prior,dict(error_kind='NOT_PUBLISHED'),NOW))
        prior['error_kind']='NOT_PUBLISHED'
        self.assertIsNone(maintenance._source_missing_since(prior,dict(error_kind='CONNECTION_FAILED'),NOW))
        self.assertEqual(maintenance._source_missing_since(prior,dict(error_kind='NOT_PUBLISHED'),NOW),'2026-11-13')
        prior['last_failed_trade_date']=NOW.date().isoformat()
        self.assertIsNone(maintenance._source_missing_since(prior,dict(error_kind='NOT_PUBLISHED'),NOW))

    def weight_run(self,weights,official,*,previous=None):
        """One close maintenance with a real index_weight batch and an optional official CSI set."""
        ledger=SimpleNamespace(ns='test',_connect=FakeConnection)
        current=mapping();current['weights']={INDEX:previous} if previous else {}
        rows=[dict(index_code=INDEX,con_code=code,trade_date='20261030',weight=value) for code,value in weights.items()]
        evidence=dict(codes=sorted(official),as_of='2026-11-13',url='https://www.csindex.com.cn/cons.xls') if official else None
        def request(source,scope,operation):
            return dict(status='FAILED',error_kind='CONNECTION_FAILED')
        with patch.object(maintenance,'_priorities',return_value=([ETF],[SID])),\
             patch.object(maintenance,'stock_identity',return_value=({},{})),\
             patch.object(maintenance,'_catalog_maintenance'),patch.object(maintenance,'_members'),\
             patch.object(maintenance,'_obtain',return_value=({'rows':rows},NOW)),\
             patch.object(maintenance,'save_reference',return_value={'batch':'weight-batch'}),\
             patch.object(maintenance,'_cached',return_value=('constituents',) if evidence else None),\
             patch.object(maintenance,'_read_market',return_value=(evidence,NOW)):
            result,status=maintenance.maintain(ledger,current,None,['2026-11-13','2026-11-16'],
                                                'private',None,NOW,request,lambda:None)
        return result['weights'].get(INDEX) or {},status

    def test_absolute_weight_proof_needs_batch_facts_not_only_a_total_in_range(self):
        full={'600000.SH':'60','600036.SH':'40'}
        basis,_=self.weight_run(full,set(full))
        self.assertTrue(basis['complete'])
        self.assertEqual(basis['absolute_weight_proof']['evidence'],'COMPLETE_OFFICIAL_SET')
        # Official set shows a missing member and the batch leaves weight for it.
        basis,_=self.weight_run({'600000.SH':'60'},set(full))
        self.assertFalse(basis['complete'])
        self.assertEqual({k:basis['absolute_weight_proof'][k] for k in
                          ('scope','index_code','as_of_date','source_ref','evidence','row_count','official_count','weight_total')},
            dict(scope='FULL_INDEX_ABSOLUTE',index_code=INDEX,as_of_date='2026-10-30',source_ref='weight-batch',
                 evidence='OFFICIAL_SET_SUBSET',row_count=1,official_count=2,weight_total='0.6'))
        for name,weights,official in (
                ('no official set this run',{'600000.SH':'60'},None),
                ('subset already sums to one',{'600000.SH':'100'},set(full)),
                ('constituent outside the official set',{'600000.SH':'60','000001.SZ':'10'},set(full))):
            with self.subTest(name):
                basis,status=self.weight_run(weights,official)
                self.assertFalse(basis['complete'])
                self.assertNotIn('absolute_weight_proof',basis)
                self.assertEqual((status.get('weight_evidence') or {}).get(INDEX,{}).get('status'),
                                 None if official else 'NOT_CHECKED')

    def test_unchanged_rows_drop_a_legacy_blanket_proof_but_keep_a_derived_one(self):
        rows=[dict(code='600000.SH',weight='0.6',mainboard=None,mainboard_source_batch=None)]
        old=dict(as_of_date='2026-10-30',rows=rows,complete=False,completeness_basis='SOURCE_WEIGHTS_UNVERIFIED',
                 source='tushare:index_weight',available_at='2026-11-13T10:00:00+08:00',source_batches=['old'])
        legacy=dict(scope='FULL_INDEX_ABSOLUTE',index_code=INDEX,as_of_date='2026-10-30',source_ref='old')
        basis,_=self.weight_run({'600000.SH':'60'},None,previous={**old,'absolute_weight_proof':legacy})
        self.assertNotIn('absolute_weight_proof',basis)
        derived={**legacy,'evidence':'OFFICIAL_SET_SUBSET','row_count':1,'official_count':2,'weight_total':'0.6'}
        basis,_=self.weight_run({'600000.SH':'60'},None,previous={**old,'absolute_weight_proof':derived})
        self.assertEqual(basis['absolute_weight_proof'],derived)

    def test_cni_partial_proof_uses_documented_sample_count(self):
        receipt={'batch':'cni-batch'}
        basis=dict(as_of_date='2026-10-30',complete=False,index_identity=dict(expected_count=3),
                   rows=[dict(code='300750.SZ',weight='0.5'),dict(code='300059.SZ',weight='0.2')])
        proof=maintenance._absolute_weight_proof(basis,'399006.SZ',receipt)
        self.assertEqual((proof['evidence'],proof['row_count'],proof['official_count'],proof['weight_total']),
                         ('OFFICIAL_COUNT_SUBSET',2,3,'0.7'))
        basis['rows'].append(dict(code='300760.SZ',weight='0.2'))
        self.assertIsNone(maintenance._absolute_weight_proof(basis,'399006.SZ',receipt))
        basis['rows'][-1:]=[];basis['rows'][0]['weight']='0.8'
        self.assertIsNone(maintenance._absolute_weight_proof(basis,'399006.SZ',receipt))

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
