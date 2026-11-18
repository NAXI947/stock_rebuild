"""Manual queue, authentication and subprocess cancellation in the isolated test DB."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from modules.strategy_hermes.manual_api import BASE, application
from modules.strategy_hermes.manual_executor import ManualExecutor
from modules.strategy_hermes.manual_queue import ManualQueue, QueueFull
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")
FIXTURE = Path(__file__).with_name("fixtures") / "stage3_manual_v14.json"


@unittest.skipUnless(ENV, "set STAGE2_TEST_ENV_FILE to run queue integration")
class ManualQueueTest(unittest.TestCase):
    def setUp(self):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, "test")
        self.ns = settings.redis_prefix + "manual-test:" + uuid4().hex
        self.ledger = PostgresLedger(replace(settings,namespace=self.ns),"FROZEN")
        self.ledger.migrate()
        self.addCleanup(self.clean)
        self.queue = ManualQueue(self.ledger)
        self.bundle = json.loads(FIXTURE.read_bytes())

    def clean(self):
        with self.ledger._connect() as conn, conn.transaction():
            conn.execute("DELETE FROM manual_jobs WHERE ns=%s",(self.ns,))
            conn.execute("DELETE FROM manual_drafts WHERE ns=%s",(self.ns,))
            for table in ("outbox","round_tasks","events","rule_versions","runs"):
                conn.execute(f"DELETE FROM {table} WHERE ns=%s",(self.ns,))

    def test_queue_bound_deduplication_and_revision_supersedes_pending_parent(self):
        parent = self.queue.create(self.bundle)
        with ThreadPoolExecutor(max_workers=3) as pool:
            returned = list(pool.map(self.queue.submit,[parent]*3))
        self.assertTrue(all(item["status"]=="QUEUED" for item in returned))
        for index in range(9):
            self.queue.submit(self.queue.create({**self.bundle,"tag":index}))
        overflow = self.queue.create({**self.bundle,"tag":"overflow"})
        with self.assertRaises(QueueFull):
            self.queue.submit(overflow)
        child = self.queue.create({**self.bundle,"initial_cash":"12000.00"},parent)
        self.assertEqual(self.queue.submit(child)["status"],"QUEUED")
        self.assertEqual(self.queue.get(parent)["job"]["error_code"],"SUPERSEDED")
        self.assertEqual(self.queue.get(child)["parent_id"],parent)
        self.assertEqual(self.queue.cancel(child)["status"],"CANCELLED")
        self.assertEqual(self.queue.submit(child)["status"],"CANCELLED")

    def test_auth_and_execution_return_saved_result(self):
        token = "test-local-" + "q"*32
        app = application(self.queue,token)

        def request(method,path,body=None,auth=True):
            raw = json.dumps(body).encode() if body is not None else b""
            env = {"REQUEST_METHOD":method,"PATH_INFO":path,"CONTENT_LENGTH":str(len(raw)),
                   "CONTENT_TYPE":"application/json","wsgi.input":BytesIO(raw)}
            if auth:
                env["HTTP_AUTHORIZATION"]="Bearer "+token
            response=[]
            output=b"".join(app(env,lambda status,headers: response.append(int(status.split()[0]))))
            return response[0],json.loads(output)

        self.assertEqual(request("POST",BASE+"/manual",{"bundle":self.bundle},False)[0],401)
        status, data = request("POST",BASE+"/manual",{"bundle":self.bundle})
        self.assertEqual(status,201)
        identifier = data["id"]
        path = BASE+"/"+identifier
        clone1 = request("POST",path+"/clone",{"request_id":"copy-1"})[1]["id"]
        self.assertEqual(clone1,request("POST",path+"/clone",{"request_id":"copy-1"})[1]["id"])
        self.assertEqual(request("GET",BASE+"/"+clone1)[1]["cloned_from"],identifier)
        self.assertEqual(request("POST",path+"/run")[0],202)
        first = self.queue.claim()
        self.assertIsNone(self.queue.claim())
        self.assertEqual(ManualExecutor(self.queue).execute(first),"SUCCEEDED")
        result = request("GET",path)[1]["job"]
        self.assertEqual((result["attempts"],result["result"]["return_pct"]),(1,"9.5000"))
        self.assertEqual(request("POST",path+"/run")[0],200)
        self.assertIsNone(self.queue.claim())

    def test_running_cancellation_reaps_subprocess_and_fences_result(self):
        self._exercise_cancel("import time; time.sleep(30)")

    def test_cpu_busy_child_keeps_http_and_hermes_responsive(self):
        self._exercise_cancel("while True:\n sum(i*i for i in range(100000))", http=True)

    def _exercise_cancel(self, child_source, http=False):
        identifier = self.queue.create(self.bundle)
        self.queue.submit(identifier)
        claim = self.queue.claim()
        original = subprocess.Popen
        started = Event()
        processes, outcome = [], []

        def slow(_command, **kwargs):
            process = original([sys.executable,"-B","-c",child_source],**kwargs)
            processes.append(process)
            started.set()
            return process

        executor = ManualExecutor(self.queue)
        with patch("modules.strategy_hermes.manual_executor.subprocess.Popen",side_effect=slow):
            worker = Thread(target=lambda: outcome.append(executor.execute(claim)))
            worker.start()
            try:
                self.assertTrue(started.wait(5))
                running = self.queue.get(identifier)["job"]
                self.assertEqual(running["status"],"RUNNING")
                if http:
                    from wsgiref.simple_server import make_server
                    from urllib.request import Request, urlopen
                    from time import monotonic
                    token="t"*40
                    server=make_server("127.0.0.1",0,application(self.queue,token))
                    thread=Thread(target=server.handle_request)
                    thread.start()
                    try:
                        started_at=monotonic()
                        request=Request(f"http://127.0.0.1:{server.server_port}{BASE}/{identifier}",headers={"Authorization":"Bearer "+token})
                        with urlopen(request,timeout=3) as response:
                            self.assertEqual(response.status,200)
                            self.assertEqual(json.load(response)["job"]["status"],"RUNNING")
                        self.assertLess(monotonic()-started_at,3)
                    finally:
                        thread.join(4)
                        server.server_close()
                from modules.strategy_hermes.stage2.worker import run_due
                from modules.strategy_hermes.simulator import moment
                from test_stage1 import D1, DAYS, report, stamp

                mapping = dict(version="manual-concurrency-test",sectors={},products={},weights={})
                at = moment(stamp(D1,"09:35"))
                calls = []

                def analyze(request, budget, timeout):
                    calls.append(request["round_slot"])
                    source = report("during-manual",D1,"09:35")
                    return dict(input_kind="FROZEN",published_at=at.isoformat(),report_json=source["report_json"])

                saved = run_due(self.ledger,at,DAYS,mapping,None,analyze)
                self.assertEqual((saved["id"],calls),("during-manual",["09:35"]))
                self.assertEqual(self.queue.get(identifier)["job"]["status"],"RUNNING")
                self.assertIsNone(processes[0].poll())
                self.assertTrue(self.queue.cancel(identifier)["cancel_requested"])
            finally:
                worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(processes[0].returncode)
        self.assertEqual(outcome,["CANCELLED"])
        self.assertEqual(self.queue.finish(claim,error="LATE_COMPLETION"),"LEASE_LOST")
        self.assertNotIn("result",self.queue.get(identifier)["job"])

    def test_missing_local_price_retains_wait_data_result(self):
        identifier = self.queue.create({**self.bundle,"quotes":[],"nodes":self.bundle["nodes"][:1]})
        self.queue.submit(identifier)
        self.assertEqual(ManualExecutor(self.queue).execute(self.queue.claim()),"WAIT_DATA")
        result = self.queue.get(identifier)["job"]["result"]
        self.assertEqual(result["nodes"][0]["reason"],"WAIT_PRICE")


    def test_expired_worker_is_visible_and_explicit_retry_fences_old_claim(self):
        identifier = self.queue.create(self.bundle)
        self.queue.submit(identifier)
        expired = self.queue.claim()
        with self.ledger._connect() as conn:
            conn.execute(
                "UPDATE manual_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE ns=%s AND draft_id=%s",
                (self.ns,identifier),
            )
        self.assertIsNone(self.queue.claim())
        failed = self.queue.get(identifier)["job"]
        self.assertEqual((failed["status"],failed["error_code"]),("FAILED","WORKER_LEASE_EXPIRED"))
        self.assertEqual(self.queue.submit(identifier)["status"],"FAILED")
        self.assertEqual(self.queue.submit(identifier,retry=True)["status"],"QUEUED")
        active = self.queue.claim()
        self.assertEqual(self.queue.finish(expired,error="LATE_COMPLETION"),"LEASE_LOST")
        self.assertEqual(ManualExecutor(self.queue).execute(active),"SUCCEEDED")
        self.assertEqual(self.queue.get(identifier)["job"]["attempts"],2)
