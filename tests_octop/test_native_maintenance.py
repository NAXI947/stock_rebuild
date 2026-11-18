"""Maintenance uses its own tools, mode, budget and saved completion receipt."""
import asyncio
import json
from types import SimpleNamespace
import unittest

from deploy.octop_native_guard import MAINTENANCE_TOOLS, TOOLS, StockNativeRoundsMiddleware


def message(kind, content="", name=""):
    return {"type": kind, "content": content, "name": name}


def instruction(mode="close"):
    return message("human", f"执行ETF资料维护 {mode}。等待已保存的最终回执后反馈。")


class NativeMaintenanceGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = StockNativeRoundsMiddleware()
        self.prepare = "stock-v4-rounds_stock_etf_maintenance_prepare"
        self.status = "stock-v4-rounds_stock_etf_maintenance_status"

    def request(self, messages):
        request = SimpleNamespace(state={"messages": messages}, tools=[
            SimpleNamespace(name=name) for name in (*TOOLS, *MAINTENANCE_TOOLS, "execute", "read_file", "cronjob_create")])
        request.override = lambda **kwargs: SimpleNamespace(**kwargs)
        return request

    def finish(self, body, *, mode="close", name=None, prefix=None):
        envelope = {"content": [{"type": "text", "text": json.dumps(body)}]}
        return self.guard.after_agent({"messages": [*(prefix or []), instruction(mode),
            message("tool", json.dumps(envelope), name or self.status)]}, None)

    def test_maintenance_prompt_exposes_only_maintenance_tools(self):
        for mode in ("market", "retry", "close", "review", "preopen"):
            with self.subTest(mode=mode):
                result = self.guard.wrap_model_call(self.request([instruction(mode)]), lambda r: r)
                self.assertEqual({tool.name for tool in result.tools}, MAINTENANCE_TOOLS)
                self.assertEqual(len(result.tools), 2)

    def test_direct_research_and_generic_tools_cannot_run_in_maintenance(self):
        for name in (*TOOLS, "execute", "read_file", "cronjob_create", "other_stock_etf_maintenance_status"):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "TOOL_NOT_ALLOWED"):
                self.guard.wrap_tool_call(SimpleNamespace(state={"messages": [instruction()]},
                    tool_call={"name": name, "args": {}}), lambda _: self.fail("forbidden tool executed"))

    def test_prepare_mode_must_match_current_prompt(self):
        for mode in ("market", "retry", "close", "review", "preopen"):
            for actual in ("market", "retry", "close", "review", "preopen", None):
                request = SimpleNamespace(state={"messages": [instruction(mode)]},
                    tool_call={"name": self.prepare, "args": {"mode": actual}})
                with self.subTest(mode=mode, actual=actual):
                    if actual == mode:
                        self.assertEqual(self.guard.wrap_tool_call(request, lambda _: "called"), "called")
                    else:
                        with self.assertRaisesRegex(RuntimeError, "SLOT_MISMATCH"):
                            self.guard.wrap_tool_call(request, lambda _: self.fail("wrong mode executed"))

    def test_24th_model_call_is_allowed_and_25th_blocked(self):
        history = [instruction()] + [message("ai", "waiting")] * 23
        self.guard.wrap_model_call(self.request(history), lambda r: r)
        with self.assertRaisesRegex(RuntimeError, "MODEL_CALL_BUDGET"):
            self.guard.wrap_model_call(self.request(history + [message("ai")]), lambda _: self.fail("25th model call"))
        self.guard.wrap_model_call(self.request(history + [message("ai"), instruction("review")]), lambda r: r)

    def test_latest_prompt_switches_back_to_research_budget_and_tools(self):
        history = [instruction()] + [message("ai")] * 24
        history.append(message("human", "执行北京时间 13:05 板块轮动与ETF穿透本轮任务。"))
        result = self.guard.wrap_model_call(self.request(history + [message("ai")] * 7), lambda r: r)
        self.assertEqual({tool.name for tool in result.tools}, TOOLS)
        with self.assertRaisesRegex(RuntimeError, "MODEL_CALL_BUDGET"):
            self.guard.wrap_model_call(self.request(history + [message("ai")] * 8), lambda _: self.fail("9th research call"))

    def test_saved_success_and_partial_receipts_finish_all_modes(self):
        for mode in ("market", "retry", "close", "review", "preopen"):
            for state in ("SUCCEEDED", "PARTIAL"):
                with self.subTest(mode=mode, state=state):
                    self.finish(dict(state=state, mode=mode, run_id="maintenance-test",
                        feedback="已保存成功项及缺口", qq={"state": "SENT", "delivery_receipt": "test-qq"}), mode=mode)
            self.finish(dict(state="SKIPPED", mode=mode, reason="NON_TRADING_DAY"), mode=mode, name=self.prepare)

    def test_incomplete_wrong_mode_failed_or_research_receipts_do_not_finish(self):
        for body in (
            dict(state="QUEUED", mode="close", run_id="test"),
            dict(state="RUNNING", mode="close", run_id="test"),
            dict(state="SUCCEEDED", mode="review", run_id="test"),
            dict(state="PARTIAL", mode="close"),
            dict(state="FAILED", mode="close", run_id="test"),
            dict(state="CANCELLED", mode="close", run_id="test"),
            dict(state="PUBLISHED", slot="13:05", report_id="research"),
        ):
            with self.subTest(body=body), self.assertRaisesRegex(RuntimeError, "NOT_COMMITTED"):
                self.finish(body)

    def test_old_turn_model_prose_and_error_envelopes_cannot_supply_receipt(self):
        body = dict(state="SUCCEEDED", mode="close", run_id="old")
        receipt = json.dumps(body)
        histories = [
            [instruction(), message("ai", receipt)],
            [instruction(), message("tool", receipt, "stock-v4-rounds_stock_hermes_status")],
            [instruction(), message("tool", receipt, self.status), instruction(), message("ai", "完成")],
            [instruction(), message("tool", json.dumps({"isError": True, "content": [{"text": receipt}]}), self.status)],
        ]
        for history in histories:
            with self.subTest(history=history), self.assertRaisesRegex(RuntimeError, "NOT_COMMITTED"):
                self.guard.after_agent({"messages": history}, None)

    def test_real_async_graph_filters_research_and_keeps_partial_receipt(self):
        from langchain.agents import create_agent
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.tools import StructuredTool

        class Model(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                if {tool.name for tool in tools} - MAINTENANCE_TOOLS:
                    raise AssertionError("maintenance model saw unrelated tools")
                return self

        body = dict(state="PARTIAL", mode="review", run_id="maintenance-test", qq={"state": "SENT"})
        tool = StructuredTool.from_function(lambda: json.dumps(body), name=self.status, description="Saved maintenance receipt")
        research = StructuredTool.from_function(lambda: self.fail("research tool executed"),
            name="stock-v4-rounds_stock_hermes_status", description="Research only")
        replies = [AIMessage(content="", tool_calls=[dict(name=self.status, args={}, id="maintenance", type="tool_call")]),
                   AIMessage(content="资料维护部分完成，缺口已记录")]
        graph = create_agent(model=Model(responses=replies), tools=[tool, research], middleware=[self.guard])
        result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content=instruction("review")["content"])]}))
        self.assertEqual(result["messages"][-1].content, "资料维护部分完成，缺口已记录")


if __name__ == "__main__":
    unittest.main()
