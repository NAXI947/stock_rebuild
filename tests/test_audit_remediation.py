"""Regression evidence for the deployment audit; no provider or production writes."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import date
from decimal import Decimal

from shared_kernel.temp_manager import TempManager, BudgetError
from modules.strategy_hermes.stage2.penetration import weights
from modules.strategy_hermes.manual_portfolio import replay


class AuditRemediation(unittest.TestCase):
    def test_configured_root_mount_is_allowed_but_nested_mount_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            manager=TempManager(root,min_free=0)
            with patch("shared_kernel.temp_manager.os.path.ismount",side_effect=lambda p:p==root):
                self.assertEqual(manager.usage()[0],0)
            with patch("shared_kernel.temp_manager.os.path.ismount",return_value=True):
                with self.assertRaises(ValueError):
                    manager.usage()

    def test_open_handle_defers_cleanup_until_descriptor_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            manager=TempManager(root,min_free=0)
            stream=None
            try:
                with self.assertRaisesRegex(ValueError,"open file handle"):
                    with manager.task() as work:
                        path=work.stage("response",io.BytesIO(b"retained"))
                        stream=path.open("rb")
                self.assertEqual(manager.usage()[0],8)
                self.assertEqual(stream.read(),b"retained")
            finally:
                if stream: stream.close()
            self.assertEqual(len(manager.collect()["removed"]),1)
            self.assertEqual(manager.usage()[0],0)

    def test_global_budget_counts_other_services_and_sdk_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for role in ("api", "worker", "web"):
                (root/role).mkdir(mode=0o700)
            (root/"web"/"buffer").write_bytes(b"x"*80)
            api=TempManager(root, service="api", total=128, per_task=128, min_free=0)
            worker=TempManager(root, service="worker", total=128, per_task=128, min_free=0)
            with api.task() as a, worker.task() as b:
                a.stage("a",io.BytesIO(b"a"*32))
                with self.assertRaises(BudgetError):
                    b.stage("b",io.BytesIO(b"b"*32))
                self.assertEqual(api.usage()[0], worker.usage()[0])
            self.assertEqual(api.usage()[0],80)
            self.assertTrue((root/"web"/"buffer").exists())

    def test_configuration_applies_to_every_manager(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,
                APP_TMP_BUDGET_BYTES="128", APP_TMP_TASK_BUDGET_BYTES="64", STORAGE_MIN_FREE_BYTES="0"):
            manager=TempManager(Path(directory))
            self.assertEqual((manager.total,manager.per_task),(128,64))
            with manager.task() as work:
                with self.assertRaises(BudgetError):
                    work.stage("large",io.BytesIO(b"x"*65))

    def test_partial_weights_keep_original_denominator(self):
        basis=dict(complete=False,as_of_date="2026-11-13",rows=[dict(code="600000.SH",weight="0.2")])
        parsed=weights(basis,date(2026,11,14))
        self.assertEqual(parsed[2]["600000.SH"],Decimal("0.2"))

    def test_same_instant_cross_etf_sales_precede_buys(self):
        a,b="510001.SH","159001.SZ"
        days=["2026-11-12","2026-11-13","2026-11-16"]
        stamp=lambda d,t:f"{d}T{t}:00+08:00"
        def quote(identifier,code,day):
            return dict(kind="quote",id=identifier,etf=code,observed_at=stamp(day,"09:36"),
                received_at=stamp(day,"09:36"),price_cny="10",source="frozen:audit",
                tradeable=True,revision=0,price_kind="ASSUMED")
        bundle=dict(schema_version="stock-v4-manual-v1",price_mode="ASSUMPTION",instruments={a:100,b:100},
            calendar=days,cutoff=stamp(days[1],"15:00"),initial_cash="10000",nodes=[
                dict(id="entry",etf=a,at=stamp(days[0],"09:35"),action="BUY",size_mode="SHARES",size=1000),
                dict(id="sell",etf=a,at=stamp(days[1],"09:35"),action="SELL",size_mode="ALL"),
                dict(id="buy",etf=b,at=stamp(days[1],"09:35"),action="BUY",size_mode="SHARES",size=1000)],
            quotes=[quote("entry",a,days[0]),quote("z",a,days[1]),quote("a",b,days[1])])
        first=replay(bundle)
        bundle["quotes"][1]["id"],bundle["quotes"][2]["id"]="a","z"
        bundle["quotes"].reverse()
        second=replay(bundle)
        self.assertEqual(first["status"],"COMPLETE")
        self.assertEqual(second["status"],"COMPLETE")
        self.assertEqual([f["action"] for f in first["fills"]],["BUY","SELL","BUY"])
        self.assertEqual(first["cash"],second["cash"])
