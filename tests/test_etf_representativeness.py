from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes.stage2.etf_representativeness import (
    evaluate_pair_representativeness, freeze_policy, shadow_result,
)
from modules.strategy_hermes.stage2.penetration import freeze_pair_facts
from modules.strategy_hermes.stage2.research_facts import ETF_SELECTION_CONTRACT, selection_detail


class PairTests(unittest.TestCase):
    def value(self, exposure, *, complete=True, proof=False, total="1", second="0", relation=True):
        weights = {"a": Decimal(exposure), "b": Decimal(second)}
        weights["other"] = Decimal(total) - sum(weights.values())
        return evaluate_pair_representativeness(
            {"a", "b", *(str(i) for i in range(8))}, weights, sector_members_complete=True,
            weights_complete_and_valid=complete, partial_upper_bound_proven=proof, explicit_relation=relation)

    def broad(self, size, **kwargs):
        members = {f"s{i}" for i in range(size)}
        weights = {"s0": Decimal(".4"), "s1": Decimal(".3"), "outside": Decimal(".3")}
        return evaluate_pair_representativeness(members, weights, sector_members_complete=True,
                                                weights_complete_and_valid=True, **kwargs)

    def test_broad_sector_needs_reviewed_attribution_for_entry(self):
        # Overlap alone is 70% here, exactly like an unrelated index inside a thousand-member concept sector.
        for size, kwargs, allowed in ((299, {}, True), (300, {}, False), (1026, {}, False),
                                      (1026, dict(attributed=True), True),
                                      (1026, dict(explicit_relation=True), True)):
            with self.subTest(size=size, **kwargs):
                result = self.broad(size, **kwargs)
                self.assertEqual(result["known_exposure"], "0.7")
                self.assertEqual(result["entry_allowed"], allowed)
                self.assertEqual(result["representativeness_status"],
                                 "RELEVANCE_NOT_REJECTED" if allowed else "BROAD_SECTOR_UNATTRIBUTED")

    def test_policy_frozen_before_the_broad_sector_rule_replays_unchanged(self):
        frozen = dict(parameters={"weak_exposure_floor": "0.03", "single_stock_guard_cap": "0.10",
                                  "single_stock_guard_min_members": 10, "core_exposure_tier_floor": "0.15"})
        self.assertTrue(self.broad(1026, policy=frozen)["entry_allowed"])

    def test_t03_decimal_floor_with_multiple_positive_constituents(self):
        for exposure, expected in ((".02899", False), (".029", True), (".02901", True)):
            with self.subTest(exposure=exposure):
                self.assertEqual(self.value(exposure, second=".001")["entry_allowed"], expected)

    def test_t29_single_stock_guard_ignores_zero_weight_and_uses_exclusive_cap(self):
        for ratio, allowed in ((".0076", False), (".03", False), (".099999", False), (".10", True)):
            with self.subTest(ratio=ratio):
                result = self.value(ratio)
                self.assertEqual((result["matched_count"], result["positive_weight_matched_count"]), (2, 1))
                self.assertEqual(result["entry_allowed"], allowed)
                if Decimal(".03") <= Decimal(ratio) < Decimal(".10"):
                    self.assertEqual(result["representativeness_status"], "SINGLE_STOCK_CROSSOVER_REJECTED")

    def test_t30_upper_bound_proof_and_strict_boundary(self):
        for proof, total, expected in ((True, ".982", "0.0256"), (True, ".9776", "0.0300"),
                                       (False, ".982", None), (True, "1.001", None)):
            with self.subTest(proof=proof, total=total):
                result = self.value(".0076", total=total, complete=False, proof=proof)
                self.assertEqual(result["max_possible_exposure"], expected)
                self.assertEqual(result["entry_allowed"], total != ".982" or not proof)
        missing_members = evaluate_pair_representativeness({"a"}, {"a": Decimal(".0076"), "b": Decimal(".98")},
            sector_members_complete=False, weights_complete_and_valid=False, partial_upper_bound_proven=True)
        self.assertIsNone(missing_members["max_possible_exposure"])

    def test_t04_and_t05_missing_inputs(self):
        partial = self.value(".04", total=".982", complete=False)
        self.assertEqual((partial["known_exposure"], partial["exposure_basis"]), ("0.04", "KNOWN_LOWER_BOUND"))
        for linked in (False, True):
            with self.subTest(linked=linked):
                result = evaluate_pair_representativeness({"a"}, {}, sector_members_complete=True,
                    weights_complete_and_valid=False, explicit_relation=linked)
                self.assertEqual(result["entry_allowed"], linked)
                for key in ("known_exposure", "max_possible_exposure", "matched_count",
                            "positive_weight_matched_count", "reverse_coverage"):
                    self.assertIsNone(result[key])

    def test_t06_explicit_link_does_not_override_zero(self):
        result = self.value("0")
        self.assertEqual(result["reason_codes"], ["NO_OVERLAP_CONFIRMED", "EXPLICIT_RELATION_CONFLICT"])

    def test_t15_and_t28_selection_validation_precedes_stripping(self):
        rejected, allowed = "512480.SH", "512760.SH"
        evidence = dict(etf_selection_contract=ETF_SELECTION_CONTRACT, etf_options={
            "products": [{"etf": allowed}],
            "observation_products": [{"etf": rejected, "representativeness": {
                "representativeness_status": "WEAK_OVERLAP_REJECTED"}}]})
        choices, errors, audit = selection_detail({"etf_choices": [rejected]}, evidence)
        self.assertEqual((choices, errors, audit["reason"]), ([], [], "EMPTY_AFTER_WEAK_PAIR_STRIP"))
        self.assertEqual(selection_detail({"etf_choices": []}, evidence), ([], [], None))
        self.assertEqual(selection_detail({"etf_choices": [rejected, allowed]}, evidence)[0], [allowed])
        for invalid in ([rejected, "999999.SH"], [rejected, rejected], [1], "512480.SH"):
            with self.subTest(invalid=invalid):
                self.assertEqual(selection_detail({"etf_choices": invalid}, evidence),
                                 (None, ["ETF_SELECTION_UNCONFIRMED"], None))

    def test_t07_and_t27_frozen_clock_and_policy(self):
        at = moment("2026-11-18T10:00:00+08:00")
        stock, index, code, sid = "600000.SH", "000001.CSI", "512480.SH", "kaipanla:1"
        mapping = dict(version="v1", sectors={sid:dict(members=[stock], as_of_date="2026-11-17",
            reported_count=1, unresolved_count=0, source_batches=["source"])},
            weights={index:dict(as_of_date="2026-11-17", available_at="2026-11-18T11:00:00+08:00",
                                rows=[dict(code=stock, weight="1")], complete=True)},
            products={code:dict(index_code=index)})
        with patch.dict(os.environ, ETF_REPRESENTATIVENESS_MODE="V3_ENFORCE"):
            policy = freeze_policy(mapping, at)
        self.assertEqual(freeze_pair_facts(mapping, at, [sid], policy), {sid:{}})
        mapping["weights"][index]["available_at"] = "2026-11-18T09:00:00+08:00"
        with patch.dict(os.environ, ETF_REPRESENTATIVENESS_MODE="V2"):
            fact = freeze_pair_facts(mapping, at, [sid], policy)[sid][code]
        self.assertEqual((fact["policy_mode"], fact["known_exposure"]), ("V3_ENFORCE", "1"))

    def test_t32_shadow_rejects_unserializable_projection(self):
        self.assertEqual(shadow_result("PREPARE", lambda: {"partial": object()}),
                         (None, {"stage":"PREPARE", "error":"TypeError"}))

    def test_t32_shadow_rejects_serializable_shape_errors(self):
        for stage, invalid in (("PREPARE", {"unknown": "not a pair group"}),
                               ("RECOMMEND", {"recommendations": {"candidates": "not an array"}}),
                               ("FINALIZE", {"policy": {}, "pairs": {}, "current_support": [], "statistics": "bad"})):
            with self.subTest(stage=stage):
                self.assertEqual(shadow_result(stage, lambda: invalid),
                                 (None, {"stage":stage, "error":"ValueError"}))

    def test_t10_cni_completeness_uses_existing_index_weight_rows(self):
        from modules.strategy_hermes.stage2.cni_weights import apply_reference
        from modules.strategy_hermes.stage2.mapping_maintenance import weight_basis

        day = moment("2026-10-30T00:00:00+08:00").date()
        rows = [dict(index_code="399006.SZ", con_code=f"{300000+i:06}.SZ", trade_date="20261030", weight="1")
                for i in range(100)]
        for count in (99, 100):
            with self.subTest(count=count):
                basis = weight_basis(rows[:count], "399006.SZ", day, day, {})
                apply_reference(basis, "399006.CNI")
                self.assertEqual(basis["complete"], count == 100)
                self.assertEqual(basis["index_identity"]["provider_index_code"], "399006")
                self.assertEqual(basis["source"], "tushare:index_weight")

    def test_t26_deferred_and_known_missing_do_not_reuse_attempts(self):
        from modules.strategy_hermes.stage2.reference_policy import ReferencePolicy

        mapping = {}
        at = moment("2026-11-18T10:00:00+08:00")
        morning = ReferencePolicy(mapping, at, "market")
        morning.defer("weights:queued", "2026-11-17")
        morning.begin("weights:unavailable", "2026-11-17")
        morning.failed("weights:unavailable", {"error_kind":"CONFIRMED_SOURCE_MISSING"})
        morning.begin("weights:network", "2026-11-17")
        morning.failed("weights:network", {"error_kind":"CONNECTION_FAILED"})
        afternoon = ReferencePolicy(mapping, at.replace(hour=14, minute=15), "retry")
        self.assertTrue(afternoon.due("weights:queued"))
        self.assertTrue(afternoon.due("weights:network"))
        self.assertFalse(afternoon.due("weights:unavailable"))
        self.assertEqual(afternoon.pending["weights:queued"]["attempts"], 0)
