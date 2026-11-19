"""ETF V3.2 savepoints and primary-source policy against PostgreSQL."""
import os
import unittest
from unittest.mock import patch

from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes.stage2.etf_representativeness import freeze_policy
from modules.strategy_hermes.stage2.worker import planned_at
from test_stage1 import A, B, D1, DAYS, ETF, report, stamp
import test_stage2_postgres as fixtures


@unittest.skipUnless(os.environ.get("STAGE2_TEST_ENV_FILE"), "requires isolated PostgreSQL")
class V32Publish(unittest.TestCase):
    setUp = fixtures.StageTwo.setUp
    clean = fixtures.StageTwo.clean

    def publish(self, mode, *, error=False, projected=None):
        when = moment(stamp(D1, "09:35"))
        task = self.store.claim_round(D1, "09:35", planned_at(D1, "09:35"))
        event = report("v32-"+mode, D1, "09:35", [(A,"新增"), (B,"新增")],
            [(ETF,A,1), (ETF,B,2)])
        source = dict(sectors={}, candidates=[])
        policy = freeze_policy(self.map, when, mode=mode)
        context = dict(mapping=self.map, policy=policy, recommendations=source,
            pairs={B:{ETF:{"exposure_tier":"CORE_REPRESENTATIVE"}}})
        metadata = dict(etf_recommendations=source)

        def project(conn, *_):
            if error:
                conn.execute("SELECT 1/0")
            return {"checked":True} if projected is None else projected

        with patch("modules.strategy_hermes.stage2.etf_support_facts.finalize_support_facts", side_effect=project):
            saved = self.store.publish(task, event, DAYS, {ETF:100}, metadata,
                                       targets=("redis",), etf_projection=context)
        return saved, self.store.inspect()["cycles"][0]

    def test_t31_same_round_core_source_has_priority(self):
        saved, cycle = self.publish("V3_ENFORCE")
        self.assertEqual(cycle["primary_sector"], B)
        self.assertEqual(saved["input_meta"]["representation"], {"checked":True})

    def test_t32_sql_failure_rolls_back_only_shadow(self):
        saved, cycle = self.publish("V3_SHADOW", error=True)
        self.assertEqual(cycle["primary_sector"], A)
        self.assertEqual(saved["input_meta"]["representation_shadow_error"],
                         dict(stage="FINALIZE", error="DivisionByZero"))
        self.assertNotIn("representation_shadow", saved["input_meta"])
        with self.store._connect() as conn:
            count, = conn.execute("SELECT count(*) FROM outbox WHERE ns=%s AND report_id=%s",
                                  (self.ns, saved["id"])).fetchone()
        self.assertEqual(count, 1)

    def test_t32_serializable_invalid_summary_is_excluded_from_the_archived_report(self):
        saved, cycle = self.publish("V3_SHADOW", projected={"statistics": "invalid"})
        self.assertEqual(cycle["primary_sector"], A)
        self.assertEqual(cycle["orders"][0]["action"], "BUY")
        self.assertEqual(saved["input_meta"]["representation_shadow_error"], dict(stage="FINALIZE", error="ValueError"))
        self.assertNotIn("representation_shadow", self.store.report(saved["id"])["input_meta"])
