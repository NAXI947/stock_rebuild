"""Coverage follows committed facts, including upserts, rollback and concurrent batches."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import os
import unittest
from uuid import uuid4

from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings

ENV = os.environ.get('STAGE2_TEST_ENV_FILE')
DAY = date(1994, 6, 7)
STAMP = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


@unittest.skipUnless(ENV, 'requires isolated PostgreSQL STAGE2_TEST_ENV_FILE')
class CoverageStorage(unittest.TestCase):
    def setUp(self):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, 'test')
        self.tag = 'coverage-' + uuid4().hex
        self.store = PostgresLedger(replace(settings, namespace=settings.redis_prefix+self.tag), 'FROZEN')
        self.store.migrate()
        self.addCleanup(self.clean)
        with self.store._connect() as c:
            c.execute("INSERT INTO supplier_batches VALUES(%s,'daily','{}',%s,1,'[]','[]')", (self.tag,STAMP))
            c.execute("INSERT INTO supplier_market_batches VALUES(%s,'tushare','daily_basic','{}',%s,NULL,1,'{}')", (self.tag,STAMP))

    def clean(self):
        with self.store._connect() as c, c.transaction():
            c.execute('DELETE FROM left_right_assessments WHERE ns LIKE %s', (self.store.ns+'%',))
            for table in ('supplier_daily_bars','supplier_lr_features','supplier_reference_rows'):
                c.execute('DELETE FROM '+table+' WHERE batch_id LIKE %s', (self.tag+'%',))
            c.execute('DELETE FROM supplier_market_batches WHERE batch_id LIKE %s', (self.tag+'%',))
            c.execute('DELETE FROM supplier_batches WHERE batch_id=%s', (self.tag,))
            c.execute('DELETE FROM data_coverage WHERE scope LIKE %s', (self.store.ns+'%',))
            c.execute("DELETE FROM data_coverage WHERE scope='' AND business_date BETWEEN %s AND %s AND row_count=0",
                      (DAY,DAY+timedelta(days=3)))

    def coverage(self, dataset, day=DAY, scope=''):
        with self.store._connect() as c:
            return c.execute('SELECT row_count,ready_count,source_at,details,updated_at FROM data_coverage '
                             'WHERE scope=%s AND dataset=%s AND business_date=%s', (scope,dataset,day)).fetchone()

    def insert_daily(self, c, code='997801.SH', day=DAY):
        c.execute("""INSERT INTO supplier_daily_bars VALUES(%s,%s,10,11,9,10,10,100,1000,0,%s)
            ON CONFLICT(code,trade_date) DO UPDATE SET close=excluded.close,batch_id=excluded.batch_id""", (code,day,self.tag))

    def test_daily_upsert_reuses_one_record_and_transaction_rollback_is_atomic(self):
        with self.store._connect() as c, c.transaction():
            self.insert_daily(c)
            self.insert_daily(c)
        before = self.coverage('daily')
        self.assertEqual(before[:3], (1,None,STAMP))
        with self.assertRaisesRegex(RuntimeError,'rollback'):
            with self.store._connect() as c, c.transaction():
                self.insert_daily(c,'997802.SH')
                self.assertEqual(c.execute("SELECT row_count FROM data_coverage WHERE scope='' AND dataset='daily' AND business_date=%s",(DAY,)).fetchone()[0],2)
                raise RuntimeError('rollback')
        self.assertEqual(self.coverage('daily'), before)
        with self.store._connect() as c:
            self.insert_daily(c)
        self.assertEqual(self.coverage('daily'), before)  # no-op retries don't freshen observation time

    def test_concurrent_page_inserts_accumulate_without_losing_counts(self):
        def write(code):
            with self.store._connect() as c, c.transaction():
                self.insert_daily(c,code)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(write, ['997801.SH','997802.SH']))
        self.assertEqual(self.coverage('daily')[0],2)

    def test_feature_upsert_and_moved_date_delete_follow_actual_rows(self):
        with self.store._connect() as c:
            c.execute("INSERT INTO supplier_lr_features VALUES('daily_basic','997801.SH',%s,'{}',%s)", (DAY,self.tag))
            c.execute("UPDATE supplier_lr_features SET trade_date=%s WHERE batch_id=%s", (DAY+timedelta(days=1),self.tag))
        self.assertEqual(self.coverage('daily_basic')[0],0)
        self.assertEqual(self.coverage('daily_basic',DAY+timedelta(days=1))[:3],(1,None,STAMP))
        with self.store._connect() as c:
            c.execute('DELETE FROM supplier_lr_features WHERE batch_id=%s',(self.tag,))
        self.assertEqual(self.coverage('daily_basic',DAY+timedelta(days=1))[:3],(0,None,None))

    def test_legacy_publications_deduplicate_and_preserve_newer_typed_values(self):
        def legacy(c, suffix, observed, value):
            payload = dict(code='997801.SH', factors={str(DAY):value}, raw=[])
            c.execute("""INSERT INTO supplier_market_batches VALUES
                (%s,'tushare','adj_factor','{"ts_code":"997801.SH"}',%s,NULL,1,%s)
                ON CONFLICT DO NOTHING""", (self.tag+suffix,observed,json.dumps(payload)))
        with self.store._connect() as c:
            c.execute("INSERT INTO supplier_lr_features VALUES('adj_factor','997801.SH',%s,'{\"adj_factor\":\"1\"}',%s)", (DAY,self.tag))
            legacy(c,'-older',STAMP-timedelta(seconds=1),'2')
            legacy(c,'-tie',STAMP,'3')
            self.assertEqual(json.loads(c.execute("SELECT body FROM supplier_lr_features WHERE api='adj_factor' AND code='997801.SH' AND trade_date=%s",(DAY,)).fetchone()[0]),dict(adj_factor='1'))
            legacy(c,'-newer',STAMP+timedelta(seconds=1),'4')
            legacy(c,'-newer',STAMP+timedelta(seconds=1),'4')
            self.assertEqual(json.loads(c.execute("SELECT body FROM supplier_lr_features WHERE api='adj_factor' AND code='997801.SH' AND trade_date=%s",(DAY,)).fetchone()[0]),dict(adj_factor='4'))
        saved = self.coverage('adj_factor')
        self.assertEqual(saved[:3],(1,None,STAMP+timedelta(seconds=1)))
        with self.assertRaisesRegex(RuntimeError,'rollback'):
            with self.store._connect() as c, c.transaction():
                legacy(c,'-rollback',STAMP+timedelta(seconds=2),'5')
                raise RuntimeError('rollback')
        self.assertEqual(self.coverage('adj_factor'),saved)
        with self.store._connect() as c:
            self.assertEqual(json.loads(c.execute("SELECT body FROM supplier_lr_features WHERE api='adj_factor' AND code='997801.SH' AND trade_date=%s",(DAY,)).fetchone()[0]),dict(adj_factor='4'))

    def test_only_legacy_normalized_complete_rows_are_projected(self):
        values = {
            'daily_basic': dict(features={str(DAY):dict(turnover_ratio='0.015',circ_mv_cny='1230000')}),
            'cyq_perf': dict(features={str(DAY):dict(cost85='10.2')}),
            'weekly': dict(weekly=[dict(trade_date=str(DAY),close='10',volume_shares='123',amount_cny='456',complete=True),
                                   dict(trade_date=str(DAY+timedelta(days=1)),close='11',complete=False)]),
        }
        with self.store._connect() as c:
            for api,body in values.items():
                c.execute("""INSERT INTO supplier_market_batches VALUES
                    (%s,'tushare',%s,'{"ts_code":"997801.SH"}',%s,NULL,1,%s)""",
                    (self.tag+'-'+api,api,STAMP,json.dumps(dict(code='997801.SH',raw=[])|body)))
            # The bulk publisher owns its typed INSERT; its normalized receipt
            # must not produce a second, inferred set of facts.
            c.execute("""INSERT INTO supplier_market_batches VALUES
                (%s,'tushare','cyq_perf','{"universe":"ALL"}',%s,NULL,1,%s)""",
                (self.tag+'-bulk',STAMP,json.dumps(dict(code='997802.SH',raw=[],features={str(DAY):dict(cost85='9')}))))
            weekly=json.loads(c.execute("SELECT body FROM supplier_lr_features WHERE api='weekly' AND code='997801.SH' AND trade_date=%s",(DAY,)).fetchone()[0])
            self.assertEqual(weekly,values['weekly']['weekly'][0])
        for api in values:
            self.assertEqual(self.coverage(api)[:3],(1,None,STAMP))
        self.assertIsNone(self.coverage('weekly',DAY+timedelta(days=1)))

    def test_reference_snapshots_and_confirmed_empty_ranges(self):
        def empty(c):
            c.execute("""INSERT INTO supplier_market_batches VALUES(%s,'tushare','share_float',%s,%s,NULL,0,
                '{"receipt":{"status":"EMPTY"}}') ON CONFLICT DO NOTHING""",
                (self.tag+'-empty',json.dumps(dict(trade_date=str(DAY),schema_version='tushare-reference-restored-v1')),STAMP))
        with self.store._connect() as c:
            empty(c)
        saved=self.coverage('share_float')
        self.assertEqual(saved[:3],(0,None,STAMP))
        self.assertEqual(json.loads(saved[3]),dict(valid_empty=True))
        with self.store._connect() as c:
            c.execute("INSERT INTO supplier_reference_rows VALUES('share_float',%s,%s,%s,'{}',%s)",(self.tag,DAY,STAMP,self.tag))
            empty(c)
            c.execute("INSERT INTO supplier_reference_rows VALUES('stock_basic',%s,%s,%s,'{}',%s)",(self.tag,DAY,STAMP,self.tag))
        self.assertEqual(self.coverage('share_float')[0],1)
        self.assertEqual(json.loads(self.coverage('share_float')[3]),{})
        self.assertEqual(self.coverage('stock_basic')[:3],(1,None,STAMP))

    def test_assessment_counts_and_ready_counts_are_namespace_scoped(self):
        with self.store._connect() as c:
            for ns,status in ((self.store.ns,'MISSING'),(self.store.ns+'-other','READY')):
                c.execute("""INSERT INTO left_right_assessments VALUES
                    (%s,%s,'997801.SH','LEFT',%s,%s,%s,NULL,0,'测试','v1','fixture','{}')""", (ns,DAY,STAMP,self.tag,status))
        self.assertEqual(self.coverage('daily_assessment',scope=self.store.ns)[:2],(1,0))
        self.assertEqual(self.coverage('daily_assessment',scope=self.store.ns+'-other')[:2],(1,1))
        with self.store._connect() as c:
            c.execute("UPDATE left_right_assessments SET status='READY' WHERE ns=%s",(self.store.ns,))
        self.assertEqual(self.coverage('daily_assessment',scope=self.store.ns)[:2],(1,1))
        with self.store._connect() as c:
            c.execute("UPDATE left_right_assessments SET status='MISSING' WHERE ns=%s",(self.store.ns,))
            c.execute("DELETE FROM left_right_assessments WHERE ns=%s",(self.store.ns+'-other',))
        self.assertEqual(self.coverage('daily_assessment',scope=self.store.ns)[:2],(1,0))
        self.assertEqual(self.coverage('daily_assessment',scope=self.store.ns+'-other')[:2],(0,0))

    def test_raw_pages_never_claim_published_coverage(self):
        with self.store._connect() as c, c.transaction():
            c.execute("INSERT INTO supplier_tushare_pages VALUES(%s,%s,'cyq_perf','{}','[]','[]',%s)",(self.tag,self.tag,STAMP))
            self.assertIsNone(self.coverage('cyq_perf',DAY+timedelta(days=3)))
            c.execute('DELETE FROM supplier_tushare_pages WHERE request_key=%s',(self.tag,))
