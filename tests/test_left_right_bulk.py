"""Full-market archive sizing and SQL reads against an isolated PostgreSQL DB."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import os
import re
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from modules.strategy_hermes import left_right_info as info
from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes.stage2.left_right_eod import _database_input, produce
from modules.strategy_hermes.stage2.left_right_refresh import build_manual_archives, load_archive, persist_archive
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings
from modules.strategy_hermes.workspace_readiness import eod_body
from tests.test_left_right_eod import fixture, DAY, CODE

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")
DAY_BULK = date(2000, 2, 4)
AS_OF = moment("2000-02-04T16:00:00+08:00")
SECTOR = "kaipanla:801027"


def dated_fixture():
    delta = DAY_BULK - DAY
    def shifted(value):
        if isinstance(value, dict):
            return {key: shifted(item) for key, item in value.items()}
        if isinstance(value, list):
            return [shifted(item) for item in value]
        if isinstance(value, str) and re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return str(date.fromisoformat(value[:10])+delta)+value[10:]
        return value
    calendar, evidence = fixture()
    evidence = shifted(evidence)
    evidence["source_batches"] = ["fixture-daily", "fixture-adj", "fixture-weekly", "fixture-members"]
    evidence["symbols"][CODE]["sectors"] = [dict(id=SECTOR, name="银行", membership_date=str(DAY_BULK),
        source="isolated 046 fixture", source_batch="fixture-members")]
    return shifted(calendar), evidence


@unittest.skipUnless(ENV, "requires isolated PostgreSQL STAGE2_TEST_ENV_FILE")
class BulkArchiveTests(unittest.TestCase):
    def setUp(self):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, "test")
        self.store = PostgresLedger(replace(settings, namespace=settings.redis_prefix+"archive-bulk:"+uuid4().hex), "FROZEN")
        self.store.migrate()
        self.batch = "archive-bulk-"+uuid4().hex
        self.codes = [f"{100000+i:06d}.SZ" for i in range(6000)]
        self.calendar, self.evidence = dated_fixture()
        with self.store._connect() as conn, conn.transaction():
            conn.execute("""INSERT INTO supplier_batches(batch_id,api,requested_scope,collected_at,row_count,raw_rows,normalized)
                VALUES(%s,'daily','{}',%s,6000,'[]','{}')""", (self.batch, AS_OF))
            conn.execute("""INSERT INTO supplier_trade_calendar(exchange,cal_date,is_open,batch_id)
                VALUES('SSE',%s,true,%s)""", (DAY_BULK, self.batch))
            with conn.cursor().copy("""COPY supplier_daily_bars
                (code,trade_date,open,high,low,close,pre_close,volume_shares,amount_cny,return_ratio,batch_id) FROM STDIN""") as writer:
                for code in self.codes:
                    writer.write_row((code, DAY_BULK, 10, 11, 9, 10, 10, 100, 1000, 0, self.batch))
        self.addCleanup(self.clean)

    def clean(self):
        with self.store._connect() as conn, conn.transaction():
            conn.execute("DELETE FROM left_right_assessments WHERE ns=%s", (self.store.ns,))
            conn.execute("DELETE FROM snapshot_state WHERE ns=%s", (self.store.ns,))
            conn.execute("DELETE FROM supplier_lr_features WHERE batch_id=%s", (self.batch,))
            conn.execute("DELETE FROM supplier_market_batches WHERE batch_id=%s", (self.batch,))
            conn.execute("DELETE FROM supplier_daily_bars WHERE batch_id=%s", (self.batch,))
            conn.execute("DELETE FROM supplier_trade_calendar WHERE batch_id=%s", (self.batch,))
            conn.execute("DELETE FROM supplier_batches WHERE batch_id=%s", (self.batch,))

    def reader(self, _ledger, day, codes, _as_of, **_kwargs):
        self.assertEqual(day, DAY_BULK)
        self.assertLessEqual(len(codes), 50)
        return dict(self.evidence, symbols={code: self.evidence["symbols"][CODE] for code in codes})

    def test_6000_stocks_stream_to_12000_rows_then_sql_page_and_sector_top_five(self):
        started = time.monotonic()
        progress = []
        with patch("modules.strategy_hermes.stage2.left_right_refresh._database_context", return_value={}) as context, \
             patch("modules.strategy_hermes.stage2.left_right_refresh._database_input", side_effect=self.reader) as reader:
            result = build_manual_archives(self.store, [str(DAY_BULK)], None, AS_OF, persist=True,
                                           checkpoint=lambda: None, progress=lambda **value: progress.append(value))
        self.assertEqual((context.call_count, reader.call_count), (1, 120))
        self.assertEqual((result["universe_count"], result["ready_items"], result["expected_items"]), (6000, 12000, 12000))
        self.assertEqual((result["archives"], result["missing"], result["archives_saved"]), ([], [], 1))
        self.assertEqual(progress[-1]["processed_stocks"], 6000)
        self.assertLess(len(encode(result)), 2000)
        # An indexed date must never decode a historical snapshot collection.
        with patch.object(info, "load_history", side_effect=lambda *_args, **kwargs:
                          {"archives": [], "issues": []} if kwargs.get("include_database") is False
                          else (_ for _ in ()).throw(AssertionError("unbounded legacy archive read"))):
            last = info.read_info(self.store, "page=60&page_size=100")
            searched = info.read_info(self.store, "q=105999.sz")
        self.assertEqual((last["total"], len(last["items"]), last["pages"]), (6000, 100, 60))
        self.assertEqual(last["items"][-1]["ts_code"], "105999.SZ")
        self.assertEqual(searched["total"], 1)
        self.assertEqual(searched["summary"]["total"], 6000)
        groups, days = info.load_sector_groups(self.store, {str(DAY_BULK): [SECTOR]})
        self.assertEqual(days, {str(DAY_BULK)})
        self.assertEqual(groups[str(DAY_BULK)][SECTOR][1], {"LEFT": 0, "RIGHT": 6000})
        self.assertEqual(len(groups[str(DAY_BULK)][SECTOR][0]["RIGHT"]), 5)
        preview = eod_body(load_archive(self.store), self.calendar)
        self.assertEqual((preview["trade_date"], len(preview["items"])), (str(DAY_BULK), 100))
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM left_right_assessments WHERE ns=%s", (self.store.ns,)).fetchone()[0], 12000)
            self.assertEqual(conn.execute("SELECT count(*) FROM snapshot_state WHERE ns=%s AND dataset LIKE 'left_right_eod%%'",
                                          (self.store.ns,)).fetchone()[0], 0)
            lineage = conn.execute("""SELECT count(*),count(s.body) FROM left_right_assessments a
                LEFT JOIN snapshot_state s ON s.ns=a.ns AND s.dataset='left_right_evidence:'||a.evidence_sha256
                WHERE a.ns=%s AND s.body->'source_batches'=%s::jsonb AND s.body->>'trade_date'=%s
                  AND s.body->>'input_as_of'=%s AND s.body->>'evidence_sha256'=a.evidence_sha256""",
                (self.store.ns, encode(self.evidence["source_batches"]), str(DAY_BULK), AS_OF.isoformat())).fetchone()
            self.assertEqual(lineage, (12000, 12000))
        before = info.read_info(self.store, "q=100001.SZ")["items"][0]
        altered = deepcopy(self.evidence)
        altered["symbols"] = {"100000.SZ": dict(altered["symbols"][CODE], name="updated fixture")}
        changed = produce(DAY_BULK, self.calendar, AS_OF+timedelta(minutes=1), altered)
        persist_archive(self.store, changed, indexed_only=True)
        self.assertEqual(info.read_info(self.store, "q=100001.SZ")["items"][0], before)
        self.assertEqual(persist_archive(self.store, changed, indexed_only=True)["state"], "UNCHANGED")
        seed = produce(DAY_BULK, self.calendar, AS_OF,
                       dict(self.evidence, symbols={"999999.SZ": self.evidence["symbols"][CODE]}))
        with patch.object(info, "read_file", return_value=seed):
            seeded = info.read_info(self.store, "q=999999.SZ", eod_file="seed.json")
        self.assertEqual((seeded["total"], seeded["summary"]["total"]), (1, 6001))
        print("BULK_ARCHIVE_6000_STOCKS", round(time.monotonic()-started, 3), "seconds; 12000 saved; 100/page; 5/sector")

    def test_normalized_features_read_without_global_json_scan_and_missing_samples_bounded(self):
        with self.store._connect() as conn, conn.transaction():
            conn.execute("""INSERT INTO supplier_market_batches(batch_id,provider,endpoint,scope,collected_at,source_clock,row_count,body)
                VALUES(%s,'tushare','adj_factor','{"trade_date":"20000204"}',%s,%s,6000,'{"full_market":"not decoded by LR"}')""",
                (self.batch, AS_OF, AS_OF))
            conn.execute("""INSERT INTO supplier_lr_features(api,code,trade_date,body,batch_id)
                VALUES('adj_factor',%s,%s,'{"adj_factor":"2.5"}',%s),
                      ('daily_basic',%s,%s,'{"turnover_ratio":"0.01","volume_ratio":"1.2","circ_mv_cny":"1000000"}',%s),
                      ('cyq_perf',%s,%s,'{"cost85":"9.9"}',%s)""",
                (self.codes[0], DAY_BULK, self.batch)*3)
        evidence = _database_input(self.store, DAY_BULK, self.codes[:2], AS_OF)
        self.assertEqual(evidence["symbols"][self.codes[0]]["daily"][0]["adj_factor"], "2.5")
        self.assertEqual(evidence["symbols"][self.codes[0]]["daily"][0]["turnover_ratio"], "0.01")
        self.assertEqual(evidence["symbols"][self.codes[0]]["cost85"]["value"], "9.9")
        self.assertNotIn("adj_factor", evidence["symbols"][self.codes[1]]["daily"][0])
        def empty(_ledger, _day, codes, _as_of, **_kwargs):
            return dict(self.evidence, symbols={code: dict(daily=[]) for code in codes})
        with patch("modules.strategy_hermes.stage2.left_right_refresh._database_context", return_value={}), \
             patch("modules.strategy_hermes.stage2.left_right_refresh._database_input", side_effect=empty):
            result = build_manual_archives(self.store, [str(DAY_BULK)], None, AS_OF, persist=True)
        self.assertEqual((result["ready_items"], result["missing_count"], len(result["missing"])), (0, 6000, 100))
        self.assertLess(len(encode(result)), 15000)
        empty_day = DAY_BULK-timedelta(days=1)
        result = build_manual_archives(self.store, [str(empty_day)], None, AS_OF, persist=True)
        self.assertEqual(result["expected_items"], 2)
        self.assertGreater(result["missing_count"], 0)

    def test_cancel_after_compute_stops_before_archive_persistence(self):
        stop = {"requested": False}
        def checkpoint():
            if stop["requested"]:
                raise RuntimeError("cancelled")
        def calculate(*args):
            stop["requested"] = True
            return produce(*args)
        with patch("modules.strategy_hermes.stage2.left_right_refresh._database_context", return_value={}), \
             patch("modules.strategy_hermes.stage2.left_right_refresh._database_input", side_effect=self.reader), \
             patch("modules.strategy_hermes.stage2.left_right_refresh.produce", side_effect=calculate), \
             patch("modules.strategy_hermes.stage2.left_right_refresh.persist_archive") as writer:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                build_manual_archives(self.store, [str(DAY_BULK)], None, AS_OF, persist=True, checkpoint=checkpoint)
            writer.assert_not_called()

    def test_legacy_snapshot_other_stock_is_imported_before_subset_update(self):
        original = produce(DAY_BULK, self.calendar, AS_OF,
            dict(self.evidence, symbols={code: self.evidence["symbols"][CODE] for code in self.codes[:2]}))
        with self.store._connect() as conn:
            conn.execute("""INSERT INTO snapshot_state(ns,dataset,observed_at,revision,body)
                VALUES(%s,%s,%s,0,%s)""", (self.store.ns, "left_right_eod:"+str(DAY_BULK), AS_OF, encode(original)))
        newer = produce(DAY_BULK, self.calendar, AS_OF+timedelta(minutes=1),
            dict(self.evidence, symbols={self.codes[0]: dict(self.evidence["symbols"][CODE], name="new name")}))
        persist_archive(self.store, newer)
        rows = info.read_info(self.store)
        self.assertEqual(rows["total"], 2)
        retained = next(row for row in rows["items"] if row["ts_code"] == self.codes[1])
        self.assertEqual(retained["published_at"], AS_OF.isoformat())
        self.assertEqual(retained["setup_score"], "83.5593")
