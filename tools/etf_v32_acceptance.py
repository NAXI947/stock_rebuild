"""ETF V3.2 specification checks, including checks that can identify release gaps."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import os
import unittest
from unittest.mock import patch

from modules.strategy_hermes import product_materials
from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes.stage2 import native_rounds, worker
from modules.strategy_hermes.stage2.etf_representativeness import (
    evaluate_pair_representativeness, freeze_policy, shadow_result,
)
from modules.strategy_hermes.stage2.etf_support_facts import coverage_statistics
from modules.strategy_hermes.stage2.mapping_maintenance import _priorities, weight_basis
from modules.strategy_hermes.stage2.penetration import (
    evaluate_recommendations, freeze_pair_facts, prepare_etf_facts,
)
from modules.strategy_hermes.stage2.research_facts import fact_catalog

import test_etf_direction_policy as direction
import test_etf_recommendations as recommendations
import test_etf_support_facts as support
import test_entry_admission as admission_tests
import test_etf_maintenance as maintenance_tests
import test_native_rounds as native_tests
import test_product_verification as verification_tests
import test_research_facts as research
import test_stage1 as stage1
import test_stage2_postgres as postgres_tests


AT = direction.AT
SID = direction.SID
ETF = direction.ETF
OTHER = recommendations.OTHER
STOCK = recommendations.CODES[0]
OUTSIDE = "600099.SH"
CHIP = "kaipanla:801001"


def policy_pair(mapping, sectors, at=AT, mode="V3_ENFORCE"):
    policy = freeze_policy(mapping, at, mode=mode)
    return policy, freeze_pair_facts(mapping, at, sectors, policy)


def evaluated(market, mapping, choices, *, states=None, event_members=None):
    states = states or [(sid, "新增") for sid in choices]
    _, pairs = policy_pair(mapping, [sid for sid, _ in states])
    source = stage1.report("acceptance", AT.date().isoformat(), "14:05", states, published="14:06")
    return evaluate_recommendations(source, mapping, market, etf_choices=choices,
                                    representation=pairs, event_members=event_members)


def formal(mapping, sid, stocks):
    mapping["sectors"][sid] = dict(
        members=stocks, reported_count=len(stocks), unresolved_count=0,
        source_batches=["fixture"], as_of_date="2026-11-12",
    )


def source_weights(mapping, entries, *, complete, proof=None):
    row = dict(as_of_date="2026-11-12", available_at="2026-11-13T09:00:00+08:00",
               complete=complete, rows=[dict(code=a, weight=b) for a, b in entries.items()])
    if proof:
        row["absolute_weight_proof"] = dict(index_code="index", as_of_date=row["as_of_date"],
                                            scope=proof, source_ref="fixture")
    mapping["weights"]["index"] = row


def explicit(mapping):
    mapping["sector_index_relations"] = [
        dict(sector_id=SID, index_code="index", verified=True,
             evidence_refs=["fixture"], available_at="2026-11-13T09:00:00+08:00"),
    ]


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.mapping, self.market = recommendations.fixture()

    def test_t25_on_released_entry_path(self):
        # Specification section 5 permits absent daily absolute price limits.
        expected = [dict(etf=ETF, sector_id=SID, rank=1, mapping_ref="fixture")]
        for day in (None, "2026-11-13", "2026-11-12"):
            with self.subTest(date=day):
                market = deepcopy(self.market)
                if day:
                    market.body["static_price_limits"] = {ETF: dict(
                        trade_date=day, observed_at=day+"T09:35:00+08:00",
                        up_limit="1", down_limit="0.9",
                    )}
                result = evaluated(market, self.mapping, {SID: [ETF]})
                self.assertEqual(result["candidates"], expected)
                self.assertEqual(result["sectors"][SID]["products"][0]["execution_eligibility"], "READY")

    def test_t01_t02_one_weak_and_one_allowed_sector_for_same_etf(self):
        new_etf = "512480.SH"
        self.mapping["products"] = {new_etf: self.mapping["products"][ETF]}
        self.market.body["quotes"][new_etf] = self.market.body["quotes"][ETF]
        formal(self.mapping, SID, [STOCK])
        core = recommendations.CODES[1]
        formal(self.mapping, CHIP, [core])
        source_weights(self.mapping, {STOCK: ".0076", core: ".52", OUTSIDE: ".4724"}, complete=True)
        _, pairs = policy_pair(self.mapping, [SID, CHIP])
        options = prepare_etf_facts(self.mapping, self.market, AT, [SID, CHIP], representation=pairs)
        self.assertEqual(options[SID]["observation_products"][0]["etf"], new_etf)
        self.assertEqual(pairs[SID][new_etf]["known_exposure"], "0.0076")
        result = evaluated(self.market, self.mapping, {SID: [new_etf], CHIP: [new_etf]})
        self.assertEqual(result["sectors"][SID]["direction"], "NEW")
        self.assertEqual([item["sector_id"] for item in result["candidates"]], [CHIP])

    def test_t04_t05_t06_partial_absent_and_conflicting_weights(self):
        explicit(self.mapping)
        formal(self.mapping, SID, [STOCK])
        for exposure, status in ((".0076", "REPRESENTATIVENESS_UNKNOWN"), (".04", "RELEVANCE_NOT_REJECTED")):
            with self.subTest(exposure=exposure):
                source_weights(self.mapping, {STOCK: exposure, OUTSIDE: ".90"}, complete=False)
                fact = policy_pair(self.mapping, [SID])[1][SID][ETF]
                self.assertEqual(fact["representativeness_status"], status)
                self.assertEqual(Decimal(fact["known_exposure"]), Decimal(exposure))
                self.assertEqual(fact["exposure_basis"], "KNOWN_LOWER_BOUND")
        source_weights(self.mapping, {STOCK: ".0076", OUTSIDE: ".9924"}, complete=True)
        weak = policy_pair(self.mapping, [SID])[1][SID][ETF]
        self.assertEqual(weak["reason_codes"], ["WEAK_OVERLAP_REJECTED"])
        self.assertFalse(weak["entry_allowed"])
        self.mapping["weights"].clear()
        known = policy_pair(self.mapping, [SID])[1][SID][ETF]
        self.assertEqual((known["exposure_basis"], known["exposure_tier"]), ("UNAVAILABLE", "UNKNOWN"))
        self.assertIsNone(known["known_exposure"])
        self.assertTrue(known["entry_allowed"])
        self.mapping["sector_index_relations"].clear()
        self.mapping["products"][ETF]["name"] = SID
        self.assertEqual(policy_pair(self.mapping, [SID])[1][SID], {})
        no_association = evaluate_pair_representativeness(
            [STOCK], {}, sector_members_complete=True, weights_complete_and_valid=False)
        self.assertEqual(no_association["representativeness_status"], "ASSOCIATION_UNCONFIRMED")

    def test_t07_duplicate_members_cross_date_percent_and_future(self):
        sample = AT.date()
        weights = weight_basis([
            dict(con_code=STOCK, trade_date="20261112", weight="40", index_code="index"),
            dict(con_code=OUTSIDE, trade_date="20261112", weight="60", index_code="index"),
            dict(con_code=STOCK, trade_date="20261111", weight="100", index_code="index"),
        ], "index", sample-timedelta(days=5), sample, {})
        self.assertEqual(weights["as_of_date"], "2026-11-12")
        self.assertEqual({r["code"]: r["weight"] for r in weights["rows"]}, {STOCK: "0.4", OUTSIDE: "0.6"})
        formal(self.mapping, SID, [STOCK, STOCK])
        self.mapping["weights"]["index"] = {**weights, "complete": True}
        fact = policy_pair(self.mapping, [SID])[1][SID][ETF]
        self.assertEqual(fact["matched_count"], 1)
        self.assertEqual(fact["exposure_basis"], "KNOWN_LOWER_BOUND")
        self.mapping["weights"]["index"]["available_at"] = (AT+timedelta(minutes=1)).isoformat()
        self.assertEqual(policy_pair(self.mapping, [SID])[1][SID], {})

    def test_t08_low_verified_adv_remains_reference(self):
        product = self.mapping["products"][ETF]
        product["rule"]["price_limit_ratio"] = ".2"
        product["recommendation_stats"].update(adv20="1", adv5="1")
        for row in self.mapping["weights"]["index"]["rows"]:
            row["mainboard"] = False
        result = evaluated(self.market, self.mapping, {SID: [ETF]})
        row, = result["sectors"][SID]["products"]
        self.assertEqual(row["execution_eligibility"], "READY")
        _, pairs = policy_pair(self.mapping, [SID])
        options = prepare_etf_facts(self.mapping, self.market, AT, [SID], representation=pairs)
        self.assertEqual(options[SID]["products"][0]["reference_stats"]["adv20"]["value"], "1")
        self.assertEqual(len(result["candidates"]), 1)

    def test_t09_full_weights_never_approve_unverified_product(self):
        formal(self.mapping, SID, [STOCK])
        source_weights(self.mapping, {STOCK: "1"}, complete=True)
        for field, value in (("scope", None), ("settlement_cycle", "T+0"), ("verified", False)):
            with self.subTest(field=field):
                mapping = deepcopy(self.mapping)
                mapping["products"][ETF]["rule"][field] = value
                self.assertEqual(policy_pair(mapping, [SID])[1][SID][ETF]["known_exposure"], "1")
                result = evaluated(self.market, mapping, {SID: [ETF]})
                self.assertEqual(result["candidates"], [])
                self.assertNotEqual(result["sectors"][SID]["products"][0]["execution_eligibility"], "READY")

    def test_t13_two_branches_union_and_no_order_candidate(self):
        case = support.SupportTests()
        case.setUp()
        case.mapping["etf_taxonomy"]["rows"][1]["canonical_branch_id"] = "independent"
        group, = case.project("UP", "UP")
        self.assertEqual((group["union_exposure"], group["branch_count"], group["selected_up_branch_count"]),
                         ("0.41", 2, 0))
        self.assertEqual(evaluated(self.market, self.mapping, {SID: []})["candidates"], [])

    def test_t14_duplicate_strength_does_not_create_effective_up_support(self):
        from modules.strategy_hermes.rotation_chain import advance

        case = support.SupportTests()
        case.setUp()
        a = dict(phase="NEW", state="UP")
        first, _ = advance(None, {"id": "entry", "evidence_key": "source"}, "a", a)
        _, repeated = advance(first, {"id": "repeat", "evidence_key": "source"}, "a",
                              dict(phase="STRENGTHEN", state="UP"))
        _, inactive = advance(None, {"id": "orphan", "evidence_key": "source"}, "b",
                              dict(phase="STRENGTHEN", state="UP"))
        self.assertTrue(repeated["duplicate_evidence"])
        self.assertEqual((repeated["state"], inactive["state"]), ("HOLD", "UNKNOWN"))
        group, = case.project(repeated["state"], inactive["state"], chosen=True)
        self.assertEqual((group["up_branch_count"], group["selected_up_branch_count"]), (0, 0))

    def test_t15_and_t28_weak_choice_does_not_change_independent_checks(self):
        weak = "512480.SH"
        weak_fact = dict(known_exposure=".0076", exposure_basis="COMPLETE",
                         representativeness_status="WEAK_OVERLAP_REJECTED")
        ctx = research.context()
        ctx["etf_options"]["observation_products"] = [
            dict(etf=weak, representativeness=weak_fact), dict(etf=OTHER),
        ]
        ctx["fact_catalog"] = fact_catalog(ctx, {})
        decision = research.decision(ctx)
        decision["etf_choices"] = [weak]
        decision["research"] = research.research("板块和512480.SH走弱")
        fact_id = "etf_exposure_512480_SH"
        decision["fact_refs"].append(fact_id)
        saved = research.rendered(decision, ctx)
        self.assertEqual(saved["normalized"][research.BANK]["phase"], "WEAKEN")
        self.assertEqual(saved["etf_choices"], [])
        self.assertEqual(saved["etf_selection_audit"]["reason"], "EMPTY_AFTER_WEAK_PAIR_STRIP")
        self.assertIn("0.76%", saved["research"]["rationale"])
        bad = research.rendered({**decision, "research": research.research("暴露0.76%")}, ctx)
        self.assertEqual(bad["judgment_review"], ["RATIONALE_NUMERIC_FACT_UNBOUND"])
        self.assertEqual(bad["normalized"][research.BANK]["phase"], "UNKNOWN")
        for bad_choices in ([weak, OTHER], [weak]+[ETF]*4, [weak, "159999.SZ"]):
            with self.subTest(choices=bad_choices):
                bad = research.rendered({**decision, "etf_choices": bad_choices}, ctx)
                self.assertEqual(bad["etf_selection_review"], ["ETF_SELECTION_UNCONFIRMED"])
                self.assertEqual(bad["normalized"][research.BANK]["phase"], "WEAKEN")
        bad = research.rendered({**decision, "fact_refs": ["foreign"]}, ctx)
        self.assertEqual(bad["normalized"][research.BANK]["phase"], "UNKNOWN")
        from modules.strategy_hermes.stage2.octop_decision import render
        invalid = json.dumps(decision)[:-1]
        with self.assertRaises(ValueError):
            render(dict(report_id="facts-report", generated_at=research.AT, finish_reason="stop", output=invalid), ctx)

    def test_t24_three_independent_counting_fixtures(self):
        for kind in ("dedup", "lower_bound", "explicit"):
            with self.subTest(kind=kind):
                mapping = deepcopy(self.mapping)
                market = deepcopy(self.market)
                sectors = [SID]
                if kind == "dedup":
                    sectors += [CHIP, "kaipanla:800001"]
                    for sid in sectors[1:]:
                        formal(mapping, sid, recommendations.CODES[:3])
                    mapping["products"][OTHER] = deepcopy(mapping["products"][ETF])
                    market.body["quotes"][OTHER] = market.body["quotes"][ETF]
                elif kind == "lower_bound":
                    source_weights(mapping, {STOCK: ".15", OUTSIDE: ".8"}, complete=False)
                else:
                    mapping["weights"].clear()
                    explicit(mapping)
                _, pairs = policy_pair(mapping, sectors)
                result = evaluated(market, mapping, {sid: [ETF] for sid in sectors})
                stats = coverage_statistics(mapping, pairs, {"sectors": {s: {"state": "UP"} for s in sectors}},
                                            result, [], AT, sectors)
                count = len(sectors)
                self.assertEqual(stats["sectors"]["with_selectable"], count)
                self.assertEqual(stats["indexes"]["total"], 1)
                self.assertEqual(stats["products"]["total"], 2 if kind == "dedup" else 1)
                self.assertEqual(stats["products"]["ai_selected"], 1)
                self.assertEqual(stats["pairs"]["selectable"], 6 if kind == "dedup" else 1)
                self.assertEqual(sum(stats["pairs"]["selectable_by_tier"].values()), stats["pairs"]["selectable"])
                self.assertEqual(stats["orders"]["total"], 0)
                if kind == "lower_bound":
                    self.assertEqual(stats["pairs"]["unproven_upper_bound"], 1)
                    self.assertEqual(stats["pairs"]["selectable_by_tier"]["core"], 1)
                    self.assertNotIn("REPRESENTATIVENESS_UNKNOWN", stats["pairs"]["by_status"])
                if kind == "explicit":
                    self.assertEqual(stats["pairs"]["selectable_by_tier"]["unknown"], 1)
                    self.assertEqual(stats["indexes"]["weights_available"], 0)

    def test_t23_only_weak_sector_source_is_removed_in_the_three_mode_fixture(self):
        formal(self.mapping, SID, [STOCK])
        core = recommendations.CODES[1]
        formal(self.mapping, CHIP, [core])
        source_weights(self.mapping, {STOCK: ".0076", core: ".52", OUTSIDE: ".4724"}, complete=True)
        event = stage1.report("t23", AT.date().isoformat(), "14:05", [(s, "新增") for s in (SID, CHIP)],
                              published="14:06")
        for mode, sources in (("V2", [SID, CHIP]), ("V3_SHADOW", [SID, CHIP]), ("V3_ENFORCE", [CHIP])):
            with self.subTest(mode=mode):
                _, pairs = policy_pair(self.mapping, [SID, CHIP], mode=mode)
                result = evaluate_recommendations(
                    event, self.mapping, self.market, etf_choices={SID:[ETF], CHIP:[ETF]},
                    representation=pairs if mode == "V3_ENFORCE" else None)
                self.assertEqual({row["sector_id"] for row in result["candidates"]}, set(sources))
                self.assertEqual({row["direction"] for row in result["sectors"].values()}, {"NEW"})

    def test_t26f_seed_gates_are_independent_of_trading_rules(self):
        self.mapping["products"][OTHER] = dict(index_code="missing", rule=dict(verified=False))
        enabled = "159002.SZ"
        off = "159003.SZ"
        unlisted = "159004.SZ"
        for code in (enabled, off, unlisted):
            self.mapping["products"][code] = deepcopy(self.mapping["products"][OTHER])
        self.mapping["products"][ETF]["rule"]["verified"] = False
        self.mapping["etf_material_seeds"] = dict(
            seed_config_version="version", source_ref="fixture",
            targets=[dict(etf=enabled, materials_enabled=True, reason="fixture"),
                     dict(etf=off, materials_enabled=False, reason="fixture")],
        )
        with patch("modules.strategy_hermes.stage2.mapping_maintenance._priorities",
                   return_value=([], [SID])), patch.object(product_materials, "_target_history", return_value=({}, set())):
            selected = {c for c, _ in product_materials.associated_targets(None, self.mapping, AT)}
            self.assertEqual(selected, {ETF, enabled})
            result = evaluated(self.market, self.mapping, {SID: [ETF, enabled]})
            self.assertEqual(result["candidates"], [])
            with patch.dict(os.environ, ETF_REPRESENTATIVENESS_MODE="V3_ENFORCE"), \
                 patch("modules.strategy_hermes.product_materials.Path.read_text", side_effect=FileNotFoundError):
                self.mapping.pop("etf_material_seeds")
                selected = {c for c, _ in product_materials.associated_targets(None, self.mapping, AT)}
                self.assertEqual(selected, {ETF})

    def test_t29_35_members_9_10_boundaries_and_partial_inputs(self):
        for size, exposure, members_complete, weights_complete, expected in (
            (35, ".068", True, True, True), (9, ".03", True, True, False),
            (9, ".099999", True, True, False), (9, ".10", True, True, False),
            (10, ".03", True, True, True), (10, ".099999", True, True, True),
            (10, ".10", True, True, False), (35, ".068", False, True, False),
            (35, ".068", True, False, False),
        ):
            with self.subTest(size=size, exposure=exposure, complete=(members_complete, weights_complete)):
                codes = ["600%03d.SH" % i for i in range(size)]
                raw = {codes[0]: Decimal(exposure), codes[1]: Decimal(0),
                       OUTSIDE: Decimal(1)-Decimal(exposure)}
                fact = evaluate_pair_representativeness(codes, raw, sector_members_complete=members_complete,
                                                        weights_complete_and_valid=weights_complete)
                self.assertEqual((fact["matched_count"], fact["positive_weight_matched_count"]), (2, 1))
                self.assertEqual(fact["representativeness_status"] == "SINGLE_STOCK_CROSSOVER_REJECTED", expected)

    def test_t29d_one_formal_fact_across_model_options_recommendation_and_finalization(self):
        codes = ["600%03d.SH" % i for i in range(35)]
        formal(self.mapping, SID, codes)
        source_weights(self.mapping, {codes[0]: ".068", codes[1]: "0", OUTSIDE: ".932"}, complete=True)
        policy, pairs = policy_pair(self.mapping, [SID])
        facts = prepare_etf_facts(self.mapping, self.market, AT, [SID], representation=pairs)
        shown = facts[SID]["observation_products"][0]["representativeness"]
        self.assertEqual(shown["sector_member_count"], 35)
        event_facts = {SID: [OUTSIDE, "600098.SH", "600097.SH", "600096.SH", "600095.SH"]}
        result = evaluated(self.market, self.mapping, {SID: [ETF]}, event_members=event_facts)
        self.assertEqual(result["candidates"], [])
        stats = coverage_statistics(self.mapping, pairs, {"sectors": {SID: {"state": "UP"}}},
                                    result, [], moment(policy["data_cutoff"]))
        self.assertEqual(stats["pairs"]["by_status"], {"SINGLE_STOCK_CROSSOVER_REJECTED": 1})

    def test_t30_proven_edges_missing_members_and_bad_weight_basis(self):
        formal(self.mapping, SID, [STOCK, recommendations.CODES[1]])
        for total, upper in ((".982", ".0256"), (".97761", ".02999"), (".9776", ".0300"), (".97759", ".03001")):
            with self.subTest(total=total):
                known = Decimal(".0076")
                source_weights(self.mapping, {STOCK: str(known), OUTSIDE: str(Decimal(total)-known)},
                               complete=False, proof="FULL_INDEX_ABSOLUTE")
                fact = policy_pair(self.mapping, [SID])[1][SID][ETF]
                self.assertEqual((fact["upper_bound_status"], fact["max_possible_exposure"]), ("PROVEN", str(Decimal(upper))))
                self.assertEqual(fact["representativeness_status"] == "WEAK_OVERLAP_UPPER_BOUND_REJECTED",
                                 Decimal(upper) < Decimal(".03"))
                self.assertEqual(fact["matched_count"], 1)
        self.mapping["sectors"][SID]["unresolved_count"] = 1
        source_weights(self.mapping, {STOCK: ".0076", OUTSIDE: ".9924"}, complete=True,
                       proof="FULL_INDEX_ABSOLUTE")
        fact = policy_pair(self.mapping, [SID])[1][SID][ETF]
        self.assertEqual((fact["upper_bound_status"], fact["max_possible_exposure"]), ("UNPROVEN", None))
        self.mapping["sectors"][SID]["unresolved_count"] = 0
        for excess, scope in (("1.001", "FULL_INDEX_ABSOLUTE"), ("1", "NORMALIZED_SUBSET")):
            source_weights(self.mapping, {STOCK: ".0076", OUTSIDE: str(Decimal(excess)-Decimal(".0076"))},
                           complete=False, proof=scope)
            fact = policy_pair(self.mapping, [SID])[1][SID][ETF]
            self.assertEqual((fact["upper_bound_status"], fact["max_possible_exposure"]), ("UNPROVEN", None))
        explicit(self.mapping)
        self.mapping["weights"].clear()
        fact = policy_pair(self.mapping, [SID])[1][SID][ETF]
        self.assertEqual((fact["upper_bound_status"], fact["exposure_basis"], fact["exposure_tier"]),
                         ("UNPROVEN", "UNAVAILABLE", "UNKNOWN"))
        for field in ("known_exposure", "matched_count", "positive_weight_matched_count", "reverse_coverage"):
            self.assertIsNone(fact[field])
        self.assertIn("REPRESENTATIVENESS_UNKNOWN", fact["reason_codes"])

    def test_t30d_missing_weight_preserves_only_the_known_count(self):
        absent = recommendations.CODES[1]
        formal(self.mapping, SID, [STOCK, absent])
        source_weights(self.mapping, {STOCK: ".15", OUTSIDE: ".8"}, complete=False)
        _, pairs = policy_pair(self.mapping, [SID])
        facts = prepare_etf_facts(self.mapping, self.market, AT, [SID], representation=pairs)
        item, = facts[SID]["products"]
        self.assertEqual(item["matched_count"], 1)
        self.assertEqual(item["representativeness"]["known_exposure"], "0.15")
        self.assertEqual(item["representativeness"]["positive_weight_matched_count"], 1)
        self.assertEqual(len(item["matched"]), 1)
        codes = [f"600{i:03}.SH" for i in range(30, 40)]
        formal(self.mapping, SID, [*codes, absent])
        source_weights(self.mapping, {**dict.fromkeys(codes, ".02"), OUTSIDE: ".7"}, complete=False)
        _, pairs = policy_pair(self.mapping, [SID])
        facts = prepare_etf_facts(self.mapping, self.market, AT, [SID], representation=pairs)
        item, = facts[SID]["products"]
        self.assertEqual(item["matched_count"], 10)
        self.assertEqual(len(item["matched"]), 8)

    def test_t32d_serializable_invalid_fields_are_rejected(self):
        # Non-objects and non-serializable values are existing guards. A JSON
        # object with a non-contract field also needs to be discarded.
        self.assertEqual(shadow_result("PREPARE", lambda: object())[1]["error"], "ValueError")
        self.assertEqual(shadow_result("PREPARE", lambda: {"broken": object()})[1]["error"], "TypeError")
        result, error = shadow_result("PREPARE", lambda: {"unexpected_field": "accepted as a dict"})
        self.assertIsNone(result)
        self.assertIsNotNone(error)


class TradingAcceptance(unittest.TestCase):
    setUp = stage1.StageOne.setUp
    replay = stage1.StageOne.replay
    mixed_lots = stage1.StageOne.mixed_lots

    def test_t17_t18_keep_original_signal_source(self):
        a, b, code = stage1.A, stage1.B, stage1.ETF
        d1, d2 = stage1.D1, stage1.D2
        events = [
            stage1.report("entry", d1, "09:35", [(a, "新增"), (b, "新增")], [(code, a, 1), (code, b, 2)]),
            stage1.quote("first-price", d1, "09:36", "10"),
            stage1.report("weak", d1, "10:35", [(a, "减弱"), (b, "加强")]),
            stage1.report("weak-again", d1, "13:05", [(a, "减弱"), (b, "加强")]),
        ]
        before = self.replay(events)["cycles"][0]
        self.assertEqual([(o["action"], o["target"]) for o in before["orders"]], [("BUY", 1000), ("REDUCE", 500)])
        after = self.replay([stage1.report("exit", d1, "14:05", [(a, "退潮"), (b, "加强")])])["cycles"][0]
        self.assertEqual(after["primary_sector"], a)
        self.assertEqual((after["orders"][1]["status"], after["orders"][2]["target"]), ("SUPERSEDED", 1000))
        self.assertEqual(after["status"], "WAIT_EXIT")
        after = self.replay([stage1.quote("exit-price", d2, "09:35", "10")])["cycles"][0]
        self.assertEqual([f["quantity"] for f in after["book"]["fills"]], [1000, 1000])

    def test_t19_mixed_lots_exit_sells_only_unlocked_quantity(self):
        from modules.strategy_hermes.adapter import normalize
        from modules.strategy_hermes.signal_cycle import apply_report, fill
        from modules.strategy_hermes.simulator import Calendar

        cycle = self.mixed_lots(200, 800)
        retired = stage1.report("exit", stage1.D2, "13:05", [(stage1.A, "退潮")])
        apply_report(cycle, normalize(retired))
        before = cycle["orders"][-1]
        self.assertEqual((before["target"], before["remaining"]), (800, 800))
        fill(cycle, stage1.quote("still-locked", stage1.D2, "13:06", "1"), Calendar(stage1.DAYS))
        self.assertEqual([f["quantity"] for f in cycle["book"]["fills"]], [200])
        fill(cycle, stage1.quote("unlocked", stage1.D3, "09:35", "1"), Calendar(stage1.DAYS))
        self.assertEqual([f["quantity"] for f in cycle["book"]["fills"]], [200, 800])
        self.assertEqual(cycle["status"], "CLOSED")

    def test_t20_old_cycle_restarts_and_duplicate_events_keep_trade_quantities(self):
        from modules.strategy_hermes.ledger import Ledger

        events = stage1.start()+[
            stage1.report("weak", stage1.D2, "09:35", [(stage1.A, "减弱")]),
            stage1.quote("reduce-price", stage1.D2, "09:36", "11"),
        ]
        first, = self.replay(events)["cycles"]
        self.ledger = Ledger(self.state)
        actual, = self.replay(events)["cycles"]
        self.assertEqual(actual, first)
        self.assertEqual((actual["primary_sector"], actual["reduced"]), (stage1.A, True))
        self.assertEqual(actual["orders"][-1]["target"], 500)
        self.assertEqual([f["quantity"] for f in actual["book"]["fills"]], [1000, 500])


@unittest.skipUnless(postgres_tests.ENV, "requires isolated PostgreSQL/Redis")
class PublishAcceptance(unittest.TestCase):
    setUp = postgres_tests.StageTwo.setUp
    clean = postgres_tests.StageTwo.clean
    market = postgres_tests.StageTwo.market

    def worker_publish(self, rid, slot, states, mapping, market, choices, *, policy=None, pairs=None):
        now = moment(stage1.stamp(stage1.D1, slot))
        if policy is None:
            policy, pairs = policy_pair(mapping, [sid for sid, _ in states], now)
        event = stage1.report(rid, stage1.D1, slot, states)
        envelope = dict(input_kind="FROZEN", published_at=now.isoformat(), report_json=event["report_json"],
                        judgments=event["judgments"], octop=dict(etf_choices=choices))
        claim = self.store.claim_round(stage1.D1, slot, now)
        return worker.publish_round(self.store, claim, envelope, stage1.DAYS, mapping, market,
                                    prepared=dict(etf_policy=policy, etf_pair_facts=pairs), targets=("octop", "redis"))

    def workbench(self, rid):
        from modules.strategy_hermes.workspace_web import API, application

        def start_response(status, headers):
            self.assertEqual(status, "200 OK")

        token = "t"*40
        env = dict(REQUEST_METHOD="GET", PATH_INFO=API+"/report", QUERY_STRING="id="+rid,
                   HTTP_AUTHORIZATION="Bearer "+token)
        return json.loads(b"".join(application(self.store, token)(env, start_response)))

    def test_t22_mapping_taxonomy_parameters_and_observer_change_do_not_replace_position(self):
        a, code, alternate, day = stage1.A, stage1.ETF, stage1.ETF2, stage1.D1
        self.map["products"][alternate] = deepcopy(self.map["products"][code])
        market = self.market(day, "09:35", "10")
        market.body["quotes"][alternate] = deepcopy(market.body["quotes"][code])
        saved = self.worker_publish("t22-original", "09:35", [(a, "新增")], self.map, market, {a:[code]})
        self.store.apply_quotes([stage1.quote("t22-entry", day, "09:36", "10")],
                               stage1.DAYS, {code:100, alternate:100})
        before, = self.store.inspect()["cycles"]
        self.assertEqual((before["etf"], before["status"]), (code, "OPEN"))
        updated = deepcopy(self.map)
        updated["version"] = "mapping-v2"
        updated["etf_taxonomy"] = dict(version="taxonomy-v2", rows=[])
        revised_market = self.market(day, "10:35", "10", revision=2)
        revised_market.body["quotes"][alternate] = deepcopy(revised_market.body["quotes"][code])
        revised_market.body["quotes"][code]["trading_status"] = "SUSPENDED"
        from modules.strategy_hermes.stage2 import etf_representativeness as representation
        params = dict(representation.PARAMETERS)
        params["core_exposure_tier_floor"] = ".75"
        with patch.object(representation, "PARAMETERS", tuple(params.items())), \
             patch.object(representation, "VERSION", "isolated-policy-v2"):
            revised = self.worker_publish("t22-revised", "10:35", [(a, "维持")], updated, revised_market, {a:[]})
        first, second = (report["input_meta"]["representation"]["policy"] for report in (saved, revised))
        self.assertNotEqual(first["parameter_digest"], second["parameter_digest"])
        self.assertEqual((second["mapping_version"], second["taxonomy_version"], second["policy_version"]),
                         ("mapping-v2", "taxonomy-v2", "isolated-policy-v2"))
        self.assertEqual(saved["input_meta"]["etf_recommendations"]["sectors"][a]["static_representatives"], [code])
        self.assertEqual(revised["input_meta"]["etf_recommendations"]["sectors"][a]["static_representatives"], [alternate])
        self.assertEqual(self.store.report(saved["id"]), saved)
        current, = self.store.inspect()["cycles"]
        self.assertEqual(current.pop("last_report"), revised["id"])
        self.assertEqual(current.pop("last_decision"), "HOLD")
        self.assertGreater(current.pop("position_duration_seconds"), before.pop("position_duration_seconds"))
        before.pop("last_report")
        before.pop("last_decision")
        self.assertEqual(current, before)

    def assert_count_surfaces(self, fixture):
        from modules.strategy_hermes.qq_templates import render_message

        a, code, alternate, day = stage1.A, stage1.ETF, stage1.ETF2, stage1.D1
        market = self.market(day, "09:35", "10")
        sectors = [a]
        index = "index-A"
        if fixture == "A":
            sectors += [stage1.B, "kaipanla:C"]
            for sid in sectors[1:]:
                self.map["sectors"][sid] = deepcopy(self.map["sectors"][a])
            self.map["products"][alternate] = deepcopy(self.map["products"][code])
            market.body["quotes"][alternate] = deepcopy(market.body["quotes"][code])
            expected = ("3/3", "6/6", "1/1", "2/2", 1, (6, 0, 0))
        elif fixture == "B":
            weight = self.map["weights"][index]
            weights = weight["rows"]
            weights[0]["weight"], weights[1]["weight"] = ".1", ".05"
            weight["complete"] = False
            expected = ("1/1", "1/1", "1/1", "1/1", 1, (1, 0, 0))
        else:
            self.map["weights"].clear()
            self.map["sector_index_relations"] = [dict(
                sector_id=a, index_code=index, verified=True, evidence_refs=["fixture"],
                available_at=stage1.stamp(day, "09:00"))]
            expected = ("1/1", "1/1", "0/1", "1/1", 1, (0, 0, 1))
        rid = "t24-"+fixture
        choices = {sid:[code, alternate] if fixture == "A" else [code] for sid in sectors}
        saved = self.worker_publish(rid, "09:35", [(sid, "新增") for sid in sectors], self.map, market, choices)
        detail = self.workbench(rid)
        stats = detail["input_meta"]["representation"]["statistics"]
        self.assertEqual(stats, saved["input_meta"]["representation"]["statistics"])
        if fixture == "B":
            self.assertEqual(stats["pairs"]["unproven_upper_bound"], 1)
            self.assertNotIn("REPRESENTATIVENESS_UNKNOWN", stats["pairs"]["by_status"])
        tiers = stats["pairs"]["selectable_by_tier"]
        observed = (f"{stats['sectors']['with_selectable']}/{stats['sectors']['total']}",
                    f"{stats['pairs']['selectable']}/{stats['pairs']['total']}",
                    f"{stats['indexes']['weights_available']}/{stats['indexes']['total']}",
                    f"{stats['products']['selectable']}/{stats['products']['total']}",
                    stats["orders"]["total"], tuple(tiers[key] for key in ("core", "below_core", "unknown")))
        self.assertEqual(observed, expected)
        self.assertEqual(sum(tiers.values()), stats["pairs"]["selectable"])
        self.assertEqual(stats["products"]["ai_selected"], 1)
        with self.store._connect() as conn:
            orders = conn.execute("SELECT action, status FROM simulation_orders WHERE ns=%s", (self.ns,)).fetchall()
            payload, = conn.execute("SELECT payload FROM outbox WHERE ns=%s AND report_id=%s AND target='octop'",
                                    (self.ns, rid)).fetchone()
        sections = json.loads(payload)["sections"] if isinstance(payload, str) else payload["sections"]
        text = render_message("research", sections)["text"]
        for label, count in zip(("板块", "配对", "指数", "ETF", "本轮节点"), expected[:5], strict=True):
            self.assertIn(f"{label} {count}", text)
        for label, count in zip(("主干", "低暴露", "未知"), expected[5], strict=True):
            self.assertIn(f"{label} {count}", text)
        self.assertIn(code+" 买入", text)
        self.assertEqual(orders, [("BUY", "PENDING")])
        print("T24_WORKBENCH", fixture, json.dumps(stats, ensure_ascii=True), flush=True)

    def test_t24_a_saved_report_qq_workbench_and_orders(self):
        self.assert_count_surfaces("A")

    def test_t24_b_saved_report_qq_workbench_and_orders(self):
        self.assert_count_surfaces("B")

    def test_t24_c_saved_report_qq_workbench_and_orders(self):
        self.assert_count_surfaces("C")

    def publish(self, rid, context=None, *, tie=False):
        a, b, code, day = stage1.A, stage1.B, stage1.ETF, stage1.D1
        now = moment(stage1.stamp(day, "09:35"))
        claim = self.store.claim_round(day, "09:35", now)
        event = stage1.report(rid, day, "09:35", [(a, "新增"), (b, "新增")], [(code, a, 1), (code, b, 2)])
        if tie:
            event["candidates"][0]["mapping_ref"] = "z-mapping"
            event["candidates"].append({**event["candidates"][0], "mapping_ref": "a-mapping"})
        recommendations = dict(method="etf-direction-first-v2", sectors={}, candidates=event["candidates"])
        return self.store.publish(claim, event, stage1.DAYS, {code: 100}, dict(etf_recommendations=recommendations),
                                  targets=("redis", "octop"), etf_projection=context)

    def test_t31_a_c_e_frozen_exposure_flows_to_primary_sector(self):
        a, b, code, day = stage1.A, stage1.B, stage1.ETF, stage1.D1
        now = moment(stage1.stamp(day, "09:35"))
        weights = self.map["weights"]["index-A"]
        stocks = [row["code"] for row in weights["rows"]]
        formal(self.map, a, [stocks[0]])
        formal(self.map, b, [stocks[1]])
        for sector in self.map["sectors"].values():
            sector["as_of_date"] = day
        self.map["weights"]["index-A"]["rows"] = [
            dict(code=stock, weight=weight) for stock, weight in zip(stocks, (".045", ".52", ".435"))
        ]
        policy, pairs = policy_pair(self.map, [a,b], now)
        self.assertEqual((pairs[a][code]["known_exposure"], pairs[b][code]["known_exposure"]), ("0.045", "0.52"))
        frozen = deepcopy(self.map)
        self.map["weights"]["index-A"]["rows"][0]["weight"] = ".52"
        self.map["weights"]["index-A"]["rows"][1]["weight"] = ".045"
        with patch.dict(os.environ, ETF_REPRESENTATIVENESS_MODE="V2"):
            saved = self.publish("t31", dict(mapping=frozen, policy=policy, pairs=pairs))
        cycle, = self.store.inspect()["cycles"]
        self.assertEqual(cycle["primary_sector"], b)
        self.assertEqual(saved["input_meta"]["representation"]["pairs"], pairs)
        for exposure, tier in ((".149999", "LOW_EXPOSURE_CAUTION"), (".15", "CORE_REPRESENTATIVE")):
            result = evaluate_pair_representativeness(
                stocks[:1], {stocks[0]: Decimal(exposure)}, sector_members_complete=True,
                weights_complete_and_valid=False)
            self.assertEqual(result["exposure_tier"], tier)
        waiting = evaluate_pair_representativeness(
            stocks[:1], {}, sector_members_complete=True, weights_complete_and_valid=False, explicit_relation=True)
        self.assertEqual(waiting["exposure_tier"], "UNKNOWN")

        from modules.strategy_hermes.stage2.postgres import PostgresLedger
        restarted = PostgresLedger(self.settings, "FROZEN")
        self.assertEqual(restarted.report(saved["id"])["input_meta"]["representation"]["pairs"], pairs)
        self.assertEqual(restarted.inspect()["cycles"][0]["primary_sector"], b)
        self.assertIsNone(restarted.claim_round(day, "09:35", now))
        self.map["version"] = "updated-mapping"
        self.map["etf_taxonomy"] = dict(version="updated-taxonomy", rows=[])
        updated_policy, updated_pairs = policy_pair(self.map, [a,b], now)
        self.assertEqual((updated_policy["mapping_version"], updated_policy["taxonomy_version"]),
                         ("updated-mapping", "updated-taxonomy"))
        self.assertEqual(updated_pairs[a][code]["known_exposure"], "0.52")
        self.assertEqual(restarted.inspect()["cycles"][0]["primary_sector"], b)

    def test_t16_t31c_same_tier_retains_mapping_ref_order_and_unique_active_cycle(self):
        now = moment(stage1.stamp(stage1.D1, "09:35"))
        self.map["sectors"][stage1.B] = deepcopy(self.map["sectors"][stage1.A])
        policy, pairs = policy_pair(self.map, [stage1.A, stage1.B], now)
        self.assertTrue(all(row[stage1.ETF]["exposure_tier"] == "CORE_REPRESENTATIVE" for row in pairs.values()))
        self.publish("t31-tie", dict(mapping=self.map, policy=policy, pairs=pairs), tie=True)
        cycle, = self.store.inspect()["cycles"]
        self.assertEqual(cycle["primary_sector"], stage1.A)
        self.assertEqual(cycle["entry_sources"][0]["mapping_ref"], "a-mapping")

    def test_t31b_shadow_preserves_original_anchor(self):
        now = moment(stage1.stamp(stage1.D1, "09:35"))
        policy = freeze_policy(self.map, now, mode="V3_SHADOW")
        pairs = {stage1.B: {stage1.ETF: {"exposure_tier": "CORE_REPRESENTATIVE"}}}
        context = dict(mapping=self.map, policy=policy, pairs=pairs, recommendations=dict(candidates=[]))
        self.publish("t31-shadow", context)
        self.assertEqual(self.store.inspect()["cycles"][0]["primary_sector"], stage1.A)

    def test_t31b_legacy_preserves_original_anchor(self):
        self.publish("t31-legacy")
        self.assertEqual(self.store.inspect()["cycles"][0]["primary_sector"], stage1.A)

    def test_t31d_multi_report_batch_uses_original_anchor(self):
        from modules.strategy_hermes.simulator import Rules
        from modules.strategy_hermes.stage2.postgres import BoundConnection, PostgresLedger

        a, b, code, day = stage1.A, stage1.B, stage1.ETF, stage1.D1
        events = [
            stage1.report("r1", day, "09:35", [(a, "新增")], [(code, a, 1)]),
            stage1.report("r2", day, "09:35", [(b, "新增")], [(code, b, 2)], published="09:36"),
        ]
        with self.store._connect() as conn, conn.transaction():
            db = BoundConnection(conn)
            db.etf_anchor = {"r2": {b: {code: {"exposure_tier": "CORE_REPRESENTATIVE"}}}}
            self.store._store(db, events, stage1.DAYS, {code:100}, Rules())
        restarted = PostgresLedger(self.settings, "FROZEN")
        with restarted._connect() as conn, conn.transaction():
            self.assertEqual(restarted._store(BoundConnection(conn), events, stage1.DAYS, {code:100}, Rules())[0], [])
        cycle, = restarted.inspect()["cycles"]
        self.assertEqual(cycle["primary_sector"], a)
        self.assertIn("r1", {source["report_id"] for source in cycle["entry_sources"]})

    def test_t26g_wait_exit_keeps_market_p0_scope(self):
        a, code, day = stage1.A, stage1.ETF, stage1.D1
        self.publish("t26-entry")
        self.store.apply_quotes([stage1.quote("t26-buy", day, "09:36", "10")], stage1.DAYS, {code:100})
        when = moment(stage1.stamp(day, "10:35"))
        claim = self.store.claim_round(day, "10:35", when)
        self.store.publish(claim, stage1.report("t26-exit", day, "10:35", [(a, "退潮")]),
                           stage1.DAYS, {code:100}, {})
        self.assertEqual(self.store.inspect()["cycles"][0]["status"], "WAIT_EXIT")
        held, sectors = _priorities(self.store, when)
        self.assertIn(code, held)
        self.assertIn(a, sectors)

    def test_t32d_invalid_shadow_field_never_reaches_archived_report(self):
        now = moment(stage1.stamp(stage1.D1, "09:35"))
        policy, pairs = policy_pair(self.map, [stage1.A, stage1.B], now, "V3_SHADOW")
        context = dict(mapping=self.map, policy=policy, pairs=pairs, recommendations=dict(candidates=[]))
        with patch("modules.strategy_hermes.stage2.etf_support_facts.finalize_support_facts",
                   return_value={"statistics": "invalid summary"}):
            saved = self.publish("t32-field", context)
        self.assertNotIn("representation_shadow", saved["input_meta"])
        self.assertEqual(saved["input_meta"]["representation_shadow_error"]["stage"], "FINALIZE")

    def test_t32e_failed_shadow_keeps_pending_order_and_all_renderers_v2(self):
        from modules.strategy_hermes.qq_templates import render_message

        a, code, day = stage1.A, stage1.ETF, stage1.D1
        now = moment(stage1.stamp(day, "09:35"))
        market = self.market(day, "09:35", "10")
        policy, pairs = policy_pair(self.map, [a], now, "V3_SHADOW")
        with patch("modules.strategy_hermes.stage2.etf_support_facts.finalize_support_facts",
                   return_value={"statistics": "must not appear"}):
            saved = self.worker_publish("t32-live-node", "09:35", [(a, "新增")], self.map, market, {a:[code]},
                                        policy=policy, pairs=pairs)
        self.assertEqual(list(saved["candidates"]), [code])
        self.assertEqual(saved["input_meta"]["etf_recommendations"]["method"], "etf-direction-first-v2")
        detail = self.workbench(saved["id"])
        self.assertNotIn("representation", detail["input_meta"])
        self.assertNotIn("representation_shadow", detail["input_meta"])
        self.assertEqual(detail["input_meta"]["representation_shadow_error"], dict(stage="FINALIZE", error="ValueError"))
        with self.store._connect() as conn:
            orders = conn.execute("SELECT action,status,target_quantity FROM simulation_orders WHERE ns=%s", (self.ns,)).fetchall()
            rows = conn.execute("SELECT target,payload FROM outbox WHERE ns=%s ORDER BY target", (self.ns,)).fetchall()
        self.assertEqual(orders, [("BUY", "PENDING", None)])
        self.assertEqual([target for target, _ in rows], ["octop", "redis"])
        payload = rows[0][1]
        sections = json.loads(payload)["sections"] if isinstance(payload, str) else payload["sections"]
        text = render_message("research", sections)["text"]
        self.assertIn(code+" 买入", text)
        self.assertNotIn("must not appear", text)
        self.assertNotIn("穿透统计", text)


@unittest.skipUnless(postgres_tests.ENV, "requires isolated PostgreSQL/Redis")
class AdmissionAcceptance(unittest.TestCase):
    setUp = admission_tests.AdmissionIntegration.setUp
    clean = admission_tests.AdmissionIntegration.clean
    publish_mapping = admission_tests.AdmissionIntegration.publish_mapping
    publish = admission_tests.AdmissionIntegration.publish

    def test_t21_one_published_review_pauses_buy_for_affected_product_and_preserves_exit(self):
        a, code, waiting = stage1.A, stage1.ETF, stage1.ETF2
        day, next_day = stage1.D1, stage1.D2
        self.publish_mapping(extra=True)
        self.publish(selected=(code, waiting))
        self.clock = moment(stage1.stamp(day, "09:36"))
        self.store.apply_quotes([stage1.quote("buy", day, "09:36", "10")], stage1.DAYS, {code:100, waiting:100})
        original = {c["etf"]:c for c in self.store.inspect()["cycles"]}[code]["rule"]
        current = self.publish_mapping(review=True, extra=True, version="v2", at="09:37")
        current["parent_mapping_version"] = "v2"
        current["version"] = "v3"
        current["products"][waiting]["rule"]["review_required"] = True
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        publish_mapping(self.store, current, {}, moment(stage1.stamp(day, "09:38")), expected_parent="v2")
        self.clock = moment(stage1.stamp(day, "09:39"))
        self.store.apply_quotes([stage1.quote("blocked", day, "09:39", "10", code=waiting)],
                               stage1.DAYS, {code:100, waiting:100})
        cycles = {c["etf"]:c for c in self.store.inspect()["cycles"]}
        self.assertEqual(cycles[waiting]["pending_reason"], "RULE_REVIEW_REQUIRED")
        self.assertEqual(cycles[waiting]["book"]["fills"], [])
        when = moment(stage1.stamp(day, "10:35"))
        self.clock = when
        claim = self.store.claim_round(day, "10:35", when)
        self.store.publish(claim, stage1.report("t21-exit", day, "10:35", [(a, "退潮")]),
                           stage1.DAYS, {code:100, waiting:100}, {})
        self.clock = moment(stage1.stamp(next_day, "09:35"))
        self.store.apply_quotes([stage1.quote("exit", next_day, "09:35", "11")], stage1.DAYS, {code:100, waiting:100})
        cycles = {c["etf"]:c for c in self.store.inspect()["cycles"]}
        self.assertEqual(cycles[code]["status"], "CLOSED")
        self.assertEqual(cycles[code]["rule"], original)
        self.assertEqual([f["quantity"] for f in cycles[code]["book"]["fills"]], [1000, 1000])


@unittest.skipUnless(postgres_tests.ENV, "requires isolated PostgreSQL/Redis")
class MaintenanceAcceptance(unittest.TestCase):
    setUp = verification_tests.PersistedReview.setUp
    clean = verification_tests.PersistedReview.clean

    def submit(self, code, content, now):
        body = verification_tests.dossier()
        body["etf_code"] = code
        digest = sha256(content).hexdigest()
        body["documents"]["prospectus"].update(url=self.evidence_url, sha256=digest)
        return verification_tests.verification.submit(
            self.store, body, {digest: content}, self.map, operator="acceptance-test", now=now)["request_id"]

    def apply(self, now, mode, digest):
        requests = []

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now+timedelta(seconds=2)

        def request(source, scope, operation):
            requests.append((source, scope))
            return dict(status="SAVED", receipt=operation())

        with patch("modules.strategy_hermes.stage2.mapping_maintenance._rule_hash", return_value=digest), \
             patch.object(verification_tests.verification, "datetime", Clock):
            _, summary = verification_tests.verification.apply_queued(
                self.store, self.map, deepcopy(self.map), {"mode": mode}, now,
                request, lambda: None, clock=lambda: now+timedelta(seconds=4))
        return summary, requests

    def test_t26b_eight_older_missing_documents_do_not_occupy_slots(self):
        now = verification_tests.NOW
        for i in range(8):
            code = f"{588110+i}.SH"
            content = f"Missing document {i} in fixture".encode()
            self.map["products"][code] = deepcopy(self.map["products"][verification_tests.ETF])
            self.submit(code, content, now + timedelta(seconds=i))
            with self.store._connect() as conn:
                conn.execute("DELETE FROM product_verification_documents WHERE ns=%s AND sha256=%s",
                             (self.ns, sha256(content).hexdigest()))
        ready = verification_tests.ETF
        req = self.submit(ready, verification_tests.CONTENT, now+timedelta(seconds=8))
        summary, requests = self.apply(now+timedelta(seconds=9), "market", verification_tests.DIGEST)
        self.assertEqual(len(requests), 1)
        self.assertEqual(summary["product_verification"][ready]["request_id"], req)
        self.assertEqual(summary["product_verification"][ready]["status"], "UPDATED")

    def test_t26d_new_midday_review_advances_in_afternoon_slot(self):
        now = verification_tests.NOW
        code = verification_tests.ETF
        first = self.submit(code, verification_tests.CONTENT, now)
        morning, requests = self.apply(now, "market", "f"*64)
        self.assertEqual(len(requests), 1)
        self.assertNotEqual(morning["product_verification"][code]["status"], "UPDATED")
        content = b"New reviewed fixture document"
        replaced = self.submit(code, content, now.replace(hour=12))
        self.assertNotEqual(replaced, first)
        digest = sha256(content).hexdigest()
        afternoon, requests = self.apply(now.replace(hour=14, minute=15), "retry", digest)
        self.assertEqual(afternoon["product_verification"][code]["request_id"], replaced)
        self.assertEqual(afternoon["product_verification"][code]["status"], "UPDATED")
        self.assertEqual(len(requests), 1)


@unittest.skipUnless(postgres_tests.ENV, "requires isolated PostgreSQL/Redis")
class MaintenanceBudgetAcceptance(unittest.TestCase):
    setUp = maintenance_tests.MaintenanceIntegration.setUp
    clean = maintenance_tests.MaintenanceIntegration.clean
    seed_material_revision = maintenance_tests.MaintenanceIntegration.seed_material_revision

    def test_t26c_morning_network_failure_and_retry_slot_count_separately(self):
        from modules.strategy_hermes.stage2.maintenance_store import live_mapping, publish_mapping
        from modules.strategy_hermes.stage2.provider_errors import ProviderFailure
        from modules.strategy_hermes.stage2.reference_policy import ReferencePolicy

        counts = []
        key = "weights:index-A"
        morning = self.now
        retry = morning.replace(hour=14, minute=15)
        dates = (morning, retry)
        mapping = deepcopy(self.map)
        for call_index, (mode, now) in enumerate(zip(("market", "retry"), dates, strict=True), 1):
            if mode == "retry":
                mapping = live_mapping(self.store, self.mapping, now)
            policy = ReferencePolicy(mapping, now, mode)
            ready = self.queue.prepare(mode, now=now)
            claim = self.queue.queue.claim()
            self.assertEqual(claim.job_id, ready["run_id"])
            self.assertTrue(policy.due(key))
            self.assertEqual(self.queue.queue.get(claim.job_id)["requests_used"], 0)
            self.assertEqual(len(counts), call_index-1)
            target = stage1.D1
            self.assertTrue(policy.begin(key, target))
            self.assertEqual(policy.pending[key]["attempts"], call_index)

            def provider():
                counts.append(mode)
                if mode == "market":
                    raise ProviderFailure("injected temporary disconnection", kind="CONNECTION_FAILED")
                return {"batch": "recovered-weight-fixture"}

            receipt = self.queue.queue.request(claim, "tushare:index_weight", dict(index_code="index-A"), provider)
            self.assertEqual(self.queue.queue.request(claim, "tushare:index_weight",
                             dict(index_code="index-A"), provider), receipt)
            self.assertEqual(self.queue.queue.get(claim.job_id)["requests_used"], 1)
            if mode == "market":
                self.assertEqual(receipt["error_kind"], "CONNECTION_FAILED")
                policy.failed(key, {"error_kind": receipt["error_kind"]})
                mapping.update(version="reference-after-morning")
                publish_mapping(self.store, mapping, {}, now)
            else:
                self.assertEqual(receipt["status"], "SAVED")
                policy.succeeded(key, target)
                self.assertNotIn(key, policy.pending)
            self.queue.queue.finish(claim, status="PARTIAL" if mode == "market" else "SUCCEEDED",
                                    result={"items":[]})
            self.assertEqual(self.queue.prepare(mode, now=now)["run_id"], claim.job_id)
        self.assertEqual(counts, ["market", "retry"])

    def test_t26e_cross_day_unrelated_mapping_does_not_reissue_exhausted_task(self):
        from modules.strategy_hermes.stage2.maintenance_store import live_mapping, publish_mapping
        from modules.strategy_hermes.stage2.reference_policy import ReferencePolicy

        job_id, changes, now = self.seed_material_revision()
        deferred, cooldown, exhausted = ('weights:deferred', 'weights:cooldown', 'weights:exhausted')
        previous = deepcopy(self.map)
        policy = ReferencePolicy(previous, now, "market")
        policy.defer(deferred, now.date().isoformat())
        policy.begin(cooldown, now.date().isoformat())
        policy.failed(cooldown, {"error_kind":"CONFIRMED_SOURCE_MISSING"})
        for days_ago in (4, 3, 2, 1):
            old = ReferencePolicy(previous, now-timedelta(days=days_ago), "market")
            self.assertTrue(old.due(exhausted))
            old.begin(exhausted, old.now.date().isoformat())
            old.failed(exhausted, {"error_kind":"CONNECTION_FAILED"})
        previous["version"] = "before-unrelated-update"
        publish_mapping(self.store, previous, {}, now)
        later = now+timedelta(days=1)
        updated = deepcopy(previous)
        updated.update(version="after-unrelated-update", parent_mapping_version=previous["version"],
                       unrelated_note="isolated fixture")
        publish_mapping(self.store, updated, {}, later)
        same_day_retry = ReferencePolicy(previous, now.replace(hour=14, minute=15), "retry")
        self.assertTrue(same_day_retry.due(deferred))
        self.assertFalse(same_day_retry.due(cooldown))
        after = ReferencePolicy(live_mapping(self.store, self.mapping, later), later, "market")
        self.assertTrue(after.due(deferred))
        self.assertEqual((after.pending[deferred]["attempts"], after.pending[deferred]["error_kind"]), (0, "DEFERRED"))
        self.assertTrue(after.due(cooldown))
        self.assertEqual(after.pending[cooldown]["attempts"], 1)
        self.assertFalse(after.due(exhausted))
        self.assertEqual(after.pending[exhausted]["attempts"], 4)

        with self.store._connect() as conn:
            conn.execute("UPDATE left_right_backfill_jobs SET requests_used=120, elapsed_seconds=720 "
                         "WHERE ns=%s AND job_id=%s", (self.ns,job_id))
        with patch.dict(os.environ, APP_MAPPING_FILE=str(self.mapping)), \
             patch("modules.strategy_hermes.product_materials.associated_targets", return_value=[("512480.SH", ["chip"])]), \
             patch("modules.strategy_hermes.product_materials.source_upgrades", return_value=changes):
            for when in (now, later, later):
                result = self.queue.prepare("materials", now=when, target_codes=["512480.SH"])
                self.assertEqual((result["run_id"], result["state"]), (job_id, "PARTIAL"))
        with self.store._connect() as conn:
            budget = conn.execute("SELECT requests_used,elapsed_seconds FROM left_right_backfill_jobs "
                                  "WHERE ns=%s AND job_id=%s", (self.ns,job_id)).fetchone()
            requests, = conn.execute("SELECT count(*) FROM left_right_backfill_requests "
                                     "WHERE ns=%s AND job_id=%s", (self.ns,job_id)).fetchone()
        self.assertEqual(budget, (120,720))
        self.assertEqual(requests, 5)
        new_job = self.queue.prepare("market", now=self.now)
        self.assertNotEqual(new_job["run_id"], job_id)
        self.assertEqual(self.queue.queue.get(new_job["run_id"])["requests_used"], 0)

@unittest.skipUnless(postgres_tests.ENV, "requires isolated PostgreSQL/Redis")
class NativeAcceptance(unittest.TestCase):
    setUp = native_tests.NativeRoundIntegration.setUp
    clean = native_tests.NativeRoundIntegration.clean
    prepare = native_tests.NativeRoundIntegration.prepare
    decisions = native_tests.NativeRoundIntegration.decisions

    def finish(self, ready):
        self.queue.submit(ready["run_id"], self.decisions(ready))
        result = native_rounds.native_tick(self.queue, self.cache, self.args, self.peer,
                                          now=self.now+timedelta(seconds=5))
        self.assertEqual(result["state"], "PUBLISHED", result)
        return self.store.report(result["report_id"])

    def assert_shadow_isolated(self, saved, stage):
        metadata = saved["input_meta"]
        self.assertEqual(metadata["representation_shadow_error"], {"stage": stage, "error": "RuntimeError"})
        self.assertEqual(metadata["etf_recommendations"]["method"], "etf-direction-first-v2")
        self.assertNotIn("representation_shadow", metadata)
        self.assertNotIn("representation", metadata)
        self.assertEqual(saved["candidates"], {})
        with self.store._connect() as conn:
            targets = conn.execute("SELECT target,payload FROM outbox WHERE ns=%s ORDER BY target", (self.ns,)).fetchall()
            orders = conn.execute("SELECT count(*) FROM simulation_orders WHERE ns=%s", (self.ns,)).fetchone()[0]
        self.assertEqual([t[0] for t in targets], ["octop", "redis"])
        self.assertNotIn("shadow-corruption", json.dumps(targets, default=str))
        self.assertEqual(orders, 0)

    def test_t27a_old_ready_job_keeps_frozen_prompt_after_deployment(self):
        with patch.dict(os.environ, ETF_REPRESENTATIVENESS_MODE="V2"):
            before = self.prepare()
        with patch.dict(os.environ, ETF_REPRESENTATIVENESS_MODE="V3_ENFORCE"):
            restarted = native_rounds.NativeRounds(self.store, self.calendar)
            self.assertEqual(restarted.status(before["run_id"])["instructions"], before["instructions"])
            with patch.object(native_rounds, "INSTRUCTIONS", "incompatible post-deploy instruction", create=True):
                after = restarted.status(before["run_id"])
        self.assertEqual(after["instructions"], before["instructions"])

    def test_t32a_b_e_mid_recommendation_mutation_and_exception(self):
        original = worker.evaluate_recommendations

        def fault(event, mapping, market, **kwargs):
            if kwargs["representation"] is None:
                return original(event, mapping, market, **kwargs)
            event["report_json"] = "shadow-corruption"
            mapping["version"] = "shadow-corruption"
            market.body["quotes"].clear()
            raise RuntimeError("injected recommendation error")

        with patch.dict(os.environ, ETF_REPRESENTATIVENESS_MODE="V3_SHADOW"):
            ready = self.prepare()
            with patch.object(worker, "evaluate_recommendations", side_effect=fault):
                saved = self.finish(ready)
        self.assert_shadow_isolated(saved, "RECOMMEND")
        self.assertEqual(saved["input_meta"]["mapping_version"], self.map["version"])
        self.assertNotIn("shadow-corruption", saved["raw"])

    def test_t32b_prepare_exception_still_publishes_v2(self):
        def fault(mapping, *args):
            mapping["version"] = "shadow-corruption"
            raise RuntimeError("injected prepare error")

        with patch.dict(os.environ, ETF_REPRESENTATIVENESS_MODE="V3_SHADOW"):
            with patch("modules.strategy_hermes.stage2.penetration.freeze_pair_facts", side_effect=fault):
                ready = self.prepare()
            saved = self.finish(ready)
        self.assert_shadow_isolated(saved, "PREPARE")
        self.assertEqual(saved["input_meta"]["mapping_version"], self.map["version"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
