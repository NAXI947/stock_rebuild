"""Task feedback cannot duplicate saved reports or affect native research outcomes."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from modules.strategy_hermes.native_mcp import application
from modules.strategy_hermes.qq_task_notices import enqueue_status, receive_native_failure


class NoticeBoundary(unittest.TestCase):
    def setUp(self):
        self.queue=Mock()
        self.queue.ledger.origin='LIVE'
        self.now=datetime(2026,11,16,5,6,tzinfo=timezone.utc)
        self.payload=dict(agent_id='6T6VMW',cron_id='c3FDPYP',thread_id='thr_0123456789abcdef',
                          observed_at=self.now.isoformat(),error_code='TimeoutError')

    def request(self, body, **changes):
        raw=json.dumps(body).encode()
        env=dict(REQUEST_METHOD='POST',PATH_INFO='/native-notice',CONTENT_TYPE='application/json',
                 CONTENT_LENGTH=str(len(raw)),HTTP_AUTHORIZATION='Bearer '+'x'*48,
                 **{'wsgi.input':BytesIO(raw)})
        env.update(changes)
        status=[]
        raw=b''.join(application(self.queue,'x'*48,wait_seconds=0)(env,lambda s,h:status.append(s)))
        return int(status[0].split()[0]),json.loads(raw)

    def test_callback_auth_and_origin_precede_receipt_handling(self):
        with patch('modules.strategy_hermes.qq_task_notices.receive_native_failure') as handler:
            self.assertEqual(self.request(self.payload,HTTP_AUTHORIZATION='Bearer wrong')[0],401)
            self.assertEqual(self.request(self.payload,HTTP_ORIGIN='https://unexpected.example')[0],403)
            handler.assert_not_called()

    def test_malformed_callback_returns_400_without_stack_failure(self):
        for body in ([],None,{},dict(self.payload,agent_id='my-stock'),dict(self.payload,extra='secret')):
            self.assertEqual(self.request(body),(400,{'error':'INVALID_NOTICE'}))

    def test_untrusted_or_old_receipts_cannot_enqueue(self):
        for changes in ({'agent_id':'ATAPBR'},{'cron_id':'unknown'},{'thread_id':'bad'},
                        {'error_code':'private model text'}, {'observed_at':(self.now-timedelta(minutes=4)).isoformat()}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                receive_native_failure(self.queue,None,dict(self.payload,**changes),now=self.now)
        self.queue.ledger._connect.assert_not_called()

    def test_disabled_notifications_are_noop(self):
        with patch.dict(os.environ,ENABLE_QQ_TASK_NOTICES='false'):
            self.assertEqual(receive_native_failure(self.queue,None,self.payload,now=self.now),{'state':'DISABLED'})
        self.queue.ledger._connect.assert_not_called()

    def notice_store(self, run_id):
        self.queue.ledger.ns='isolated-notice-test'
        context=MagicMock()
        self.queue.ledger._connect.return_value=context
        connection=context.__enter__.return_value
        connection.execute.side_effect=[Mock(fetchone=Mock(return_value=(run_id,))),
                                        Mock(fetchone=Mock(return_value=None)),Mock(),
                                        Mock(fetchone=Mock(return_value=('PENDING',)))]
        return connection

    def inserted_sections(self, connection):
        inserts=[call for call in connection.execute.call_args_list if call.args[0].startswith('INSERT INTO outbox')]
        self.assertEqual(len(inserts),1)
        payload=json.loads(inserts[0].args[1][3])
        self.assertEqual((payload['format'],payload['template_kind']),('qq-template-v2','status'))
        return payload['sections']

    def test_published_research_callback_does_not_enqueue_another_notice(self):
        connection=self.notice_store('already-published')
        self.queue.status.return_value=dict(state='PUBLISHED',report_id='saved-report')
        with patch.dict(os.environ,ENABLE_QQ_TASK_NOTICES='true'), \
             patch('modules.strategy_hermes.qq_task_notices.enqueue_status') as enqueue:
            result=receive_native_failure(self.queue,None,self.payload,now=self.now)
        self.assertEqual(result,dict(state='SKIPPED',reason='REPORT_ALREADY_PUBLISHED'))
        self.queue.status.assert_called_once_with('already-published')
        enqueue.assert_not_called()
        self.assertEqual(connection.execute.call_count,1)

    def test_active_research_callback_records_session_issue_and_actual_task_state(self):
        for state in ('SUBMITTED','PUBLISHING'):
            with self.subTest(state=state):
                connection=self.notice_store('active-research')
                actual=dict(state=state,run_id='active-research',qq={'state':'NOT_QUEUED'})
                self.queue.status.return_value=actual
                with patch.dict(os.environ,ENABLE_QQ_TASK_NOTICES='true'):
                    result=receive_native_failure(self.queue,None,self.payload,now=self.now)
                self.assertEqual(result['state'],'PENDING')
                sections=self.inserted_sections(connection)
                self.assertEqual(sections['status'],'🟠 原生会话异常')
                self.assertEqual(sections['result'],'会话已中断；后台任务状态：'+state)
                self.assertNotIn('任务执行失败',sections['result'])
                self.assertEqual(actual['state'],state)
        self.queue.submit.assert_not_called()

    def test_active_maintenance_callback_records_session_issue_while_work_continues(self):
        payload=dict(self.payload,agent_id='ATAPBR',cron_id='cMWFP36')
        maintenance=Mock()
        for state in ('QUEUED','RUNNING'):
            with self.subTest(state=state):
                connection=self.notice_store('active-maintenance')
                actual=dict(state=state,run_id='active-maintenance',qq={'state':'NOT_QUEUED'})
                maintenance.status.return_value=actual
                with patch.dict(os.environ,ENABLE_QQ_TASK_NOTICES='true'):
                    result=receive_native_failure(self.queue,maintenance,payload,now=self.now)
                self.assertEqual(result['state'],'PENDING')
                sections=self.inserted_sections(connection)
                self.assertEqual(sections['status'],'🟠 原生会话异常')
                self.assertEqual(sections['result'],'会话已中断；后台维护仍在处理，状态：'+state)
                self.assertNotIn('任务执行失败',sections['result'])
                self.assertEqual(actual['state'],state)
                maintenance.status.assert_called_with('active-maintenance')
        maintenance.prepare.assert_not_called()
        self.queue.status.assert_not_called()

    def test_reused_maintenance_cron_ids_resolve_new_task_modes_and_titles(self):
        for cron,mode,slot in [('cMWFP36','market','10:00'),('c5PT18W','retry','14:15')]:
            connection=self.notice_store('failed-maintenance')
            maintenance=Mock()
            maintenance.status.return_value=dict(state='FAILED',run_id='failed-maintenance',qq={'state':'NOT_QUEUED'})
            payload=dict(self.payload,agent_id='ATAPBR',cron_id=cron)
            with patch.dict(os.environ,ENABLE_QQ_TASK_NOTICES='true'):
                result=receive_native_failure(self.queue,maintenance,payload,now=self.now)
            self.assertEqual(result['state'],'PENDING')
            self.assertEqual(connection.execute.call_args_list[0].args[1][1],
                             'etf-maint-2026-11-16-'+mode)
            self.assertEqual(self.inserted_sections(connection)['task_name'],'ETF资料维护 '+slot)

    def test_notification_failure_preserves_failed_native_result(self):
        self.queue.status.return_value=dict(state='FAILED',run_id='round',error='TimeoutError')
        request=dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(
            name='stock_hermes_status',arguments={'run_id':'round'}))
        with patch.dict(os.environ,ENABLE_QQ_TASK_NOTICES='true'), \
             patch('modules.strategy_hermes.qq_task_notices.enqueue_status',side_effect=OSError('private failure')):
            with self.assertLogs('modules.strategy_hermes.native_mcp',level='WARNING') as logs:
                code,body=self.request(request,PATH_INFO='/mcp')
        saved=json.loads(body['result']['content'][0]['text'])
        self.assertEqual((code,saved['state'],saved['error']),(200,'FAILED','TimeoutError'))
        self.assertEqual(saved['status_notification'],dict(state='UNAVAILABLE',error='OSError'))
        self.assertNotIn('private failure',' '.join(logs.output))


@unittest.skipUnless(os.environ.get('STAGE2_TEST_ENV_FILE'),'requires isolated PostgreSQL/Redis')
class NoticePersistence(unittest.TestCase):
    def setUp(self):
        from test_stage2_postgres import StageTwo
        StageTwo.setUp(self)
        self.store.origin='LIVE'
        self.patch=patch.dict(os.environ,ENABLE_QQ_TASK_NOTICES='true')
        self.patch.start();self.addCleanup(self.patch.stop)
        self.now=datetime(2026,11,16,5,6,tzinfo=timezone.utc)

    def clean(self):
        from test_stage2_postgres import StageTwo
        StageTwo.clean(self)

    def rows(self):
        with self.store._connect() as conn:
            return conn.execute("SELECT report_id,event_key,payload FROM outbox WHERE ns=%s AND target='octop'",(self.ns,)).fetchall()

    def test_repeated_terminal_poll_creates_one_durable_notice(self):
        result=dict(state='FAILED',run_id='run-failed',error='TimeoutError')
        first=enqueue_status(self.store,result,'research','13:05',at=self.now)
        second=enqueue_status(self.store,result,'research','13:05',at=self.now)
        self.assertEqual((first['state'],second['state']),('PENDING','PENDING'))
        rows=self.rows();self.assertEqual(len(rows),1)
        payload=json.loads(rows[0][2]);self.assertEqual(payload['template_kind'],'status')
        self.assertNotIn('text',payload)
        self.assertIn('执行失败',payload['sections']['status'])

    def test_saved_maintenance_result_is_reused(self):
        from modules.strategy_hermes.ledger import encode
        with self.store._connect() as conn:
            conn.execute("INSERT INTO outbox(ns,report_id,event_key,target,payload) VALUES(%s,'maintenance-run','maintenance-result','octop',%s)",
                         (self.ns,encode({'text':'saved maintenance result'})))
        result=enqueue_status(self.store,dict(state='FAILED',run_id='maintenance-run'),'maintenance','close',at=self.now)
        self.assertEqual(result['reason'],'EXISTING_RESULT_NOTIFICATION')
        self.assertEqual(len(self.rows()),1)

    def test_callback_without_business_job_queues_once_per_session(self):
        queue=SimpleNamespace(ledger=self.store)
        payload=dict(agent_id='6T6VMW',cron_id='c3FDPYP',thread_id='thr_0123456789abcdef',
                     observed_at=self.now.isoformat(),error_code='TimeoutError')
        first=receive_native_failure(queue,None,payload,now=self.now)
        second=receive_native_failure(queue,None,payload,now=self.now)
        self.assertEqual(first['event_key'],second['event_key'])
        self.assertEqual(len(self.rows()),1)


if __name__=='__main__':unittest.main()
