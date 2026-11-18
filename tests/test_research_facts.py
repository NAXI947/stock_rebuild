"""Model numbers are scoped evidence; failures affect only the relevant decision."""
import copy
import json
from types import SimpleNamespace
import unittest

from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes.signal_cycle import apply_report
from modules.strategy_hermes.stage2.octop_analyst import OctopAnalyst
from modules.strategy_hermes.stage2.octop_decision import render
from modules.strategy_hermes.stage2.research_facts import (
    NUMERIC_CONTRACT, ETF_SELECTION_CONTRACT, fact_catalog, market_facts,
)

AT = "2026-11-17T13:06:16+08:00"
SOURCE_AT = "2026-11-17T13:05:40+08:00"
BANK, CHIP = "kaipanla:801027", "kaipanla:801001"
ETF = "512800.SH"


def context(sid=BANK, name="银行"):
    row = dict(sector_id=sid, sector_name=name, trade_date="2026-11-17", data_as_of=SOURCE_AT,
        prior_active=True, held=True,
        current=[dict(ref="current:"+sid, observed_at=SOURCE_AT, board_return_ratio="-.00146")],
        previous=[dict(ref="previous:"+sid, observed_at="2026-11-17T10:35:10+08:00", board_return_ratio="-.00019")],
        stock_evidence=[dict(code="600036.SH", ref="stock:bank", observed_at=SOURCE_AT, return_ratio="-.00622")],
        facts=dict(board_return_ratio="-.00146", quality=[], board_return_change_ratio="-.00127",
            comparison_observed_at="2026-11-17T10:35:10+08:00", comparison_ref="previous:"+sid,
            breadth=dict(member_count=42, observed_count=42, up=14, down=28, flat=0,
                up_ratio=".333333333333", median_return="-.002978447836", batch="stocks",
                oldest_at=SOURCE_AT, newest_at=SOURCE_AT)),
        numeric_contract=NUMERIC_CONTRACT, etf_selection_contract=ETF_SELECTION_CONTRACT,
        etf_options=dict(products=[dict(etf=ETF)]))
    row["fact_catalog"] = fact_catalog(row, {})
    return row


def decision(evidence, **changes):
    return dict(sector_id=evidence["sector_id"], phase="WEAKEN",
        evidence_refs=[evidence["current"][0]["ref"]], fact_refs=["sector_return", "sector_breadth"],
        etf_choices=[], **changes)


def research(rationale="板块与权重股走弱，成员表现分化", **changes):
    return dict(role="WATCH", rationale=rationale, counter_evidence_status="FOUND",
                counter_evidence_refs=["stock:bank"], invalidation_condition="后续涨幅超过1%且上涨占比回到60%",
                key_unknown="后续资金承接", **changes)


def rendered(item, evidence):
    return render(dict(report_id="facts-report", generated_at=AT, finish_reason="stop", output=json.dumps(item)), evidence)


class ScopedFacts(unittest.TestCase):
    def test_market_scope_is_calculated_separately_from_sector_breadth(self):
        def quote(value, **changes):
            return dict(return_ratio=value, observed_at=SOURCE_AT, price_cny="10", source="frozen", **changes)
        quotes = {"a":quote(".01"), "b":quote("-.02"), "c":quote("0"),
                  "closed":quote(".2", trading_status="SUSPENDED")}
        quotes["old"] = {**quote(".3"), "observed_at":"2026-11-17T13:00:00+08:00"}
        quotes["future"] = {**quote(".4"), "observed_at":"2026-11-17T13:07:00+08:00"}
        market = SimpleNamespace(body=dict(trade_date="2026-11-17", sectors=[
            dict(observed_at=SOURCE_AT, return_ratio=".1", source_batch="boards"),
            dict(observed_at=SOURCE_AT, return_ratio="-.1", source_batch="boards")],
            stock_research=dict(quotes=quotes, batch="stock-batch")))
        common = market_facts(market, moment(AT))
        self.assertEqual(common["market_stocks"]["metrics"]["observed_count"], 3)
        self.assertEqual(common["market_stocks"]["metrics"]["up"], 1)
        self.assertIn("33.33%", common["market_stocks"]["text"])
        self.assertEqual(common["market_stocks"]["scope"], "MARKET_STOCKS")
        self.assertIn("50.00%", common["market_boards"]["text"])
        self.assertEqual(common["market_boards"]["scope"], "MARKET_BOARDS")

    def test_valid_judgment_keeps_free_phase_and_program_renders_the_numbers(self):
        evidence = context()
        result = rendered(decision(evidence, research=research()), evidence)
        self.assertEqual(result["normalized"][BANK]["phase"], "WEAKEN")
        self.assertEqual(result["judgment_review"], [])
        explanation = result["research"]
        self.assertEqual(explanation["source"], "AI_WITH_FACTS")
        self.assertEqual(explanation["qualitative_rationale"], research()["rationale"])
        self.assertIn("银行板块涨跌-0.15%", explanation["rationale"])
        self.assertIn("上涨14只（33.33%）", explanation["rationale"])
        self.assertIn("1%", explanation["invalidation_condition"])
        self.assertEqual(explanation["fact_evidence"][1]["subject_id"], BANK)

    def test_bad_current_number_marks_the_direction_unknown_and_creates_no_reduction(self):
        evidence = context()
        raw = decision(evidence, research=research("弱于全市场普涨，上涨占比61%"))
        saved = copy.deepcopy(raw)
        result = rendered(raw, evidence)
        self.assertEqual(raw, saved)
        self.assertEqual(result["normalized"][BANK]["phase"], "UNKNOWN")
        self.assertEqual(result["judgment_review"], ["RATIONALE_NUMERIC_FACT_UNBOUND"])
        cycle = dict(status="OPEN", primary_sector=BANK, orders=[], reduced=False)
        apply_report(cycle, dict(id="facts-report", sectors=result["normalized"]))
        self.assertEqual(cycle["orders"], [])
        self.assertFalse(cycle["reduced"])
        self.assertEqual(result["research"]["source"], "PROGRAM_FACTS")
        self.assertEqual(result["research"]["qualitative_rationale"], "")
        self.assertNotIn("61%", result["research"]["rationale"])

    def test_source_codes_dates_and_clocks_are_allowed_but_chinese_ratios_are_not(self):
        evidence = context()
        text = "600036.SH较2026-11-17的10:35走弱，当前13:05可比证据需要跟踪"
        self.assertEqual(rendered(decision(evidence, research=research(text)), evidence)["judgment_review"], [])
        for text in ("全市场上涨六成", "上涨百分之六十一", "共有十四只上涨", "上涨61％", "上涨600036%"):
            with self.subTest(text=text):
                result = rendered(decision(evidence, research=research(text)), evidence)
                self.assertEqual(result["normalized"][BANK]["phase"], "UNKNOWN")

    def test_foreign_fact_and_missing_local_fact_are_local_review_conditions(self):
        evidence = context()
        for refs in (["another_sector_breadth"], [], ["sector_return", "sector_return"], [123]):
            item = decision(evidence)
            item["fact_refs"] = refs
            self.assertEqual(rendered(item, evidence)["normalized"][BANK]["phase"], "UNKNOWN")
        self.assertEqual(rendered(decision(evidence), evidence)["research"]["source"], "PROGRAM_FACTS")

    def test_bad_or_absent_etf_selection_does_not_change_the_valid_direction(self):
        evidence = context()
        for choices in (None, ["999999.SH"], [ETF,ETF], [ETF]*4):
            item = decision(evidence)
            if choices is None:
                item.pop("etf_choices")
            else:
                item["etf_choices"] = choices
            result = rendered(item, evidence)
            self.assertEqual(result["normalized"][BANK]["phase"], "WEAKEN")
            self.assertIsNone(result["etf_choices"])
            self.assertEqual(result["etf_selection_review"], ["ETF_SELECTION_UNCONFIRMED"])
        item = decision(evidence)
        self.assertEqual(rendered(item,evidence)["etf_choices"], [])
        item["etf_choices"] = [ETF]
        self.assertEqual(rendered(item,evidence)["etf_choices"], [ETF])

    def test_legacy_frozen_context_keeps_its_original_contract(self):
        evidence = context()
        evidence.pop("numeric_contract")
        evidence.pop("etf_selection_contract")
        item = decision(evidence, research=research("旧版事实上涨61%"))
        item.pop("fact_refs"); item.pop("etf_choices")
        result = rendered(item, evidence)
        self.assertEqual(result["normalized"][BANK]["phase"], "WEAKEN")
        self.assertEqual(result["research"], item["research"])
        self.assertEqual(result["judgment_review"], [])

    def test_one_bad_sector_preserves_other_sector_and_raw_model_decisions(self):
        bank, chip = context(), context(CHIP, "芯片")
        chip["prior_active"] = False
        decisions = [decision(bank, research=research("全市场上涨61%")), decision(chip)]
        decisions[1]["phase"] = "NEW"
        class Analyst(OctopAnalyst):
            def _analyze(self, request, budget):
                return dict(status="READY", generated_at=AT, output=json.dumps(dict(decisions=decisions))), {d["sector_id"]:d for d in decisions}, set()
        analyst = Analyst(SimpleNamespace(ns="test:facts", origin="FROZEN"), {}, None, None, moment(SOURCE_AT))
        result = analyst(dict(evidence={BANK:bank,CHIP:chip}, mx=dict(status="NOT_NEEDED"),
            trade_date="2026-11-17", planned_at=SOURCE_AT, market_snapshot_id="snapshot",
            tracked_sectors=[BANK], numeric_contract=NUMERIC_CONTRACT,
            etf_selection_contract=ETF_SELECTION_CONTRACT), None)
        raw = json.loads(result["report_json"])
        self.assertEqual([row["observation_state"] for row in raw["sectors"]], ["待核实", "新增"])
        self.assertEqual(result["octop"]["judgment_review"], {BANK:["RATIONALE_NUMERIC_FACT_UNBOUND"]})
        self.assertIn("61%", result["octop"]["receipt"]["output"])
        self.assertEqual(set(result["octop"]["research_explanations"]), {BANK,CHIP})
        self.assertEqual(result["octop"]["tracked_sectors"], [BANK])


if __name__ == "__main__":
    unittest.main()
