"""Real ModelRequest verification of ephemeral, channel-specific presentation."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage
from deploy import octop_qq_daily as daily


class DailyFormattingTests(unittest.TestCase):
    def setUp(self):
        self.middleware = daily.StockQQDailyMiddleware()
        self.request = ModelRequest(model=Mock(), messages=[HumanMessage(content="分析银行")], tools=[Mock()],
                                    system_message=SystemMessage(content="原研判规则", id="system-original"),
                                    model_settings={"temperature": 0.2})
        self.stream = MagicMock()
        self.stream.__enter__.return_value.read.return_value = "【答复】\n{answer}\n【依据】\n{evidence}"
        self.path = SimpleNamespace(open=Mock(return_value=self.stream))

    def test_qq_changes_only_ephemeral_system_presentation(self):
        with patch.object(daily, "DAILY_TEMPLATE", self.path), daily.qq_daily_scope("my-stock", "qq/channel"):
            result = self.middleware._format_request(self.request)
        self.assertIsNot(result, self.request)
        self.assertTrue(result.system_message.content.startswith("原研判规则\n"))
        self.assertIn("不得改研究结论，不补事实", result.system_message.content)
        self.assertIn("【答复】", result.system_message.content)
        self.assertEqual(result.system_message.id, "system-original")
        self.assertEqual(self.request.system_message.content, "原研判规则")
        for name in ("model", "messages", "tools", "state", "runtime", "model_settings", "tool_choice", "response_format"):
            self.assertIs(getattr(result, name), getattr(self.request, name))

    def test_other_channels_and_native_experts_do_not_read_template(self):
        with patch.object(daily, "DAILY_TEMPLATE", self.path):
            for agent, source in (("my-stock", "dashboard/session"), ("my-stock", "wechat/channel"),
                                  ("6T6VMW", "qq/channel"), ("ATAPBR", "dashboard")):
                with daily.qq_daily_scope(agent, source):
                    self.assertIs(self.middleware._format_request(self.request), self.request)
        self.path.open.assert_not_called()

    def test_scope_resets_after_error_and_template_refreshes_each_turn(self):
        with patch.object(daily, "DAILY_TEMPLATE", self.path):
            with self.assertRaisesRegex(RuntimeError, "test"), daily.qq_daily_scope("my-stock", "qq/channel"):
                self.assertIsNot(self.middleware._format_request(self.request), self.request)
                raise RuntimeError("test")
            self.assertIs(self.middleware._format_request(self.request), self.request)
            self.stream.__enter__.return_value.read.return_value = "新版模板"
            with daily.qq_daily_scope("my-stock", "qq/channel"):
                self.assertIn("新版模板", self.middleware._format_request(self.request).system_message.content)
        self.assertEqual(self.path.open.call_count, 2)

    def test_missing_or_oversized_template_preserves_research(self):
        with patch.object(daily, "DAILY_TEMPLATE", self.path), daily.qq_daily_scope("my-stock", "qq/channel"):
            for text in ("", "x" * 8193):
                self.stream.__enter__.return_value.read.return_value = text
                with self.assertLogs(daily.__name__, level="WARNING"):
                    self.assertIs(self.middleware._format_request(self.request), self.request)
            self.path.open.side_effect = FileNotFoundError("private path")
            with self.assertLogs(daily.__name__, level="WARNING") as logs:
                self.assertIs(self.middleware._format_request(self.request), self.request)
            self.assertNotIn("private path", str(logs.output))

    def test_concurrent_web_and_qq_turns_have_isolated_context(self):
        async def run(agent, source):
            with daily.qq_daily_scope(agent, source):
                await asyncio.sleep(0)
                return self.middleware._format_request(self.request)
        async def scenario():
            return await asyncio.gather(run("my-stock", "qq/a"), run("my-stock", "dashboard/b"))
        with patch.object(daily, "DAILY_TEMPLATE", self.path):
            qq, web = asyncio.run(scenario())
        self.assertIsNot(qq, self.request)
        self.assertIs(web, self.request)

    def test_structured_system_blocks_are_preserved(self):
        original = SystemMessage(content=[{"type": "text", "text": "原规则"}], id="structured")
        request = self.request.override(system_message=original)
        with patch.object(daily, "DAILY_TEMPLATE", self.path), daily.qq_daily_scope("my-stock", "qq/channel"):
            result = self.middleware._format_request(request)
        self.assertEqual(result.system_message.content[0], original.content[0])
        self.assertEqual(len(original.content), 1)
        self.assertEqual(len(result.system_message.content), 2)


if __name__ == "__main__":
    unittest.main()
