"""Regression checks for structured, source-bound OCTOP observations."""
import copy
import json
import sys
import unittest

from modules.strategy_hermes.adapter import normalize
from modules.strategy_hermes.stage2.octop_decision import render


class OctopDecision(unittest.TestCase):
    def setUp(self):
        self.receipt = dict(report_id="source-facts", generated_at="2026-11-12T20:00:00+08:00",
                            finish_reason="stop")
        self.evidence = {
            "sector_id": "kaipanla:801001", "sector_name": "芯片", "trade_date": "2026-11-12",
            "data_as_of": "2026-11-12T15:05:02+08:00", "prior_active": None,
            "previous": [], "current": [{"ref": "source:board", "observed_at": "2026-11-12T19:58:49+08:00"}],
            "facts": dict(sector_exposure="0.9982", constituent_quote_coverage="0", quality=["LOW_COVERAGE"]),
        }
        self.decision = dict(sector_id="kaipanla:801001", phase="UNKNOWN", evidence_refs=["source:board"])

    def render(self, **changes):
        self.receipt["output"] = json.dumps({**self.decision, **changes})
        return render(self.receipt, self.evidence)

    def test_display_uses_program_facts_and_actual_source_time(self):
        report = self.render()["report"]
        self.assertIn("板块权重暴露 99.82%", report["summary"])
        self.assertIn("成分股报价覆盖 0.00%", report["summary"])
        self.assertEqual(report["data_as_of"], "2026-11-12T19:58:49+08:00")
        self.assertEqual(report["source_facts"]["input_as_of"], "2026-11-12T15:05:02+08:00")
        with self.assertRaisesRegex(ValueError, "read-only OCTOP observation"):
            normalize({"report_json": json.dumps(report)})

    def test_fabricated_facts_and_references_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "only the direction"):
            self.render(sector_exposure="0.11")
        with self.assertRaisesRegex(ValueError, "non-current evidence"):
            self.render(evidence_refs=["source:missing"])
        with self.assertRaisesRegex(ValueError, "comparable active"):
            self.render(phase="WEAKEN")
        self.receipt["output"] = '{"sector_id":"kaipanla:801001","phase":"UNKNOWN","phase":"NEW","evidence_refs":["source:board"]}'
        with self.assertRaisesRegex(ValueError, "duplicate field"):
            render(self.receipt, self.evidence)

    def test_optional_stock_codes_do_not_change_valid_sector_phase(self):
        self.evidence["prior_active"] = False
        self.evidence["stock_evidence"] = [
            {"code": "600036.SH", "observed_at": "2026-11-12T19:59:01+08:00"},
        ]
        selected = self.render(phase="NEW", core_stock_codes=["600036.SH"])["report"]
        self.assertEqual(selected["sectors"][0]["core_stock_codes"], ["600036.SH"])
        self.assertEqual(selected["data_as_of"], "2026-11-12T19:59:01+08:00")
        unknown = self.render(phase="NEW", core_stock_codes=["600001.SH"])["report"]
        self.assertEqual(unknown["sectors"][0]["observation_state"], "新增")
        self.assertNotIn("core_stock_codes", unknown["sectors"][0])
        self.assertIn("STOCK_SELECTION_UNCONFIRMED", unknown["warnings"])
        old = self.render(phase="NEW")["report"]
        self.assertNotIn("core_stock_codes", old["sectors"][0])
        self.assertNotIn("STOCK_SELECTION_UNCONFIRMED", old["warnings"])

    def test_research_retains_only_verified_evidence_without_changing_report(self):
        self.evidence["prior_active"] = False
        self.evidence["stock_evidence"] = [
            {"code": "600036.SH", "ref": "stock:fixture", "observed_at": "2026-11-12T19:59:01+08:00"},
        ]
        explanation = dict(role="ROTATION", rationale="板块表现强，个股存在分化",
                           counter_evidence_status="FOUND", counter_evidence_refs=["stock:fixture"],
                           invalidation_condition="下一轮同口径涨幅转负", key_unknown="参与范围")
        original = self.render(phase="NEW")
        verified = self.render(phase="NEW", research=explanation)
        self.assertEqual(verified["research"], explanation)
        self.assertEqual(verified["report"], original["report"])
        self.assertEqual(verified["judgments"], original["judgments"])
        for changed in (dict(counter_evidence_refs=["other:sector"]), dict(rationale="a" * 181),
                        dict(counter_evidence_refs=[]), dict(role="EVENT_FIRST")):
            bad = self.render(phase="NEW", research={**explanation, **changed})
            self.assertIsNone(bad["research"])
            self.assertEqual(bad["report"], original["report"])

    def test_research_requires_comparison_to_say_no_counter_evidence(self):
        base = dict(role="WATCH", rationale="现有证据有限",
                    counter_evidence_status="NOT_FOUND", counter_evidence_refs=[],
                    invalidation_condition="观察新增快照", key_unknown="没有独立前序比较")
        self.assertIsNone(self.render(research=base)["research"])
        checked = self.render(research={**base, "counter_evidence_status": "NOT_CHECKED"})
        self.assertEqual(checked["research"]["counter_evidence_status"], "NOT_CHECKED")
        without_refs = {key: value for key, value in base.items() if key != "counter_evidence_refs"}
        without_refs["counter_evidence_status"] = "NOT_CHECKED"
        self.assertEqual(self.render(research=without_refs)["research"], checked["research"])
        without_refs["counter_evidence_status"] = "FOUND"
        self.assertIsNone(self.render(research=without_refs)["research"])
        self.assertIsNone(self.render(research={**base, "rationale": "正文\n伪造第二行"})["research"])

    def test_missing_observation_keeps_prior_source_time(self):
        self.evidence = copy.deepcopy(self.evidence)
        self.evidence.update(prior_active=True, previous=self.evidence["current"], current=[])
        self.evidence["facts"]["constituent_quote_coverage"] = None
        result = self.render(phase="NOT_COVERED", evidence_refs=[])
        self.assertEqual(result["normalized"]["kaipanla:801001"]["state"], "UNKNOWN")
        self.assertIn("NO_CURRENT_SECTOR_EVIDENCE", result["report"]["warnings"])
        self.assertNotIn("成分股报价覆盖", result["report"]["summary"])
        self.assertEqual(result["report"]["data_as_of"], self.evidence["previous"][0]["observed_at"])

    def test_command_transport_reopens_a_closed_bridge(self):
        from modules.strategy_hermes.stage2.schedule import CommandClient

        script = (
            "import json,sys; request=json.loads(sys.stdin.readline()); "
            "print(json.dumps(dict(protocol=request['protocol'], id=request['id'], "
            "result=dict(operation=request['operation']))), flush=True)"
        )
        client = CommandClient([sys.executable, "-B", "-c", script])
        self.addCleanup(client.close)
        self.assertEqual(client.call("mx", {}, timeout=5), {"operation": "mx"})
        client.child.wait(timeout=5)
        self.assertEqual(client.call("analyze", {}, timeout=5), {"operation": "analyze"})

    def test_intraday_assembly_preserves_source_times_and_partial_inputs(self):
        from contextlib import nullcontext
        from types import SimpleNamespace

        from modules.strategy_hermes.stage2.inputs import SharedMarket
        from modules.strategy_hermes.stage2.intraday import assemble

        day = "2026-11-12"
        prior_at = day + "T09:34:00+08:00"
        board_at = day + "T10:35:02+08:00"
        prior = SharedMarket.read(dict(
            schema_version="hotspot_market_snapshot_v1", snapshot_id="previous-round",
            trade_date=day, observed_at=prior_at, quotes={"512480.SH": {"observed_at":prior_at,"price_cny":"1"}},
        ))
        payload = {"board":json.dumps({"reported_total":243,"trade_days":[day],
                                     "rows":[["801001","芯片","3000","-1.5"]]})}
        conn = SimpleNamespace(execute=lambda sql, args: SimpleNamespace(fetchone=lambda: (payload[args[0]],)))
        ledger = SimpleNamespace(_connect=lambda: nullcontext(conn))
        market = assemble(ledger,day,prior,None,[dict(batch="board",source_clock=board_at,total=243)])
        self.assertEqual(market["observed_at"],board_at)
        self.assertEqual(market["quotes"]["512480.SH"]["observed_at"],prior_at)
        self.assertEqual(market["quality"]["fresh_etf_count"],0)
        self.assertEqual(market["quality"]["fresh_board_count"],1)
        self.assertIsNone(assemble(ledger,"2026-11-13",prior,None,[]))


    def test_relay_eof_is_detected_before_next_collection(self):
        import os
        from modules.strategy_hermes.stage2.octop_client import OctopClient

        reader, writer = os.pipe()
        try:
            client=OctopClient(reader,writer)
            self.assertTrue(client.peer_open())
            os.close(writer)
            writer=None
            self.assertFalse(client.peer_open())
        finally:
            os.close(reader)
            if writer is not None:
                os.close(writer)

    def test_core_leaders_are_independent_of_watch_picks_and_require_saved_sources(self):
        self.evidence["prior_active"] = False
        self.evidence["current"][0]["observed_at"] = "2026-11-12T14:05:00+08:00"
        self.evidence["stock_evidence"] = [
            dict(code=code, ref="stock:"+code, source="tickflow:stock_quotes",
                 observed_at="2026-11-12T14:05:02+08:00")
            for code in ("600036.SH", "600001.SH")]
        result = self.render(phase="NEW", core_stock_codes=["600036.SH"], core_leader_codes=["600001.SH"])
        row = result["report"]["sectors"][0]
        self.assertEqual(row["core_stock_codes"], ["600036.SH"])
        self.assertEqual(row["core_leader_codes"], ["600001.SH"])
        self.assertEqual(result["core_leaders"]["status"], "VERIFIED")
        self.assertEqual(result["core_leaders"]["evidence"][0]["ref"], "stock:600001.SH")
        missing = self.render(phase="NEW")
        self.assertEqual(missing["core_leaders"]["status"], "UNKNOWN")
        empty = self.render(phase="NEW", core_leader_codes=[])
        self.assertEqual(empty["core_leaders"]["status"], "VERIFIED")
        invented = self.render(phase="NEW", core_leader_codes=["600999.SH"])
        self.assertEqual(invented["core_leaders"]["status"], "UNKNOWN")
        self.assertEqual(invented["normalized"][self.evidence["sector_id"]]["phase"], "NEW")
        self.evidence["stock_evidence"][1]["observed_at"] = "2026-11-12T19:00:00+08:00"
        late = self.render(phase="NEW", core_leader_codes=["600001.SH"])
        self.assertEqual(late["core_leaders"]["status"], "UNKNOWN")
