from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from modules.strategy_hermes.ledger import Ledger
from shared_kernel.temp_manager import BudgetError, TempManager

from test_stage1 import A, D1, D2, D3, bundle, quote, report, start


class Runtime(unittest.TestCase):
    def setUp(self):
        root=TemporaryDirectory(dir=os.environ["STAGE1_TEST_ROOT"])
        self.addCleanup(root.cleanup)
        self.home=Path(root.name)
        self.state=self.home/"ledger"; self.state.mkdir(mode=0o700)
        self.tmp=self.home/"tmp"; self.tmp.mkdir(mode=0o700)
        self.env=dict(os.environ,APP_TMP_ROOT=str(self.tmp))

    def child(self,code,*argv,data=None):
        return subprocess.run([sys.executable,"-B","-c",code,*map(str,argv)],
            input=data,text=True,capture_output=True,timeout=15,env=self.env)

    def test_d07_kill_after_commit_then_retry_is_idempotent(self):
        data=bundle(start()+[report("weak",D2,"09:35",[(A,"减弱")]),quote("fill",D2,"09:36","11")])
        proc=self.child("""
import json,os,signal,sys
from modules.strategy_hermes.ledger import Ledger
Ledger(sys.argv[1]).replay(json.load(sys.stdin))
os.kill(os.getpid(),signal.SIGKILL)
""",self.state,data=json.dumps(data))
        self.assertEqual(proc.returncode,-signal.SIGKILL,proc.stderr)
        result=Ledger(self.state).replay(data)
        self.assertEqual([f["quantity"] for f in result["cycles"][0]["book"]["fills"]],[1000,500])
        self.assertEqual(result["cycles"][0]["book"]["cash"],"5500.00")
        self.assertEqual(result["cycles"][0]["orders"][-1]["remaining"],0)

    def test_d08_concurrent_same_event_uses_one_ledger(self):
        store=Ledger(self.state)
        def replay():
            return store.replay(bundle(start()))
        with ThreadPoolExecutor(2) as pool:
            a,b=[pool.submit(replay) for _ in range(2)]
            self.assertEqual(a.result(timeout=10),b.result(timeout=10))
        self.assertEqual(len(store.inspect("stage1:test")["cycles"]),1)

    def test_c01_c02_normal_and_exception_cleanup(self):
        m=TempManager(self.tmp,persistent=self.state)
        with m.task() as work:
            p=work.stage("bundle.json",io.BytesIO(b"{}"))
            self.assertTrue(p.is_relative_to(self.tmp))
            self.assertEqual(p.read_bytes(),b"{}")
        self.assertFalse(p.exists())
        with self.assertRaisesRegex(RuntimeError,"test interruption"):
            with m.task() as work:
                p=work.stage("bundle.json",io.BytesIO(b"{}"))
                raise RuntimeError("test interruption")
        self.assertFalse(p.exists())

    def test_c03_crash_recovery_skips_active_lease(self):
        code="""
import io,os,signal,sys
from shared_kernel.temp_manager import TempManager
manager=TempManager(sys.argv[1],persistent=sys.argv[2])
with manager.task() as work:
    work.stage("bundle.json",io.BytesIO(b"test"))
    os.kill(os.getpid(),signal.SIGKILL)
"""
        m=TempManager(self.tmp,persistent=self.state)
        with m.task() as active:
            p=active.stage("bundle.json",io.BytesIO(b"alive"))
            proc=self.child(code,self.tmp,self.state)
            self.assertEqual(proc.returncode,-signal.SIGKILL,proc.stderr)
            recovered=m.collect()
            self.assertEqual(len(recovered["removed"]),1)
            self.assertEqual(recovered["failed"],[])
            self.assertEqual(p.read_bytes(),b"alive")

    def test_c05_download_holds_lease_until_response_closes(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from modules.strategy_hermes.manual_api import BASE, application
        from modules.strategy_hermes.temp_maintenance import INTERVAL, TempMaintenance

        manager = TempManager(self.tmp, persistent=self.state)
        maintenance = TempMaintenance(manager)
        queue = SimpleNamespace(get=Mock(return_value={"job":{"result":{"value":"x"*100000}}}))
        app = application(queue, "t"*40, export_manager=manager)
        identifier = "manual-draft-" + "a"*32
        request = {"REQUEST_METHOD":"GET", "PATH_INFO":f"{BASE}/{identifier}/download"}
        received = []
        start = lambda status,headers: received.append((status,dict(headers)))
        self.assertIn(b"AUTH_REQUIRED",b"".join(app(request,start)))
        queue.get.assert_not_called()

        response = app(request | {"HTTP_AUTHORIZATION":"Bearer " + "t"*40}, start)
        self.addCleanup(response.close)
        self.assertIn('attachment; filename=',received[-1][1]["Content-Disposition"])
        chunks = iter(response)
        self.assertTrue(next(chunks))
        (active,) = list((self.tmp/"stage1").glob("task-*"))
        old = 1
        os.utime(active,(old,old))
        os.utime(active/"result.json",(old,old))

        # An expired active export survives recovery while a killed process's
        # unattended task is reclaimed in the same maintenance pass.
        maintenance.tick(now=0)
        killed = self.child("""
import io,os,signal,sys
from shared_kernel.temp_manager import TempManager
with TempManager(sys.argv[1],persistent=sys.argv[2]).task() as work:
    work.stage('abandoned.json',io.BytesIO(b'data'))
    os.kill(os.getpid(),signal.SIGKILL)
""",self.tmp,self.state)
        self.assertEqual(killed.returncode,-signal.SIGKILL,killed.stderr)
        maintenance.tick(now=INTERVAL-1)
        self.assertEqual(maintenance.last_cleanup["removed"],0)
        maintenance.tick(now=INTERVAL)
        self.assertEqual(maintenance.last_cleanup["removed"],1)
        self.assertGreater(maintenance.status()["managed_bytes"],0)
        self.assertTrue(active.is_dir())
        response.close()
        self.assertFalse(active.exists())
        self.assertEqual(maintenance.status()["managed_bytes"],0)

    def test_c04_c06_shared_budget_and_chunk_limit(self):
        m=TempManager(self.tmp,persistent=self.state,total=150000,per_task=100000,min_free=0)
        with m.task() as a, m.task() as b:
            a.stage("a",io.BytesIO(b"a"*80000))
            with self.assertRaisesRegex(BudgetError,"ROOT_TEMP_BUDGET"):
                b.stage("b",io.BytesIO(b"b"*80000))
        with m.task() as a:
            with self.assertRaisesRegex(BudgetError,"TASK_TEMP_BUDGET"):
                a.stage("large",io.BytesIO(b"a"*120000))

    def test_c08_c09_unsafe_input_is_reported_and_target_untouched(self):
        m=TempManager(self.tmp,persistent=self.state)
        durable=self.state/"evidence"
        durable.write_text("keep")
        proc=self.child("""
import os,signal,sys
from shared_kernel.temp_manager import TempManager
with TempManager(sys.argv[1],persistent=sys.argv[2]).task() as work:
    (work.path/"pointer").symlink_to(sys.argv[3])
    os.kill(os.getpid(),signal.SIGKILL)
""",self.tmp,self.state,durable)
        self.assertEqual(proc.returncode,-signal.SIGKILL,proc.stderr)
        with self.assertLogs("shared_kernel.temp_manager",level="WARNING"):
            r=m.collect()
        self.assertEqual((len(r["removed"]),len(r["failed"])),(0,1))
        self.assertEqual(durable.read_text(),"keep")
        clean=self.home/"clean"; clean.mkdir(mode=0o700)
        with TempManager(clean,persistent=self.state).task() as work:
            with self.assertRaises(ValueError):
                work.stage("../evidence",io.BytesIO(b"bad"))

    def test_c10_b12_real_cli_keeps_evidence_and_replays_after_cleanup(self):
        prefix=[sys.executable,"-B","-m","modules.strategy_hermes","--state-dir",str(self.state)]
        frozen=self.home/"replay.json"
        raw_entry=start()[0]["report_json"]
        frozen.write_text(json.dumps(bundle(start()+[
            report("exit",D2,"09:35",[(A,"退潮")]),quote("exit",D2,"09:36","11")
        ])),encoding="utf-8")
        def command(*args):
            p=subprocess.run(prefix+list(map(str,args)),env=self.env,capture_output=True,text=True,timeout=15)
            self.assertEqual(p.returncode,0,p.stderr)
            return json.loads(p.stdout)
        result=command("replay",frozen)
        self.assertEqual(result["cycles"][0]["valuation"]["profit"],"1000.00")
        self.assertEqual(list((self.tmp/"stage1").glob("task-*")),[])
        self.assertEqual(command("recover-temp"),{"removed":[],"failed":[]})
        self.assertEqual(command("inspect","stage1:test"),result)
        self.assertEqual(command("report","stage1:test","entry")["raw"],raw_entry)

    def test_c11_low_disk_does_not_publish_result(self):
        m=TempManager(self.tmp,persistent=self.state,min_free=10**20)
        with self.assertRaisesRegex(BudgetError,"LOW_DISK"):
            with m.task():
                self.fail("disk guard unexpectedly allowed workspace")
        self.assertEqual(list((self.tmp/"stage1").glob("task-*")),[])
        self.assertFalse((self.state/"stage1.sqlite3").exists())
