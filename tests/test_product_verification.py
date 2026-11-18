"""Evidence intake must not create eligibility before controlled publication."""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modules.strategy_hermes import product_verification as verification
from modules.strategy_hermes.simulator import CHINA

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=CHINA)
ETF = '588100.SH'
INDEX = '000685.SH'
CONTENT = b'Explicit unit-test document. Never a production source.'
DIGEST = sha256(CONTENT).hexdigest()


def dossier():
    doc = dict(url='https://www.sse.com.cn/disclosure/test-prospectus.pdf', sha256=DIGEST,
               acquired_at=(NOW-timedelta(days=1)).isoformat(), effective_at='2026-01-01T00:00:00+08:00')
    return dict(schema_version=verification.SCHEMA, etf_code=ETF, expected_mapping_version='review-base',
                scope='domestic_passive_sector', index_code=INDEX, trade_unit=100,
                price_limit_ratio='0.20', settlement_cycle='T+1', effective_at='2026-01-01T00:00:00+08:00',
                reviewed_at=(NOW-timedelta(hours=1)).isoformat(), reviewed_by='unit-test-reviewer',
                reason='Synthetic evidence intake contract test', documents={'prospectus':doc},
                claims={field:[dict(document='prospectus', locator='test section '+field,
                                    excerpt='Synthetic assertion for '+field)] for field in verification.FIELDS})


def mapping():
    return dict(schema_version='stock-v4-etf-mapping-v1', version='review-base',
                sectors={'kaipanla:chip':dict(members=['688001.SH'],reported_count=1,unresolved_count=0,
                    source_batches=['synthetic-member-batch'],as_of_date='2026-11-16')},
                weights={INDEX:dict(complete=True,as_of_date='2026-11-16',
                    rows=[dict(code='688001.SH',weight='1',mainboard=False)])},
                products={ETF:dict(index_code=INDEX, list_status='L', rule=dict(verified=False))})


class Contract(unittest.TestCase):
    def test_scope_accepts_real_twenty_percent_limit_without_mainboard_gate(self):
        value = verification.validate_dossier(dossier(), NOW)
        self.assertEqual(value['price_limit_ratio'], '0.2')
        self.assertEqual(value['settlement_cycle'], 'T+1')
        self.assertNotIn('verified', value)

    def test_each_material_field_and_all_temporal_evidence_are_required(self):
        for field in verification.FIELDS:
            value = dossier(); value['claims'].pop(field)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'FIELD_EVIDENCE_REQUIRED'):
                verification.validate_dossier(value, NOW)
        for key, value in [('trade_unit', True), ('price_limit_ratio','0'), ('settlement_cycle','T0'),
                           ('scope','overseas'), ('reviewed_at',(NOW+timedelta(seconds=1)).isoformat())]:
            body=dossier(); body[key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):
                verification.validate_dossier(body,NOW)
        body=dossier(); body['documents']['prospectus']['acquired_at']=NOW.isoformat()
        with self.assertRaisesRegex(ValueError,'INVALID_DOCUMENT_CHRONOLOGY'):
            verification.validate_dossier(body,NOW)

    def test_official_url_does_not_accept_credentials_custom_port_or_host_suffix_trick(self):
        for url in ('http://www.sse.com.cn/x','https://sse.com.cn.evil.test/x','https://u:p@sse.com.cn/x',
                    'https://sse.com.cn:8443/x','https://127.0.0.1/x','https://sse.com.cn/x#frag'):
            with self.subTest(url=url), self.assertRaises(ValueError):verification.official_url(url)
        self.assertEqual(verification.official_url('https://docs.static.szse.cn/x.pdf'),
                         'https://docs.static.szse.cn/x.pdf')

    def test_local_import_checks_real_bytes_and_never_fetches(self):
        with TemporaryDirectory(dir=os.environ.get('STAGE1_TEST_ROOT')) as directory:
            root=Path(directory); body=dossier(); body['documents']['prospectus']['file']='document.pdf'
            (root/'document.pdf').write_bytes(CONTENT)
            (root/'review.json').write_text(json.dumps(body),encoding='utf8')
            with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('network forbidden')):
                value, contents=verification.load_bundle(root/'review.json',NOW)
            self.assertEqual(contents,{DIGEST:CONTENT})
            self.assertNotIn('file',value['documents']['prospectus'])
            (root/'document.pdf').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'LOCAL_DOCUMENT_HASH_MISMATCH'):
                verification.load_bundle(root/'review.json',NOW)

    def test_outside_maintenance_window_never_reads_requests_or_changes_product(self):
        original=mapping(); updated=deepcopy(original)
        store=SimpleNamespace(_connect=lambda: self.fail('must not touch storage'))
        result,summary=verification.apply_queued(store,original,updated,{'mode':'market'},
            NOW.replace(hour=19),lambda *args:self.fail('must not request'),lambda:None)
        self.assertEqual(result,original)
        self.assertEqual(summary,{'mode':'market'})

    def test_malformed_or_oversized_document_inventory_is_rejected_before_files(self):
        with TemporaryDirectory(dir=os.environ.get('STAGE1_TEST_ROOT')) as directory:
            target=Path(directory,'review.json')
            for value in ([],{'documents':[]},{'documents':{str(i):{} for i in range(5)}}):
                target.write_text(json.dumps(value),encoding='utf8')
                with self.subTest(value=value),self.assertRaisesRegex(ValueError,'INVALID_VERIFICATION_BUNDLE'):
                    verification.load_bundle(target,NOW)


@unittest.skipUnless(os.environ.get('STAGE2_TEST_ENV_FILE'),'requires isolated PostgreSQL/Redis')
class PersistedReview(unittest.TestCase):
    def setUp(self):
        import test_stage2_postgres as fixtures
        self.fixtures=fixtures
        fixtures.StageTwo.setUp(self)
        self.map=mapping()
        # Supplier cache is shared across namespaces by design. Give every test
        # its own URL scope rather than letting one test's successful official
        # receipt make a later mismatch test skip its mocked provider boundary.
        self.evidence_url='https://www.sse.com.cn/disclosure/test-'+self.ns.rsplit(':',1)[-1]+'.pdf'
        self.import_body=None

    def clean(self):
        with self.store._connect() as conn:
            conn.execute('DELETE FROM product_verification_requests WHERE ns=%s',(self.ns,))
            conn.execute('DELETE FROM product_verification_documents WHERE ns=%s',(self.ns,))
            conn.execute('DELETE FROM maintained_mappings WHERE ns=%s',(self.ns,))
            conn.execute('DELETE FROM left_right_backfill_requests WHERE ns=%s',(self.ns,))
            conn.execute('DELETE FROM left_right_backfill_jobs WHERE ns=%s',(self.ns,))
            conn.execute("DELETE FROM supplier_market_batches WHERE provider='maintenance' AND endpoint IN ('rule_verification','rule_hash') AND scope->>'url'=%s",
                         (self.evidence_url,))
        self.fixtures.StageTwo.clean(self)

    def enqueue(self, body=None):
        body=deepcopy(body or dossier())
        body['documents']['prospectus']['url']=self.evidence_url
        self.import_body=deepcopy(body)
        return verification.submit(self.store,body,{DIGEST:CONTENT},self.map,
                                   operator='test-operator',now=NOW)

    def apply(self, original=None, *, hash_value=DIGEST, updated=None, summary=None):
        from modules.strategy_hermes.stage2 import mapping_maintenance
        original=original or self.map
        checkpoint_calls=[];requests=[]
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return NOW+timedelta(seconds=5)
        def request(source,scope,operation):
            requests.append((source,scope))
            return dict(status='SAVED',receipt=operation())
        with patch.object(mapping_maintenance,'_rule_hash',return_value=hash_value), \
             patch.object(verification,'datetime',Clock):
            result=verification.apply_queued(self.store,original,updated or deepcopy(original),summary or {'mode':'market'},NOW,
                    request,lambda:checkpoint_calls.append(True),clock=lambda:NOW+timedelta(seconds=10))
        return result,requests,checkpoint_calls

    def test_new_unverified_product_requires_worker_check_then_atomic_publication(self):
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        from modules.strategy_hermes.stage2.penetration import verified_product
        receipt=self.enqueue();request_id=receipt['request_id']
        self.assertEqual(receipt['state'],'PENDING')
        self.assertFalse(verified_product(self.map['products'][ETF],NOW))
        (updated,summary),requests,checkpoints=self.apply()
        self.assertEqual(len(requests),1)
        self.assertGreaterEqual(len(checkpoints),3)
        self.assertEqual(verification.status(self.store,request_id)['state'],'VERIFIED')
        self.assertTrue(updated['products'][ETF]['rule']['verified'])
        self.assertFalse(verified_product(updated['products'][ETF],NOW))
        published_at=NOW+timedelta(seconds=11)
        self.assertTrue(publish_mapping(self.store,updated,summary,published_at,expected_parent=self.map['version']))
        self.assertEqual(verification.status(self.store,request_id)['state'],'PUBLISHED')
        self.assertTrue(verified_product(updated['products'][ETF],published_at))
        self.assertEqual(verification.submit(self.store,self.import_body,{},updated,operator='same-review',now=published_at)['request_id'],request_id)
        self.assertFalse(publish_mapping(self.store,updated,summary,published_at))

    def test_mismatch_is_persistent_review_and_never_grants_verified(self):
        receipt=self.enqueue()
        (updated,_),_,_=self.apply(hash_value='f'*64)
        self.assertFalse(updated['products'][ETF]['rule']['verified'])
        state=verification.status(self.store,receipt['request_id'])
        self.assertEqual(state['state'],'REVIEW_REQUIRED')
        self.assertTrue(state['result']['reason'].startswith('OFFICIAL_DOCUMENT_CHANGED'))

    def test_parent_change_rolls_back_publication_and_explicit_rebase_can_resume(self):
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        receipt=self.enqueue()
        (candidate,summary),_,_=self.apply()
        other=deepcopy(self.map);other.update(version='other-update',parent_mapping_version=self.map['version'])
        publish_mapping(self.store,other,{},NOW+timedelta(seconds=11),expected_parent=self.map['version'])
        with self.assertRaisesRegex(ValueError,'MAPPING_PARENT_CONFLICT'):
            publish_mapping(self.store,candidate,summary,NOW+timedelta(seconds=12),expected_parent=self.map['version'])
        self.assertEqual(verification.status(self.store,receipt['request_id'])['state'],'VERIFIED')
        rebased=verification.rebase(self.store,receipt['request_id'],other,operator='review-operator',reason='only unrelated mapping updated',now=NOW+timedelta(seconds=13))
        self.assertEqual(rebased['state'],'PENDING')
        self.assertEqual(verification.status(self.store,receipt['request_id'])['state'],'SUPERSEDED')

    def test_changed_current_identity_blocks_review_even_with_matching_documents(self):
        receipt=self.enqueue()
        altered=deepcopy(self.map);altered['products'][ETF]['index_code']='000300.SH'
        (updated,_),requests,_=self.apply(altered)
        self.assertEqual(requests,[])
        self.assertFalse(updated['products'][ETF]['rule']['verified'])
        self.assertEqual(verification.status(self.store,receipt['request_id'])['state'],'REVIEW_REQUIRED')

    def test_document_cache_resumes_verified_request_without_an_extra_physical_call(self):
        self.enqueue()
        (first,_),requests,_=self.apply()
        (second,_),later,_=self.apply()
        self.assertEqual(len(requests),1)
        self.assertEqual(later,[])
        self.assertEqual(first,second)

    def test_successful_new_review_clears_pending_review_without_changing_old_mapping(self):
        self.map['products'][ETF]['rule']=dict(verified=True,review_required=True,index_code=INDEX,
            scope='domestic_passive_sector',price_limit_ratio='.1',verified_at=(NOW-timedelta(days=2)).isoformat())
        self.enqueue()
        (updated,_),_,_=self.apply()
        self.assertTrue(self.map['products'][ETF]['rule']['review_required'])
        rule=updated['products'][ETF]['rule']
        self.assertFalse(rule['review_required'])
        self.assertEqual(rule['price_limit_ratio'],'0.2')
        self.assertEqual(rule['settlement_cycle'],'T+1')
        self.assertEqual(rule['reviewed_by'],'unit-test-reviewer')
        self.assertIn('source_batch',rule['evidence']['prospectus'])

    def test_published_review_fields_cannot_be_reused_for_changed_rules_or_clearing_review(self):
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        self.enqueue(); (first,summary),_,_=self.apply()
        publish_mapping(self.store,first,summary,NOW+timedelta(seconds=11),expected_parent=self.map['version'])
        changed=deepcopy(first);changed.update(version='tampered-rule',parent_mapping_version=first['version'])
        changed['products'][ETF]['rule']['price_limit_ratio']='0.1'
        with self.assertRaisesRegex(ValueError,'PUBLISHED_VERIFICATION_MUTATED'):
            publish_mapping(self.store,changed,{},NOW+timedelta(seconds=12),expected_parent=first['version'])
        pending=deepcopy(first);pending.update(version='changed-official-doc',parent_mapping_version=first['version'])
        pending['products'][ETF]['rule'].update(review_required=True,review_reason='OFFICIAL_REFERENCE_CHANGED')
        publish_mapping(self.store,pending,{},NOW+timedelta(seconds=13),expected_parent=first['version'])
        cleared=deepcopy(pending);cleared.update(version='unreviewed-clear',parent_mapping_version=pending['version'])
        cleared['products'][ETF]['rule']['review_required']=False
        with self.assertRaisesRegex(ValueError,'RULE_REACTIVATION_REQUIRES_NEW_VERIFICATION'):
            publish_mapping(self.store,cleared,{},NOW+timedelta(seconds=14),expected_parent=pending['version'])

    def test_superseded_request_during_probe_is_not_staged(self):
        self.enqueue()
        with patch.object(verification,'_record_attempt',return_value=False):
            (updated,summary),_,_=self.apply()
        self.assertFalse(updated['products'][ETF]['rule']['verified'])
        self.assertEqual(summary['product_verification'][ETF]['reason'],'REQUEST_SUPERSEDED')

    def test_same_maintenance_candidate_version_does_not_invalidate_original_parent(self):
        from modules.strategy_hermes.stage2.mapping_maintenance import _finish
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        request=self.enqueue()
        earlier=deepcopy(self.map)
        earlier['products'][ETF]['stats']={'aum_yuan':'123456789'}
        staged,summary=_finish(self.map,earlier,{'mode':'market'})
        self.assertNotEqual(staged['version'],self.map['version'])
        (updated,summary),_,_=self.apply(updated=staged,summary=summary)
        self.assertEqual(updated['parent_mapping_version'],self.map['version'])
        self.assertEqual(updated['products'][ETF]['stats'],{'aum_yuan':'123456789'})
        self.assertTrue(updated['products'][ETF]['rule']['verified'])
        publish_mapping(self.store,updated,summary,NOW+timedelta(seconds=11),expected_parent=self.map['version'])
        self.assertEqual(verification.status(self.store,request['request_id'])['state'],'PUBLISHED')

    def test_unverified_review_publish_ai_choice_buy_t1_reduce_and_exit_full_chain(self):
        from modules.strategy_hermes.stage2 import entry_admission
        from modules.strategy_hermes.stage2.inputs import SharedMarket
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        from modules.strategy_hermes.stage2.penetration import prepare_etf_facts
        from modules.strategy_hermes.stage2.worker import planned_at,publish_round
        from test_stage1 import quote,report,stamp
        from modules.strategy_hermes.simulator import moment
        sid='kaipanla:chip'
        self.map['sectors']={sid:dict(members=['688001.SH'],reported_count=1,unresolved_count=0,
            source_batches=['frozen-evidence-members'],as_of_date='2026-11-16')}
        self.map['weights']={INDEX:dict(complete=True,as_of_date='2026-11-16',
            rows=[dict(code='688001.SH',weight='1',mainboard=False)])}
        days=['2026-11-17','2026-11-18','2026-11-19']
        def market(day,hour):
            at=stamp(day,hour)
            return SharedMarket.read(dict(schema_version='hotspot_market_snapshot_v1',snapshot_id='review-'+day+hour.replace(':',''),
                trade_date=day,observed_at=at,source='frozen:review-test',mode='PARTIAL_REALTIME',
                quotes={ETF:dict(price_cny='10',observed_at=at,source='frozen:review-test')}))
        before=prepare_etf_facts(self.map,market(days[0],'10:00'),NOW,[sid])
        self.assertEqual(before[sid]['products'][0]['execution_eligibility'],'PENDING')
        request=self.enqueue()
        (updated,summary),_,_=self.apply()
        publish_mapping(self.store,updated,summary,NOW+timedelta(seconds=11),expected_parent=self.map['version'])
        self.assertEqual(verification.status(self.store,request['request_id'])['state'],'PUBLISHED')
        facts=prepare_etf_facts(updated,market(days[0],'10:34'),moment(stamp(days[0],'10:35')),[sid])
        self.assertEqual(facts[sid]['products'][0]['execution_eligibility'],'READY')
        self.assertEqual(facts[sid]['products'][0]['etf'],ETF)
        # Exercise LIVE admission branches inside the isolated test namespace.
        self.store.origin='LIVE'
        def publish(rid,day,hour,state):
            source=report(rid,day,hour,[(sid,state)])
            envelope=dict(input_kind='LIVE',published_at=stamp(day,hour),report_json=source['report_json'],
                judgments=source['judgments'],octop=dict(etf_choices={sid:[ETF] if state=='新增' else []}))
            claim=self.store.claim_round(day,hour,planned_at(day,hour))
            with patch.object(entry_admission,'_database_clock',return_value=moment(stamp(day,hour))):
                return publish_round(self.store,claim,envelope,days,updated,market(day,hour))
        def price(rid,day,hour,value):
            with patch.object(entry_admission,'_database_clock',return_value=moment(stamp(day,hour))):
                self.store.apply_quotes([quote(rid,day,hour,value,code=ETF)],days,{ETF:100})
        publish('review-entry',days[0],'10:35','新增')
        price('review-buy-price',days[0],'10:36','10')
        cycle,=self.store.inspect()['cycles']
        self.assertEqual(cycle['etf'],ETF)
        self.assertEqual(cycle['book']['fills'][0]['quantity'],1000)
        publish('review-reduce',days[0],'13:05','减弱')
        price('same-day-t1-price',days[0],'13:06','11')
        cycle,=self.store.inspect()['cycles']
        self.assertEqual(len(cycle['book']['fills']),1)
        self.assertEqual(cycle['pending_reason'],'WAIT_T1')
        price('next-day-reduce-price',days[1],'09:31','11')
        publish('review-repeated-weak',days[1],'09:35','减弱')
        publish('review-exit',days[1],'10:35','退潮')
        price('exit-price',days[1],'10:36','12')
        cycle,=self.store.inspect()['cycles']
        self.assertEqual([row['action'] for row in cycle['orders']],['BUY','REDUCE','EXIT'])
        self.assertEqual([row['quantity'] for row in cycle['book']['fills']],[1000,500,500])
        self.assertEqual(cycle['status'],'CLOSED')
        self.assertEqual(cycle['valuation']['profit'],'1500.00')

    def test_worker_prioritizes_verification_then_publishes_it_when_regular_maintenance_exhausts_budget(self):
        from modules.strategy_hermes import etf_maintenance
        from modules.strategy_hermes.left_right_backfill import Interrupted
        from modules.strategy_hermes.stage2 import mapping_maintenance
        from modules.strategy_hermes.stage2.maintenance_store import live_mapping
        request=self.enqueue();seen=[]
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return NOW
        def exhausted(ledger, reviewed, *args, **kwargs):
            seen.append(reviewed['products'][ETF]['rule']['verification_id'])
            self.assertTrue(reviewed['products'][ETF]['rule']['verified'])
            raise Interrupted('REQUEST_BUDGET_EXHAUSTED')
        with TemporaryDirectory(dir=os.environ.get('STAGE1_TEST_ROOT')) as directory:
            root=Path(directory);calendar=root/'calendar.json';reference=root/'mapping.json'
            days=['2026-11-16','2026-11-17','2026-11-18']
            calendar.write_text(json.dumps(dict(schema_version='hotspot_trade_calendar_v1',source='quant.trade_calendar',
                exchange='SSE',range_start=days[0],range_end=days[-1],days={day:True for day in days})),encoding='utf8')
            reference.write_text(json.dumps(self.map),encoding='utf8')
            queue=etf_maintenance.MaintenanceQueue(self.store,calendar)
            prepared=queue.prepare('market',now=NOW)
            claim=queue.queue.claim()
            executor=SimpleNamespace(queue=queue.queue,stop=Event(),paused=lambda:False,external_enabled=lambda:True,
                                     config='unused',captures='unused')
            with patch.object(etf_maintenance,'datetime',Clock),patch.object(verification,'datetime',Clock), \
                 patch.object(etf_maintenance,'_reference'),patch.object(etf_maintenance,'_history_targets',return_value=[]), \
                 patch.object(mapping_maintenance,'_maintain',side_effect=exhausted), \
                 patch.object(mapping_maintenance,'_rule_hash',return_value=DIGEST), \
                 patch.dict(os.environ,APP_MAPPING_FILE=str(reference),ENABLE_QQ_PUSH='false'):
                state=etf_maintenance.execute_job(executor,claim)
            self.assertEqual(state,'PARTIAL')
            self.assertEqual(seen,[request['request_id']])
            self.assertEqual(verification.status(self.store,request['request_id'])['state'],'PUBLISHED')
            current=live_mapping(self.store,reference)
            self.assertEqual(current['parent_mapping_version'],self.map['version'])
            self.assertTrue(current['products'][ETF]['rule']['verified'])
            result=queue.queue.get(prepared['run_id'])['result']
            self.assertEqual(result['mapping_details']['interrupted'],'REQUEST_BUDGET_EXHAUSTED')
            with self.store._connect() as conn:
                self.assertEqual(conn.execute('SELECT requests_used FROM left_right_backfill_jobs WHERE ns=%s AND job_id=%s',
                    (self.ns,prepared['run_id'])).fetchone()[0],1)

    def test_fresh_verified_document_replaces_older_weekly_hash_before_regular_maintenance(self):
        from modules.strategy_hermes.stage2.market_bootstrap import save
        from modules.strategy_hermes.stage2.mapping_maintenance import _cached,_read_market
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        request=self.enqueue()
        scope=dict(url=self.evidence_url,week=f'{NOW.isocalendar().year}-W{NOW.isocalendar().week}')
        save(self.store,'maintenance','rule_hash',scope,{'sha256':'f'*64},1,None,NOW-timedelta(minutes=1))
        (updated,summary),requests,_=self.apply()
        self.assertEqual(len(requests),1)
        latest=_cached(self.store,'rule_hash',scope)
        body,at=_read_market(self.store,latest[0])
        self.assertEqual(body['sha256'],DIGEST)
        self.assertEqual(at,NOW+timedelta(seconds=5))
        self.assertFalse(updated['products'][ETF]['rule']['review_required'])
        publish_mapping(self.store,updated,summary,NOW+timedelta(seconds=11),expected_parent=self.map['version'])
        self.assertEqual(verification.status(self.store,request['request_id'])['state'],'PUBLISHED')


if __name__=='__main__':unittest.main()
