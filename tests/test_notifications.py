"""QQ summaries preserve committed facts and an explicit stock watchlist."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from modules.strategy_hermes.stage2.notifications import QQ_FORMAT, brief_sections, report_market_scope, stock_watchlist
from modules.strategy_hermes.stage2.octop_bridge import render_brief

TEMPLATE = Path(__file__).resolve().parents[1] / "skills/a-share-hotspot-scout/qq-brief.md"


def notice(slot, report, orders, *, preview=False):
    return render_brief({"version":QQ_FORMAT, **brief_sections(slot, report, orders, preview=preview)}, TEMPLATE.read_text())

SID = "kaipanla:801027"
ETF = "512800.SH"
STOCK = "600000.SH"


def report():
    return {
        "id": "round-1", "at": "2026-11-13T14:30:35+08:00", "status": "PARTIAL",
        "raw": json.dumps({"summary": "收尾研判", "sectors": [
            {"sector_name": "银行", "core_stock_codes": [STOCK, "601398.SH", "601288.SH"]},
        ]}),
        "sectors": {SID: {"row": 0, "phase": "STRENGTHEN", "state": "UP", "label": "加强×2",
                          "sources": ["kaipanla:board-1"]}},
        "candidates": {ETF: [{"sector_id": SID, "rank": 1}], "510300.SH": [{"sector_id": SID, "rank": 2}]},
        "input_meta": {"penetration": {"sectors": {SID: {"representatives": [ETF]}}}},
    }


class QQReport(TestCase):
    def test_report_without_orders_still_has_each_section(self):
        message = notice("14:30", report(), [])
        for expected in ("收尾研判", "银行 加强×2", ETF, "无已核验个股", "无待执行节点", "报告 round-1"):
            self.assertIn(expected, message)
        self.assertNotIn("510300.SH", message)

    def test_qq_summarizes_verified_research_explanation(self):
        entry = report()
        entry["input_meta"]["octop"] = {"research_explanations": {
            SID: {"rationale": "银行板块日内强度保持"},
            "kaipanla:other": {"rationale": "未保存的板块"},
        }}
        message = notice("14:30", entry, [])
        self.assertIn("银行依据 银行板块日内强度保持", message)
        self.assertNotIn("未保存的板块", message)
        self.assertTrue(message.endswith("报告 round-1"))

    def test_stock_requires_an_explicit_code_membership_and_prior_quote(self):
        market = SimpleNamespace(body={"trade_date": "2026-11-13", "mode": "PARTIAL_REALTIME", "quotes": {
            STOCK: {"observed_at": "2026-11-13T14:28:00+08:00", "price_cny": "10.15", "source": "stock-feed"},
            "601398.SH": {"observed_at": "2026-11-13T14:31:00+08:00", "price_cny": "7.02", "source": "stock-feed"},
            "601939.SH": {"observed_at": "2026-11-13T14:28:00+08:00", "price_cny": "7.02", "source": "stock-feed"},
            ETF: {"observed_at": "2026-11-13T14:28:00+08:00", "price_cny": "1.20", "source": "tickflow"},
        }})
        entry = report()
        watch = stock_watchlist(entry, {"sectors": {SID: {"members": [STOCK, "601398.SH", "601939.SH"]}}}, market)
        self.assertEqual(watch, {SID: [STOCK]})
        entry["input_meta"]["stock_watchlist"] = watch
        message = notice("14:30", entry, [(ETF, "EXIT", 500, "PENDING", "2026-11-16T09:30:00+08:00", False)])
        self.assertIn(f"个股观察 银行 {STOCK}", message)
        self.assertIn("清仓 500 份，待 T+1（前轮）", message)

    def test_round_stock_evidence_reaches_qq_after_code_verification(self):
        market = SimpleNamespace(body={"trade_date": "2026-11-13", "mode": "PARTIAL_REALTIME", "quotes": {},
            "stock_research": {"round_slot": "14:30", "batch": "stock-1", "research_scope": {
                SID: [{"code": STOCK, "weight_ratio": ".10", "weight_as_of": "2026-10-30", "index_code": "399986.SZ"}],
            }, "quotes": {STOCK: {"observed_at": "2026-11-13T14:30:15+08:00", "price_cny": "10.15",
                                  "source": "tickflow:stock_quotes"}}}})
        mapping = {"sectors": {SID: {"members": [STOCK]}}}
        entry = report()
        self.assertEqual(stock_watchlist(entry, mapping, market, slot="14:05"), {})
        entry["input_meta"]["stock_watchlist"] = stock_watchlist(entry, mapping, market, slot="14:30")
        self.assertIn(f"个股观察 银行 {STOCK}", notice("14:30", entry, []))

    def test_summary_stays_bounded_with_stable_sections_and_report_id(self):
        entry = report()
        raw = json.loads(entry["raw"])
        raw["summary"] = "报告\n" * 1000
        raw["sectors"][0]["sector_name"] = "银行\n" * 1000
        entry["raw"] = json.dumps(raw)
        orders = [(ETF, "BUY", 100, "PENDING", entry["at"], True)] * 15
        text = notice("14:30", entry, orders)
        self.assertLessEqual(len(text), 1500)
        self.assertIn("ETF 本轮候选", text)
        self.assertIn("等 15 项", text)
        self.assertTrue(text.endswith("报告 round-1"))


    def test_hold_etf_is_an_observation_and_research_is_complete(self):
        entry = report()
        entry["sectors"][SID].update(state="HOLD", phase="MAINTAIN", label="维持")
        explanation = "一" * 179 + "。"
        entry["input_meta"].update(source_observed_at=entry["at"], octop={
            "receipt": {"status": "READY"},
            "research_explanations": {SID: {"rationale": explanation, "counter_evidence_status": "NOT_CHECKED",
                "invalidation_condition": "板块转跌", "key_unknown": "缺个股数据"}},
        })
        entry["input_meta"]["penetration"]["sectors"][SID]["products"] = [
            {"etf": "510300.SH", "rejection_reasons": ["MAINBOARD_LOW_OR_UNKNOWN"]},
        ]
        market = SimpleNamespace(body={"sectors": [
            {"sector_id": SID, "sector_name": "银行", "return_ratio": ".0155"},
            {"sector_id": "kaipanla:outside", "sector_name": "煤炭", "return_ratio": "-.025"},
        ], "quotes": {ETF: {}}, "quality": {"stock_quotes": "NOT_COLLECTED", "iopv": "NOT_COLLECTED"}})
        entry["input_meta"]["market_scope"] = report_market_scope(market, {"sectors": {SID: {}}}, entry)
        text = notice("14:30", entry, [], preview=True)
        for expected in ("市场 2板块，涨1／跌1", "已映射1／研判1", "领跌 煤炭 -2.50%",
                         "ETF 本轮候选 本轮无候选", "ETF 穿透观察 银行 512800.SH（维持）",
                         "主板权重不足或待核验", "缺本轮已核验个股报价", "IOPV 本轮缺数据",
                         explanation, "银行反证 待核实", "银行失效条件 板块转跌", "银行待核实 缺个股数据"):
            self.assertIn(expected, text)
        self.assertNotIn("无通过筛选", text)

    def test_long_research_keeps_report_id_and_marks_omitted_details(self):
        entry = report()
        raw = json.loads(entry["raw"])
        entry["input_meta"]["octop"] = {"research_explanations": {}}
        research = entry["input_meta"]["octop"]["research_explanations"]
        for i in range(6):
            sid = f"kaipanla:test{i}"
            entry["sectors"][sid] = dict(row=len(raw["sectors"]), phase="NEW", state="UP")
            raw["sectors"].append({"sector_name": f"板块{i}"})
            research[sid] = {"rationale": "一" * 179 + "。", "counter_evidence_status": "NOT_CHECKED",
                             "invalidation_condition": "二" * 119 + "。", "key_unknown": "三" * 119 + "。"}
        entry["raw"] = json.dumps(raw)
        text = notice("14:30", entry, [])
        self.assertLessEqual(len(text), 1500)
        self.assertIn("其余内容见工作台报告详情", text)
        self.assertTrue(text.endswith("报告 round-1"))


    def test_ten_discovered_boards_keep_breadth_and_fit_bridge_limits(self):
        entry = json.loads((Path(__file__).with_name("fixtures") / "qq_discovery_ten_boards.json").read_text())
        original = json.dumps(entry, sort_keys=True)
        sections = brief_sections("14:30", entry, [])
        self.assertLessEqual(len(sections["details"]), 14)
        self.assertTrue(all(len(line) <= 300 for line in sections["details"]))
        research = [line for line in sections["details"] if "依据 " in line]
        self.assertEqual(len(research), 3)
        self.assertTrue(research[0].startswith("银行依据 "))
        self.assertTrue(research[1].startswith("半导体依据 "))
        self.assertTrue(research[2].startswith("证券依据 "))
        self.assertIn("普涨观察", sections["details"][0])
        self.assertIn("覆盖75%", sections["details"][0])
        self.assertIn("上涨占比80%", sections["details"][0])
        self.assertIn("中位+1.20%", sections["details"][0])
        text = notice("14:30", entry, [])
        self.assertLessEqual(len(text), 1500)
        self.assertIn("普涨观察", text)
        self.assertIn("其余内容见工作台报告详情", text)
        self.assertTrue(text.endswith("报告 discovery-ten-boards"))
        self.assertEqual(json.dumps(entry, sort_keys=True), original)


    def test_recommendations_keep_final_top_three_order(self):
        entry = report()
        entry["input_meta"]["etf_recommendations"] = {"sectors": {SID: {
            "representatives": ["512900.SH", "512800.SH", "512700.SH"],
            "products": [dict(etf=code, recommendation_status="CONFIRMED",
                              execution_status="COMPLETE", representative_current=True)
                         for code in ["512600.SH", "512700.SH", "512800.SH", "512900.SH"]],
        }}}
        candidates = brief_sections("14:30", entry, [])["etf_candidates"]
        self.assertNotIn("512600.SH", candidates)
        self.assertLess(candidates.index("512900.SH"), candidates.index("512800.SH"))
        self.assertLess(candidates.index("512800.SH"), candidates.index("512700.SH"))

    def test_legacy_saved_unweighted_catalog_is_summarized_without_mutating_report(self):
        from modules.strategy_hermes.stage2.notifications import _recommendation_sections, _etf_gaps
        entry = report()
        products = [dict(etf=f'{158000+i:06}.SZ', matched=[], quality=['WEIGHTS_UNVERIFIED'],
                         recommendation_status='PENDING') for i in range(1351)]
        products.append(dict(etf=ETF, matched=[dict(code=STOCK, weight='1')],
            recommendation_status='STATIC_ONLY', execution_status='PARTIAL'))
        entry['input_meta']['etf_recommendations'] = dict(sectors={SID:dict(products=products,
            representatives=[], static_representatives=[ETF])})
        original = json.dumps(entry, sort_keys=True)
        candidates, observations, gaps = _recommendation_sections(entry, [SID], {SID:'银行'})
        self.assertEqual(candidates, [])
        self.assertEqual(observations, ['银行 '+ETF+'（仅静态匹配）'])
        self.assertEqual(gaps, [])
        self.assertEqual(_etf_gaps(entry, [SID], {SID:'银行'}), ['目录1351只ETF缺成分权重，穿透待核验'])
        self.assertEqual(json.dumps(entry, sort_keys=True), original)
        # A report that carries the strategy-scope count pushes only that; the catalog count stays on the workbench.
        group = entry['input_meta']['etf_recommendations']['sectors'][SID]
        group['coverage_gaps'] = dict(weights=dict(reason='WEIGHTS_UNVERIFIED', products=1351, scope_products=0, indexes=373))
        self.assertEqual(_etf_gaps(entry, [SID], {SID:'银行'}), [])
        group['coverage_gaps']['weights']['scope_products'] = 4
        self.assertEqual(_etf_gaps(entry, [SID], {SID:'银行'}), ['策略范围内4只ETF缺成分权重，穿透待核验'])

    def test_observations_prioritize_static_representative_then_known_exposure(self):
        from modules.strategy_hermes.stage2.notifications import _recommendation_sections
        entry = report()
        entry['input_meta']['etf_recommendations'] = dict(sectors={SID:dict(
            representatives=[], static_representatives=[ETF], products=[
                dict(etf='510000.SH', metrics=dict(E='.2'), matched=[dict(code=STOCK)], recommendation_status='PENDING'),
                dict(etf='510001.SH', metrics=dict(E='.7'), matched=[dict(code=STOCK)], recommendation_status='PENDING'),
                dict(etf=ETF, metrics=dict(E='.6'), matched=[dict(code=STOCK)], recommendation_status='STATIC_ONLY')])})
        _, observations, _ = _recommendation_sections(entry, [SID], {SID:'银行'})
        self.assertIn(ETF, observations[0])
        self.assertIn('510001.SH', observations[1])
        self.assertIn('510000.SH', observations[2])

    def test_static_representative_precedes_ordinary_relationships_from_other_boards(self):
        from modules.strategy_hermes.stage2.notifications import _recommendation_sections
        entry = report()
        chip = 'kaipanla:801001'
        entry['input_meta']['etf_recommendations'] = dict(sectors={
            chip:dict(representatives=[], products=[dict(etf='159001.SZ', matched=[dict(code=STOCK)],
                metrics=dict(E='.8'), recommendation_status='PENDING')]),
            SID:dict(representatives=[], static_representatives=[ETF], products=[dict(etf=ETF,
                matched=[dict(code=STOCK)], metrics=dict(E='.6'), recommendation_status='STATIC_ONLY')])})
        _, observations, _ = _recommendation_sections(entry, [chip, SID], {chip:'芯片', SID:'银行'})
        self.assertIn('银行 '+ETF, observations[0])
        self.assertIn('芯片 159001.SZ', observations[1])
