"""Cadence and retry decisions must not create extra supplier traffic."""
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from modules.strategy_hermes import etf_maintenance as maintenance
from modules.strategy_hermes.maintenance_policy import completed_session, allowed_window
from modules.strategy_hermes.simulator import moment
from tools.octop_native_local import cron_migration_plan

NOW = moment('2026-11-17T10:00:00+08:00')


class MaintenancePolicy(unittest.TestCase):
    def test_legacy_requests_are_readable_but_cannot_enqueue(self):
        ledger=Mock()
        queue=maintenance.MaintenanceQueue(ledger,'unused')
        for mode in ('close','review','preopen'):
            self.assertEqual(queue.prepare(mode,now=NOW)['reason'],'SCHEDULE_REPLACED')
            self.assertFalse(allowed_window(mode,NOW))
        ledger._connect.assert_not_called()

    def test_daytime_business_date_is_previous_completed_session(self):
        calendar=['2026-11-13','2026-11-16','2026-11-17']
        self.assertEqual(completed_session(calendar,NOW),'2026-11-16')
        self.assertEqual(completed_session(calendar,NOW.replace(hour=14,minute=15)),'2026-11-16')
        self.assertIsNone(completed_session(['2026-11-17'],NOW))

    def test_both_windows_stop_before_research_and_old_slots_are_closed(self):
        for mode,begin,end in (('market',(10,0),(10,15)),('retry',(14,15),(14,27))):
            self.assertTrue(allowed_window(mode,NOW.replace(hour=begin[0],minute=begin[1])))
            self.assertFalse(allowed_window(mode,NOW.replace(hour=end[0],minute=end[1])))
        for hour,minute in ((9,35),(10,35),(13,5),(14,5),(14,30),(18,0),(21,0),(8,45)):
            for mode in ('market','retry'):
                self.assertFalse(allowed_window(mode,NOW.replace(hour=hour,minute=minute)))

    def reference(self,mode,ages,previous,*,broken_config=False):
        def execute(sql,params):
            age=ages[params[0]]
            return SimpleNamespace(fetchone=lambda:(None if age is None else NOW-timedelta(days=age),))
        conn=SimpleNamespace(execute=execute)
        ledger=SimpleNamespace(_connect=lambda:nullcontext(conn),ns='test')
        executor=SimpleNamespace(queue=SimpleNamespace(ledger=ledger),config='unused')
        claim=SimpleNamespace(body=dict(mode=mode,business_date='2026-11-16'))
        result=dict(items=[])
        with patch.object(maintenance,'_latest_market_result',return_value=previous), \
             patch('modules.strategy_hermes.data_updates_collect.UpdateTushare') as client, \
             patch('modules.strategy_hermes.data_updates_collect.collect_reference',return_value=dict(status='OK',row_count=1)) as collect:
            if broken_config:client.side_effect=OSError('unavailable')
            maintenance._reference(executor,claim,Mock(),Mock(),NOW,result)
        return result,client,[call.args[2] for call in collect.call_args_list]

    def test_weekly_references_noop_until_due_and_retry_does_not_refresh_successes(self):
        for mode in ('market','retry'):
            result,client,datasets=self.reference(mode,dict(stock_basic=1,trade_cal=1),{})
            self.assertEqual(datasets,[])
            client.assert_not_called()
            self.assertEqual(result['items'][0]['status'],'CURRENT')
        _,_,datasets=self.reference('market',dict(stock_basic=7,trade_cal=6),{})
        self.assertEqual(datasets,['stock_basic'])

    def test_retry_only_failed_reference_and_missing_batch(self):
        previous=dict(items=[dict(dataset='stock_basic',status='FAILED'),dict(dataset='trade_cal',status='OK')])
        _,_,datasets=self.reference('retry',dict(stock_basic=1,trade_cal=1),previous)
        self.assertEqual(datasets,['stock_basic'])
        _,_,datasets=self.reference('retry',dict(stock_basic=1,trade_cal=None),{})
        self.assertEqual(datasets,['trade_cal'])

    def test_identity_gap_can_advance_stock_directory_refresh(self):
        _,_,datasets=self.reference('market',dict(stock_basic=1,trade_cal=1),
            dict(mapping_details=dict(identity_status='UNAVAILABLE')))
        self.assertEqual(datasets,['stock_basic'])

    def test_unavailable_config_remains_explicit_when_a_reference_is_due(self):
        result,_,datasets=self.reference('market',dict(stock_basic=None,trade_cal=1),{},broken_config=True)
        self.assertEqual(result['items'][0]['status'],'CONFIG_UNAVAILABLE')
        self.assertEqual(datasets,[])

    def test_history_retry_only_uses_failed_codes_from_today_market_receipt(self):
        previous=dict(items=[dict(dataset='etf_history',status='PARTIAL',etf_code='512800.SH'),
                             dict(dataset='etf_history',status='READY',etf_code='512480.SH')])
        mapping=dict(products={code:dict(index_code='index',list_status='L',rule={'verified':False})
                               for code in ('512800.SH','512480.SH')})
        with patch.object(maintenance,'_latest_market_result',return_value=previous) as read, \
             patch('modules.strategy_hermes.stage2.etf_statistics.select_targets') as select:
            self.assertEqual(maintenance._history_targets(None,mapping,[],NOW,'retry'),['512800.SH'])
            read.assert_called_once_with(None,day='2026-11-17')
            select.assert_not_called()
            read.return_value={}
            self.assertEqual(maintenance._history_targets(None,{},[],NOW,'retry'),[])


class MaintenanceCronMigration(unittest.TestCase):
    def test_old_ids_are_reused_and_retired_timer_remains_identifiable(self):
        old=[SimpleNamespace(name='ETF资料维护 '+slot,cron_id=key)
             for slot,key in [('18:00','close-id'),('21:00','review-id'),('08:45','preopen-id')]]
        jobs=[dict(name='ETF资料维护 '+slot) for slot in ('10:00','14:15')]
        selected,retired=cron_migration_plan(old,jobs,maintenance=True)
        self.assertEqual([selected[j['name']].cron_id for j in jobs],['close-id','review-id'])
        self.assertEqual([j.cron_id for j in retired],['preopen-id'])
        updated=[SimpleNamespace(name=name,cron_id=row.cron_id) for name,row in selected.items()]+retired
        again,_=cron_migration_plan(updated,jobs,maintenance=True)
        self.assertEqual([again[j['name']].cron_id for j in jobs],['close-id','review-id'])

    def test_unknown_or_duplicate_crons_fail_before_mutation(self):
        jobs=[dict(name='ETF资料维护 '+slot) for slot in ('10:00','14:15')]
        for old in ([SimpleNamespace(name='unrelated',cron_id='other')],
                    [SimpleNamespace(name='ETF资料维护 '+slot,cron_id=slot) for slot in ('18:00','10:00')]):
            with self.assertRaises(ValueError):cron_migration_plan(old,jobs,maintenance=True)
