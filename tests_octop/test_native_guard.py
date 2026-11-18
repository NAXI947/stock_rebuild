"""Run with OCTOP's installed LangChain runtime; no model or network requests."""
import asyncio
import json
from types import SimpleNamespace
import unittest

from deploy.octop_native_guard import MAINTENANCE_TOOLS, MAX_MODEL_CALLS, TOOLS, StockNativeRoundsMiddleware, native_filesystem_middleware

INSTRUCTION = "执行北京时间 13:05 板块轮动与ETF穿透本轮任务。"


def message(kind, content="", name=""):
    if kind == "human" and not content:
        content = INSTRUCTION
    return {"type": kind, "content": content, "name": name}


class NativeGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = StockNativeRoundsMiddleware()
        self.name = "stock-v4-rounds_stock_hermes_status"

    def request(self, messages=None):
        request = SimpleNamespace(state={"messages": messages or [message("human")]},
                                  tools=[SimpleNamespace(name=name) for name in [*TOOLS, *MAINTENANCE_TOOLS, "task", "execute", "web_fetch", "cronjob_create"]])
        request.override = lambda **kwargs: SimpleNamespace(**kwargs)
        return request

    def test_only_three_model_tools(self):
        result = self.guard.wrap_model_call(self.request(), lambda r: r)
        self.assertEqual({t.name for t in result.tools}, TOOLS)
        self.assertEqual(len(result.tools), 3)
        self.assertEqual(MAX_MODEL_CALLS, 8)

    def test_direct_forbidden_calls_never_reach_handler(self):
        for name in ("task", "execute", "read_file", "cronjob_create", "other_stock_hermes_submit", *MAINTENANCE_TOOLS):
            with self.assertRaisesRegex(RuntimeError, "TOOL_NOT_ALLOWED"):
                self.guard.wrap_tool_call(SimpleNamespace(tool_call={"name": name}, state={"messages": [message("human")]}),
                                          lambda _: self.fail("forbidden handler executed"))

    def test_async_allowed_and_forbidden(self):
        async def handler(request):
            return "called"
        self.assertEqual(asyncio.run(self.guard.awrap_tool_call(
            SimpleNamespace(tool_call={"name": self.name}, state={"messages": [message("human")]}), handler)), "called")
        with self.assertRaisesRegex(RuntimeError, "TOOL_NOT_ALLOWED"):
            asyncio.run(self.guard.awrap_tool_call(SimpleNamespace(tool_call={"name": "task"}, state={"messages": [message("human")]}), handler))

    def test_eighth_model_reply_exhausts_budget(self):
        history = [message("human")] + [message("ai", "working")] * MAX_MODEL_CALLS
        with self.assertRaisesRegex(RuntimeError, "MODEL_CALL_BUDGET"):
            self.guard.wrap_model_call(self.request(history), lambda _: self.fail("model called"))
        self.guard.wrap_model_call(self.request(history + [message("human")]), lambda r: r)

    def test_real_nested_published_receipt(self):
        body = {"content": [{"type": "text", "text": json.dumps({"state": "PUBLISHED", "slot": "13:05", "report_id": "report-test", "run_id": "r", "qq": "FAILED"})}]}
        self.guard.after_agent({"messages": [message("human"), message("tool", json.dumps(body), self.name)]}, None)

    def test_skip_is_terminal(self):
        body = dict(state="SKIPPED", slot="13:05", reason="NON_TRADING_DAY")
        self.guard.after_agent({"messages": [message("human"), message("tool", json.dumps(body), self.name)]}, None)

    def test_failed_mcp_envelope_keeps_trusted_stage(self):
        for stage in ("PREPARE", "ANALYZE", "PUBLISH"):
            body = {"isError": True, "content": [{"text": json.dumps(dict(state="FAILED", slot="13:05", stage=stage))}]}
            with self.assertRaisesRegex(RuntimeError, f"STOCK_NATIVE_{stage}_FAILED"):
                self.guard.after_agent({"messages": [message("human"), message("tool", json.dumps(body), self.name)]}, None)

    def test_wrong_slot_and_missing_report_fail(self):
        for body in (dict(state="PUBLISHED", slot="09:35", report_id="old"),
                     dict(state="PUBLISHED", slot="13:05"),
                     dict(state="SKIPPED", slot="13:05", reason="invented")):
            with self.assertRaisesRegex(RuntimeError, "ROUND_NOT_COMMITTED"):
                self.guard.after_agent({"messages": [message("human"), message("tool", json.dumps(body), self.name)]}, None)
        with self.assertRaisesRegex(RuntimeError, "SLOT_MISMATCH"):
            self.guard.wrap_tool_call(SimpleNamespace(state={"messages": [message("human")]},
                tool_call={"name": "stock-v4-rounds_stock_hermes_prepare", "args": {"slot": "09:35"}}),
                lambda _: self.fail("wrong slot executed"))

    def test_fabricated_prose_old_turn_and_failed_receipt_are_rejected(self):
        samples = [
            [message("human"), message("ai", '{"state":"PUBLISHED"}')],
            [message("human"), message("tool", '{"state":"PUBLISHED"}', "unrelated")],
            [message("human"), message("tool", '{"state":"FAILED"}', self.name)],
            [message("human"), message("tool", '{"state":"PUBLISHED"}', self.name), message("human"), message("ai", "done")],
            [message("human"), message("tool", '{"isError":true,"content":[{"text":"{\\"state\\":\\"PUBLISHED\\"}"}]}', self.name)],
        ]
        for messages in samples:
            with self.assertRaisesRegex(RuntimeError, "ROUND_NOT_COMMITTED"):
                self.guard.after_agent({"messages": messages}, None)

    def _graph(self, replies, result):
        from langchain.agents import create_agent
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.tools import StructuredTool

        class ToolModel(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                if {tool.name for tool in tools} - TOOLS:
                    raise AssertionError("Model saw an out-of-scope tool")
                return self

        tool = StructuredTool.from_function(lambda: json.dumps(result), name=self.name, description="Frozen test receipt")
        forbidden = StructuredTool.from_function(lambda: self.fail("forbidden tool executed"), name="execute", description="Must be blocked")
        return create_agent(model=ToolModel(responses=replies), tools=[tool, forbidden], middleware=[self.guard])

    def test_actual_async_graph_published(self):
        from langchain_core.messages import AIMessage, HumanMessage
        replies = [AIMessage(content="", tool_calls=[{"name": self.name, "args": {}, "id": "call1", "type": "tool_call"}]),
                   AIMessage(content="报告已保存")]
        graph = self._graph(replies, {"state": "PUBLISHED", "slot": "13:05", "report_id": "test-report", "run_id": "test-only"})
        output = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content=INSTRUCTION)]}))
        self.assertEqual(output["messages"][-1].content, "报告已保存")

    def test_actual_async_graph_false_success_raises(self):
        from langchain_core.messages import AIMessage, HumanMessage
        graph = self._graph([AIMessage(content="已经完成")], {"state": "FAILED"})
        with self.assertRaisesRegex(RuntimeError, "ROUND_NOT_COMMITTED"):
            asyncio.run(graph.ainvoke({"messages": [HumanMessage(content=INSTRUCTION)]}))

    def test_actual_deepagent_large_receipt_stays_in_context(self):
        from deepagents import create_deep_agent
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from langchain_core.tools import StructuredTool

        class ToolModel(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                if {tool.name for tool in tools} - TOOLS:
                    raise AssertionError("Model saw an out-of-scope deepagent tool")
                return self

        content = json.dumps(dict(state="PUBLISHED", slot="13:05", report_id="large-report",
                                  feedback="evidence " * 40000 + "COMPLETE_RECEIPT_END"))
        tool = StructuredTool.from_function(lambda: content, name=self.name, description="Large frozen receipt")
        replies = [AIMessage(content="", tool_calls=[dict(name=self.name, args={}, id="large", type="tool_call")]),
                   AIMessage(content="完成")]
        graph = create_deep_agent(model=ToolModel(responses=replies), tools=[tool],
                                  middleware=[native_filesystem_middleware(), self.guard])
        output = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content=INSTRUCTION)]}))
        results = [m.content for m in output["messages"] if isinstance(m, ToolMessage)]
        self.assertEqual(results, [content])


if __name__ == "__main__":
    unittest.main()
