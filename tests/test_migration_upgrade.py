"""Upgrade a disposable stock_v4_test database with a populated previous schema."""
from dataclasses import replace
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from modules.strategy_hermes.manual_executor import ManualExecutor
from modules.strategy_hermes.manual_queue import ManualQueue
from modules.strategy_hermes.runtime import _schema_ready
from modules.strategy_hermes.simulator import Rules
from modules.strategy_hermes.stage2 import postgres
from modules.strategy_hermes.stage2.settings import Settings

ENV = os.environ.get("STAGE2_MIGRATION_ENV_FILE")
FIXTURES = Path(__file__).with_name("fixtures")


@unittest.skipUnless(ENV, "set STAGE2_MIGRATION_ENV_FILE to an empty disposable test database")
class MigrationUpgrade(unittest.TestCase):
    def test_existing_history_survives_backfill_schema_and_remains_replayable(self):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, "test")
        ledger = postgres.PostgresLedger(replace(settings, namespace="stock-v4:test:migration"), "FROZEN")
        migrations = postgres.MIGRATIONS
        previous = [migrations / f"{number:04d}.sql" for number in range(1, 13)]
        with ledger._connect() as conn:
            self.assertIsNone(conn.execute("SELECT to_regnamespace('stock_v4_stage2')").fetchone()[0],
                              "migration test requires a fresh disposable database")
        with patch.object(postgres, "MIGRATIONS") as stage3:
            stage3.glob.return_value = previous
            ledger.migrate()

        bundle = json.loads((FIXTURES / "stage1_trade_cycle.json").read_text())
        events = bundle["events"]
        with ledger._connect() as conn, conn.transaction():
            ledger._store(postgres.BoundConnection(conn), events[:-1], bundle["calendar"],
                          bundle["instruments"], Rules())
        before = ledger.inspect()
        old_report = ledger.report("entry")
        self.assertEqual(before["cycles"][0]["status"], "WAIT_EXIT")
        self.assertEqual(json.loads(old_report["raw"])["research_context"]["evidence_key"], "entry")
        manual = json.loads((FIXTURES / "stage3_manual_v14.json").read_text())
        queue = ManualQueue(ledger)
        draft = queue.create(manual)
        queue.submit(draft)
        self.assertEqual(ManualExecutor(queue).execute(queue.claim()), "SUCCEEDED")
        old_draft = queue.get(draft)
        self.assertEqual(old_draft["job"]["result"]["return_pct"], "9.5000")
        fill = dict(request_id="upgrade-fill", etf="512480.SH", side="BUY", quantity=100,
                    price_cny="0.81", fees_cny="0", executed_at="2026-11-10T10:00:00+08:00")
        with ledger._connect() as conn:
            conn.execute("INSERT INTO actual_trades(ns,request_id,executed_at,body) VALUES(%s,%s,%s,%s)",
                         (ledger.ns, fill["request_id"], fill["executed_at"], json.dumps(fill)))
            conn.execute("""INSERT INTO left_right_backfill_jobs
                (ns,job_id,request_id,payload_hash,body,status,request_budget,requests_used,result)
                VALUES(%s,'upgrade-old-job','old','old','{"codes":["000001.SZ"]}',
                       'PARTIAL',800,17,'{"ready_items":2}')""", (ledger.ns,))
            conn.execute("""INSERT INTO supplier_batches VALUES
                ('coverage-upgrade','daily','{}','2026-11-14T12:00:00Z',1,'[]','[]')""")
            conn.execute("""INSERT INTO supplier_market_batches VALUES
                ('coverage-upgrade','tushare','adj_factor','{}','2026-11-14T12:00:00Z',NULL,1,'{}')""")
            conn.execute("""INSERT INTO supplier_daily_bars VALUES
                ('997899.SH','1994-06-06',10,11,9,10,10,100,1000,0,'coverage-upgrade')""")
            conn.execute("""INSERT INTO supplier_lr_features VALUES
                ('adj_factor','997899.SH','1994-06-06','{"adj_factor":"1"}','coverage-upgrade')""")
            conn.execute("""INSERT INTO supplier_reference_rows VALUES
                ('stock_basic','997899.SH','1994-06-06','2026-11-14T12:00:00Z','{}','coverage-upgrade')""")
            conn.execute("""INSERT INTO left_right_assessments VALUES
                (%s,'1994-06-06','997899.SH','LEFT','2026-11-14T12:00:00Z','upgrade-proof','MISSING',
                 NULL,0,'升级保留','v1','fixture','{}')""",(ledger.ns,))
            conn.execute("""INSERT INTO supplier_market_batches VALUES
                ('coverage-upgrade-empty','tushare','share_float',
                 '{"trade_date":"1994-06-01","schema_version":"tushare-reference-restored-v1"}',
                 '2026-11-14T12:00:00Z',NULL,0,'{"receipt":{"status":"EMPTY"}}')""")
            legacy = {
                'adj_factor': dict(factors={'1994-06-05':'2'}),
                'daily_basic': dict(features={'1994-06-05':dict(turnover_ratio='0.015',circ_mv_cny='1230000')}),
                'cyq_perf': dict(features={'1994-06-05':dict(cost85='10.2')}),
                'weekly': dict(weekly=[dict(trade_date='1994-06-05',close='10',volume_shares='123',amount_cny='456',complete=True),
                                       dict(trade_date='1994-06-04',close='9',complete=False)]),
            }
            for api,body in legacy.items():
                conn.execute("""INSERT INTO supplier_market_batches VALUES
                    (%s,'tushare',%s,'{"ts_code":"997898.SH"}','2026-11-14T12:00:00Z',NULL,1,%s)""",
                    ('coverage-upgrade-legacy-'+api,api,json.dumps(dict(code='997898.SH',raw=[])|body)))
            for suffix,code,day,stamp,value in (
                ('newer','997898.SH','1994-06-05','2026-11-14T13:00:00Z','3'),
                ('older','997899.SH','1994-06-06','2026-11-14T11:00:00Z','8'),
                ('tie','997899.SH','1994-06-06','2026-11-14T12:00:00Z','9')):
                conn.execute("""INSERT INTO supplier_market_batches VALUES
                    (%s,'tushare','adj_factor',%s,%s,NULL,1,%s)""",
                    ('coverage-upgrade-legacy-'+suffix,json.dumps(dict(ts_code=code)),stamp,
                     json.dumps(dict(code=code,factors={day:value},raw=[]))))

        # Preserve the original 0012 -> 0013 backfill assertions while also
        # exercising a populated 0015 native queue across the recovery upgrade.
        with patch.object(postgres, "MIGRATIONS") as pre_recovery:
            pre_recovery.glob.return_value = [migrations / f"{number:04d}.sql" for number in range(1, 16)]
            ledger.migrate()
        failed_native = str(uuid4())
        old_enqueue = '''INSERT INTO native_round_jobs(ns,run_id,trade_date,slot,state)
            VALUES(%s,%s,%s,%s,'QUEUED') ON CONFLICT(ns,trade_date,slot) DO NOTHING RETURNING run_id'''
        with ledger._connect() as conn:
            conn.execute('''INSERT INTO native_round_jobs
                (ns,run_id,trade_date,slot,state,context,decisions,decision_hash,error_code,failure_stage)
                VALUES(%s,%s,'2026-11-17','10:35','FAILED',%s,%s,'frozen-hash','ValueError','PUBLISH')''',
                (ledger.ns, failed_native, json.dumps({'inputs': {'as_of': '2026-11-17T10:35:00+08:00'}}),
                 json.dumps({'sectors': [], 'original': True})))
            native_before = conn.execute('SELECT * FROM native_round_jobs WHERE ns=%s AND run_id=%s',
                                         (ledger.ns, failed_native)).fetchone()

        # First reach the exact previous 0016 schema with retained reference
        # data. 0017 must add empty verification intake, never approve existing
        # catalog rows or rewrite their explicit historical rule fields.
        with patch.object(postgres, "MIGRATIONS") as pre_verification:
            pre_verification.glob.return_value = [migrations / f"{number:04d}.sql" for number in range(1, 17)]
            ledger.migrate()
        mapping_before = dict(schema_version='stock-v4-etf-mapping-v1',version='upgrade-retained-reference',
            products={'588100.SH':dict(index_code='000685.SH',rule=dict(verified=False))},sectors={},weights={})
        with ledger._connect() as conn:
            conn.execute('INSERT INTO maintained_mappings(ns,version,body,summary) VALUES(%s,%s,%s,%s)',
                         (ledger.ns,mapping_before['version'],json.dumps(mapping_before),'{}'))

        ledger.migrate()
        ledger.migrate()
        _schema_ready(ledger)
        self.assertEqual(ledger.inspect(), before)
        self.assertEqual(ledger.report("entry"), old_report)
        self.assertEqual(ManualQueue(ledger).get(draft), old_draft)
        with ledger._connect() as conn:
            self.assertEqual(json.loads(conn.execute('SELECT body FROM maintained_mappings WHERE ns=%s AND version=%s',
                (ledger.ns,mapping_before['version'])).fetchone()[0]),mapping_before)
            for table in ('product_verification_documents','product_verification_requests'):
                self.assertEqual(conn.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT count(*) FROM schema_revisions WHERE revision='0017'").fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT * FROM native_round_jobs WHERE ns=%s AND run_id=%s',
                                          (ledger.ns, failed_native)).fetchone(), native_before)
            self.assertEqual(conn.execute('''SELECT state,retry_of FROM native_round_attempts
                WHERE ns=%s AND run_id=%s''', (ledger.ns, failed_native)).fetchone(), ('FAILED', None))
            # This exact old-image conflict target must keep matching a full
            # unique constraint; a partial index would reject it in PostgreSQL.
            self.assertIsNone(conn.execute(old_enqueue,
                (ledger.ns, str(uuid4()), '2026-11-17', '10:35')).fetchone())
            legacy_new = str(uuid4())
            self.assertEqual(str(conn.execute(old_enqueue,
                (ledger.ns, legacy_new, '2026-11-17', '13:05')).fetchone()[0]), legacy_new)
            self.assertIsNone(conn.execute(old_enqueue,
                (ledger.ns, str(uuid4()), '2026-11-17', '13:05')).fetchone())
            self.assertEqual(conn.execute('SELECT count(*) FROM native_round_jobs WHERE ns=%s',
                                          (ledger.ns,)).fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT * FROM native_round_jobs WHERE ns=%s AND run_id=%s',
                                          (ledger.ns, failed_native)).fetchone(), native_before)
            coverage=conn.execute("""SELECT scope,dataset,row_count,ready_count FROM data_coverage
                WHERE business_date='1994-06-06' ORDER BY scope,dataset""").fetchall()
            self.assertEqual(coverage,[('', 'adj_factor',1,None),('', 'daily',1,None),
                                       ('','stock_basic',1,None),(ledger.ns,'daily_assessment',1,0)])
            empty=conn.execute("SELECT row_count,details FROM data_coverage WHERE dataset='share_float' AND business_date='1994-06-01'").fetchone()
            self.assertEqual((empty[0],json.loads(empty[1])),(0,dict(valid_empty=True)))
            self.assertEqual(conn.execute("SELECT dataset,row_count FROM data_coverage WHERE business_date='1994-06-05' ORDER BY dataset").fetchall(),
                             [('adj_factor',1),('cyq_perf',1),('daily_basic',1),('weekly',1)])
            self.assertEqual(conn.execute("SELECT count(*) FROM data_coverage WHERE business_date='1994-06-04'").fetchone()[0],0)
            for api,body in legacy.items():
                projected=json.loads(conn.execute("SELECT body FROM supplier_lr_features WHERE api=%s AND code='997898.SH' AND trade_date='1994-06-05'",(api,)).fetchone()[0])
                expected = dict(adj_factor='3') if api=='adj_factor' else (body['weekly'][0] if api=='weekly' else body['features']['1994-06-05'])
                self.assertEqual(projected,expected)
            retained=json.loads(conn.execute("SELECT body FROM supplier_lr_features WHERE api='adj_factor' AND code='997899.SH' AND trade_date='1994-06-06'").fetchone()[0])
            self.assertEqual(retained,dict(adj_factor='1'))  # newer/equal typed observations retain precedence
            self.assertEqual(conn.execute("SELECT source_at='2026-11-14T13:00:00Z'::timestamptz FROM data_coverage WHERE dataset='adj_factor' AND business_date='1994-06-05'").fetchone()[0],True)
            # Counts survive a repeated migration, then follow explicit fixture cleanup.
            conn.execute('DELETE FROM left_right_assessments WHERE ns=%s',(ledger.ns,))
            for table in ('supplier_daily_bars','supplier_lr_features','supplier_reference_rows'):
                conn.execute('DELETE FROM '+table+" WHERE batch_id LIKE 'coverage-upgrade%'")
            conn.execute("DELETE FROM supplier_market_batches WHERE batch_id LIKE 'coverage-upgrade%'")
            conn.execute("DELETE FROM supplier_batches WHERE batch_id='coverage-upgrade'")
            self.assertEqual(conn.execute("SELECT sum(row_count) FROM data_coverage WHERE business_date='1994-06-06'").fetchone()[0],0)
            self.assertEqual(json.loads(conn.execute("SELECT body FROM actual_trades WHERE ns=%s", (ledger.ns,)).fetchone()[0]), fill)
            old_job = conn.execute("SELECT request_budget,requests_used,result,body,status FROM left_right_backfill_jobs WHERE ns=%s",
                                   (ledger.ns,)).fetchone()
            self.assertEqual(old_job[:2], (800,17))
            self.assertEqual(json.loads(old_job[2]), {"ready_items":2})
            self.assertEqual(json.loads(old_job[3]), {"codes":["000001.SZ"]})
            self.assertEqual(old_job[4], "PARTIAL")
            conn.execute("UPDATE left_right_backfill_jobs SET request_budget=12000 WHERE ns=%s", (ledger.ns,))
            for table in ("left_right_backfill_requests", "left_right_assessments", "left_right_assessment_sectors",
                          "supplier_tushare_pages", "supplier_tushare_eod_runs", "supplier_lr_features",
                          "data_update_units", "supplier_reference_rows"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            conn.execute("DELETE FROM left_right_backfill_jobs WHERE ns=%s", (ledger.ns,))
        ledger.apply_quotes(events[-1:], bundle["calendar"], bundle["instruments"])
        after = ledger.inspect()
        self.assertEqual(after["cycles"][0]["status"], "CLOSED")
        self.assertEqual(len(after["cycles"][0]["book"]["fills"]), 3)
