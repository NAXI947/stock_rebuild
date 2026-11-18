"""Integration checks for Stage 2 using a private namespace in stock_v4_test."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.simulator import moment
from test_stage1 import A, D1, D2, D3, DAYS, ETF, ETF2, quote, report, stamp

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")
STOCK_A, STOCK_B, STOCK_C = "600001.SH", "600002.SH", "600003.SH"


@unittest.skipUnless(ENV, "set STAGE2_TEST_ENV_FILE to run PostgreSQL/Redis integration")
class StageTwo(unittest.TestCase):
    def setUp(self):
        from modules.strategy_hermes.stage2.inputs import cache_client
        from modules.strategy_hermes.stage2.postgres import PostgresLedger
        from modules.strategy_hermes.stage2.settings import Settings
        settings=Settings.read(ENV)
        self.assertEqual(settings.env,"test")
        self.ns=settings.redis_prefix+"integration:"+uuid4().hex
        self.settings=replace(settings,namespace=self.ns)
        self.store=PostgresLedger(self.settings,"FROZEN")
        self.store.migrate()
        self.cache=cache_client(self.settings)
        self.key=settings.redis_prefix+"fixture:"+uuid4().hex
        self.addCleanup(self.clean)
        self.map=dict(schema_version="stock-v4-etf-mapping-v1",version="fixture-v1",
                      sectors={A:{"members":[STOCK_A,STOCK_B], "reported_count":2, "unresolved_count":0,
                                  "source_batches":["frozen-members"], "as_of_date":D1}},
                      products={ETF:dict(index_code="index-A",trade_unit=100,
                          rule=dict(verified=True,index_code="index-A",scope="domestic_passive_sector",
                                    price_limit_ratio=".1",verified_at=stamp(D1,"09:00")))},
                      weights={"index-A":dict(complete=True,as_of_date="2026-08-05",rows=[
                          dict(code=STOCK_A,weight=".60",mainboard=True),
                          dict(code=STOCK_B,weight=".10",mainboard=True),
                          dict(code=STOCK_C,weight=".30",mainboard=False),
                      ])})

    def clean(self):
        prefix=self.settings.redis_prefix+self.ns.removeprefix(self.settings.redis_prefix)+":"
        keys=list(self.cache.scan_iter(match=prefix+"snap:*"))
        keys.extend(self.cache.scan_iter(match=self.ns+":market:*"))
        self.cache.delete(self.key,self.key+":observed_at",self.key+":revision_seq",*keys)
        self.cache.close()
        with self.store._connect() as conn, conn.transaction():
            for table in ("simulation_fills","simulation_orders","outbox","round_tasks","snapshot_state",
                          "rotation","latest","cycles","events","rule_versions","runs"):
                conn.execute(f"DELETE FROM {table} WHERE ns=%s",(self.ns,))

    def market(self, day, hour, price, revision=0):
        from modules.strategy_hermes.stage2.inputs import shared_market
        at=stamp(day,hour)
        doc=dict(schema_version="hotspot_market_snapshot_v1",snapshot_id=f"fixture-{revision}",
                 revision_seq=revision,trade_date=day,observed_at=at,source="frozen:feed",
                 mode="PARTIAL_REALTIME",quality={"coverage_ratio":"0.6"},
                 quotes={STOCK_A:dict(observed_at=at,return_ratio=".02",price_cny="10.2",source="frozen:feed"),
                         ETF:dict(observed_at=at,price_cny=price,revision_seq=revision,source="frozen:feed"),
                         ETF2:dict(observed_at=at,price_cny="invalid",source="frozen:feed")},
                 stock_research=dict(quotes={STOCK_A:dict(observed_at=at,return_ratio=".02",
                                                         price_cny="10.2",source="frozen:feed")}))
        self.cache.set(self.key,encode(doc),ex=60)
        return shared_market(self.store,self.cache,key=self.key)

    def publish(self, rid, day, slot, state, market, *, targets=("redis","octop")):
        from modules.strategy_hermes.stage2.worker import planned_at, publish_round
        task=self.store.claim_round(day,slot,planned_at(day,slot))
        self.assertIsNotNone(task)
        source=report(rid,day,slot,[(A,state)])
        envelope=dict(input_kind="FROZEN",published_at=stamp(day,slot),
                      report_json=source["report_json"],judgments=source["judgments"],
                      octop=dict(etf_choices={A:[ETF] if state in ("新增", "加强", "维持") else []}))
        saved=publish_round(self.store,task,envelope,DAYS,self.map,market,targets=targets)
        self.assertEqual(self.store.report(rid)["raw"],source["report_json"])
        self.assertIsNone(self.store.claim_round(day,slot,planned_at(day,slot)))
        return saved

    def quotes(self, day, hour, price, revision):
        market=self.market(day,hour,price,revision)
        events,issues=market.quote_events({ETF:100, ETF2:100},moment(stamp(day,hour)))
        self.assertEqual(issues,{ETF2:"QUOTE_INVALID_OR_UNVERIFIED"})
        self.store.apply_quotes(events,DAYS,{ETF:100})
        self.store.apply_quotes(events,DAYS,{ETF:100})

    def test_preview_cannot_publish_as_a_round(self):
        from modules.strategy_hermes.stage2.worker import planned_at, publish_round
        slot = "09:35"
        claim = self.store.claim_round(D1, slot, planned_at(D1, slot))
        event = report("preview", D1, slot, [(A, "新增")])
        raw = json.loads(event["report_json"])
        raw["purpose"] = "READ_ONLY_OBSERVATION"
        envelope = dict(input_kind="FROZEN", published_at=stamp(D1, slot),
                        report_json=json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "read-only OCTOP observation"):
            publish_round(self.store, claim, envelope, DAYS, self.map, None)
        with self.assertRaisesRegex(ValueError, "read-only OCTOP observation"):
            self.store.publish(claim, dict(kind="report", **envelope), DAYS, {ETF:100}, {})
        with self.store._connect() as conn:
            for table in ("events", "simulation_orders", "outbox"):
                self.assertEqual(conn.execute(
                    f"SELECT count(*) FROM {table} WHERE ns=%s", (self.ns,)
                ).fetchone()[0], 0)

    def test_run_due_five_slots_tracks_open_holdings(self):
        from modules.strategy_hermes.stage2.worker import SLOTS, run_due
        requests = []

        def analyst(request, budget, timeout):
            requests.append(request)
            slot = request["round_slot"]
            states = [(A, "新增")] if slot == SLOTS[0] else []
            event = report("five-" + slot, D1, slot, states)
            return dict(input_kind="FROZEN", published_at=request["planned_at"],
                        report_json=event["report_json"], judgments=event["judgments"],
                        octop=dict(etf_choices={A:[ETF]} if states else {}))

        for slot in SLOTS:
            now = moment(stamp(D1, slot))
            market = self.market(D1, slot, "10")
            saved = run_due(self.store, now, DAYS, self.map, market, analyst)
            self.assertEqual(saved["round_slot"], slot)
            self.assertEqual(run_due(self.store, now, DAYS, self.map, market, analyst),
                             "ROUND_ALREADY_CLAIMED")
            if slot == SLOTS[0]:
                self.quotes(D1, "09:36", "10", 1)

        self.assertEqual([row["round_slot"] for row in requests], list(SLOTS))
        self.assertEqual([row["tracked_sectors"] for row in requests], [[], [A], [A], [A], [A]])
        cycle, = self.store.inspect()["cycles"]
        self.assertEqual(cycle["status"], "OPEN")
        self.assertEqual([order["action"] for order in cycle["orders"]], ["BUY"])

    @unittest.skipUnless(os.environ.get("OCTOP_DECISION_ACCEPTANCE_FILE"), "set archived OCTOP acceptance path")
    def test_archived_octop_decisions_drive_frozen_cycle(self):
        from modules.strategy_hermes.stage2.postgres import PostgresLedger
        from modules.strategy_hermes.stage2.worker import run_due

        path = Path(os.environ["OCTOP_DECISION_ACCEPTANCE_FILE"])
        batch = json.loads(path.read_bytes())
        cases = {item["case"]: item for item in batch["results"]}
        schedule = {
            (D1, "09:35"): "new",
            (D1, "10:35"): "maintain",
            (D1, "13:05"): "strengthen-low-coverage",
            (D1, "14:05"): "weaken-held",
            (D1, "14:30"): "not-covered-held",
            (D2, "10:35"): "retreat-held",
        }
        for name in schedule.values():
            self.assertTrue(cases[name]["passed"])
            self.assertEqual(cases[name]["evidence"]["input_kind"], "FROZEN")

        sector_id = cases["new"]["evidence"]["sector_id"]
        self.map["sectors"][sector_id] = self.map["sectors"].pop(A)
        calls = []

        def analyst(request, budget, timeout):
            key = (request["trade_date"], request["round_slot"])
            calls.append((key, request["tracked_sectors"]))
            name = schedule[key]
            sample = cases[name]
            at = request["planned_at"]
            frozen = {
                "schema_version": "hermes_sector_rotation_v1", "report_id": "frozen-" + name,
                "trade_date": key[0], "generated_at": at, "data_as_of": at,
                "status": sample["report"]["status"], "sectors": sample["report"]["sectors"],
                "research_context": {"evidence_key": "frozen:" + name},
            }
            return dict(input_kind="FROZEN", published_at=at, report_json=encode(frozen),
                        judgments=sample["judgments"], octop=dict(etf_choices={sector_id:[ETF]}))

        for revision, ((day, slot), name) in enumerate(schedule.items(), 1):
            market = self.market(day, slot, "10", revision)
            saved = run_due(self.store, moment(stamp(day, slot)), DAYS, self.map, market, analyst)
            self.assertEqual(saved["round_slot"], slot)
            self.assertEqual(run_due(self.store, moment(stamp(day, slot)), DAYS, self.map, market, analyst),
                             "ROUND_ALREADY_CLAIMED")
            if name == "new":
                self.quotes(D1, "09:36", "10", revision)
            if name == "weaken-held":
                self.quotes(D1, "14:06", "11", revision)
                self.assertEqual(self.store.inspect()["cycles"][0]["pending_reason"], "WAIT_T1")
            if name == "not-covered-held":
                result = self.store.inspect()
                self.assertIn(sector_id, result["rotation"])
                self.assertEqual(result["cycles"][0]["pending_reason"], "WAIT_T1")
                self.quotes(D2, "09:36", "11", revision)
            if name == "retreat-held":
                self.assertNotIn(sector_id, self.store.inspect()["rotation"])
                self.quotes(D2, "10:36", "12", revision)

        self.assertEqual([tracked for _, tracked in calls], [[], *([[sector_id]] * 5)])
        recovered = PostgresLedger(self.settings, "FROZEN").inspect()
        cycle, = recovered["cycles"]
        self.assertEqual([row["action"] for row in cycle["orders"]], ["BUY", "REDUCE", "EXIT"])
        self.assertEqual([row["quantity"] for row in cycle["book"]["fills"]], [1000, 500, 500])
        self.assertEqual((cycle["status"], cycle["valuation"]["profit"]), ("CLOSED", "1500.00"))

    def test_scheduled_octop_five_slots_and_mx_budget(self):
        from modules.strategy_hermes.stage2.inputs import SharedMarket
        from modules.strategy_hermes.stage2.octop_analyst import OctopAnalyst
        from modules.strategy_hermes.stage2.worker import SLOTS, scheduled_tick

        class Peer:
            def __init__(self):
                self.operations = []
                self.mx_fails = False
                self.reject_model = False
                self.analysis_failures = []
                self.sectors = []

            def call(self, operation, payload, *, timeout):
                self.operations.append(operation)
                if operation == "mx":
                    if self.mx_fails:
                        raise TimeoutError("frozen provider timeout")
                    return dict(status="READY", source="octop:mx-search", requests=1, http_status=200, items=[
                        dict(title="早间公告", published_at=stamp(D1, "09:20")),
                        dict(title="盘后公告", published_at=stamp(D1, "15:30")),
                    ])
                if self.reject_model:
                    return dict(status="UNAVAILABLE", retryable=False, requests=1, http_status=[401])
                if self.analysis_failures:
                    failure = self.analysis_failures.pop(0)
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                sector, = payload["sectors"]
                self.sectors.append(sector)
                decision = dict(sector_id=A, phase="UNKNOWN",
                                fact_refs=["sector_return"], etf_choices=[],
                                evidence_refs=[row["ref"] for row in sector["current"]])
                if payload["round_slot"] in ("09:35", "10:35"):
                    ref = sector["current"][0]["ref"] if payload["round_slot"] == "09:35" else "invented"
                    decision["research"] = dict(role="WATCH", rationale="方向待核实",
                                                counter_evidence_status="FOUND", counter_evidence_refs=[ref],
                                                invalidation_condition="后续可比观测改变", key_unknown="参与范围")
                assert payload["research_explanations"] is True
                assert payload["research_sector_ids"] == [A]
                return dict(status="READY", requests=1, http_status=[200], tool_calls=0,
                            finish_reason="stop", generated_at=payload["planned_at"],
                            output=encode({"decisions": [decision]}))

        def tick(day, slot, peer, revision=0):
            at = stamp(day, slot)
            market = SharedMarket.read(dict(
                schema_version="hotspot_market_snapshot_v1", snapshot_id=f"schedule-{revision}",
                trade_date=day, observed_at=at, quotes={},
                sectors=[dict(sector_id=A, sector_name="测试板块", observed_at=at,
                              source_batch=f"schedule-{revision}", return_ratio="0.01", strength="60")],
            ))
            now = moment(at)
            analyst = OctopAnalyst(self.store, self.map, market, peer, now)
            return scheduled_tick(self.store, self.cache, now, DAYS, self.map, market, analyst)

        peer = Peer()
        reports = {}
        for revision, slot in enumerate(SLOTS):
            result = tick(D1, slot, peer, revision)
            self.assertEqual(result["round"], "PUBLISHED")
            self.assertEqual(tick(D1, slot, peer, revision)["round"], "ROUND_ALREADY_CLAIMED")
            reports[slot] = self.store.report(result["report_id"])
        self.assertEqual(peer.operations, ["analyze", "mx", "analyze", "analyze", "analyze", "analyze"])
        first = reports["09:35"]["input_meta"]["octop"]
        self.assertEqual(first["research_explanations"][A]["role"], "WATCH")
        ref, = first["research_explanations"][A]["counter_evidence_refs"]
        self.assertEqual(first["research_explanation_evidence"][A][ref]["ref"], ref)
        second = reports["10:35"]["input_meta"]["octop"]
        self.assertEqual(second["research_explanations"][A]["source"], "PROGRAM_FACTS")
        self.assertIn("测试板块板块涨跌+1.00%", second["research_explanations"][A]["rationale"])
        self.assertEqual(second["research_explanations_unconfirmed"], [A])
        self.assertEqual(reports["09:35"]["sectors"][A]["phase"], "UNKNOWN")
        self.assertEqual(reports["10:35"]["sectors"][A]["phase"], "UNKNOWN")
        self.assertEqual([item["title"] for item in reports["10:35"]["input_meta"]["octop"]["mx"]["items"]],
                         ["早间公告"])
        self.assertEqual([item["title"] for item in peer.sectors[1]["current"][1:]], ["早间公告"])
        self.assertEqual(tick("2026-10-17", "10:35", peer)["round"], "NO_DUE_ROUND")
        self.assertEqual(tick(D1, "10:43", peer)["round"], "NO_DUE_ROUND")

        peer.mx_fails = True
        failure = tick(D2, "10:35", peer)
        self.assertEqual((failure["round"], failure["mx"]), ("PUBLISHED", "UNAVAILABLE"))
        self.assertEqual(peer.operations[-2:], ["mx", "analyze"])
        peer.reject_model = True
        logger = "modules.strategy_hermes.stage2.octop_analyst"
        with self.assertLogs(logger, level="WARNING") as rejected_logs:
            rejected = tick(D2, "13:05", peer)
        self.assertEqual((rejected["round"], rejected["round_error"]), ("FAILED", "OctopRejected"))
        self.assertIn('"http_status":401', rejected_logs.output[0])
        calls = len(peer.operations)
        self.assertEqual(tick(D2, "13:05", peer)["round"], "ROUND_ALREADY_CLAIMED")
        self.assertEqual(len(peer.operations), calls)

        peer.reject_model = False
        peer.analysis_failures = [
            dict(status="UNAVAILABLE", retryable=True, requests=0, http_status=[],
                 error_type="APIConnectionError", error="sensitive response"),
            dict(status="UNAVAILABLE", retryable=True, requests=1, http_status=[503],
                 error_type="sensitive exception name", output="sensitive output"),
        ]
        with self.assertLogs(logger, level="WARNING") as exhausted_logs:
            exhausted = tick(D2, "14:05", peer)
        self.assertEqual((exhausted["round"], exhausted["round_error"]), ("FAILED", "TimeoutError"))
        first, second = [json.loads(record.getMessage().partition("failed ")[2]) for record in exhausted_logs.records]
        self.assertEqual((first["error"], first["http_requests"], first["http_status"]),
                         ("APIConnectionError", 0, None))
        self.assertEqual((second["error"], second["http_requests"], second["http_status"]),
                         ("UNCLASSIFIED_REPLY", 1, 503))
        self.assertNotIn("sensitive", " ".join(exhausted_logs.output))
        calls = len(peer.operations)
        self.assertEqual(tick(D2, "14:05", peer)["round_error"], "TimeoutError")
        self.assertEqual(len(peer.operations), calls)
        with self.store._connect() as conn:
            failed = conn.execute(
                "SELECT state,error_code,calls FROM round_tasks WHERE ns=%s AND trade_date=%s AND slot='14:05'",
                (self.ns, D2),
            ).fetchone()
        self.assertEqual((failed[0],failed[1],json.loads(failed[2])),("FAILED","TimeoutError",{"hermes":2}))

        peer.analysis_failures = [TimeoutError("sensitive transport detail")]
        with self.assertLogs(logger, level="WARNING") as bridge_logs:
            recovered = tick(D2, "14:30", peer)
        self.assertEqual(recovered["round"], "PUBLISHED")
        self.assertIn('"error":"BRIDGE_TIMEOUT"', bridge_logs.output[0])
        self.assertIn('"http_requests":null', bridge_logs.output[0])
        self.assertNotIn("sensitive", bridge_logs.output[0])

        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT slot,calls,state FROM round_tasks WHERE ns=%s AND trade_date=%s ORDER BY slot",
                (self.ns, D1),
            )
            actual = [(slot, json.loads(calls), state) for slot, calls, state in rows]
        self.assertEqual(actual, [(slot, dict(hermes=1, **({"mx": 1} if slot == "10:35" else {})),
                                  "PUBLISHED") for slot in SLOTS])

    def test_qq_outbox_retries_rate_limit_and_stops_ambiguous_send(self):
        from modules.strategy_hermes.stage2.notifications import deliver_qq
        from modules.strategy_hermes.stage2.outbox import dispatch

        from modules.strategy_hermes.stage2.octop_bridge import render_brief

        store = self.store

        class Peer:
            def __init__(self):
                self.responses = iter([
                    {"status":"RATE_LIMITED","http_status":429,"message_posts":1},
                    {"status":"ACCEPTED","http_status":200,"message_posts":1},
                    {"status":"UNCERTAIN","message_posts":1},
                ])
                self.formats, self.sends = [], []

            def call(self, operation, payload, *, timeout):
                if operation == "format":
                    template = Path(__file__).resolve().parents[1] / "skills/a-share-hotspot-scout/qq-brief.md"
                    text = render_brief(payload, template.read_text())
                    self.formats.append(text)
                    return {"status":"READY", "text":text, "template":"octop-markdown-v1:deployment:" + "a"*64}
                self.sends.append((operation, payload))
                with store._connect() as conn:
                    body, = conn.execute("SELECT payload FROM outbox WHERE ns=%s AND event_key=%s AND target='octop'",
                                         (store.ns,payload["event_key"])).fetchone()
                self.assert_frozen_text(body, payload["text"])
                return next(self.responses)

            @staticmethod
            def assert_frozen_text(body, text):
                assert json.loads(body)["text"] == text

        self.publish("qq-entry",D1,"09:35","新增",self.market(D1,"09:34","10"))
        before = self.store.inspect()["cycles"][0]
        self.assertEqual(before["pending_reason"], "WAIT_PRICE")
        self.assertEqual(before["orders"][0]["status"], "PENDING")
        self.assertEqual(before["book"]["fills"], [])
        self.quotes(D1,"09:36","10",1)
        peer = Peer()
        send = lambda event: deliver_qq(self.store,peer,event)
        self.assertEqual(dispatch(self.store,"octop",send,retry_delay=0),"PENDING")
        self.assertEqual(dispatch(self.store,"octop",send,retry_delay=0),"SENT")
        self.assertEqual(peer.sends[0],peer.sends[1])
        self.assertEqual(len(peer.formats),0)
        # The body freezes at report publication, before the following quote fills it.
        self.assertIn("510001.SH 买入 [等待节点后新报价]",peer.sends[0][1]["text"])
        after = self.store.inspect()["cycles"][0]
        self.assertIsNone(after["pending_reason"])
        self.assertEqual(after["orders"][0]["status"], "FILLED")
        self.assertEqual(after["orders"][0]["target"], 1000)
        self.assertNotIn("1000份",peer.sends[0][1]["text"])
        self.assertIn("🤖 节点：",peer.sends[0][1]["text"])
        self.publish("qq-weak",D1,"10:35","减弱",self.market(D1,"10:34","11",2))
        self.assertEqual(dispatch(self.store,"octop",send,retry_delay=0),"FAILED")
        self.assertEqual(dispatch(self.store,"octop",send,retry_delay=0),"EMPTY")
        self.assertEqual(len(peer.formats),0)
        self.assertEqual(self.store.inspect()["evidence_counts"]["report"],2)
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT event_key,state,attempts,error_code,delivery_receipt FROM outbox WHERE ns=%s AND target='octop' ORDER BY id",
                (self.ns,),
            ).fetchall()
        self.assertEqual(rows[0][:4],("report:qq-entry","SENT",2,None))
        self.assertEqual(json.loads(rows[0][4])["http_status"],200)
        self.assertEqual(rows[1][:4],("report:qq-weak","FAILED",1,"DeliveryUncertain"))

    def test_qq_enqueues_a_report_without_trade_nodes(self):
        self.publish("qq-no-nodes",D1,"09:35","维持",None)
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT event_key,payload FROM outbox WHERE ns=%s AND target='octop'", (self.ns,),
            ).fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][0],"report:qq-no-nodes")
        payload = json.loads(rows[0][1])
        self.assertEqual(payload["format"],"qq-template-v2")
        self.assertEqual(payload["template_kind"],"research")
        self.assertNotIn("text", payload)
        sections = payload["sections"]
        self.assertEqual((sections["etf_candidates"],sections["stocks"],sections["simulation"]),
                         ("本轮无","无已核验个股","无待执行"))

    def test_legacy_qq_format_retries_before_send_and_freezes_text_for_rate_limit(self):
        from modules.strategy_hermes.stage2.notifications import brief_sections, deliver_qq
        from modules.strategy_hermes.stage2.outbox import dispatch
        from modules.strategy_hermes.stage2.octop_bridge import render_brief
        saved=self.publish("qq-legacy",D1,"09:35","维持",None)
        body=dict(format="octop-markdown-v1",sections=brief_sections("09:35",saved,[]))
        with self.store._connect() as conn:
            conn.execute("UPDATE outbox SET payload=%s WHERE ns=%s AND target='octop'",(encode(body),self.ns))
        calls=[];formatted=[]
        template=Path(__file__).resolve().parents[1]/"skills/a-share-hotspot-scout/qq-brief.md"
        def call(operation,payload,*,timeout):
            calls.append((operation,payload))
            if operation=="format":
                if len([item for item in calls if item[0]=="format"])==1:
                    return {"status":"UNAVAILABLE"}
                text=render_brief(payload,template.read_text())
                formatted.append(text)
                return dict(status="READY",text=text,template="octop-markdown-v1:deployment:"+"a"*64)
            with self.store._connect() as conn:
                stored=conn.execute("SELECT payload FROM outbox WHERE ns=%s AND target='octop'",(self.ns,)).fetchone()[0]
            self.assertEqual(json.loads(stored)["text"],payload["text"])
            return dict(status="RATE_LIMITED" if len([item for item in calls if item[0]=="qq"])==1 else "ACCEPTED",
                        message_posts=1,http_status=429 if len([item for item in calls if item[0]=="qq"])==1 else 200)
        from types import SimpleNamespace
        send=lambda event:deliver_qq(self.store,SimpleNamespace(call=call),event)
        self.assertEqual(dispatch(self.store,"octop",send,retry_delay=0),"PENDING")
        self.assertEqual([item[0] for item in calls],["format"])
        self.assertEqual(dispatch(self.store,"octop",send,retry_delay=0),"PENDING")
        self.assertEqual(dispatch(self.store,"octop",send,retry_delay=0),"SENT")
        self.assertEqual([item[0] for item in calls],["format","format","qq","qq"])
        self.assertEqual(calls[-2],calls[-1])
        self.assertEqual(len(formatted),1)

    def test_expired_qq_render_without_text_can_retry(self):
        from modules.strategy_hermes.stage2.outbox import claim, save_qq_text

        self.publish("qq-format-interrupted",D1,"09:35","维持",None)
        first = claim(self.store,"octop")
        with self.store._connect() as conn:
            conn.execute("UPDATE outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (first.id,))
        second = claim(self.store,"octop")
        self.assertEqual((second.id,second.attempt),(first.id,2))
        self.assertFalse(save_qq_text(self.store,first,"old body","test"))
        self.assertTrue(save_qq_text(self.store,second,"saved body","test"))
        with self.store._connect() as conn:
            conn.execute("UPDATE outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (first.id,))
        self.assertIsNone(claim(self.store,"octop"))

    def test_copy_automatic_cycle_does_not_modify_source(self):
        from modules.strategy_hermes.manual_cases import copy_auto
        from modules.strategy_hermes.manual_portfolio import replay

        self.publish("copy-entry",D1,"09:35","新增",self.market(D1,"09:34","10"),targets=("redis",))
        self.quotes(D1,"09:36","10",1)
        self.publish("copy-weak",D2,"10:35","减弱",self.market(D2,"10:34","11",2),targets=("redis",))
        self.quotes(D2,"10:36","11",3)
        self.publish("copy-exit",D3,"14:30","退潮",self.market(D3,"14:20","12",4),targets=("redis",))
        self.quotes(D3,"14:31","12",5)
        before = self.store.inspect()
        cycle, = before["cycles"]
        draft = copy_auto(self.store,cycle["id"])
        result = replay(draft)
        self.assertEqual(draft["source"]["cycle_id"],cycle["id"])
        self.assertEqual(draft["source"]["origin"],"FROZEN")
        self.assertEqual([fill["quantity"] for fill in result["fills"]],[1000,500,500])
        self.assertEqual(result["profit"],cycle["valuation"]["profit"])
        self.assertEqual(self.store.inspect(),before)

    def test_findesk_sidecar_keeps_quote_clock_and_candidates(self):
        market = self.market(D1, "09:34", "10")
        reading = dict(etf=ETF, source="findesk", iopv="9.92", observed_at=stamp(D1, "09:34:15"),
                       source_time=f"{D1} 09:34:15")
        market.body["iopv"] = {"source": "findesk", "batch": "fixture", "readings": {ETF: reading}}
        saved = self.publish("findesk-entry", D1, "09:35", "新增", market)
        product = saved["input_meta"]["penetration"]["sectors"][A]["products"][0]
        self.assertEqual(product["iopv"], reading)
        self.assertNotIn("IOPV_MISSING", product["quality"])
        self.assertEqual(saved["candidates"][ETF][0]["rank"], 1)
        self.assertEqual(market.body["quotes"][ETF]["observed_at"], stamp(D1, "09:34"))
        self.assertEqual(saved["input_meta"]["source_observed_at"], stamp(D1, "09:34"))

    def test_report_to_fills_outbox_and_cached_snapshot(self):
        from modules.strategy_hermes.stage2.market_notices import stream_key
        from modules.strategy_hermes.stage2.outbox import dispatch, redis_delivery
        from modules.strategy_hermes.stage2.postgres import PostgresLedger
        saved=self.publish("entry",D1,"09:35","新增",self.market(D1,"09:34","10"))
        row=saved["input_meta"]["penetration"]["sectors"][A]["products"][0]
        self.assertEqual((row["metrics"]["E"],row["metrics"]["Q"]),("0.7","0.6"))
        self.assertIn("OLDER_MAPPING", row["quality"])
        self.assertEqual(row["checks"]["resonance"]["value"]["Q"], "0.6")
        self.assertEqual(row["checks"]["premium"]["reason"], "IOPV_UNAVAILABLE")
        self.assertEqual(row["checks"]["premium"]["effect"], "REFERENCE")
        self.assertEqual(row["execution_eligibility"], "READY")
        self.assertEqual(saved["candidates"][ETF][0]["rank"],1)
        self.assertEqual(self.cache.xlen(stream_key(self.ns)),0)
        self.assertEqual(dispatch(self.store,"redis",lambda event:redis_delivery(self.store,self.cache,event)),"SENT")
        deliveries=[]
        def unavailable(event):
            deliveries.append(event.event_key)
            raise TimeoutError("fixture")
        self.assertEqual(dispatch(self.store,"octop",unavailable,retry_delay=0),"PENDING")
        self.assertEqual(dispatch(self.store,"octop",lambda event:deliveries.append(event.event_key),retry_delay=0),"SENT")
        self.assertEqual(deliveries,["report:entry","report:entry"])
        self.assertEqual(self.store.inspect()["evidence_counts"],{"report":1})
        self.quotes(D1,"09:36","10",1)
        self.publish("weak",D1,"10:35","减弱",self.market(D1,"10:34","11",2),targets=("redis",))
        self.quotes(D1,"10:36","11",3)
        self.assertEqual(self.store.inspect()["cycles"][0]["pending_reason"],"WAIT_T1")
        self.quotes(D2,"09:36","11",4)
        self.publish("exit",D3,"14:30","退潮",self.market(D3,"14:20","12",5),targets=("redis",))
        self.quotes(D3,"14:31","12",6)
        restarted=PostgresLedger(self.settings,"FROZEN")
        result=restarted.inspect()
        c,=result["cycles"]
        self.assertEqual([f["quantity"] for f in c["book"]["fills"]],[1000,500,500])
        self.assertEqual((c["valuation"]["profit"],c["status"]),("1500.00","CLOSED"))
        self.assertEqual(result["input_kind"],"FROZEN")
        self.assertEqual(result["rotation"],{})
        self.assertEqual(len(restarted.report("exit")["input_meta"]["rotation_window"]),8)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM simulation_fills WHERE ns=%s",(self.ns,)).fetchone()[0],3)
        self.assertEqual(dispatch(restarted,"redis",lambda event:redis_delivery(restarted,self.cache,event)),"SENT")
        key=self.ns+":snap:hermes:cycle_positions"
        payload=json.loads(self.cache.get(key))
        self.assertEqual(payload["positions"][0]["status"],"CLOSED")
        self.assertEqual(dispatch(restarted,"redis",lambda event:redis_delivery(restarted,self.cache,event)),"SENT")
        notices=[json.loads(fields[b"data"]) for _,fields in self.cache.xrange(stream_key(self.ns))]
        self.assertEqual([notice["revision"] for notice in notices],["entry","exit"])
        self.assertTrue(all(notice["topic"]=="report" and notice["dataset"]=="etf.rotation.results.v1"
                            for notice in notices))

    def test_shared_cache_fallback_and_late_snapshot(self):
        from modules.strategy_hermes.stage2.inputs import shared_market
        latest=self.market(D2,"13:15","11",5)
        older=self.market(D2,"13:10","10",6)
        self.assertEqual(older.snapshot_id,latest.snapshot_id)
        self.cache.delete(self.key)
        fallback=shared_market(self.store,self.cache,key=self.key)
        self.assertEqual((fallback.snapshot_id,fallback.observed_at),(latest.snapshot_id,latest.observed_at))

    def test_publish_snapshot_merges_equal_source_time_as_new_revision(self):
        from modules.strategy_hermes.stage2.build_sources import publish_snapshot
        from modules.strategy_hermes.stage2.inputs import shared_market

        at = stamp(D2,"13:15")
        base = dict(schema_version="hotspot_market_snapshot_v1",trade_date=D2,observed_at=at,
                    snapshot_id="board-only",quotes={})
        enriched = {**base,"snapshot_id":"board-and-etf","quotes":{ETF:{"price_cny":"10","observed_at":at}}}
        for body in (base,enriched,enriched):
            publish_snapshot(self.store,self.cache,body,key=self.key)
        current=shared_market(self.store,self.cache,key=self.key)
        self.assertEqual((current.snapshot_id,current.observed_at,current.revision),("board-and-etf",at,1))
        self.assertEqual(current.body["quotes"],enriched["quotes"])
        older={**base,"snapshot_id":"late","observed_at":stamp(D2,"13:14")}
        publish_snapshot(self.store,self.cache,older,key=self.key)
        self.cache.delete(self.key)
        self.assertEqual(shared_market(self.store,self.cache,key=self.key).revision,1)

    def test_cli_tick_commits_due_report_and_keeps_matching_after_bad_round(self):
        with TemporaryDirectory(dir=os.environ["STAGE1_TEST_ROOT"]) as path:
            root=Path(path)
            config=root/"config.env"
            original=Path(ENV).read_text()
            config.write_text("\n".join(
                "AUTO_RUN_NAMESPACE="+self.ns if line.startswith("AUTO_RUN_NAMESPACE=") else line
                for line in original.splitlines()
            )+"\n")
            config.chmod(0o600)
            mapping=root/"mapping.json"
            mapping.write_text(encode(self.map))
            calendar=root/"calendar.json"
            calendar.write_text(encode(dict(schema_version="hotspot_trade_calendar_v1",
                source="quant.trade_calendar",exchange="SSE",range_start=D1,range_end=DAYS[-1],
                days={d:True for d in DAYS})))
            inbox=root/"inbox"
            (inbox/D1).mkdir(parents=True)
            first=report("early",D1,"09:35",[(A,"新增")])
            (inbox/D1/"0935.json").write_text(encode(dict(input_kind="FROZEN",
                published_at=stamp(D1,"09:35"),report_json=first["report_json"],
                octop=dict(etf_choices={A:[ETF]}))))

            def tick(at):
                cmd=[sys.executable,"-B","-m","modules.strategy_hermes.stage2",
                     "--env-file",str(config),"--input-kind","FROZEN","tick","--inbox",str(inbox),
                     "--mapping",str(mapping),"--calendar",str(calendar),"--snapshot-key",self.key,
                     "--received-at",at]
                result=subprocess.run(cmd,check=False,capture_output=True,text=True,timeout=10)
                return result,json.loads(result.stdout)

            self.market(D1,"09:34","10",1)
            result,out=tick(stamp(D1,"09:35"))
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(out["round"],"PUBLISHED")
            self.assertEqual(self.store.inspect()["cycles"][0]["status"],"WAIT_BUY")

            (inbox/D1/"1035.json").write_text("{")
            self.market(D1,"10:36","10",2)
            result,out=tick(stamp(D1,"10:36"))
            self.assertEqual(result.returncode,1,result.stderr)
            self.assertEqual(out["round"],"FAILED")
            self.assertEqual(self.store.inspect()["cycles"][0]["status"],"OPEN")
            self.assertEqual(len(self.store.inspect()["cycles"][0]["book"]["fills"]),1)

    def test_bulk_request_reuse_and_persisted_limit(self):
        from modules.strategy_hermes.stage2.bulk_collect import RequestLog
        from modules.strategy_hermes.stage2.market_bootstrap import ProbeError
        calls=[]
        run_key=self.ns+":bulk"
        def cleanup():
            with self.store._connect() as connection:
                connection.execute("DELETE FROM collection_requests WHERE run_key=%s",(run_key,))
        self.addCleanup(cleanup)
        def operation():
            calls.append("request")
            return {"rows":99}
        first=RequestLog(self.store,run_key)
        self.assertEqual(first.call("quotes",["CN_ETF"],operation,limit=1),{"rows":99})
        fresh=RequestLog(self.store,run_key)
        self.assertEqual(fresh.call("quotes",["CN_ETF"],operation,limit=1),{"rows":99})
        with self.assertRaisesRegex(ProbeError,"budget exhausted"):
            fresh.call("quotes",["individual"],operation,limit=1)
        self.assertEqual((calls,first.new_calls,fresh.new_calls),(["request"],1,0))

    def test_round_lease_and_request_budget_survive_retry(self):
        from modules.strategy_hermes.stage2.postgres import LeaseLost
        from modules.strategy_hermes.stage2.worker import RequestBudget, planned_at, publish_round
        planned=planned_at(D1,"09:35")
        with ThreadPoolExecutor(2) as pool:
            tasks=[pool.submit(self.store.claim_round,D1,"09:35",planned) for _ in range(2)]
            claims=[future.result() for future in tasks]
        claim,=[claim for claim in claims if claim]
        budget=RequestBudget(self.store,claim)
        calls=[]
        def timeout(*,timeout):
            calls.append(timeout)
            raise TimeoutError()
        with self.assertRaises(TimeoutError):
            budget.call("mx",timeout,attempts=2,timeout=1)
        self.store.fail_round(claim,"TimeoutError")
        retry=self.store.claim_round(D1,"09:35",planned)
        with self.assertRaises(TimeoutError):
            RequestBudget(self.store,retry).call("mx",timeout,attempts=2,timeout=1)
        self.assertEqual(len(calls),2)
        frozen=report("late",D1,"09:35",[(A,"新增")])
        envelope=dict(input_kind="FROZEN",report_json=frozen["report_json"],published_at=stamp(D1,"09:35"))
        with self.assertRaises(LeaseLost):
            publish_round(self.store,claim,envelope,DAYS,self.map,None)
        with self.assertRaisesRegex(ValueError,"unknown archived report"):
            self.store.report("late")
