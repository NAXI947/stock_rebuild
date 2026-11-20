"""Stored stock names supplement 046 names without changing sector evidence."""
from dataclasses import replace
from datetime import date, timedelta
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes.stage2.left_right_eod import _database_context, _database_input
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")
DAY = date(2003, 2, 7)
AS_OF = moment("2003-02-07T16:00:00+08:00")
CODE, OTHER = "989901.SZ", "989902.SH"


@unittest.skipUnless(ENV, "requires isolated PostgreSQL STAGE2_TEST_ENV_FILE")
class ReferenceNames(unittest.TestCase):
    def setUp(self):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, "test")
        self.store = PostgresLedger(replace(settings, namespace=settings.redis_prefix+"reference-names:"+uuid4().hex), "FROZEN")
        self.store.migrate()
        self.batches = []
        self.addCleanup(self.clean)

    def clean(self):
        with self.store._connect() as conn, conn.transaction():
            conn.execute("DELETE FROM supplier_reference_rows WHERE batch_id=ANY(%s)", (self.batches,))
            conn.execute("DELETE FROM supplier_market_batches WHERE batch_id=ANY(%s)", (self.batches,))

    def save(self, code, name, observed, business_date, dataset="stock_basic", **fields):
        batch = "reference-name-"+uuid4().hex
        self.batches.append(batch)
        with self.store._connect() as conn, conn.transaction():
            conn.execute("""INSERT INTO supplier_market_batches
                (batch_id,provider,endpoint,scope,collected_at,row_count,body)
                VALUES(%s,'tushare',%s,'{}',%s,1,'{}')""", (batch,dataset,observed))
            conn.execute("""INSERT INTO supplier_reference_rows
                (dataset,entity_key,business_date,observed_at,body,batch_id)
                VALUES(%s,%s,%s,%s,%s,%s)""", (dataset,code,business_date,observed,
                    encode(dict(ts_code=code,name=name,**fields)),batch))
        return batch

    def test_latest_visible_valid_name_reaches_evidence_and_source_batch(self):
        self.save(CODE,"旧名称",AS_OF-timedelta(days=2),DAY-timedelta(days=4))
        chosen = self.save(CODE,"  基础资料名称  ",AS_OF-timedelta(days=1),DAY-timedelta(days=3))
        self.save(CODE,"未来名称",AS_OF+timedelta(seconds=1),DAY-timedelta(days=2))
        self.save(CODE,"  ",AS_OF-timedelta(minutes=1),DAY-timedelta(days=1))
        evidence = _database_input(self.store,DAY,[CODE],AS_OF)
        asset = evidence["symbols"][CODE]
        self.assertEqual(asset["name"],"基础资料名称")
        self.assertEqual(asset["name_source"]["source_batch"],chosen)
        self.assertEqual(asset["name_source"]["available_at"],(AS_OF-timedelta(days=1)).isoformat())
        self.assertIn(chosen,evidence["source_batches"])
        self.assertEqual(set(evidence["source_batches"]) & set(self.batches),{chosen})

    def test_scheduled_refresh_scope_tuple_reads_the_same_names_as_a_list(self):
        # LeftRightRefresh keeps its configured codes as a tuple.
        self.save(CODE,"基础资料名称",AS_OF-timedelta(days=1),DAY-timedelta(days=3))
        listed = _database_input(self.store,DAY,[CODE],AS_OF)["symbols"][CODE]
        self.assertEqual(listed["name"],"基础资料名称")
        self.assertEqual(_database_input(self.store,DAY,(CODE,),AS_OF)["symbols"][CODE]["name"],listed["name"])

    def test_existing_046_name_keeps_precedence_and_does_not_claim_unused_source(self):
        batch = self.save(CODE,"基础资料名称",AS_OF-timedelta(days=1),DAY)
        with patch("modules.strategy_hermes.stage2.left_right_eod._membership_links",
                   return_value=({}, {CODE:"历史046名称"})):
            evidence = _database_input(self.store,DAY,[CODE],AS_OF)
        self.assertEqual(evidence["symbols"][CODE]["name"],"历史046名称")
        self.assertNotIn("name_source",evidence["symbols"][CODE])
        self.assertNotIn(batch,evidence["source_batches"])

    def test_reference_metadata_does_not_supply_sector_membership_or_relative_strength(self):
        self.save(CODE,"基础资料名称",AS_OF-timedelta(days=1),DAY,industry="银行")
        self.save(CODE,"行业名单名称",AS_OF-timedelta(hours=1),DAY,"index_member_all",l1_name="金融",l2_name="银行")
        context = _database_context(self.store,DAY,[CODE],AS_OF)
        self.assertEqual(context["names"][CODE],"基础资料名称")
        self.assertFalse(context["links"].get(CODE))
        self.assertNotIn(CODE,context["sector_values"])

    def test_shared_context_only_attaches_used_name_batch_to_each_chunk(self):
        chosen = self.save(CODE,"甲名称",AS_OF-timedelta(days=1),DAY)
        unrelated = self.save(OTHER,"乙名称",AS_OF-timedelta(days=1),DAY)
        context = _database_context(self.store,DAY,[CODE,OTHER],AS_OF)
        evidence = _database_input(self.store,DAY,[CODE],AS_OF,context=context)
        self.assertIn(chosen,evidence["source_batches"])
        self.assertNotIn(unrelated,evidence["source_batches"])
