"""Ephemeral QQ-only presentation context for the existing my-stock assistant."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from pathlib import Path

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage


DAILY_TEMPLATE = Path("/data/.octop/qq-templates/daily.txt")
_QQ_DAILY = ContextVar("stock_qq_daily", default=False)
_FORMAT_RULES = """本轮答复经 QQ 渠道发送。以下模板只约束最终答复的内容分组与排版。
保留现有任务目标、资料来源和研究流程；不得改研究结论，不补事实，不把模板示例当作事实。
模板中的占位符用本轮已知信息替换；不适用的区块省略，缺失但必要的信息简要注明待核实。
保持简明、短段落；不得为满足格式捏造数据或完成情况，不增加工具调用或改变工具选择。
仅最终 QQ 回复使用以下模板，不改变工具参数、研究稿、文件或后台记录格式：
"""


@contextmanager
def qq_daily_scope(agent_id, source):
    token = _QQ_DAILY.set(agent_id == "my-stock" and isinstance(source, str)
                         and (source == "qq" or source.startswith("qq/")))
    try:
        yield
    finally:
        _QQ_DAILY.reset(token)


class StockQQDailyMiddleware(AgentMiddleware):
    def _format_request(self, request):
        if not _QQ_DAILY.get():
            return request
        try:
            with DAILY_TEMPLATE.open("r", encoding="utf-8-sig") as stream:
                template = stream.read(8193).strip()
            if not template or len(template) > 8192:
                raise ValueError("Invalid daily template")
            suffix = _FORMAT_RULES + "\n<qq_daily_template>\n" + template + "\n</qq_daily_template>"
            original = request.system_message
            if original is None:
                system = SystemMessage(content=suffix)
            else:
                content = original.content
                updated = (content + "\n\n" + suffix if isinstance(content, str)
                           else [*content, {"type": "text", "text": suffix}])
                system = original.model_copy(update={"content": updated})
            return request.override(system_message=system)
        except (OSError, UnicodeError, ValueError) as exc:
            # Template maintenance must not block a user's ordinary research turn.
            logging.getLogger(__name__).warning("Stock QQ daily template unavailable: %s", type(exc).__name__)
            return request

    def wrap_model_call(self, request, handler):
        return handler(self._format_request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._format_request(request))
