"""QQ formatting keeps terminal states, evidence gaps and delivery outcomes truthful."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modules.strategy_hermes.qq_message_fields import maintenance_fields, status_fields, validation_fields


class MessageFields(unittest.TestCase):
    def test_verification_feedback_keeps_durable_states_and_request_identity(self):
        snapshot=dict(counts={'NEEDS_MATERIALS':2,'AWAITING_REVIEW':1,'PENDING':1,'PUBLISHED':1,'REVIEW_REQUIRED':1},
            items=[dict(etf_code='588100.SH',request_id='pv-first',state='PUBLISHED',published_version='new-map'),
                   dict(etf_code='512480.SH',request_id='pv-second',state='REVIEW_REQUIRED',reason='OFFICIAL_DOCUMENT_CHANGED'),
                   dict(etf_code='159001.SZ',target_id='target-third',state='AWAITING_REVIEW')],omitted=3)
        fields=maintenance_fields(dict(mode='verify'),dict(verification=snapshot), 'PARTIAL')
        text='\n'.join(fields['items'])
        self.assertEqual(fields['slot'],'手动静态核验')
        for phrase in ('待材料2','待审阅1','待回核1','已发布1','需复核1','588100.SH','pv-first','new-map','pv-second','OFFICIAL_DOCUMENT_CHANGED','target-third','其余3项'):
            self.assertIn(phrase,text)

    def test_catalog_relationship_review_is_visible_without_source_failure(self):
        fields=maintenance_fields(dict(mode='market'),dict(items=[dict(dataset='mapping',status='READY')],
            mapping_details=dict(catalog={'L':dict(status='CURRENT')},
                catalog_review={'L':dict(count=302),'D':dict(count=128),'P':dict(count=24)})), 'SUCCEEDED')
        self.assertTrue(any('454条产品范围或指数关联待人工整理' in line for line in fields['items']))
        self.assertFalse(fields['pending_items'])

    def test_failed_worker_is_not_displayed_as_completed_even_with_good_saved_items(self):
        body = dict(mode='close', business_date='2026-11-16', request_id='maint-1')
        result = dict(items=[dict(dataset='stock_basic', status='OK', message='5924条已保存')])
        fields = maintenance_fields(body, result, 'FAILED')
        self.assertEqual(fields['status'], '🔴 维护失败')
        self.assertIn('未完成读取', fields['mapping_counts'])
        self.assertEqual(fields['items'], ['✅ 证券主数据：5924条已保存'])

    def test_partial_mapping_gaps_are_visible_and_input_is_unchanged(self):
        body = dict(mode='review', business_date='2026-11-16', request_id='maint-2')
        result = dict(mapping=dict(sectors=5, products=1467, weights=9, version='mapping-a'),
                      calendar=dict(range_end='2027-12-31'),
                      items=[dict(dataset='mapping', status='PARTIAL', message='真实来源汇总')],
                      mapping_details=dict(
                          catalog={'L': dict(status='UPDATED'), 'D': dict(status='CURRENT')},
                          members={'bank': dict(status='RETAINED', error='SourceFailure')},
                          weights={'399986.SZ': dict(status='UPDATED', as_of='2026-10-30', pending_as_of='2026-11-13')},
                          unverified_products=1466))
        before = deepcopy(result)
        fields = maintenance_fields(body, result, 'PARTIAL')
        self.assertEqual(result, before)
        self.assertEqual(fields['status'], '🟡 部分完成')
        self.assertEqual(fields['calendar_end'], '2027-12-31')
        self.assertIn('待补1项', '\n'.join(fields['items']))
        self.assertIn('1466只待核验', '\n'.join(fields['items']))
        self.assertIn('bank', fields['pending_items'])
        self.assertIn('2026-10-30', fields['weight_sources'])
        self.assertIn('新期资料待核验', fields['weight_sources'])

    def test_empty_results_do_not_invent_data_or_counts(self):
        fields = maintenance_fields(dict(mode='preopen'), {}, 'PARTIAL')
        self.assertEqual(fields['slot'], '08:45')
        self.assertEqual(fields['items'], ['⚪ 暂无已保存的资料结果'])
        self.assertEqual(fields['calendar_end'], '')
        self.assertEqual(fields['weight_sources'], '')
        self.assertEqual(fields['pending_items'], '')

    def test_new_receipt_distinguishes_run_date_batch_and_source_diagnostic(self):
        fields=maintenance_fields(dict(mode='market',scheduled_date='2026-11-17',business_date='2026-11-16'),
            dict(items=[dict(dataset='mapping',status='PARTIAL')],mapping_details=dict(
                aum={'2026-11-16':dict(status='RETAINED',error_code='SourceFailure',error_kind='NOT_PUBLISHED')})), 'PARTIAL')
        self.assertEqual(fields['slot'],'10:00')
        self.assertEqual(fields['business_date'],'执行 2026-11-17 / 资料 2026-11-16')
        self.assertIn('ETF规模（日批次）',' '.join(fields['items']))
        self.assertIn('来源尚未发布',fields['pending_items'])
        self.assertNotIn('正常研判',fields['research_impact'])

    def test_not_due_cache_absorption_and_bounded_retry_queue_are_distinct(self):
        fields=maintenance_fields(dict(mode='retry'),dict(items=[dict(dataset='mapping',status='PARTIAL')],
            mapping_details=dict(catalog={'L':dict(status='NOT_DUE')},
                cache_members=dict(checked=12,updated=2,error="FIELD_MISMATCH"),
                retry_queue=dict(pending_count=3,review_required=1))), 'PARTIAL')
        lines=' '.join(fields['items'])
        self.assertIn('未到期，复用已存资料',lines)
        self.assertIn('检查12项，吸收2项',lines)
        self.assertIn('缓存校验失败，保留当前映射',lines)
        self.assertIn('持久待补：3项',lines)
        self.assertIn('1项达自动重试上限',lines)
        self.assertEqual(fields['pending_items'],'')

    def test_long_item_lists_keep_a_visible_omission_notice(self):
        fields = maintenance_fields(dict(mode='close'), dict(items=[
            dict(dataset='etf_history', label=f'ETF-{index}', status='READY') for index in range(20)]), 'SUCCEEDED')
        self.assertEqual(len(fields['items']), 12)
        self.assertIn('其余9项', fields['items'][-1])

    def test_status_uses_actual_delivery_receipt_and_beijing_time(self):
        fields = status_fields('研判 13:05', 'PUBLISHED', at='2026-11-16T05:06:00+00:00',
                               task_id='run-1', qq=dict(state='SENT', receipt=dict(http_status=200)))
        self.assertEqual(fields['time'], '2026-11-16 13:06:00（北京时间）')
        self.assertEqual(fields['qq_state'], '已发送 (HTTP 200)')
        self.assertEqual(fields['result'], '报告已保存')
        self.assertEqual(fields['next_action'], '查看任务记录')

    def test_unknown_delivery_is_not_promoted_to_sent(self):
        fields = status_fields('维护 18:00', 'SKIPPED', at=datetime(2026, 11, 16, 7, tzinfo=timezone.utc),
                               task_id='run-2', reason='OUTSIDE_MAINTENANCE_WINDOW')
        self.assertEqual(fields['status'], '⏭️ 已跳过')
        self.assertEqual(fields['reason'], '当前不在维护时段')
        self.assertEqual(fields['qq_state'], '投递状态待核实')
        self.assertNotIn('重试', fields['next_action'])
        failed = status_fields('研判', 'FAILED', at='2026-11-16T13:06:00+08:00', task_id='run-3',
                               reason='TimeoutError', qq=dict(state='FAILED', error='DeliveryUncertain'))
        self.assertEqual(failed['reason'], '模型调用超时')
        self.assertIn('发送失败', failed['qq_state'])
        self.assertNotIn('已发送', failed['qq_state'])

    def test_naive_task_time_is_rejected_instead_of_assuming_source_timezone(self):
        with self.assertRaises(ValueError):
            status_fields('研判', 'FAILED', at=datetime(2026, 11, 16, 13, 6), task_id='run-4')

    def test_acceptance_fields_require_supplied_facts(self):
        values = dict(time='2026-11-16 16:20（北京时间）', data_result='243板块', conclusion='🟡 部分完成')
        before = deepcopy(values)
        fields = validation_fields(values)
        self.assertEqual(values, before)
        self.assertEqual(fields['conclusion'], '🟡 部分完成')
        self.assertEqual(fields['qq_result'], '待核实')
        self.assertEqual(fields['maintenance_result'], '待核实')
        with self.assertRaises(ValueError):
            validation_fields(dict(values, unknown_field='untrusted'))


@unittest.skipUnless(os.environ.get('STAGE2_TEST_ENV_FILE'), 'requires isolated PostgreSQL/Redis')
class MaintenanceMessageIntegration(unittest.TestCase):
    def setUp(self):
        from test_etf_maintenance import MaintenanceIntegration
        MaintenanceIntegration.setUp(self)

    def clean(self):
        from test_etf_maintenance import MaintenanceIntegration
        MaintenanceIntegration.clean(self)

    def test_failed_maintenance_saves_structured_qq_fields_with_real_failure_status(self):
        from modules.strategy_hermes.etf_maintenance import execute_job
        from modules.strategy_hermes.qq_templates import MESSAGE_FORMAT

        prepared = self.queue.prepare('market', now=self.now)
        claim = self.queue.queue.claim()
        executor = SimpleNamespace(queue=self.queue.queue, stop=Event(), paused=lambda: False,
                                   external_enabled=lambda: True, config='unused', captures='unused')
        frozen = self.now

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen

        with patch('modules.strategy_hermes.etf_maintenance.datetime', Clock), \
             patch('modules.strategy_hermes.etf_maintenance._reference', side_effect=ValueError('fixture source failure')), \
             patch.dict(os.environ, APP_MAPPING_FILE=str(self.mapping), ENABLE_QQ_PUSH='true'):
            self.assertEqual(execute_job(executor, claim), 'FAILED')
        saved = self.queue.status(prepared['run_id'])
        self.assertEqual(saved['state'], 'FAILED')
        self.assertIn('维护失败', saved['feedback'])
        self.assertEqual(saved['qq']['state'], 'PENDING')
        with self.store._connect() as conn:
            row = conn.execute("SELECT payload FROM outbox WHERE ns=%s AND report_id=%s AND target='octop'",
                               (self.ns, prepared['run_id'])).fetchone()
        payload = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        self.assertNotIn('text', payload)
        self.assertEqual(payload['format'], MESSAGE_FORMAT)
        self.assertEqual(payload['template_kind'], 'maintenance')
        self.assertEqual(payload['sections']['status'], '🔴 维护失败')


if __name__ == '__main__':
    unittest.main()
