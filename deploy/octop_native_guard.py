"""OCTOP middleware scoped to the dedicated stock native-rounds expert.

Mounted as octop.infra.agents.middleware.stock_native_rounds. The deployment
patch attaches it only when config.stock_native_rounds is explicitly true.
"""
from __future__ import annotations

import asyncio
import json
import re
from langchain.agents.middleware import AgentMiddleware

SERVER = "stock-v4-rounds"
TOOLS = frozenset(f"{SERVER}_stock_hermes_{operation}" for operation in ("prepare", "submit", "status"))
MAINTENANCE_TOOLS = frozenset(
    [f"{SERVER}_stock_etf_maintenance_{op}" for op in ('prepare','status')]
    + [f"{SERVER}_stock_etf_verification_{op}" for op in ('context','submit')])
MAX_MODEL_CALLS = 20
MODEL_TIMEOUT_SECONDS = 120
SLOT_PATTERN = re.compile(r"执行北京时间\s+(09:35|10:35|13:05|14:05|14:30)\s+板块轮动")
MAINTENANCE_PATTERN = re.compile(r"执行ETF资料维护\s+(market|retry|verify|materials|close|review|preopen)\b")


def native_filesystem_middleware():
    """Keep complete MCP evidence in context; expose no filesystem tools."""
    from deepagents.backends.filesystem import FilesystemBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware
    return FilesystemMiddleware(
        backend=FilesystemBackend(root_dir="/data/.octop/stock-native-workspace", virtual_mode=True),
        # Deepagents requires read_file in this middleware; the native guard
        # removes it from model tools and rejects direct invocation.
        tools=["read_file"], tool_token_limit_before_evict=None, human_message_token_limit_before_evict=None)


def name_of(value):
    if isinstance(value, dict):
        return str((value.get("function") or {}).get("name") or value.get("name") or "")
    return str(getattr(value, "name", "") or "")


def kind_of(message):
    if isinstance(message, dict):
        return message.get("type") or message.get("role")
    return getattr(message, "type", None)


def current_messages(state):
    messages = list(state.get("messages") or [])
    start = next((i + 1 for i in range(len(messages) - 1, -1, -1)
                  if kind_of(messages[i]) in ("human", "user")), len(messages))
    return messages[start:]


def expected_slot(state):
    for message in reversed(list(state.get("messages") or [])):
        if kind_of(message) in ("human", "user"):
            content = message.get("content") if isinstance(message, dict) else message.content
            match = SLOT_PATTERN.search(content) if isinstance(content, str) else None
            if match:
                return match.group(1)
            maintenance = MAINTENANCE_PATTERN.search(content) if isinstance(content,str) else None
            if maintenance:
                return 'maintenance:' + maintenance.group(1)
            break
    raise RuntimeError("STOCK_NATIVE_SLOT_INSTRUCTION_MISSING")


def receipts(content, depth=0):
    """Unwrap MCP's text/content envelope; never parse prose as a receipt."""
    if depth > 8:
        return []
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (ValueError, TypeError):
            return []
    if isinstance(content, list):
        return [item for part in content for item in receipts(part, depth + 1)]
    if not isinstance(content, dict):
        return []
    is_error = content.get("isError") is True
    if content.get("state") in ("PUBLISHED", "SKIPPED", "FAILED", "SUCCEEDED", "PARTIAL", "CANCELLED"):
        return [content] if not is_error or content["state"] == "FAILED" else []
    for key in ("content", "text", "result", "structuredContent"):
        if key in content:
            found = receipts(content[key], depth + 1)
            if found:
                return [item for item in found if item["state"] == "FAILED"] if is_error else found
    return []


def verification_receipts(state,operation):
    def unpack(content,depth=0):
        if depth>8:return []
        if isinstance(content,str):
            try:return unpack(json.loads(content),depth+1)
            except (ValueError,TypeError):return []
        if isinstance(content,list):
            return [row for item in content for row in unpack(item,depth+1)]
        if not isinstance(content,dict) or content.get('isError') is True:return []
        if isinstance(content.get('state'),str):return [content]
        return [row for key in ('content','text','result','structuredContent') if key in content
                for row in unpack(content[key],depth+1)]
    result=[]
    for message in current_messages(state):
        if kind_of(message)=='tool' and name_of(message)==f'{SERVER}_stock_etf_verification_{operation}':
            result.extend(unpack(message.get('content') if isinstance(message,dict) else message.content))
    return result


def submitted_verification_ids(state):
    """Only a validated submit tool receipt authorizes follow-up static checks."""
    return {row['request_id'] for row in verification_receipts(state,'submit')
            if row.get('state')=='PENDING' and isinstance(row.get('request_id'),str)}


def verification_scope(value):
    """OCTOP transports arrays as strings; retain the same bounded ID set."""
    if isinstance(value,str):
        if len(value)>2048:return None
        text=value.strip()
        if text.startswith('['):
            try:value=json.loads(text)
            except ValueError:return None
        else:value=[text]
    if (not isinstance(value,list) or not 1<=len(value)<=8
            or any(not isinstance(item,str) or re.fullmatch(r'pv-[0-9a-f]{40}',item) is None for item in value)
            or len(set(value))!=len(value)):
        return None
    return value


def check_verification_progress(state,observed):
    calls=sum(kind_of(message) in ('ai','assistant') for message in current_messages(state))
    awaiting={row.get('target_id') for receipt in observed
              for row in (receipt.get('verification') or {}).get('items',[])
              if row.get('state')=='AWAITING_REVIEW' and row.get('target_id')}
    context={row.get('target_id') for row in verification_receipts(state,'context')}
    reviewed={row.get('target_id') for row in verification_receipts(state,'submit')
              if row.get('state') in ('PENDING','PUBLISHED','REVIEW_REQUIRED')}
    if awaiting and calls<22 and not awaiting.intersection(context,reviewed):
        raise RuntimeError('STOCK_NATIVE_VERIFICATION_REVIEW_REQUIRED')
    pending=submitted_verification_ids(state)
    checked={rid for row in observed if row.get('mode')=='verify'
             and row.get('state') in ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','SKIPPED')
             for rid in row.get('verification_request_ids',[])}
    if pending-checked and calls<23:
        raise RuntimeError('STOCK_NATIVE_VERIFICATION_NOT_CHECKED')


class StockNativeRoundsMiddleware(AgentMiddleware):
    """Hard tool boundary, verified completion and per-round model-call limits."""

    def _filter(self, request):
        slot = expected_slot(request.state)
        maintenance = slot.startswith('maintenance:')
        if sum(kind_of(m) in ("ai", "assistant") for m in current_messages(request.state)) >= (24 if maintenance else MAX_MODEL_CALLS):
            raise RuntimeError("STOCK_NATIVE_MODEL_CALL_BUDGET_EXHAUSTED")
        allowed = MAINTENANCE_TOOLS if maintenance else TOOLS
        return request.override(tools=[tool for tool in request.tools or [] if name_of(tool) in allowed])

    def wrap_model_call(self, request, handler):
        return handler(self._filter(request))

    async def awrap_model_call(self, request, handler):
        return await asyncio.wait_for(handler(self._filter(request)), timeout=MODEL_TIMEOUT_SECONDS)

    @staticmethod
    def _check_tool(request):
        slot = expected_slot(request.state)
        maintenance = slot.startswith('maintenance:')
        allowed = MAINTENANCE_TOOLS if maintenance else TOOLS
        if request.tool_call.get("name") not in allowed:
            raise RuntimeError("STOCK_NATIVE_TOOL_NOT_ALLOWED")
        if (request.tool_call["name"].endswith("_prepare")
                and request.tool_call.get("args", {}).get('mode' if maintenance else 'slot') != (slot.split(':',1)[1] if maintenance else slot)):
            args=request.tool_call.get('args',{})
            ids=verification_scope(args.get('request_ids'))
            # Malformed/missing scope reaches MCP's recoverable tool error;
            # the endpoint requires explicit valid IDs before invoking service.
            if not (maintenance and args.get('mode')=='verify'
                    and (ids is None or set(ids)<=submitted_verification_ids(request.state))):
                raise RuntimeError("STOCK_NATIVE_SLOT_MISMATCH")

    def wrap_tool_call(self, request, handler):
        self._check_tool(request)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        self._check_tool(request)
        return await handler(request)

    def after_agent(self, state, runtime):
        slot = expected_slot(state)
        maintenance = slot.startswith('maintenance:')
        allowed = MAINTENANCE_TOOLS if maintenance else TOOLS
        observed = []
        for message in current_messages(state):
            if kind_of(message) != "tool" or name_of(message) not in allowed:
                continue
            content = message.get("content") if isinstance(message, dict) else message.content
            observed.extend(receipts(content))
        if not observed:
            raise RuntimeError("STOCK_NATIVE_ROUND_NOT_COMMITTED")
        last = observed[-1]
        if maintenance:
            check_verification_progress(state,observed)
            expected=slot.split(':',1)[1]
            followup=(last.get('mode')=='verify' and any(row.get('mode')==expected
                      and row.get('state') in ('SUCCEEDED','PARTIAL') for row in observed)
                      and bool(submitted_verification_ids(state)))
            if (last.get('mode') != expected and not followup) or last.get('state') not in ('SUCCEEDED','PARTIAL','SKIPPED'):
                raise RuntimeError('STOCK_NATIVE_MAINTENANCE_NOT_COMMITTED')
            if last['state'] != 'SKIPPED' and not last.get('run_id'):
                raise RuntimeError('STOCK_NATIVE_MAINTENANCE_NOT_COMMITTED')
            return None
        if last.get("slot") == slot and last.get("state") == "FAILED" and last.get("stage") in ("PREPARE", "ANALYZE", "PUBLISH"):
            raise RuntimeError(f"STOCK_NATIVE_{last['stage']}_FAILED")
        valid = (last.get("slot") == slot and (
            (last.get("state") == "PUBLISHED" and isinstance(last.get("report_id"), str) and last["report_id"])
            or (last.get("state") == "SKIPPED" and last.get("reason") in ("NON_TRADING_DAY", "OUTSIDE_ROUND_WINDOW"))))
        if not valid:
            raise RuntimeError("STOCK_NATIVE_ROUND_NOT_COMMITTED")
        return None

    async def aafter_agent(self, state, runtime):
        return self.after_agent(state, runtime)
