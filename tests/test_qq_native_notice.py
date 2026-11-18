"""Private OCTOP failure receipts use a single sanitized, bounded HTTP request."""
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from deploy import octop_native_delivery as delivery


class NativeNoticeTests(unittest.TestCase):
    def setUp(self):
        self.command = SimpleNamespace(agent_id="6T6VMW", cron_id="c3FDPYP")
        self.session = SimpleNamespace(thread_id="thr_01JQQSTYLEISOLATEDTEST01")

    def test_bound_failure_transmits_only_receipt_fields(self):
        with patch.object(delivery, "_post_native_notice") as send:
            asyncio.run(delivery.notify_native_failure(self.command, self.session, "TimeoutError"))
        send.assert_called_once()
        body = send.call_args.args[0]
        self.assertEqual(set(body), {"agent_id", "cron_id", "thread_id", "observed_at", "error_code"})
        self.assertEqual(body["error_code"], "TimeoutError")
        self.assertLess(abs((datetime.now(timezone.utc) - datetime.fromisoformat(body["observed_at"])).total_seconds()), 5)

    def test_cross_expert_or_unknown_cron_never_calls_backend(self):
        with patch.object(delivery, "_post_native_notice") as send:
            for expert, cron in (("my-stock", "c3FDPYP"), ("6T6VMW", "cMWFP36"), ("ATAPBR", "c3FDPYP"),
                                 ("ATAPBR", "unknown")):
                asyncio.run(delivery.notify_native_failure(SimpleNamespace(agent_id=expert, cron_id=cron),
                                                          self.session, "RuntimeError"))
        send.assert_not_called()

    def test_unstructured_error_text_is_not_sent(self):
        with patch.object(delivery, "_post_native_notice") as send:
            asyncio.run(delivery.notify_native_failure(self.command, self.session, "private model text/token=secret"))
        self.assertEqual(send.call_args.args[0]["error_code"], "RuntimeError")

    def test_receipt_matches_backend_validator_without_db_or_queue_write(self):
        if importlib.util.find_spec("modules") is None:
            self.skipTest("Backend validator is covered in the stock runtime test suite")
        from modules.strategy_hermes.qq_task_notices import receive_native_failure
        queue = SimpleNamespace(ledger=SimpleNamespace(origin="TEST"))
        for expert, crons in delivery.NOTICE_BINDINGS.items():
            for cron in crons:
                with patch.object(delivery, "_post_native_notice") as send:
                    asyncio.run(delivery.notify_native_failure(SimpleNamespace(agent_id=expert, cron_id=cron),
                                                              self.session, "STOCK_NATIVE_ANALYZE_FAILED"))
                self.assertEqual(receive_native_failure(queue, Mock(), send.call_args.args[0]), {"state": "DISABLED"})

    def test_callback_failure_does_not_retry_or_log_exception_text(self):
        with patch.object(delivery, "_post_native_notice", side_effect=OSError("secret body")) as send:
            with self.assertLogs(delivery.__name__, level="WARNING") as logs:
                asyncio.run(delivery.notify_native_failure(self.command, self.session, "TimeoutError"))
        send.assert_called_once()
        self.assertIn("OSError", logs.output[0])
        self.assertNotIn("secret", logs.output[0])

    def test_http_uses_fixed_endpoint_auth_timeout_and_disables_proxy_redirect(self):
        stream = MagicMock()
        stream.__enter__.return_value.read.return_value = "a" * 48
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.status = 202
        with patch.object(delivery, "NOTICE_TOKEN_PATH", SimpleNamespace(open=Mock(return_value=stream))):
            with patch.object(delivery, "build_opener", return_value=opener) as build:
                delivery._post_native_notice({"error_code": "TimeoutError"})
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, delivery.NOTICE_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + "a" * 48)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)
        self.assertEqual(json.loads(request.data), {"error_code": "TimeoutError"})
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsNone(build.call_args.args[1].redirect_request(None, None, 302, None, None, "https://elsewhere"))


if __name__ == "__main__":
    unittest.main()
