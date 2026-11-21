"""Workspace command routes and independent actual fills on the test DB."""
from dataclasses import replace
from datetime import datetime
from io import BytesIO
import json
import os
import unittest
from uuid import uuid4

from modules.strategy_hermes import actual_trades
from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.simulator import CHINA, moment
from modules.strategy_hermes.stage2.inputs import cache_client
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings
from modules.strategy_hermes.workspace_readiness import runtime_status, worker_status_key
from modules.strategy_hermes.workspace_web import API, application

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")
DAYS = ["2026-11-09","2026-11-10","2026-11-11","2026-11-12"]


@unittest.skipUnless(ENV,"set STAGE2_TEST_ENV_FILE to verify the workspace database")
class WorkspaceWebTest(unittest.TestCase):
    def test_overview_limits_closed_rows_but_keeps_full_profit_statistics(self):
        from dataclasses import asdict
        from modules.strategy_hermes.simulator import Rules
        at="2026-11-12T15:00:00+08:00"
        body=dict(etf="512480.SH",rule=asdict(Rules()),status="CLOSED",orders=[],closed_at=at,
                  book=dict(cash="10500.00",realized="500.00",lots=[],fills=[dict(action="BUY",filled_at="2026-11-10T10:00:00+08:00")]))
        with self.ledger._connect() as conn:
            conn.execute("INSERT INTO runs(ns,config,watermark,origin) VALUES(%s,'{}',%s,'FROZEN')",(self.ns,at))
            for index in range(53):
                conn.execute("INSERT INTO cycles(ns,id,etf,opened_at,status,body) VALUES(%s,%s,'512480.SH',%s,'CLOSED',%s)",
                             (self.ns,f"audit-cycle-{index:03}",at,encode(body)))
        try:
            status,raw=self.request("GET",API+"/overview")
            self.assertEqual(status,200,raw)
            result=json.loads(raw)
            self.assertEqual(len(result["cycles"]),50)
            self.assertEqual(result["new_cycle_rule"],self.ledger.settings.rules().snapshot())
            self.assertEqual(result["new_cycle_rule"]["version"],"hermes_signal_cycle_v1")
            self.assertEqual(result["evaluation"]["GROSS"]["closed"],53)
            self.assertEqual(result["evaluation_recent"]["closed"],53)
            self.assertEqual(float(result["evaluation"]["profit_statistics"]["GROSS"]["average_profit"]),500)
        finally:
            with self.ledger._connect() as conn:
                conn.execute("DELETE FROM cycles WHERE ns=%s",(self.ns,))

    def test_database_write_failure_has_stable_diagnostics_and_no_fill(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        original=self.ledger._connect
        @contextmanager
        def readonly():
            with original() as conn:
                conn.execute("SET default_transaction_read_only=on")
                yield conn
        with patch.object(self.ledger,"_connect",readonly):
            status,raw=self.request("POST",API+"/trades",self.fill("2026-11-10"))
            status2,raw2=self.request("POST",API+"/trades",self.fill("2026-11-10"))
        self.assertEqual((status,status2),(503,503))
        self.assertEqual(json.loads(raw)["error_id"],json.loads(raw2)["error_id"])
        with original() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM actual_trades WHERE ns=%s",(self.ns,)).fetchone()[0],0)

    def test_archive_keyset_finds_older_drafts_and_preview_does_not_save(self):
        from modules.strategy_hermes.manual_queue import ManualQueue
        from pathlib import Path
        bundle=json.loads((Path(__file__).parent/"fixtures/stage3_manual_v14.json").read_text())
        queue=ManualQueue(self.ledger)
        for index in range(53):
            queue.create(dict(bundle,initial_cash=str(10000+index)))
        status,raw=self.request("GET",API+"/archive?kind=drafts")
        self.assertEqual(status,200)
        page=json.loads(raw)
        self.assertEqual(len(page["items"]),50)
        from urllib.parse import urlencode
        status,raw=self.request("GET",API+"/archive?"+urlencode(dict(kind="drafts",cursor=page["next_cursor"])))
        self.assertEqual(len(json.loads(raw)["items"]),3)
        status,raw=self.request("POST",API+"/preview",dict(bundle=bundle))
        self.assertEqual(status,200,raw)
        self.assertEqual(json.loads(raw)["return_pct"],"9.5000")
        with self.ledger._connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM manual_drafts WHERE ns=%s",(self.ns,)).fetchone()[0],53)

    def setUp(self):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env,"test")
        self.ns = settings.redis_prefix + "workspace-test:" + uuid4().hex
        self.ledger = PostgresLedger(replace(settings,namespace=self.ns),"FROZEN")
        self.ledger.migrate()
        self.token = "q"*40
        self.app = application(self.ledger,self.token,calendar=DAYS)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with self.ledger._connect() as conn:
            for name in ("left_right_backfill_requests","left_right_backfill_jobs",
                         "actual_trades","round_tasks","outbox","latest","events","rule_versions","runs"):
                conn.execute(f"DELETE FROM {name} WHERE ns=%s",(self.ns,))

    def request(self, method, path, body=None, auth=True):
        raw = json.dumps(body).encode() if body is not None else b""
        path, _, query = path.partition("?")
        env = {"REQUEST_METHOD":method,"PATH_INFO":path,"QUERY_STRING":query,"CONTENT_LENGTH":str(len(raw)),
               "CONTENT_TYPE":"application/json","wsgi.input":BytesIO(raw)}
        if auth:
            env["HTTP_AUTHORIZATION"]="Bearer "+self.token
        statuses=[]
        result=b"".join(self.app(env,lambda status,headers: statuses.append(int(status.split()[0]))))
        return statuses[0],result

    def fill(self, day, **values):
        return dict(request_id="request-"+day,etf="512480.SH",side="BUY",price_cny="0.81",quantity=100,
                    fees_cny="0",executed_at=day+"T10:00:00+08:00") | values

    def test_public_shell_private_data_and_empty_source(self):
        status, raw = self.request("GET","/",auth=False)
        self.assertEqual(status,200)
        self.assertIn(b"Stocknew",raw)
        self.assertIn("股票研究".encode(),raw)
        self.assertEqual(self.request("GET", "/favicon.ico", auth=False), (204, b""))
        self.assertEqual(self.request("GET",API+"/overview",auth=False)[0],401)
        status, raw=self.request("GET",API+"/overview")
        self.assertEqual(status,200)
        self.assertIsNone(json.loads(raw)["market"])
        self.assertEqual(json.loads(raw)["reports"],[])
        self.assertEqual(json.loads(raw)["round_sectors"],[])
        self.assertEqual(json.loads(raw)["rotation"],{})

    def test_secondary_views_are_authenticated_and_have_no_synthetic_data(self):
        self.assertEqual(self.request("GET", API + "/status", auth=False)[0], 401)
        self.assertEqual(self.request("GET", API + "/left-right", auth=False)[0], 401)
        status, raw = self.request("GET", API + "/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["rounds"], [])
        self.assertEqual(self.request("GET", API + "/left-right"), (200, b'{"reference":null}'))
        self.assertEqual(self.request("GET", API + "/market-panorama", auth=False)[0], 401)
        from unittest.mock import patch
        with patch("modules.strategy_hermes.stage2.market_bootstrap.fetch", side_effect=AssertionError("no GET collection")), \
             patch("modules.strategy_hermes.stage2.market_turnover.get_json", side_effect=AssertionError("no GET collection")):
            status, raw = self.request("GET", API + "/market-panorama")
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(raw)), {"background", "regime", "turnover", "global_indices"})
        self.assertEqual(self.request("GET", API + "/sector-etf-info", auth=False)[0], 401)
        with patch("modules.strategy_hermes.stage2.market_bootstrap.fetch", side_effect=AssertionError("no GET collection")), \
             patch("modules.strategy_hermes.stage2.market_turnover.get_json", side_effect=AssertionError("no GET collection")):
            status, raw = self.request("GET", API + "/sector-etf-info?kind=etfs&page_size=20")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["items"], [])
        self.assertEqual(self.request("GET", API + "/sector-etf-info?page_size=101")[0], 400)
        self.assertEqual(self.request("GET", API + "/sector-etf-penetration", auth=False)[0], 401)
        status, raw = self.request("GET", API + "/sector-etf-penetration")
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(raw)), {"current", "mapping", "recent", "recent_since", "recent_etf_count", "gaps", "metadata_issues"})
        for route in ("left-right-info", "hot-sectors-info"):
            self.assertEqual(self.request("GET", API + "/" + route, auth=False)[0], 401)
            with patch("modules.strategy_hermes.stage2.market_bootstrap.fetch", side_effect=AssertionError("no GET collection")):
                status, raw = self.request("GET", API + "/" + route)
            self.assertEqual(status, 200)
            result = json.loads(raw)
            self.assertEqual(result["available_dates"], [])
            self.assertEqual(result["items" if route == "left-right-info" else "days"], [])
        self.assertEqual(self.request("GET", API + "/left-right-info?page_size=101")[0], 400)
        self.assertEqual(self.request("GET", API + "/hot-sectors-info?limit=101")[0], 400)

    def test_active_direction_names_are_read_from_the_report_that_last_moved_them(self):
        from modules.strategy_hermes.workspace_web import _named_rotation
        known, lost = "kaipanla:801001", "kaipanla:801002"
        payload = dict(status="PARTIAL", sectors={known: dict(row=0, phase="NEW")},
                       raw=json.dumps(dict(sectors=[dict(sector_name="芯片")])))
        rotation = {known: dict(phase="NEW", streak=0, report_id="older"),
                    lost: dict(phase="NEW", streak=0, report_id="gone")}
        with self.ledger._connect() as conn:
            conn.execute("INSERT INTO runs(ns,config,origin) VALUES (%s,'{}','FROZEN')", (self.ns,))
            conn.execute(
                "INSERT INTO events(ns,kind,id,at,fingerprint,payload) VALUES (%s,'report','older',%s,'hash',%s)",
                (self.ns, "2026-11-10T14:05:31+08:00", encode(payload)),
            )
            named = _named_rotation(conn, self.ns, rotation, [])
        self.assertEqual(named, {known: dict(rotation[known], name="芯片"), lost: dict(rotation[lost], name=None)})

    def test_report_detail_reads_only_its_saved_qq_body_and_delivery(self):
        at = "2026-11-10T14:05:31+08:00"
        route = API + "/report?id=report"
        payload = {"id": "report", "raw": "research", "status": "PARTIAL"}
        with self.ledger._connect() as conn:
            conn.execute("INSERT INTO runs(ns,config,origin) VALUES (%s,'{}','FROZEN')", (self.ns,))
            conn.execute(
                "INSERT INTO events(ns,kind,id,at,fingerprint,payload) VALUES (%s,'report','report',%s,'hash',%s)",
                (self.ns, at, encode(payload)),
            )
            conn.execute(
                """INSERT INTO outbox(ns,report_id,event_key,target,payload)
                   VALUES (%s,'report','report:report','redis',%s)""",
                (self.ns, encode({"text": "redis-only"})),
            )
        self.assertEqual(self.request("GET", route, auth=False)[0], 401)
        self.assertEqual(self.request("GET", API + "/report?id=unrelated")[0], 404)
        status, raw = self.request("GET", route)
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual({key: body[key] for key in payload}, payload)
        self.assertEqual(body["qq_brief"]["state"], "NOT_QUEUED")
        self.assertIsNone(body["qq_brief"]["text"])
        with self.ledger._connect() as conn:
            conn.execute(
                """INSERT INTO outbox(ns,report_id,event_key,target,payload)
                   VALUES (%s,'report','report:report','octop',%s)""",
                (self.ns, encode({"format": "octop-markdown-v1", "sections": ["private draft"]})),
            )
        pending = json.loads(self.request("GET", route)[1])["qq_brief"]
        self.assertEqual((pending["state"], pending["format_state"]), ("PENDING", "PENDING"))
        self.assertNotIn("sections", pending)
        self.assertIsNone(pending["text"])
        with self.ledger._connect() as conn:
            conn.execute(
                """UPDATE outbox SET state='FAILED',error_code='QQFormatRejected'
                   WHERE ns=%s AND target='octop'""", (self.ns,),
            )
        failed = json.loads(self.request("GET", route)[1])["qq_brief"]
        self.assertEqual((failed["state"], failed["format_state"], failed["error"]),
                         ("FAILED", "FAILED", "QQFormatRejected"))
        text = "## 板块判断\n银行加强\n"
        template = "octop-markdown-v1:workspace:" + "c" * 64
        with self.ledger._connect() as conn:
            conn.execute(
                """UPDATE outbox SET state='SENT',error_code=NULL,delivered_at=%s,attempts=2,
                     payload=%s,delivery_receipt=%s WHERE ns=%s AND target='octop'""",
                (at, encode({"text": text, "format": "octop-markdown-v1", "template": template,
                             "workspace_template_invalid": True}),
                 encode({"status": "ACCEPTED", "http_status": 200, "secret": "ignore"}), self.ns),
            )
        sent = json.loads(self.request("GET", route)[1])["qq_brief"]
        self.assertEqual(sent, {"text": text, "format": "octop-markdown-v1", "template": template,
                                "template_fallback": True,
                                "state": "SENT", "format_state": "READY", "attempts": 2, "error": None,
                                "delivered_at": at, "receipt": {"status": "ACCEPTED", "http_status": 200}})
        with self.ledger._connect() as conn:
            conn.execute("UPDATE outbox SET payload=%s WHERE ns=%s AND target='octop'",
                         (encode({"text": text}), self.ns))
        legacy = json.loads(self.request("GET", route)[1])["qq_brief"]
        self.assertEqual((legacy["text"], legacy["format"], legacy["format_state"]),
                         (text, "historical-text", "HISTORICAL"))

        # Current QQ templates share delivery states while retaining their own version.
        template="qq-template-v2:research:managed:"+"d"*64
        for state,format_state in (("PENDING","PENDING"),("SENDING","FORMATTING"),("FAILED","FAILED")):
            with self.subTest(state=state):
                with self.ledger._connect() as conn:
                    conn.execute("UPDATE outbox SET state=%s,payload=%s WHERE ns=%s AND target='octop'",
                                 (state,encode(dict(format="qq-template-v2",template_kind="research",sections={})),self.ns))
                current=json.loads(self.request("GET",route)[1])["qq_brief"]
                self.assertEqual((current["format"],current["format_state"]),("qq-template-v2",format_state))
                self.assertIsNone(current["text"])
        with self.ledger._connect() as conn:
            conn.execute("UPDATE outbox SET state='SENT',payload=%s WHERE ns=%s AND target='octop'",
                         (encode(dict(format="qq-template-v2",text=text,template=template,workspace_template_invalid=True)),self.ns))
        current=json.loads(self.request("GET",route)[1])["qq_brief"]
        self.assertEqual((current["format"],current["format_state"],current["template"],current["text"]),
                         ("qq-template-v2","READY",template,text))
        self.assertTrue(current["template_fallback"])

    def test_backfill_routes_auth_idempotency_bounds_and_release_pause(self):
        from unittest.mock import patch
        route = API + "/left-right-backfill"
        body = dict(request_id=str(uuid4()), mode="collect", date_from=DAYS[0], date_to=DAYS[-1], codes=["000001.SZ"])
        for method, path, request in (("GET", route, None), ("POST", route, body),
                                       ("POST", route+"/cancel", {"job_id": "unknown"})):
            self.assertEqual(self.request(method, path, request, auth=False)[0], 401)
        with patch("modules.strategy_hermes.stage2.tushare_bootstrap.Tushare.fetch", side_effect=AssertionError("API cannot collect")), \
             patch("modules.strategy_hermes.stage2.market_bootstrap.fetch", side_effect=AssertionError("API cannot collect")):
            self.assertEqual(json.loads(self.request("GET", route)[1])["jobs"], [])
            status, raw = self.request("POST", route, body)
            self.assertEqual(status, 202, raw)
            job = json.loads(raw)
            self.assertEqual((job["status"], job["requests_used"]), ("QUEUED", 0))
            self.assertEqual(json.loads(self.request("POST", route, body)[1])["job_id"], job["job_id"])
            self.assertEqual(self.request("POST", route, dict(body, mode="recompute"))[0], 409)
            for changed in (dict(codes=[]), dict(codes=["bad"]), dict(date_from="2026-01-01"), dict(extra=True)):
                self.assertEqual(self.request("POST", route, dict(body, **changed))[0], 400)
            self.assertEqual(self.request("GET", route+"?unknown=1")[0], 400)
            self.assertEqual(len(json.loads(self.request("GET", route)[1])["jobs"]), 1)
            self.app = application(self.ledger, self.token, calendar=DAYS, paused=lambda: True)
            self.assertEqual(self.request("POST", route, dict(body, request_id=str(uuid4())))[0], 503)
            self.assertEqual(self.request("GET", route)[0], 200)
            self.app = application(self.ledger, self.token, calendar=DAYS)
            status, raw = self.request("POST", route+"/cancel", {"job_id": job["job_id"]})
            self.assertEqual(status, 200, raw)
            self.assertEqual(json.loads(raw)["status"], "CANCELLED")

    def test_all_market_backfill_only_queues_explicit_date_request(self):
        route = API + "/left-right-backfill"
        body = dict(request_id=str(uuid4()), mode="collect", scope="ALL", date_from=DAYS[0], date_to=DAYS[-1])
        status, raw = self.request("POST", route, body)
        self.assertEqual(status, 202, raw)
        job = json.loads(raw)
        self.assertNotIn("codes", job)
        self.assertEqual((job["scope"],job["request_budget"],job["requests_used"]), ("ALL",12000,0))
        limits = json.loads(self.request("GET", route)[1])["limits"]
        self.assertEqual((limits["scope"],limits["page_size"],limits["max_pages"],limits["max_seconds"]),
                         ("ALL",3000,10,5400))
        self.assertNotIn("max_codes", limits)
        self.assertEqual(self.request("POST", route, body | {"codes": ["000001.SZ"]})[0], 400)

    def test_status_uses_observed_worker_calendar_and_committed_delivery(self):
        now = moment("2026-11-10T14:06:00+08:00")
        with cache_client(self.ledger.settings) as cache:
            self.addCleanup(self.clear_worker_status)
            cache.set(worker_status_key(self.ns), encode(dict(
                observed_at=now.isoformat(), hermes_schedule=True, official_collection=True,
                qq_push=False, stock_research=True, unexpected="must not be exposed",
            )), ex=180)
        with self.ledger._connect() as conn, conn.transaction():
            for slot, rid in (("13:05", "prior"), ("14:05", "latest")):
                conn.execute(
                    """INSERT INTO round_tasks(ns,trade_date,slot,planned_at,report_id,state,attempts)
                       VALUES (%s,%s,%s,%s,%s,'PUBLISHED',1)""",
                    (self.ns, "2026-11-10", slot, f"2026-11-10T{slot}:00+08:00", rid),
                )
            for target, rid, key, delivered, receipt in (
                ("redis", "latest", "report:latest", "2026-11-10T14:05:22+08:00", None),
                ("redis", "latest", "quotes:newest", "2026-11-10T14:05:54+08:00", None),
                ("octop", "prior", "report:prior", "2026-11-10T13:05:28+08:00",
                 encode(dict(status="ACCEPTED", http_status=200, message_posts=1, token="ignore"))),
            ):
                conn.execute(
                    """INSERT INTO outbox(ns,report_id,event_key,target,payload,state,attempts,delivered_at,delivery_receipt)
                       VALUES (%s,%s,%s,%s,'{}','SENT',1,%s,%s)""",
                    (self.ns, rid, key, target, delivered, receipt),
                )

        data = runtime_status(self.ledger, False, DAYS, now=now)
        self.assertEqual(data["worker"], dict(observed_at=now.isoformat(),
                                              hermes_schedule=True, official_collection=True, qq_push=False, stock_research=True, findesk_iopv=None))
        next_slot = data["schedule"]["upcoming"][0]
        self.assertEqual((next_slot["trade_date"], next_slot["slot"]), ("2026-11-10", "14:30"))
        self.assertEqual(data["schedule"]["upcoming"][1]["trade_date"], "2026-11-11")
        redis, qq = data["deliveries"]
        self.assertEqual((redis["state"], redis["delivered_at"], redis["last_sent_at"]),
                         ("SENT", "2026-11-10T14:05:22+08:00", "2026-11-10T14:05:22+08:00"))
        self.assertEqual(qq["format_state"], "UNAVAILABLE")
        self.assertEqual((qq["state"], qq["last_sent_at"]),
                         ("NOT_QUEUED", "2026-11-10T13:05:28+08:00"))
        self.assertIsNone(runtime_status(self.ledger, True, DAYS, now=datetime.now(CHINA))["worker"])

        with self.ledger._connect() as conn:
            conn.execute("DELETE FROM round_tasks WHERE ns=%s AND report_id='latest'", (self.ns,))
        previous = runtime_status(self.ledger, False, DAYS, now=now)
        self.assertEqual(previous["deliveries"][1]["receipt"],
                         {"status":"ACCEPTED", "http_status":200, "message_posts":1})

    def clear_worker_status(self):
        with cache_client(self.ledger.settings) as cache:
            cache.delete(worker_status_key(self.ns))

    def test_status_exposes_only_safe_octop_model_identity_and_recorded_failure(self):
        at = "2026-11-10T14:05:31+08:00"
        with self.ledger._connect() as conn, conn.transaction():
            conn.execute("INSERT INTO runs(ns,config,origin) VALUES (%s,'{}','FROZEN')", (self.ns,))
            conn.execute(
                """INSERT INTO round_tasks(ns,trade_date,slot,planned_at,report_id,state,attempts)
                   VALUES (%s,'2026-11-10','14:05','2026-11-10T14:05:00+08:00','report','PUBLISHED',1)""",
                (self.ns,),
            )
            conn.execute(
                """INSERT INTO round_tasks(ns,trade_date,slot,planned_at,state,error_code,attempts)
                   VALUES (%s,'2026-11-10','14:30','2026-11-10T14:30:00+08:00','FAILED','TimeoutError',2)""",
                (self.ns,),
            )
            receipt = dict(model_expert="my-stock", configured_model="汇飞智能/deepseek-v4.1-flash",
                           output="private response", api_key="private key")
            body = {"input_meta":{"octop":{"receipt":receipt}}}
            conn.execute(
                "INSERT INTO events(ns,kind,id,at,fingerprint,payload) VALUES (%s,'report','report',%s,'hash',%s)",
                (self.ns, at, encode(body)),
            )
        status, raw = self.request("GET", API + "/status")
        self.assertEqual(status, 200)
        self.assertNotIn(b"private", raw)
        self.assertEqual(json.loads(raw)["octop"], dict(
            report_id="report", published_at=at, model_expert="my-stock",
            configured_model="汇飞智能/deepseek-v4.1-flash",
            last_failure=dict(trade_date="2026-11-10", slot="14:30", error="TimeoutError"),
        ))
        with self.ledger._connect() as conn:
            conn.execute("UPDATE events SET payload=%s WHERE ns=%s",
                         (encode({"input_meta":{"octop":{"receipt":{"configured_model":"https://private.invalid"}}}}),
                          self.ns))
        status, raw = self.request("GET", API + "/status")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(raw)["octop"]["model_expert"])
        self.assertIsNone(json.loads(raw)["octop"]["configured_model"])

    def test_recent_evaluation_uses_entry_fill_and_excludes_unfilled_cycles(self):
        from modules.strategy_hermes.workspace_readiness import recent_evaluation

        def cycle(status, fill, profit=None):
            fills = [] if fill is None else [dict(action="BUY", filled_at=fill + "T10:00:00+08:00")]
            return dict(status=status, book=dict(fills=fills), valuation=dict(basis="GROSS", profit=profit))

        view = dict(watermark="2026-11-12T14:30:00+08:00", cycles=[
            cycle("CLOSED", "2026-11-08", "50"),
            cycle("CLOSED", "2026-11-09", "30"),
            cycle("OPEN", "2026-11-12"),
            cycle("WAIT_BUY", None),
        ])
        evaluation = recent_evaluation(self.ledger, view, DAYS)
        self.assertEqual((evaluation["start"], evaluation["end"]), ("2026-11-09", "2026-11-12"))
        self.assertEqual((evaluation["entered"], evaluation["closed"]), (2, 1))
        self.assertEqual(evaluation["GROSS"]["wins"], 1)
        self.assertEqual(evaluation["open_or_pending"], 1)

    def test_idempotency_actual_t1_and_no_simulation_change(self):
        initial=self.fill("2026-11-10")
        latest=self.fill("2026-11-11",quantity=200,fees_cny=None)
        self.assertEqual(self.request("POST",API+"/trades",initial,auth=False)[0],401)
        self.assertEqual(self.request("POST",API+"/trades",initial)[0],201)
        self.assertEqual(self.request("POST",API+"/trades",initial)[0],200)
        self.assertEqual(self.request("POST",API+"/trades",{**initial,"quantity":200})[0],409)
        self.assertEqual(self.request("POST",API+"/trades",latest)[0],201)
        sale=self.fill("2026-11-11",request_id="actual-sale",side="SELL",quantity=100,
                       executed_at="2026-11-11T11:00:00+08:00")
        self.assertEqual(self.request("POST",API+"/trades",sale)[0],201)
        result=actual_trades.read(self.ledger,DAYS,moment("2026-11-11T14:00:00+08:00"))
        self.assertEqual(len(result["fills"]),3)
        position,=result["positions"]
        self.assertEqual((position["quantity"],position["sellable"],position["locked"]),(200,0,200))
        self.assertIsNone(position["basis_cny"])
        with self.assertRaisesRegex(ValueError,"unknown replay namespace"):
            self.ledger.inspect()


    def test_archived_quote_history_retains_timestamps_and_origin(self):
        from test_stage1 import quote
        code="512480.SH"
        self.ledger.apply_quotes([quote("history-1","2026-11-10","10:02","0.810",code=code)],DAYS,{code:100})
        body={"etf":code,"from":"2026-11-10T09:30:00+08:00","cutoff":"2026-11-10T15:00:00+08:00"}
        status,raw=self.request("POST",API+"/history",body)
        self.assertEqual(status,200)
        result=json.loads(raw)
        self.assertEqual(result["price_mode"],"ASSUMPTION")
        self.assertEqual(result["instruments"],{code:100})
        self.assertEqual(result["quotes"][0]["observed_at"],"2026-11-10T10:02:00+08:00")
        self.assertEqual(result["quotes"][0]["price_kind"],"ASSUMED")


class RoundSectorsTest(unittest.TestCase):
    """The five-round matrix reads the committed rotation chain; it does not recount it."""

    def setUp(self):
        self.chain, self.loaded = {}, []

    def publish(self, day, slot, judgments):
        from modules.strategy_hermes.rotation_chain import advance
        clock = f"{day}T{slot}:40+08:00"
        report = dict(id=f"report-{day}-{slot}", evidence_key=None, sectors={})
        named = []
        for index, (sid, (phase, evidence)) in enumerate(judgments.items()):
            row = dict(phase=phase, state="UNKNOWN", row=index, sources=[f"{sid}@{evidence}"], data_as_of=evidence,
                       reasons=[f"板块涨跌 {index}.00%"])
            updated, effective = advance(self.chain.get(sid), report, sid, row)
            if updated is not None:
                self.chain[sid] = updated
            report["sectors"][sid] = dict(effective, declared_phase=phase)
            named.append(dict(sector_name=f"板块{sid[-1]}"))
        facts = {sid: dict(board_return_ratio=f"0.0{index}") for index, sid in enumerate(judgments)}
        self.loaded.insert(0, (dict(id=report["id"], at=clock, slot=slot), report, dict(sectors=named, source_facts=facts)))

    def matrix(self):
        from modules.strategy_hermes.rotation_chain import describe
        from modules.strategy_hermes.workspace_web import _round_sectors
        rotation = {sid: dict(state, **describe(state["phase"], state["streak"]))
                    for sid, state in self.chain.items() if state["member"]}
        return {row["name"]: row["rounds"] for row in _round_sectors(self.loaded[:8], rotation)}

    def test_rounds_follow_entry_counts_retreat_and_carried_state(self):
        a, b, c, d, e = (f"kaipanla:80100{n}" for n in range(1, 6))
        self.publish("2026-11-18", "14:30", {a: ("NEW", "e0"), c: ("NEW", "e0"), e: ("NEW", "e0")})
        for slot in ("09:35", "10:35", "13:05", "14:05", "14:30"):
            self.publish("2026-11-19", slot, {a: ("WEAKEN", slot), c: ("MAINTAIN", slot)})
        self.publish("2026-11-20", "09:35", {a: ("WEAKEN", "f1"), c: ("MAINTAIN", "f1"), d: ("MAINTAIN", "f1"), e: ("UNKNOWN", "f1")})
        self.publish("2026-11-20", "10:35", {a: ("WEAKEN", "f1"), b: ("NEW", "f2"), c: ("RETREAT", "f2")})
        self.publish("2026-11-20", "13:05", {a: ("UNKNOWN", "f3"), b: ("STRENGTHEN", "f3"), c: ("WEAKEN", "f3")})
        self.publish("2026-11-20", "14:05", {a: ("MAINTAIN", "f4"), b: ("STRENGTHEN", "f4")})
        rounds = self.matrix()
        self.assertEqual(list(rounds), ["板块1", "板块2", "板块3", "板块4", "板块5"])
        self.assertNotIn("14:30", rounds["板块1"])

        def cells(name, *keys):
            return [tuple(rounds[name][slot].get(key) for key in keys) for slot in ("09:35", "10:35", "13:05", "14:05")]

        self.assertEqual(cells("板块1", "advanced", "phase", "streak", "skipped"), [
            ("WEAKEN", "WEAKEN", 6, None), (None, "WEAKEN", 6, "DUPLICATE_EVIDENCE"),
            (None, "WEAKEN", 6, None), ("MAINTAIN", "MAINTAIN", 1, None)])
        self.assertEqual(cells("板块2", "covered", "advanced", "active", "streak"), [
            (False, None, False, 0), (True, "NEW", True, 0), (True, "STRENGTHEN", True, 1), (True, "STRENGTHEN", True, 2)])
        self.assertEqual(cells("板块3", "advanced", "was_active", "active", "skipped"), [
            ("MAINTAIN", True, True, None), ("RETREAT", True, False, None),
            (None, False, False, "NO_ACTIVE_DIRECTION"), (None, False, False, None)])
        self.assertEqual({(cell["active"], cell["was_active"]) for cell in rounds["板块4"].values()}, {(False, False)})
        # 板块5 last advanced before the eight-report window, so its state comes from the rotation table.
        self.assertEqual(cells("板块5", "judged", "active", "phase", "covered"), [
            ("UNKNOWN", True, "NEW", True), (None, True, "NEW", False), (None, True, "NEW", False), (None, True, "NEW", False)])
        self.assertEqual(rounds["板块1"]["09:35"]["change"], "0.00")
        self.assertEqual(rounds["板块1"]["09:35"]["reasons"], ["板块涨跌 0.00%"])

    def test_active_directions_take_their_name_from_the_window_or_their_last_report(self):
        from modules.strategy_hermes.workspace_web import _named_rotation
        a, b, c = (f"kaipanla:80100{n}" for n in range(1, 4))
        self.publish("2026-11-18", "14:30", {a: ("NEW", "e0"), b: ("NEW", "e0")})
        self.publish("2026-11-19", "09:35", {a: ("WEAKEN", "e1")})
        stored = {entry["id"]: json.dumps(dict(row, raw=json.dumps(source))) for entry, row, source in self.loaded}
        asked = []

        class Reports:
            def execute(self, sql, args):
                asked.append(args)
                return self

            def fetchall(self):
                return [(stored[rid],) for rid in asked[-1][1]]

        rotation = {sid: dict(phase=state["phase"], streak=state["streak"], report_id=state["report_id"])
                    for sid, state in self.chain.items()}
        rotation[c] = dict(phase="MAINTAIN", streak=1, report_id=None)
        named = _named_rotation(Reports(), "ns", rotation, self.loaded[:1])
        self.assertEqual({sid: row["name"] for sid, row in named.items()}, {a: "板块1", b: "板块2", c: None})
        self.assertEqual(asked, [("ns", ["report-2026-11-18-14:30"])])
        self.assertEqual(named[a], dict(rotation[a], name="板块1"))
        asked.clear()
        self.assertEqual(_named_rotation(Reports(), "ns", {a: rotation[a]}, self.loaded[:1])[a]["name"], "板块1")
        self.assertEqual(asked, [])

    def test_no_reports_and_missing_optional_fields_yield_an_empty_or_partial_matrix(self):
        from modules.strategy_hermes.workspace_web import _round_sectors
        self.assertEqual(_round_sectors([], {}), [])
        sector = dict(phase="UNKNOWN", state="UNKNOWN")
        row, = _round_sectors([(dict(id="r", at="2026-11-20T09:35:40+08:00", slot="09:35"), dict(sectors={"kaipanla:801001": sector}), {})], {})
        self.assertIsNone(row["name"])
        self.assertEqual(row["rounds"]["09:35"], dict(active=False, phase=None, streak=0, covered=True, was_active=False,
                                                     judged="UNKNOWN", advanced=None, skipped=None, reasons=[], change=None))
