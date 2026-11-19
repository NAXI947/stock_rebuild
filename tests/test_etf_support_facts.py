import os
import unittest
from unittest.mock import patch

from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes.stage2.etf_representativeness import freeze_policy
from modules.strategy_hermes.stage2.etf_support_facts import support_facts
from modules.strategy_hermes.stage2.penetration import freeze_pair_facts


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.at = moment("2026-11-18T10:35:00+08:00")
        self.code = "512480.SH"
        self.index = "H30184.CSI"
        stocks = [f"60000{i}.SH" for i in range(4)]
        self.mapping = dict(version="frozen", products={self.code: {"index_code": self.index}},
            sectors={sid:dict(members=members, as_of_date="2026-11-17", reported_count=2,
                unresolved_count=0, source_batches=["members"])
                for sid, members in {"a":stocks[:2], "b":stocks[1:3]}.items()},
            weights={self.index:dict(complete=True, as_of_date="2026-11-17",
                rows=[dict(code=stock, weight=weight) for stock, weight in zip(stocks, (".07",".27",".07",".59"))])},
            etf_taxonomy=dict(version="tax-v1", rows=[dict(sector_id=sid, index_code=self.index,
                family_id="semiconductor", canonical_branch_id="chips", available_at=self.at.isoformat(),
                evidence_refs=["taxonomy-review"]) for sid in ("a", "b")]))
        with patch.dict(os.environ, ETF_REPRESENTATIVENESS_MODE="V3_ENFORCE"):
            self.policy = freeze_policy(self.mapping, self.at)
        self.facts = freeze_pair_facts(self.mapping, self.at, ["a", "b"], self.policy)

    def project(self, a, b, *, chosen=False):
        report = dict(id="report-id", sectors={"a": {"state": a}, "b": {"state": b}})
        recs = dict(sectors={"a":{"products":[dict(etf=self.code, representative_current=chosen)]}})
        return support_facts(self.mapping, self.facts, self.policy, published=report, recommendations=recs)

    def test_t11_and_t13_union_deduplicates_members_and_does_not_choose(self):
        group, = self.project("UP", "HOLD")
        self.assertEqual((group["union_exposure"], group["branch_count"], group["selected_up_branch_count"]),
                         ("0.41", 1, 0))

    def test_t12_conflict_uses_the_same_branch_base(self):
        group, = self.project("UP", "WEAK", chosen=True)
        self.assertEqual((group["up_branch_count"], group["selected_up_branch_count"]), (1, 1))
        self.assertEqual(group["direction_conflicts"], {"chips":["UP","WEAK"]})
        self.mapping["etf_taxonomy"]["rows"][1]["family_id"] = "another-industry"
        self.assertEqual(len(self.project("UP", "UP")), 2)

    def test_t14_uses_only_effective_direction(self):
        group, = self.project("UNKNOWN", "EXIT", chosen=True)
        self.assertEqual((group["up_branch_count"], group["selected_up_branch_count"]), (0, 0))
        self.assertIsNone(group["union_exposure"])
