"""Reference upkeep must never block or rewrite Hermes decisions."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from modules.strategy_hermes.etf_maintenance import MaintenanceQueue,allowed_window,execute_job,feedback,_reference
from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes.stage2.maintenance_store import live_mapping,publish_mapping
import test_stage2_postgres as fixtures
from test_stage1 import D1,D2,DAYS,stamp


class Windows(unittest.TestCase):
    def test_maintenance_cannot_run_during_research(self):
        for hour in ('09:00','09:35','13:05','14:30','15:35'):
            for mode in ('market','retry','close','review','preopen'):
                self.assertFalse(allowed_window(mode,moment(stamp(D1,hour))))
        for mode,hour in [('market','10:00'),('retry','14:15')]:
            self.assertTrue(allowed_window(mode,moment(stamp(D1,hour))))

    def test_missing_configuration_is_visible_partial_feedback(self):
        text=feedback(dict(mode='close',business_date=D1,request_id='test'),
                      dict(items=[dict(dataset='history',status='CONFIG_UNAVAILABLE')]))
        self.assertIn('部分完成',text)
        self.assertIn('CONFIG_UNAVAILABLE',text)
        self.assertIn('Hermes照常研判',text)


@unittest.skipUnless(os.environ.get('STAGE2_TEST_ENV_FILE'),'requires isolated PostgreSQL/Redis')
class MaintenanceIntegration(unittest.TestCase):
    def setUp(self):
        fixtures.StageTwo.setUp(self)
        self.work=TemporaryDirectory(dir=os.environ.get('STAGE1_TEST_ROOT'))
        self.addCleanup(self.work.cleanup)
        self.calendar=Path(self.work.name,'calendar.json')
        self.calendar.write_text(encode(dict(schema_version='hotspot_trade_calendar_v1',source='quant.trade_calendar',
            exchange='SSE',range_start=DAYS[0],range_end=DAYS[-1],days={d:True for d in DAYS})),encoding='utf-8')
        self.mapping=Path(self.work.name,'mapping.json')
        self.mapping.write_text(encode(self.map),encoding='utf-8')
        self.queue=MaintenanceQueue(self.store,self.calendar)
        self.now=moment(stamp(D2,'10:00'))

    def clean(self):
        with self.store._connect() as conn:
            conn.execute('DELETE FROM product_verification_targets WHERE ns=%s',(self.ns,))
            conn.execute('DELETE FROM product_verification_requests WHERE ns=%s',(self.ns,))
            conn.execute('DELETE FROM product_verification_documents WHERE ns=%s',(self.ns,))
            conn.execute('DELETE FROM maintained_mappings WHERE ns=%s',(self.ns,))
            conn.execute('DELETE FROM left_right_backfill_requests WHERE ns=%s',(self.ns,))
            conn.execute('DELETE FROM left_right_backfill_jobs WHERE ns=%s',(self.ns,))
            conn.execute("DELETE FROM supplier_market_batches WHERE provider='maintenance' AND endpoint IN ('rule_verification','rule_hash') AND scope->>'url'=%s",
                         ('https://www.sse.com.cn/disclosure/maintenance-'+self.ns.rsplit(':',1)[-1]+'.pdf',))
        fixtures.StageTwo.clean(self)

    def imported_verification(self,code='588100.SH'):
        from test_product_verification import dossier,mapping,CONTENT,DIGEST,NOW
        from modules.strategy_hermes.product_verification import submit
        self.map=mapping()
        self.map['products']={code:self.map['products']['588100.SH']}
        self.mapping.write_text(encode(self.map),encoding='utf-8')
        body=dossier()
        body['etf_code']=code
        body['documents']['prospectus']['url']='https://www.sse.com.cn/disclosure/maintenance-'+self.ns.rsplit(':',1)[-1]+'.pdf'
        return submit(self.store,body,{DIGEST:CONTENT},self.map,operator='isolated-test',now=NOW)['request_id']

    def test_mixed_old_and_new_verification_scope_is_explicit_not_silently_omitted(self):
        from test_product_verification import NOW
        first=self.imported_verification()
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping)):
            original=self.queue.prepare('verify',now=NOW,request_ids=[first])
            second=self.imported_verification('512480.SH')
            mixed=self.queue.prepare('verify',now=NOW,request_ids=[first,second])
        self.assertEqual((mixed['state'],mixed['reason']),('SKIPPED','VERIFICATION_SCOPE_ALREADY_ASSIGNED'))
        self.assertEqual(mixed['existing_run_id'],original['run_id'])
        self.assertEqual(mixed['unbound_request_ids'],[second])
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM left_right_backfill_jobs WHERE ns=%s',(self.ns,)).fetchone()[0],1)
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping)):
            other=self.queue.prepare('verify',now=NOW,request_ids=[second])
            mixed=self.queue.prepare('verify',now=NOW,request_ids=[first,second])
        self.assertEqual(mixed['unbound_request_ids'],[])
        self.assertCountEqual(mixed['existing_run_ids'],[original['run_id'],other['run_id']])

    def test_manual_static_verification_runs_at_night_and_repeated_calls_reuse_budget(self):
        from test_product_verification import NOW,DIGEST
        request_id=self.imported_verification()
        frozen=NOW.replace(hour=22)
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return frozen
        forbidden=Mock(side_effect=AssertionError('static verification cannot collect market/reference/history'))
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping),ENABLE_QQ_PUSH='false'):
            with ThreadPoolExecutor(max_workers=3) as pool:
                prepared=list(pool.map(lambda _:self.queue.prepare('verify',now=frozen,request_ids=[request_id]),range(3)))
            self.assertEqual(len({row['run_id'] for row in prepared}),1)
            claim=self.queue.queue.claim()
            executor=SimpleNamespace(queue=self.queue.queue,stop=Event(),paused=lambda:False,
                external_enabled=lambda:True,config='unused',captures='unused')
            with patch('modules.strategy_hermes.etf_maintenance.datetime',Clock), \
                 patch('modules.strategy_hermes.product_verification.datetime',Clock), \
                 patch('modules.strategy_hermes.etf_maintenance._reference',forbidden), \
                 patch('modules.strategy_hermes.etf_maintenance.runtime_calendar',forbidden), \
                 patch('modules.strategy_hermes.product_verification.prepare_materials',forbidden), \
                 patch('modules.strategy_hermes.stage2.mapping_maintenance.maintain',forbidden), \
                 patch('modules.strategy_hermes.stage2.etf_statistics.refresh',forbidden), \
                 patch('modules.strategy_hermes.stage2.mapping_maintenance._rule_hash',return_value=DIGEST) as fetch:
                self.assertEqual(execute_job(executor,claim),'SUCCEEDED')
                fetch.assert_called_once()
            repeated=self.queue.prepare('verify',now=frozen,request_ids=[request_id])
        self.assertEqual(repeated['run_id'],claim.job_id)
        self.assertEqual(repeated['requests_used'],1)
        self.assertEqual(repeated['request_budget'],120)
        self.assertEqual(repeated['verification']['counts'].get('PUBLISHED'),1)
        self.assertIn(request_id,repeated['feedback'])
        forbidden.assert_not_called()
        with self.store._connect() as conn:
            for table in ('events','cycles','simulation_orders','simulation_fills','outbox'):
                self.assertEqual(conn.execute(f'SELECT count(*) FROM {table} WHERE ns=%s',(self.ns,)).fetchone()[0],0)

    def test_static_verification_pause_preserves_request_budget_and_time_ceiling(self):
        from test_product_verification import NOW
        request_id=self.imported_verification()
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping),ENABLE_QQ_PUSH='false'):
            prepared=self.queue.prepare('verify',now=NOW.replace(hour=22),request_ids=[request_id])
        claim=self.queue.queue.claim()
        self.queue.queue.request(claim,'maintenance:rule_verification',['saved'],lambda:dict(saved=True))
        executor=SimpleNamespace(queue=self.queue.queue,stop=Event(),paused=lambda:True,
            external_enabled=lambda:True,config='unused',captures='unused')
        executor.stop.set()
        self.assertEqual(execute_job(executor,claim),'QUEUED')
        saved=self.queue.queue.get(claim.job_id)
        self.assertEqual((saved['requests_used'],saved['request_budget'],saved['finished_at']),(1,120,None))
        resumed=self.queue.queue.claim()
        with self.store._connect() as conn:
            conn.execute('UPDATE left_right_backfill_jobs SET elapsed_seconds=721 WHERE ns=%s AND job_id=%s',(self.ns,resumed.job_id))
        executor.stop.clear();executor.paused=lambda:False
        with patch('modules.strategy_hermes.product_verification.apply_queued',side_effect=AssertionError('exhausted time')):
            self.assertEqual(execute_job(executor,resumed),'PARTIAL')
        self.assertEqual(self.queue.queue.get(claim.job_id)['requests_used'],1)
        self.assertIn('MAINTENANCE_TIME_BUDGET',self.queue.status(prepared['run_id'])['feedback'])

    def test_static_request_wrapper_rejects_nonofficial_source_before_call(self):
        from test_product_verification import NOW
        request_id=self.imported_verification()
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping)):
            self.queue.prepare('verify',now=NOW.replace(hour=22),request_ids=[request_id])
        claim=self.queue.queue.claim()
        executor=SimpleNamespace(queue=self.queue.queue,stop=Event(),paused=lambda:False,
            external_enabled=lambda:True,config='unused',captures='unused')
        operation=Mock()
        def wrong_source(ledger,mapping,updated,summary,now,request,checkpoint,**kwargs):
            request('tickflow:quotes',[],operation)
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping),ENABLE_QQ_PUSH='false'), \
             patch('modules.strategy_hermes.product_verification.apply_queued',side_effect=wrong_source):
            self.assertEqual(execute_job(executor,claim),'FAILED')
        operation.assert_not_called()
        self.assertEqual(self.queue.queue.get(claim.job_id)['requests_used'],0)

    def test_pause_after_verification_before_publish_returns_same_job_to_queue(self):
        from test_product_verification import NOW,DIGEST
        from modules.strategy_hermes.product_verification import apply_queued
        request_id=self.imported_verification()
        stopping=Event()
        executor=SimpleNamespace(queue=self.queue.queue,stop=stopping,paused=lambda:False,
            external_enabled=lambda:True,config='unused',captures='unused')
        def pause_before_publish(*args,**kwargs):
            value=apply_queued(*args,**kwargs)
            stopping.set()
            return value
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping),ENABLE_QQ_PUSH='false'):
            prepared=self.queue.prepare('verify',now=NOW,request_ids=[request_id])
            claim=self.queue.queue.claim()
            with patch('modules.strategy_hermes.product_verification.apply_queued',side_effect=pause_before_publish), \
                 patch('modules.strategy_hermes.stage2.mapping_maintenance._rule_hash',return_value=DIGEST):
                self.assertEqual(execute_job(executor,claim),'QUEUED')
            saved=self.queue.status(prepared['run_id'])
            self.assertEqual((saved['requests_used'],saved['verification']['counts'].get('VERIFIED')),(1,1))
            with self.store._connect() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM maintained_mappings WHERE ns=%s',(self.ns,)).fetchone()[0],0)
            stopping.clear()
            with patch('modules.strategy_hermes.stage2.mapping_maintenance._rule_hash',side_effect=AssertionError('durable receipt must resume')):
                self.assertEqual(execute_job(executor,self.queue.queue.claim()),'SUCCEEDED')
            self.assertEqual(self.queue.status(prepared['run_id'])['requests_used'],1)

    def test_manual_materials_uses_static_sources_and_preserves_scope_budget(self):
        from test_product_verification import NOW
        frozen=NOW.replace(hour=22)
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return frozen
        forbidden=Mock(side_effect=AssertionError('materials must not run unrelated collectors'))
        def material(ledger,mapping,market,now,request,checkpoint,**kwargs):
            self.assertEqual(request('maintenance:official_discovery',{'url':'fixture'},lambda:{'saved':True})['status'],'SAVED')
            with self.assertRaisesRegex(ValueError,'SOURCE_FORBIDDEN'):
                request('maintenance:rule_verification',{},forbidden)
            with self.assertRaisesRegex(ValueError,'SOURCE_FORBIDDEN'):
                request('kaipanla:quotes',{},forbidden)
            return {'counts':{},'items':[],'omitted':0}
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping),ENABLE_QQ_PUSH='false'), \
             patch('modules.strategy_hermes.etf_maintenance.datetime',Clock), \
             patch('modules.strategy_hermes.etf_maintenance._reference',forbidden), \
             patch('modules.strategy_hermes.etf_maintenance.runtime_calendar',forbidden), \
             patch('modules.strategy_hermes.product_verification.apply_queued',forbidden), \
             patch('modules.strategy_hermes.stage2.mapping_maintenance.maintain',forbidden), \
             patch('modules.strategy_hermes.stage2.etf_statistics.refresh',forbidden), \
             patch('modules.strategy_hermes.product_materials.associated_targets',side_effect=[
                 [('588100.SH',['fixture-sector'])],[('512480.SH',['fixture-sector'])]]), \
             patch('modules.strategy_hermes.product_verification.prepare_materials',side_effect=material):
            first=self.queue.prepare('materials',now=frozen)
            self.assertEqual(execute_job(SimpleNamespace(queue=self.queue.queue,stop=Event(),paused=lambda:False,
                external_enabled=lambda:True,config='unused',captures='unused'),self.queue.queue.claim()),'SUCCEEDED')
            repeated=self.queue.prepare('materials',now=frozen)
            self.assertEqual((repeated['run_id'],repeated['requests_used'],repeated['request_budget']),(first['run_id'],1,120))
            with self.assertRaises(ValueError):self.queue.prepare('materials',now=frozen,request_ids=['forbidden'])
        forbidden.assert_not_called()
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM maintained_mappings WHERE ns=%s',(self.ns,)).fetchone()[0],0)

    def test_real_material_selector_rotation_does_not_create_another_manual_budget(self):
        from test_product_verification import mapping,NOW,ETF
        from modules.strategy_hermes.product_materials import associated_targets,product_basis
        self.map=mapping()
        for i in range(9):self.map['products'][f'{588101+i}.SH']=deepcopy(self.map['products'][ETF])
        self.mapping.write_text(encode(self.map),encoding='utf-8')
        with self.store._connect() as conn:
            conn.execute("INSERT INTO runs(ns,config,origin) VALUES(%s,'{}','FROZEN')",(self.ns,))
            conn.execute('INSERT INTO rotation(ns,sector_id,body) VALUES(%s,%s,%s)',
                         (self.ns,'kaipanla:chip',encode(dict(member=True))))
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping)):
            first=self.queue.prepare('materials',now=NOW)
            selected=self.queue.queue.get(first['run_id'])['material_targets']
            self.assertEqual(len(selected),8)
            with self.store._connect() as conn:
                for index,code in enumerate(selected):
                    conn.execute('''INSERT INTO product_verification_targets
                        (ns,target_id,etf_code,mapping_version,product_basis,sector_ids,state,created_at,updated_at)
                        VALUES(%s,%s,%s,%s,%s,%s,'AWAITING_REVIEW',%s,%s)''',
                        (self.ns,'selection-'+str(index),code,self.map['version'],encode(product_basis(self.map['products'][code])),
                         encode(['kaipanla:chip']),NOW,NOW))
            self.assertFalse(set(selected)&{code for code,_ in associated_targets(self.store,self.map,NOW)})
            repeated=self.queue.prepare('materials',now=NOW)
            self.assertEqual(repeated['run_id'],first['run_id'])
            self.assertEqual(self.queue.queue.get(first['run_id'])['material_targets'],selected)
            explicit=self.queue.prepare('materials',now=NOW,target_codes=selected[:2])
            reversed_scope=self.queue.prepare('materials',now=NOW,target_codes=list(reversed(selected[:2])))
            self.assertEqual(explicit['run_id'],reversed_scope['run_id'])
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM left_right_backfill_jobs WHERE ns=%s',(self.ns,)).fetchone()[0],2)

    def seed_material_revision(self):
        from test_product_verification import NOW,mapping,ETF
        from modules.strategy_hermes.product_materials import product_basis
        from modules.strategy_hermes.ledger import fingerprint
        self.map=mapping();self.map['products']['512480.SH']=self.map['products'].pop(ETF)
        self.mapping.write_text(encode(self.map),encoding='utf-8')
        basis=product_basis(self.map['products']['512480.SH'])
        target_id='pt-'+fingerprint(['512480.SH',basis])[:40]
        changes=[dict(etf_code='512480.SH',target_id=target_id,label='price_rule',
                      previous_url='https://www.sse.com.cn/old.pdf',url='https://www.sse.com.cn/new.docx')]
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping)), \
             patch('modules.strategy_hermes.product_materials.associated_targets',return_value=[('512480.SH',['chip'])]):
            prepared=self.queue.prepare('materials',now=NOW,target_codes=['512480.SH'])
        claim=self.queue.queue.claim()
        for index in range(5):
            self.queue.queue.request(claim,'maintenance:official_material',{'fixture':index},lambda:{'saved':True})
        self.queue.queue.finish(claim,status='PARTIAL',result={'items':[],'verification':{}})
        with self.store._connect() as conn:
            conn.execute('''INSERT INTO product_verification_targets
                (ns,target_id,etf_code,mapping_version,product_basis,sector_ids,state,created_at,updated_at)
                VALUES(%s,%s,'512480.SH',%s,%s,'[]','AWAITING_REVIEW',%s,%s)''',
                (self.ns,target_id,self.map['version'],encode(basis),NOW,NOW))
            conn.execute('UPDATE left_right_backfill_jobs SET elapsed_seconds=12.25 WHERE ns=%s AND job_id=%s',
                         (self.ns,claim.job_id))
            conn.execute("""INSERT INTO outbox(ns,report_id,event_key,target,payload,state,delivery_receipt)
                VALUES(%s,%s,%s,'octop','{}','SENT',%s)""",
                (self.ns,claim.job_id,'maintenance:'+claim.job_id,encode({'status':'ACCEPTED','message_posts':1,'http_status':200})))
        return prepared['run_id'],changes,NOW

    def test_static_source_upgrade_reuses_original_job_across_parent_and_keeps_notification_revision(self):
        job_id,changes,now=self.seed_material_revision()
        with self.store._connect() as conn:
            before=conn.execute('SELECT body,payload_hash,request_id,requests_used,elapsed_seconds FROM left_right_backfill_jobs WHERE ns=%s AND job_id=%s',
                                (self.ns,job_id)).fetchone()
        newer=deepcopy(self.map);newer['version']='unrelated-new-parent'
        publish_mapping(self.store,newer,{},now)
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping),ENABLE_QQ_PUSH='true'), \
             patch('modules.strategy_hermes.product_materials.associated_targets',return_value=[('512480.SH',['chip'])]), \
             patch('modules.strategy_hermes.product_materials.source_upgrades',return_value=changes) as revisions:
            with ThreadPoolExecutor(max_workers=3) as pool:
                receipts=list(pool.map(lambda _:self.queue.prepare('materials',now=now,target_codes=['512480.SH']),range(3)))
            self.assertEqual({row['run_id'] for row in receipts},{job_id})
            self.assertEqual({row['state'] for row in receipts},{'QUEUED'})
            self.assertEqual(receipts[0]['qq']['state'],'NOT_QUEUED')
            self.assertEqual(receipts[0]['qq']['previous_notification']['state'],'SENT')
            self.assertFalse(receipts[0]['qq']['previous_notification']['is_current_revision'])
            with self.store._connect() as conn:
                self.assertEqual(conn.execute('SELECT body,payload_hash,request_id,requests_used,elapsed_seconds FROM left_right_backfill_jobs WHERE ns=%s AND job_id=%s',
                                 (self.ns,job_id)).fetchone(),before)
                self.assertEqual(conn.execute('SELECT count(*) FROM left_right_backfill_jobs WHERE ns=%s',(self.ns,)).fetchone()[0],1)
            def upgraded(ledger,mapping,market,current,request,checkpoint,**options):
                self.assertEqual(options['target_codes'],['512480.SH'])
                self.assertEqual(options['source_upgrades'],changes)
                receipt=request('maintenance:official_material',{'url':changes[0]['url']},lambda:{'saved':True})
                self.assertEqual(receipt['status'],'SAVED')
                return {'counts':{},'items':[],'omitted':0}
            with patch('modules.strategy_hermes.product_verification.prepare_materials',side_effect=upgraded):
                self.assertEqual(execute_job(SimpleNamespace(queue=self.queue.queue,stop=Event(),paused=lambda:False,
                    external_enabled=lambda:True,config='unused',captures='unused'),self.queue.queue.claim()),'PARTIAL')
            revisions.return_value=[]
            repeated=self.queue.prepare('materials',now=now,target_codes=['512480.SH'])
        self.assertEqual((repeated['run_id'],repeated['state'],repeated['requests_used']),(job_id,'PARTIAL',6))
        self.assertEqual(repeated['qq']['state'],'PENDING')
        self.assertEqual(len(repeated['result']['material_source_upgrade_records']),1)
        self.assertEqual(repeated['qq']['previous_notification']['state'],'SENT')
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM left_right_backfill_requests WHERE ns=%s AND job_id=%s',(self.ns,job_id)).fetchone()[0],6)
            self.assertGreaterEqual(conn.execute('SELECT elapsed_seconds FROM left_right_backfill_jobs WHERE ns=%s AND job_id=%s',(self.ns,job_id)).fetchone()[0],12.25)
            self.assertEqual(conn.execute('SELECT count(*) FROM outbox WHERE ns=%s AND report_id=%s',(self.ns,job_id)).fetchone()[0],2)

    def test_static_source_upgrade_does_not_reset_exhausted_cancelled_or_full_queue(self):
        job_id,changes,now=self.seed_material_revision()
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping)), \
             patch('modules.strategy_hermes.product_materials.associated_targets',return_value=[('512480.SH',['chip'])]), \
             patch('modules.strategy_hermes.product_materials.source_upgrades',return_value=changes):
            for update in (dict(cancel_requested=True),dict(requests_used=120),dict(elapsed_seconds=720)):
                with self.subTest(update=update):
                    with self.store._connect() as conn:
                        conn.execute('UPDATE left_right_backfill_jobs SET cancel_requested=false,requests_used=5,elapsed_seconds=12.25 WHERE ns=%s AND job_id=%s',(self.ns,job_id))
                        for key,value in update.items():
                            conn.execute(f'UPDATE left_right_backfill_jobs SET {key}=%s WHERE ns=%s AND job_id=%s',(value,self.ns,job_id))
                    receipt=self.queue.prepare('materials',now=now,target_codes=['512480.SH'])
                    self.assertEqual((receipt['run_id'],receipt['state']),(job_id,'PARTIAL'))
                    self.assertNotIn('material_source_upgrade',receipt['result'])
            with self.store._connect() as conn:
                conn.execute('UPDATE left_right_backfill_jobs SET requests_used=5,elapsed_seconds=12.25 WHERE ns=%s AND job_id=%s',(self.ns,job_id))
            others=[self.queue.queue.enqueue(dict(request_id='capacity-'+str(index),task='fixture'),1)['job_id'] for index in range(3)]
            receipt=self.queue.prepare('materials',now=now,target_codes=['512480.SH'])
            self.assertEqual(receipt['state'],'PARTIAL')
            self.assertIn('队列已满',receipt['next_action'])
            self.assertNotIn('material_source_upgrade',receipt['result'])
            for other in others:self.queue.queue.cancel(other)

    def test_failed_source_revision_and_worker_pause_preserve_attempt_marker(self):
        job_id,changes,now=self.seed_material_revision()
        with patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping),ENABLE_QQ_PUSH='false'), \
             patch('modules.strategy_hermes.product_materials.associated_targets',return_value=[('512480.SH',['chip'])]), \
             patch('modules.strategy_hermes.product_materials.source_upgrades',return_value=changes):
            self.queue.prepare('materials',now=now,target_codes=['512480.SH'])
            executor=SimpleNamespace(queue=self.queue.queue,stop=Event(),paused=lambda:True,
                external_enabled=lambda:True,config='unused',captures='unused')
            self.assertEqual(execute_job(executor,self.queue.queue.claim()),'QUEUED')
            paused=self.queue.status(job_id)
            self.assertEqual(len(paused['result']['material_source_upgrade_records']),1)
            executor.paused=lambda:False
            with patch('modules.strategy_hermes.product_verification.prepare_materials',side_effect=ValueError('official failure')):
                self.assertEqual(execute_job(executor,self.queue.queue.claim()),'FAILED')
            receipt=self.queue.prepare('materials',now=now,target_codes=['512480.SH'])
            self.assertEqual((receipt['run_id'],receipt['state'],receipt['requests_used']),(job_id,'FAILED',5))
            self.assertEqual(len(receipt['result']['material_source_upgrade_records']),1)

    def test_native_trigger_is_idempotent_and_intraday_is_skipped(self):
        first=self.queue.prepare('market',now=self.now)
        second=self.queue.prepare('market',now=self.now)
        self.assertEqual(first['run_id'],second['run_id'])
        self.assertEqual(first['state'],'QUEUED')
        self.assertEqual(self.queue.prepare('market',now=moment(stamp(D1,'13:05')))['state'],'SKIPPED')
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM events WHERE ns=%s',(self.ns,)).fetchone()[0],0)

    def test_local_versions_are_immutable_and_cutoff_aware(self):
        changed=deepcopy(self.map);changed['version']='maintained-fixture'
        changed['sectors']['new:partial']=dict(members=[],unresolved_count=20)
        self.assertTrue(publish_mapping(self.store,changed,{'status':'PARTIAL'},self.now))
        self.assertEqual(live_mapping(self.store,self.mapping,moment(stamp(D1,'13:05')))['version'],self.map['version'])
        self.assertEqual(live_mapping(self.store,self.mapping,self.now),changed)
        self.assertFalse(publish_mapping(self.store,changed,{'status':'PARTIAL'},self.now))
        modified=deepcopy(changed);modified['products']={}
        with self.assertRaises(ValueError):publish_mapping(self.store,modified,{},self.now)

    def test_stale_parent_cannot_replace_a_newer_published_mapping(self):
        first=deepcopy(self.map);first['version']='maintained-first'
        publish_mapping(self.store,first,{},self.now,expected_parent=self.map['version'])
        second=deepcopy(first);second['version']='maintained-second'
        publish_mapping(self.store,second,{},self.now,expected_parent=first['version'])
        stale=deepcopy(first);stale['version']='maintained-stale'
        with self.assertRaisesRegex(ValueError,'MAPPING_PARENT_CONFLICT'):
            publish_mapping(self.store,stale,{},self.now,expected_parent=first['version'])
        self.assertEqual(live_mapping(self.store,self.mapping,self.now)['version'],second['version'])

    def test_cancelled_maintenance_cannot_activate_completed_partial_mapping(self):
        prepared=self.queue.prepare('market',now=self.now)
        claim=self.queue.queue.claim()
        executor=SimpleNamespace(queue=self.queue.queue,stop=Event(),paused=lambda:False,external_enabled=lambda:True,
                                 config='unused',captures='unused')
        changed=deepcopy(self.map);changed['version']='cancelled-partial'
        def maintain(*args,**kwargs):
            self.queue.queue.cancel(prepared['run_id'])
            return changed,dict(status='PARTIAL',interrupted='LEASE_LOST_OR_CANCELLED')
        frozen=self.now
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return frozen
        with patch('modules.strategy_hermes.etf_maintenance.datetime',Clock), \
             patch('modules.strategy_hermes.etf_maintenance._reference'), \
             patch('modules.strategy_hermes.stage2.mapping_maintenance.maintain',side_effect=maintain), \
             patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping),ENABLE_QQ_PUSH='true'):
            self.assertEqual(execute_job(executor,claim),'CANCELLED')
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM maintained_mappings WHERE ns=%s',(self.ns,)).fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM outbox WHERE ns=%s',(self.ns,)).fetchone()[0],0)

    def test_source_failure_still_saves_successful_mapping_and_feedback(self):
        prepared=self.queue.prepare('market',now=self.now)
        claim=self.queue.queue.claim()
        executor=SimpleNamespace(queue=self.queue.queue,stop=Event(),paused=lambda:False,external_enabled=lambda:True,
                                 config='unused',captures='unused')
        changed=deepcopy(self.map);changed['version']='maintenance-partial'
        def partial_reference(executor,claim,request,checkpoint,now,result):
            result['items'].append(dict(dataset='stock_basic',status='FAILED',message='SourceFailure'))
        frozen=self.now
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return frozen
        with patch('modules.strategy_hermes.etf_maintenance.datetime',Clock), \
             patch('modules.strategy_hermes.etf_maintenance._reference',partial_reference), \
             patch('modules.strategy_hermes.stage2.mapping_maintenance.maintain',return_value=(changed,{'status':'PARTIAL'})), \
             patch('modules.strategy_hermes.stage2.etf_statistics.select_targets',return_value=[]), \
             patch.dict(os.environ,APP_MAPPING_FILE=str(self.mapping),ENABLE_QQ_PUSH='true'):
            self.assertEqual(execute_job(executor,claim),'PARTIAL')
        saved=self.queue.status(prepared['run_id'])
        self.assertEqual(saved['state'],'PARTIAL')
        self.assertEqual(saved['qq']['state'],'PENDING')
        self.assertEqual(saved['result']['mapping']['version'],changed['version'])
        with self.store._connect() as conn:
            for table in ('events','cycles','simulation_orders','simulation_fills','native_round_jobs'):
                self.assertEqual(conn.execute(f'SELECT count(*) FROM {table} WHERE ns=%s',(self.ns,)).fetchone()[0],0)


if __name__=='__main__':unittest.main()
