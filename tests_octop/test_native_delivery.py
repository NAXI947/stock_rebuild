"""Dedicated delivery behavior with OCTOP trackers and isolated service fakes."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from deploy.octop_native_delivery import deliver_native_round


class NativeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.command = SimpleNamespace(agent_id="native-test", cron_name="13:05", prompt="frozen test")
        self.session = SimpleNamespace(thread_id="thr_01JQQSTYLEISOLATEDTEST01", user_id=1)
        self.harness = SimpleNamespace(aappend_messages=AsyncMock(side_effect=lambda _, messages: messages))
        self.service = SimpleNamespace(
            _build_agent_request=AsyncMock(return_value={"messages": [{"role": "user", "content": "test"}]}),
            _agent_manager=SimpleNamespace(get_agent=Mock(return_value=self.harness)),
            _project_best_effort=Mock(), _remember_thread_title=Mock(),
            _gateway=SimpleNamespace(push_session_text=AsyncMock()), _notify_best_effort=AsyncMock(),
            _repos=SimpleNamespace(usage_repo=Mock()))

    def stream(self, *, exception=None, error_chunk=False):
        async def chunks(*_):
            yield {"type": "token", "content": "已生成部分输出"}
            yield {"type": "usage", "usage": {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168},
                   "model": "test-model", "id": "call1"}
            if error_chunk:
                yield {"type": "error", "message": "frozen internal error"}
            if exception:
                raise exception
        self.service._agent_manager.stream = chunks

    def run_delivery(self):
        return asyncio.run(deliver_native_round(self.service, self.command, self.session))

    def test_failure_retains_usage_once_pushes_feedback_and_raises(self):
        self.stream(exception=RuntimeError("STOCK_NATIVE_ROUND_NOT_COMMITTED"))
        with patch("octop.infra.cron.delivery.record_turn_usage") as record:
            with self.assertRaisesRegex(RuntimeError, "ROUND_NOT_COMMITTED"):
                self.run_delivery()
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["usage"]["total_tokens"], 168)
        self.harness.aappend_messages.assert_awaited_once()
        self.assertIn("执行失败", self.service._gateway.push_session_text.call_args.args[1])

    def test_stream_error_cannot_turn_partial_text_into_success(self):
        self.stream(error_chunk=True)
        with patch("octop.infra.cron.delivery.record_turn_usage") as record:
            with self.assertRaisesRegex(RuntimeError, "STREAM_FAILED"):
                self.run_delivery()
        record.assert_called_once()

    def test_success_usage_is_not_duplicated(self):
        self.stream()
        with patch("octop.infra.cron.delivery.record_turn_usage") as record:
            self.run_delivery()
        record.assert_called_once()
        self.harness.aappend_messages.assert_not_called()
        self.assertEqual(self.service._gateway.push_session_text.call_args.args[1], "已生成部分输出")

    def test_pre_model_failure_has_feedback_without_fake_usage(self):
        self.service._build_agent_request.side_effect = RuntimeError("private error detail")
        with patch("octop.infra.cron.delivery.record_turn_usage") as record:
            with self.assertRaisesRegex(RuntimeError, "private error detail"):
                self.run_delivery()
        record.assert_not_called()
        text = self.service._gateway.push_session_text.call_args.args[1]
        self.assertIn("RuntimeError", text)
        self.assertNotIn("private error detail", text)

    def test_failure_callback_does_not_replace_original_error_or_usage(self):
        self.command.agent_id, self.command.cron_id = "6T6VMW", "c3FDPYP"
        self.stream(exception=TimeoutError("private provider details"))
        with patch("deploy.octop_native_delivery._post_native_notice", side_effect=OSError("private callback details")) as notice:
            with patch("octop.infra.cron.delivery.record_turn_usage") as record:
                with self.assertLogs("deploy.octop_native_delivery", level="WARNING"):
                    with self.assertRaisesRegex(TimeoutError, "private provider details"):
                        self.run_delivery()
        notice.assert_called_once()
        self.assertEqual(notice.call_args.args[0]["error_code"], "TimeoutError")
        record.assert_called_once()

    def test_success_never_sends_failure_callback(self):
        self.command.agent_id, self.command.cron_id = "6T6VMW", "c3FDPYP"
        self.stream()
        with patch("deploy.octop_native_delivery._post_native_notice") as notice:
            with patch("octop.infra.cron.delivery.record_turn_usage"):
                self.run_delivery()
        notice.assert_not_called()


if __name__ == "__main__":
    unittest.main()
