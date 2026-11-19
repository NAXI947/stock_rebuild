"""Real Harness middleware graph: node budget and model budget are distinct."""
import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

from deploy.octop_native_guard import MAINTENANCE_TOOLS, TOOLS, StockNativeRoundsMiddleware, native_filesystem_middleware


class NativeRecursionTests(unittest.TestCase):
    def invoke(self, calls, limit, terminal=True, maintenance=False):
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.tools import StructuredTool
        from octop_harness.agent import HarnessAgent
        from octop_harness.config import HarnessAgentConfig, ProviderConfig, ModelConfig
        from octop_harness.llm.factory import ChatModelFactory

        allowed = MAINTENANCE_TOOLS if maintenance else TOOLS

        class Model(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                if {tool.name for tool in tools} - allowed:
                    raise AssertionError("out-of-scope tool")
                return self

        name = "stock-v4-rounds_stock_etf_maintenance_status" if maintenance else "stock-v4-rounds_stock_hermes_status"
        receipt = (dict(state="PARTIAL", mode="close", run_id="frozen-maintenance", qq={"state": "SENT"})
                   if maintenance else dict(state="PUBLISHED", slot="13:05", report_id="frozen-report"))
        content = json.dumps(receipt)
        tool = StructuredTool.from_function(lambda: content, name=name, description="Frozen receipt")
        replies = [AIMessage(content="", tool_calls=[dict(name=name, args={}, id=f"call{i}", type="tool_call")])
                   for i in range(calls - int(terminal))]
        if terminal:
            replies.append(AIMessage(content="完成"))
        model = Model(responses=replies)
        provider = ProviderConfig(id="test", base_url="http://not-used.invalid", api_key="test-only",
                                  models=[ModelConfig(id="fake")])
        factory = ChatModelFactory([provider])
        with tempfile.TemporaryDirectory(prefix="stock-native-recursion-") as root:
            config = HarnessAgentConfig(providers=[provider], default_model="test/fake", workspace_dir=root,
                backend=dict(type="filesystem", root_dir=root, virtual_mode=True),
                bootstrap_enabled=False, memory_enabled=False, memory=(), model_retry_enabled=False, checkpointer=False,
                subagents_auto_load=False, team_enabled=False, tool_search_mode="eager",
                tools_disabled=frozenset({"task"}), tools=[tool],
                middleware=[native_filesystem_middleware(), StockNativeRoundsMiddleware()])
            with patch.object(factory, "get_chat_model", return_value=model):
                harness = HarnessAgent(config, model_factory=factory)
                try:
                    prompt = "执行ETF资料维护 close。" if maintenance else "执行北京时间 13:05 板块轮动与ETF穿透本轮任务。"
                    return asyncio.run(harness.graph.ainvoke(
                        {"messages": [HumanMessage(content=prompt)]},
                        config={"recursion_limit": limit}))
                finally:
                    harness.close()
                    factory.close()

    def test_original_24_node_limit_is_too_small(self):
        from langgraph.errors import GraphRecursionError
        with self.assertRaises(GraphRecursionError):
            self.invoke(8, 24)

    def test_768_nodes_complete_twenty_model_calls(self):
        result = self.invoke(20, 768)
        self.assertEqual(sum(m.type == "ai" for m in result["messages"]), 20)
        self.assertEqual(result["messages"][-1].content, "完成")

    def test_21st_research_model_call_is_blocked(self):
        with self.assertRaisesRegex(RuntimeError, "MODEL_CALL_BUDGET"):
            self.invoke(21, 768, terminal=False)

    def test_768_nodes_complete_24_maintenance_model_calls(self):
        result = self.invoke(24, 768, maintenance=True)
        self.assertEqual(sum(m.type == "ai" for m in result["messages"]), 24)
        self.assertEqual(result["messages"][-1].content, "完成")

    def test_25th_maintenance_model_call_is_blocked(self):
        with self.assertRaisesRegex(RuntimeError, "MODEL_CALL_BUDGET"):
            self.invoke(25, 768, terminal=False, maintenance=True)


if __name__ == "__main__":
    unittest.main()
