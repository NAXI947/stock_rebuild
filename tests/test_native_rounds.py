"""Native OCTOP handoff: durable inputs, publication and retry boundaries."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes.stage2.native_rounds import NativeRounds, native_tick, _validate_decisions
import test_stage2_postgres as fixtures
from test_stage1 import A, D1, D2, DAYS, ETF, stamp


class SubmissionValidation(unittest.TestCase):
    def test_incomplete_duplicate_or_unexpected_sectors_are_rejected(self):
        row = dict(sector_id=A,phase='UNKNOWN',evidence_refs=['current'])
        for decisions in ([],[row,row],[{**row,'sector_id':'foreign'}],[{**row,'phase':'BUY'}],
                          [{**row,'price':100}],[{**row,'evidence_refs':'current'}]):
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                _validate_decisions(decisions,dict(evidence={A:{}}))
        _validate_decisions([row],dict(evidence={A:{}}))


@unittest.skipUnless(os.environ.get('STAGE2_TEST_ENV_FILE'), 'requires isolated PostgreSQL/Redis')
class NativeRoundIntegration(unittest.TestCase):
    def setUp(self):
        fixtures.StageTwo.setUp(self)
        root = os.environ.get('STAGE1_TEST_ROOT')
        if root:
            Path(root).mkdir(parents=True,exist_ok=True)
        self.work = TemporaryDirectory(dir=root)
        self.addCleanup(self.work.cleanup)
        self.calendar = Path(self.work.name,'calendar.json')
        self.calendar.write_text(encode(dict(schema_version='hotspot_trade_calendar_v1',
            source='quant.trade_calendar',exchange='SSE',range_start=DAYS[0],range_end=DAYS[-1],
            days={day:True for day in DAYS})),encoding='utf-8')
        self.mapping = Path(self.work.name,'mapping.json')
        self.mapping.write_text(encode(self.map),encoding='utf-8')
        self.queue = NativeRounds(self.store,self.calendar)
        self.now = moment(stamp(D1,'09:35'))
        self.args = SimpleNamespace(mapping=self.mapping,snapshot_key=self.key,snapshot_file=None,
                                    tickflow_config=None,kaipanla_captures=None,qq=True)
        self.peer = SimpleNamespace(call=lambda *args,**kwargs: self.fail('no model/MX expected in frozen first round'))
        market = dict(schema_version='hotspot_market_snapshot_v1',snapshot_id='native-frozen-market',
                      revision_seq=1,trade_date=D1,observed_at=stamp(D1,'09:35'),source='frozen:feed',
                      mode='PARTIAL_REALTIME',quality={},
                      quotes={ETF:dict(observed_at=stamp(D1,'09:35'),price_cny='10',revision_seq=1)},
                      sectors=[dict(sector_id=A,sector_name='fixture',observed_at=stamp(D1,'09:35'),
                                    return_ratio='.03',source_batch='native-frozen-board')])
        self.cache.set(self.key,encode(market),ex=600)

    def clean(self):
        with self.store._connect() as conn:
            conn.execute('DELETE FROM native_round_jobs WHERE ns=%s',(self.ns,))
        fixtures.StageTwo.clean(self)

    def prepare(self):
        result = self.queue.prepare('09:35',self.now)
        self.assertEqual(result['state'],'QUEUED')
        with patch('modules.strategy_hermes.stage2.etf_statistics.overlay_mapping',side_effect=lambda l,m,d,n:m):
            result = native_tick(self.queue,self.cache,self.args,self.peer,now=self.now)
        self.assertEqual(result['state'],'READY',result)
        return result

    def decisions(self,ready):
        return [dict(sector_id=row['sector_id'],phase='NEW' if row['current'] else 'NOT_COVERED',
                     fact_refs=['sector_return'] if 'sector_return' in row.get('fact_catalog', {}) else [],
                     etf_choices=[],
                     evidence_refs=[item['ref'] for item in row['current']]) for row in ready['analysis']['sectors']]

    def test_native_prepare_submit_publish_is_idempotent_and_no_provider_receipt_is_invented(self):
        ready = self.prepare()
        run_id = ready['run_id']
        decisions = self.decisions(ready)
        self.assertEqual(self.queue.prepare('09:35',self.now)['run_id'],run_id)
        self.assertEqual(self.queue.submit(run_id,decisions)['state'],'SUBMITTED')
        self.assertEqual(self.queue.submit(run_id,decisions)['state'],'SUBMITTED')
        result = native_tick(self.queue,self.cache,self.args,self.peer,now=self.now+timedelta(seconds=5))
        self.assertEqual(result['state'],'PUBLISHED',result)
        self.assertEqual(result['qq']['state'],'PENDING')
        for part in ('🎯 ETF 与个股','🟢 候选：','📈 个股：','🤖 节点：'):
            self.assertIn(part,result['feedback'])
        self.assertLessEqual(len(result['feedback']),1500)
        self.assertEqual(self.queue.submit(run_id,decisions)['report_id'],result['report_id'])
        report = self.store.report(result['report_id'])
        receipt = report['input_meta']['octop']['receipt']
        self.assertEqual(receipt['source'],'octop-native-cron')
        self.assertEqual(receipt['native_run_id'],run_id)
        self.assertEqual(receipt['usage_source'],'octop:usage_log')
        self.assertFalse({'http_status','requests','usage','finish_reason'} & receipt.keys())
        with self.store._connect() as conn:
            count = conn.execute("SELECT count(*) FROM events WHERE ns=%s AND kind='report'",(self.ns,)).fetchone()[0]
            outbox = conn.execute('SELECT target FROM outbox WHERE ns=%s ORDER BY target',(self.ns,)).fetchall()
        self.assertEqual(count,1)
        self.assertEqual(outbox,[('octop',),('redis',)])
        self.assertEqual(native_tick(self.queue,self.cache,self.args,self.peer,now=self.now)['state'],'IDLE')

    def test_native_feedback_keeps_legacy_queued_report_support(self):
        from modules.strategy_hermes.stage2.notifications import brief_sections
        ready=self.prepare()
        self.queue.submit(ready['run_id'],self.decisions(ready))
        published=native_tick(self.queue,self.cache,self.args,self.peer,now=self.now+timedelta(seconds=5))
        saved=self.store.report(published['report_id'])
        sections=brief_sections('09:35',saved,[])
        with self.store._connect() as conn:
            conn.execute("UPDATE outbox SET payload=%s WHERE ns=%s AND target='octop'",
                         (encode(dict(format='octop-markdown-v1',sections=sections)),self.ns))
        result=self.queue.status(ready['run_id'])
        self.assertEqual(result['state'],'PUBLISHED')
        for part in ('ETF候选','ETF穿透','个股观察','模拟节点'):
            self.assertIn(part,result['feedback'])
        self.assertEqual(result['qq']['state'],'PENDING')

    def test_submission_validation_preserves_ready_inputs_for_correction(self):
        ready = self.prepare()
        with self.assertRaisesRegex(ValueError,'every input sector'):
            self.queue.submit(ready['run_id'],[])
        self.assertEqual(self.queue.status(ready['run_id'])['state'],'READY')
        decisions = self.decisions(ready)
        self.queue.submit(ready['run_id'],decisions)
        with self.assertRaisesRegex(ValueError,'different content'):
            self.queue.submit(ready['run_id'],[{**decisions[0],'phase':'UNKNOWN'}])

    def test_calendar_window_pause_and_concurrent_duplicate_protection(self):
        outside = self.queue.prepare('09:35',self.now+timedelta(minutes=8))
        self.assertEqual(outside['reason'],'OUTSIDE_ROUND_WINDOW')
        closed = self.queue.prepare('09:35',moment('2026-10-17T09:35:00+08:00'))
        self.assertEqual(closed['reason'],'NON_TRADING_DAY')
        paused = NativeRounds(self.store,self.calendar,paused=lambda:True)
        self.assertEqual(paused.prepare('09:35',self.now)['state'],'PAUSED')
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _:self.queue.prepare('09:35',self.now)['run_id'],range(4)))
        self.assertEqual(len(set(ids)),1)
        with self.assertRaises(ValueError):
            self.queue.status('not-a-uuid')

    def test_ready_deadline_fails_without_new_collection_or_model_calls(self):
        ready = self.prepare()
        with self.store._connect() as conn:
            conn.execute("UPDATE native_round_jobs SET expires_at=clock_timestamp()-interval '1 second' WHERE ns=%s",(self.ns,))
        self.assertEqual(self.queue.status(ready['run_id'])['error'],'NATIVE_ROUND_DEADLINE')
        self.assertEqual(self.queue.status(ready['run_id'])['stage'],'ANALYZE')
        self.assertEqual(native_tick(self.queue,self.cache,self.args,self.peer,now=self.now)['state'],'IDLE')
        with self.store._connect() as conn:
            state,error = conn.execute('SELECT state,error_code FROM round_tasks WHERE ns=%s',(self.ns,)).fetchone()
        self.assertEqual((state,error),('FAILED','NATIVE_ROUND_DEADLINE'))

    def test_frozen_inputs_survive_restart_and_changed_live_snapshot(self):
        ready = self.prepare()
        decisions = self.decisions(ready)
        self.queue.submit(ready['run_id'],decisions)
        self.cache.set(self.key,'{}',ex=60)
        restarted = NativeRounds(self.store,self.calendar)
        result = native_tick(restarted,self.cache,self.args,self.peer,now=self.now+timedelta(seconds=5))
        self.assertEqual(result['state'],'PUBLISHED',result)
        report = self.store.report(result['report_id'])
        self.assertEqual(report['input_meta']['octop']['market_snapshot_id'],'native-frozen-market')

    def test_recovery_after_publication_commit_does_not_publish_twice(self):
        ready = self.prepare()
        self.queue.submit(ready['run_id'],self.decisions(ready))
        result = native_tick(self.queue,self.cache,self.args,self.peer,now=self.now+timedelta(seconds=5))
        with self.store._connect() as conn:
            conn.execute("UPDATE native_round_jobs SET state='PUBLISHING',expires_at=clock_timestamp()-interval '1 second' WHERE ns=%s",(self.ns,))
        self.assertEqual(self.queue.status(ready['run_id'])['state'],'PUBLISHED')
        native_tick(self.queue,self.cache,self.args,self.peer,now=self.now)
        with self.store._connect() as conn:
            state,rid = conn.execute('SELECT state,report_id FROM native_round_jobs WHERE ns=%s',(self.ns,)).fetchone()
            count = conn.execute("SELECT count(*) FROM events WHERE ns=%s AND kind='report'",(self.ns,)).fetchone()[0]
        self.assertEqual((state,rid,count),('PUBLISHED',result['report_id'],1))


if __name__ == '__main__':
    unittest.main()
