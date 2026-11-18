"""Real database recovery: frozen facts, old history, bounded attempts, OCTOP gate."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import unittest
from unittest.mock import patch

from modules.strategy_hermes.stage2.native_errors import error_code
from modules.strategy_hermes.stage2.native_recovery import authorize, RecoveryError
from modules.strategy_hermes.stage2.native_rounds import NativeRounds, native_tick
from modules.strategy_hermes.simulator import moment
import test_native_rounds as native
from test_stage1 import D1, D2, stamp


class SafeDiagnostics(unittest.TestCase):
    def test_known_cause_is_preserved_and_arbitrary_exception_text_is_not(self):
        self.assertEqual(error_code(ValueError('calendar update inserts an already processed trading day')),
                         'EXECUTION_CALENDAR_HISTORY_CONFLICT')
        self.assertEqual(error_code(ValueError('private-token-in-arbitrary-message')),'ValueError')
        self.assertEqual(error_code(RecoveryError('RECOVERY_AUTHORIZATION_EXPIRED')),'RECOVERY_AUTHORIZATION_EXPIRED')


@unittest.skipUnless(os.environ.get('STAGE2_TEST_ENV_FILE'),'requires isolated PostgreSQL/Redis')
class PublicationRecovery(unittest.TestCase):
    setUp = native.NativeRoundIntegration.setUp
    prepare = native.NativeRoundIntegration.prepare
    decisions = native.NativeRoundIntegration.decisions

    def clean(self):
        with self.store._connect() as conn:
            for table in ('native_round_recovery_jobs','native_publication_recoveries'):
                conn.execute('DELETE FROM '+table+' WHERE ns=%s',(self.ns,))
        native.NativeRoundIntegration.clean(self)

    def failed(self):
        ready = self.prepare()
        self.queue.submit(ready['run_id'],self.decisions(ready))
        with self.store._connect() as conn:
            conn.execute('UPDATE round_tasks SET calls=\'{"mx":1}\' WHERE ns=%s',(self.ns,))
        with patch('modules.strategy_hermes.stage2.native_rounds.publish_round',
                   side_effect=ValueError('calendar update inserts an already processed trading day')):
            result = native_tick(self.queue,self.cache,self.args,self.peer,now=self.now+timedelta(seconds=5))
        self.assertEqual((result['state'],result['stage'],result['error']),
                         ('FAILED','PUBLISH','EXECUTION_CALENDAR_HISTORY_CONFLICT'))
        return result['run_id']

    def allow(self,parent,now=None):
        return authorize(self.queue,parent,operator='fixture-operator',reason='verified calendar fix',
                         now=now or self.now+timedelta(minutes=30))

    def snapshot(self,parent):
        with self.store._connect() as conn:
            return conn.execute('SELECT to_jsonb(j) FROM native_round_jobs j WHERE ns=%s AND run_id=%s',
                                (self.ns,parent)).fetchone()[0]

    def test_operator_authorization_does_not_run_and_octop_consumes_once(self):
        parent = self.failed()
        original = self.snapshot(parent)
        self.assertEqual(self.queue.prepare('09:35',self.now+timedelta(minutes=30))['run_id'],parent)
        permitted = self.allow(parent)
        child = permitted['run_id']
        self.assertNotEqual(parent,child)
        self.assertEqual(self.allow(parent)['run_id'],child)
        self.assertEqual(native_tick(self.queue,self.cache,self.args,self.peer,now=self.now)['state'],'IDLE')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _:self.queue.prepare('09:35',self.now+timedelta(minutes=30)),range(4)))
        self.assertEqual({x['run_id'] for x in results},{child})
        self.assertEqual({x['state'] for x in results},{'SUBMITTED'})
        # Source collection and model are prohibited in a publication-only recovery.
        with patch('modules.strategy_hermes.stage2.native_rounds._prepare_context',side_effect=AssertionError('no collection')):
            result = native_tick(self.queue,self.cache,self.args,self.peer,now=self.now+timedelta(minutes=31))
        self.assertEqual(result['state'],'PUBLISHED',result)
        self.assertEqual(self.snapshot(parent),original)
        self.assertEqual(self.queue.status(parent)['state'],'FAILED')
        self.assertEqual(self.queue.prepare('09:35',self.now+timedelta(minutes=32))['run_id'],child)
        saved = self.store.report(result['report_id'])
        recovery = saved['input_meta']['manual_recovery']
        self.assertEqual(recovery['retry_of'],parent)
        self.assertEqual(moment(recovery['source_cutoff']),self.now)
        self.assertEqual(moment(recovery['executed_at']),self.now+timedelta(minutes=31))
        self.assertEqual(moment(saved['at']),self.now+timedelta(minutes=31))
        self.assertEqual(saved['input_meta']['octop']['receipt']['native_run_id'],parent)
        self.assertEqual(saved['input_meta']['octop']['receipt']['source'],'octop-native-recovery')
        self.assertIn('手动恢复',result['feedback'])
        # A late OCTOP session error resolves the recovered attempt, not its old parent.
        from modules.strategy_hermes.qq_task_notices import receive_native_failure
        callback_time = self.now+timedelta(minutes=31)
        payload = dict(agent_id='6T6VMW',cron_id='cFSMGMB',thread_id='thr_recovery_verified',
                       observed_at=callback_time.isoformat(),error_code='TimeoutError')
        self.store.origin='LIVE'
        try:
            with patch.dict(os.environ,ENABLE_QQ_TASK_NOTICES='true'):
                notice = receive_native_failure(self.queue,None,payload,now=callback_time)
            self.assertEqual(notice,dict(state='SKIPPED',reason='REPORT_ALREADY_PUBLISHED'))
        finally:
            self.store.origin='FROZEN'
        with self.store._connect() as conn:
            task = conn.execute('SELECT attempts,calls FROM round_tasks WHERE ns=%s',(self.ns,)).fetchone()
            audit = conn.execute('SELECT parent_task FROM native_publication_recoveries WHERE ns=%s',(self.ns,)).fetchone()[0]
            self.assertEqual(task[0],2)
            self.assertEqual(json.loads(task[1]),{'mx':1})
            self.assertEqual(json.loads(audit)['state'],'FAILED')
            self.assertEqual(json.loads(audit)['attempts'],1)
            self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE ns=%s AND kind='report'",(self.ns,)).fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT count(*) FROM outbox WHERE ns=%s',(self.ns,)).fetchone()[0],2)
            self.assertEqual(conn.execute('SELECT count(*) FROM native_round_recovery_jobs WHERE ns=%s',(self.ns,)).fetchone()[0],1)
            # Recover the crash interval after a committed publication as well.
            conn.execute("UPDATE native_round_recovery_jobs SET state='PUBLISHING',expires_at=clock_timestamp()-interval '1 second' WHERE ns=%s",(self.ns,))
        self.assertEqual(self.queue.status(child)['state'],'PUBLISHED')
        self.assertEqual(native_tick(self.queue,self.cache,self.args,self.peer,now=self.now)['state'],'IDLE')
        self.assertEqual(self.snapshot(parent),original)

    def test_other_stage_and_changed_frozen_decisions_are_rejected(self):
        parent = self.failed()
        with self.store._connect() as conn:
            conn.execute("UPDATE native_round_jobs SET failure_stage='PREPARE' WHERE ns=%s",(self.ns,))
        with self.assertRaisesRegex(RecoveryError,'REQUIRES_ORIGINAL_PUBLISH_FAILURE'):
            self.allow(parent)
        with self.store._connect() as conn:
            conn.execute("UPDATE native_round_jobs SET failure_stage='PUBLISH',decision_hash='changed' WHERE ns=%s",(self.ns,))
        with self.assertRaisesRegex(RecoveryError,'FROZEN_INPUTS_INVALID'):
            self.allow(parent)

    def test_authorization_expires_without_rearming_original(self):
        parent = self.failed()
        child = self.allow(parent)['run_id']
        with self.store._connect() as conn:
            conn.execute("UPDATE native_publication_recoveries SET expires_at=clock_timestamp()-interval '1 second' WHERE ns=%s",(self.ns,))
        with self.assertRaisesRegex(RecoveryError,'AUTHORIZATION_EXPIRED'):
            self.queue.prepare('09:35',self.now+timedelta(minutes=30))
        self.assertEqual(self.allow(parent)['run_id'],child)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT state,attempts FROM round_tasks WHERE ns=%s',(self.ns,)).fetchone(),('FAILED',1))
            self.assertEqual(conn.execute('SELECT count(*) FROM native_round_recovery_jobs WHERE ns=%s',(self.ns,)).fetchone()[0],0)

    def test_cross_day_after_close_and_pause_are_rejected(self):
        parent = self.failed()
        for clock in (moment(stamp(D2,'10:00')),moment(stamp(D1,'15:00'))):
            with self.subTest(clock=clock), self.assertRaisesRegex(RecoveryError,'OUTSIDE_SAME_TRADING_DAY'):
                self.allow(parent,clock)
        with self.assertRaisesRegex(RecoveryError,'RELEASE_PAUSED'):
            authorize(NativeRounds(self.store,self.calendar,paused=lambda:True),parent,
                      operator='fixture',reason='paused',now=self.now)
        self.store.origin='LIVE'
        with self.assertRaisesRegex(ValueError,'explicit clock requires frozen'):
            self.allow(parent,self.now)

    def test_active_or_later_published_round_blocks_authorization_and_commit(self):
        parent = self.failed()
        with self.store._connect() as conn:
            conn.execute('''INSERT INTO round_tasks(ns,trade_date,slot,planned_at,state,attempts)
                VALUES(%s,%s,'10:35',%s,'RUNNING',1)''',(self.ns,D1,stamp(D1,'10:35')))
        with self.assertRaisesRegex(RecoveryError,'OTHER_ROUND_ACTIVE'):
            self.allow(parent)
        with self.store._connect() as conn:
            conn.execute("UPDATE round_tasks SET state='FAILED' WHERE ns=%s AND slot='10:35'",(self.ns,))
        self.allow(parent)
        child = self.queue.prepare('09:35',self.now+timedelta(minutes=30))['run_id']
        with self.store._connect() as conn:
            conn.execute("UPDATE round_tasks SET state='PUBLISHED',report_id='later-fixture' WHERE ns=%s AND slot='10:35'",(self.ns,))
        result = native_tick(self.queue,self.cache,self.args,self.peer,now=self.now+timedelta(minutes=31))
        self.assertEqual((result['state'],result['error']),('FAILED','RECOVERY_LATER_ROUND_PUBLISHED'))
        self.assertEqual(self.queue.status(parent)['state'],'FAILED')
        self.assertEqual(self.queue.prepare('09:35',self.now+timedelta(minutes=32))['run_id'],child)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM outbox WHERE ns=%s',(self.ns,)).fetchone()[0],0)

    def test_failed_recovery_remains_failed_and_cannot_create_a_third_attempt(self):
        parent = self.failed()
        self.allow(parent)
        child = self.queue.prepare('09:35',self.now+timedelta(minutes=30))['run_id']
        with patch('modules.strategy_hermes.stage2.native_rounds.publish_round',side_effect=ValueError('fixture fail')):
            self.assertEqual(native_tick(self.queue,self.cache,self.args,self.peer,now=self.now)['state'],'FAILED')
        self.assertEqual(self.allow(parent)['run_id'],child)
        self.assertEqual(self.queue.prepare('09:35',self.now)['state'],'FAILED')
        with self.assertRaisesRegex(RecoveryError,'REQUIRES_ORIGINAL_PUBLISH_FAILURE'):
            self.allow(child)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT attempts FROM round_tasks WHERE ns=%s',(self.ns,)).fetchone()[0],2)

    def test_claim_and_consumption_rollback_together_on_crash(self):
        parent = self.failed()
        child = self.allow(parent)['run_id']
        claim_round = self.store._claim_round
        def crash(*args,**kwargs):
            self.assertIsNotNone(claim_round(*args,**kwargs))
            raise RuntimeError('fixture process interruption')
        with patch.object(self.store,'_claim_round',side_effect=crash), self.assertRaisesRegex(RuntimeError,'interruption'):
            self.queue.prepare('09:35',self.now+timedelta(minutes=30))
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT state,attempts FROM round_tasks WHERE ns=%s',(self.ns,)).fetchone(),('FAILED',1))
            self.assertIsNone(conn.execute('SELECT consumed_at FROM native_publication_recoveries WHERE ns=%s',(self.ns,)).fetchone()[0])
        self.assertEqual(self.queue.prepare('09:35',self.now+timedelta(minutes=30))['run_id'],child)

    def test_authorized_inputs_cannot_change_and_child_deadline_keeps_parent(self):
        parent = self.failed()
        self.allow(parent)
        original = self.snapshot(parent)
        with self.store._connect() as conn:
            conn.execute("UPDATE native_round_jobs SET context=jsonb_set(context,'{analysis,fixture_changed}','true') WHERE ns=%s",(self.ns,))
        with self.assertRaisesRegex(RecoveryError,'FROZEN_INPUTS_CHANGED'):
            self.queue.prepare('09:35',self.now+timedelta(minutes=30))
        with self.store._connect() as conn:
            conn.execute('UPDATE native_round_jobs SET context=%s WHERE ns=%s',(json.dumps(json.loads(original)['context']),self.ns))
        child = self.queue.prepare('09:35',self.now+timedelta(minutes=30))['run_id']
        with self.store._connect() as conn:
            conn.execute("UPDATE native_round_recovery_jobs SET expires_at=clock_timestamp()-interval '1 second' WHERE ns=%s",(self.ns,))
        self.assertEqual(native_tick(self.queue,self.cache,self.args,self.peer,now=self.now)['state'],'IDLE')
        self.assertEqual(self.queue.status(child)['error'],'NATIVE_ROUND_DEADLINE')
        self.assertEqual(self.snapshot(parent),original)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT state,attempts FROM round_tasks WHERE ns=%s',(self.ns,)).fetchone(),('FAILED',2))

    def test_recovery_ownership_loss_is_diagnosed_at_publish(self):
        from uuid import uuid4
        parent = self.failed()
        self.allow(parent)
        self.queue.prepare('09:35',self.now+timedelta(minutes=30))
        with self.store._connect() as conn:
            conn.execute('UPDATE round_tasks SET lease_token=%s WHERE ns=%s',(uuid4(),self.ns))
        result = native_tick(self.queue,self.cache,self.args,self.peer,now=self.now+timedelta(minutes=31))
        self.assertEqual((result['state'],result['stage'],result['error']),('FAILED','PUBLISH','ROUND_OWNERSHIP_LOST'))


if __name__ == '__main__':
    unittest.main()
