"""Exercise the Findesk batch contract without a network request."""
from contextlib import nullcontext
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import findesk_iopv as adapter
from modules.strategy_hermes.stage2.market_bootstrap import ProbeError


class FindeskIOPV(TestCase):
    def test_one_bearer_request_and_per_symbol_nulls(self):
        now = datetime(2026, 11, 13, 14, 5, 30, tzinfo=CHINA)
        codes = ["512800.SH", "512480.SH", "159915.SZ", "159995.SZ", "512800.SH"]
        response = {"code": 200, "data": {
            "code": ["512800.SH", "512480.SH", "159995.SZ"],
            "iopv": ["0.9123", "1.02", "0"],
            "time": ["2026-11-13T06:05:20.123Z", None, "2026-11-13 14:05:21"],
        }}
        with patch.object(adapter, "_credential", return_value="fixture-key"), \
             patch.object(adapter, "fetch", return_value=response) as fetch, \
             patch.object(adapter, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            result = adapter.fetch_iopv("/fixture/findesk.json", codes)
        fetch.assert_called_once()
        request, = fetch.call_args.args
        self.assertEqual(fetch.call_args.kwargs, {"timeout": 15})
        self.assertEqual(request.full_url, "https://ffd.findesk.cn/api/query")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-key")
        self.assertEqual(json.loads(request.data), {"function": "realtime_quote",
            "codes": "159915.SZ,159995.SZ,512480.SH,512800.SH", "indicators": "iopv;time"})
        values = {row["etf"]: row for row in result}
        self.assertEqual(values["512800.SH"], {"etf": "512800.SH", "iopv": "0.9123",
            "observed_at": "2026-11-13T14:05:20.123000+08:00", "source_time": "2026-11-13T06:05:20.123Z",
            "source": "findesk"})
        self.assertIsNone(values["159915.SZ"]["iopv"])
        self.assertIsNone(values["512480.SH"]["observed_at"])
        self.assertIsNone(values["512480.SH"]["iopv"])
        self.assertIsNone(values["159995.SZ"]["iopv"])
        self.assertEqual(values["159995.SZ"]["observed_at"], "2026-11-13T14:05:21+08:00")

    def test_invalid_scope_and_private_file_are_checked_before_transport(self):
        too_many = [f"{code:06d}.SH" for code in range(101)]
        with patch.object(adapter, "fetch") as fetch, TemporaryDirectory() as work:
            config = Path(work, "findesk.json")
            config.write_text('{"api_key":"fixture-key"}')
            config.chmod(0o640)
            with self.assertRaises(ProbeError):
                adapter.fetch_iopv(config, ["512800.SH"])
            config.chmod(0o600)
            self.assertEqual(adapter._credential(config), "fixture-key")
            link = Path(work, "link.json")
            link.symlink_to(config)
            with self.assertRaises(ProbeError):
                adapter.fetch_iopv(link, ["512800.SH"])
            with self.assertRaises(ValueError):
                adapter.fetch_iopv(config, too_many)
            self.assertEqual(len(adapter._scope(too_many[:100])), 100)
            fetch.assert_not_called()

    def test_rejects_invalid_envelope_and_marks_future_sample_missing(self):
        now = datetime(2026, 11, 13, 14, 5, 30, tzinfo=CHINA)
        code = "512800.SH"
        future = "2026-11-13 14:06:00"
        row, = adapter._normalize({"code": 200, "data": {
            "code": [code], "iopv": ["1.00"], "time": [future],
        }}, [code], now)
        self.assertEqual((row["iopv"], row["observed_at"], row["source_time"]), (None, None, future))
        for body in (
            {"code": 403},
            {"code": 200, "data": {"code": [code], "iopv": [], "time": [future]}},
            {"code": 200, "data": {"code": ["512480.SH"], "iopv": ["1"], "time": [future]}},
            {"code": 200, "data": {"code": [code, code], "iopv": ["1", "1"], "time": [future, future]}},
        ):
            with self.subTest(body=body), self.assertRaises(ProbeError):
                adapter._normalize(body, [code, "159915.SZ"], now)

    def test_collector_reserves_one_batch_prioritizes_quotes_and_keeps_clocks_separate(self):
        now = datetime(2026, 11, 13, 14, 5, 30, tzinfo=CHINA)
        quoted = "599999.SH"
        eligible = {f"{code:06d}.SH": 100 for code in range(100)} | {quoted: 100}
        market = {"quotes": {quoted: {"observed_at": "2026-11-13T14:05:10+08:00", "price_cny": "0.98"}},
                  "observed_at": "2026-11-13T14:05:10+08:00", "snapshot_id": "original", "quality": {}}
        config = "/fixture/findesk.json"
        store = Mock()
        connection = Mock()
        store._connect.return_value = nullcontext(connection)
        budget = Mock()
        budget.call.side_effect = lambda source, scope, operation, limit: operation()
        read = {"etf": quoted, "iopv": "0.975", "observed_at": "2026-11-13T14:05:20+08:00",
                "source_time": "2026-11-13 14:05:20", "source": "findesk"}
        stale = {**read, "etf": "000000.SH", "observed_at": "2026-11-12T14:05:20+08:00"}
        connection.execute.return_value.fetchone.return_value = (json.dumps({"readings": [read, stale]}),)
        with patch("modules.strategy_hermes.stage2.penetration.instrument_units", return_value=eligible),              patch.object(adapter, "_credential", return_value="fixture-key"),              patch.object(adapter, "_query", return_value=([read, stale], now)) as query,              patch.object(adapter, "save", return_value={"batch": "fixture", "source_clock": read["observed_at"]}),              patch.object(adapter, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            result = adapter.collect(store, budget, config, {"version": "fixture"}, market, now)
        budget.call.assert_called_once()
        source, scope, _ = budget.call.call_args.args
        self.assertEqual(source, adapter.SOURCE)
        self.assertEqual(budget.call.call_args.kwargs, {"limit": 1})
        self.assertEqual(len(scope["codes"]), 100)
        self.assertIn(quoted, scope["codes"])
        self.assertNotIn("000099.SH", scope["codes"])
        self.assertEqual(query.call_args.args[1], scope["codes"])
        self.assertEqual(result["valid"], 1)
        adapter.mark_quality(market, result)
        self.assertNotEqual(market["snapshot_id"], "original")
        self.assertEqual(market["quality"], {"iopv": "AVAILABLE", "iopv_count": 1})
        self.assertEqual(market["iopv"]["readings"], {quoted: read})
        self.assertEqual(market["observed_at"], "2026-11-13T14:05:10+08:00")
        self.assertEqual(market["quotes"][quoted]["price_cny"], "0.98")

    def test_missing_findesk_configuration_uses_no_budget(self):
        budget = Mock()
        with patch("modules.strategy_hermes.stage2.penetration.instrument_units", return_value={"512800.SH": 100}):
            result = adapter.collect(Mock(), budget, "/fixture/nonexistent-findesk.json", {"version": "test"},
                                     {"quotes": {}}, datetime(2026, 11, 13, 14, 5, tzinfo=CHINA))
        self.assertEqual(result, {"status": "CONFIG_UNAVAILABLE", "requested": 0})
        budget.call.assert_not_called()

    def test_optional_provider_failure_still_publishes_shared_market(self):
        from modules.strategy_hermes.stage2 import intraday

        now = datetime(2026, 11, 13, 14, 5, tzinfo=CHINA)
        market = {"snapshot_id": "fixture", "quality": {}}
        ledger = SimpleNamespace(ns="stock-v4:prod:fixture", settings=SimpleNamespace(env="production"))
        with patch("modules.strategy_hermes.stage2.inputs.shared_market", return_value=None),              patch.object(intraday, "RequestLog") as log,              patch.object(intraday, "collect_boards", return_value=[]),              patch.object(intraday, "assemble", return_value=market),              patch.object(adapter, "collect", side_effect=ProbeError("simulated provider unavailable")) as collect,              patch.object(intraday, "publish_snapshot") as publish:
            log.return_value.new_calls = 2
            result = intraday._refresh(ledger, None, "fixture", (now.date().isoformat(), "14:05", now),
                                       None, None, None, findesk=("/fixture/findesk.json", {}))
        collect.assert_called_once()
        self.assertIs(collect.call_args.args[1], log.return_value)
        publish.assert_called_once_with(ledger, None, market, key="fixture")
        self.assertEqual(result["findesk_iopv"]["status"], "UNAVAILABLE")
        self.assertEqual(result["physical_requests"], 2)
        self.assertEqual(market["quality"]["iopv_count"], 0)
