"""Dedicated native-round cron delivery: failed turns retain usage and feedback."""
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


NOTICE_URL = "http://stock-v4-backend.internal:8001/native-notice"
NOTICE_TOKEN_PATH = Path("/data/.octop/stock-native-notice.token")
NOTICE_BINDINGS = {
    "6T6VMW": frozenset(("cFSMGMB", "cKAZXDS", "c3FDPYP", "cG7V9GS", "c2XGZ0F")),
    "ATAPBR": frozenset(("cMWFP36", "c5PT18W", "cY8HM7S")),
}


class _NoNoticeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_native_notice(payload):
    # Keep the credential scoped to the fixed private backend, even if the
    # container has proxy environment variables or receives an HTTP redirect.
    with NOTICE_TOKEN_PATH.open("r", encoding="ascii") as stream:
        token = stream.read(130).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("Invalid notice credential")
    request = Request(NOTICE_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with build_opener(ProxyHandler({}), _NoNoticeRedirect()).open(request, timeout=5) as response:
        if response.status not in (200, 202):
            raise RuntimeError("Notice response rejected")


async def notify_native_failure(command, session, error_code):
    """One bounded best-effort receipt; backend owns run lookup and deduplication."""
    agent_id, cron_id = str(command.agent_id), str(getattr(command, "cron_id", ""))
    if cron_id not in NOTICE_BINDINGS.get(agent_id, ()):
        return
    # Do not transmit model output, exception messages, token usage, or prompts.
    code = str(error_code)
    if len(code) > 128 or not re.fullmatch(r"(?:STOCK_NATIVE_[A-Z_]+|[A-Za-z][A-Za-z0-9]*(?:Error|Exception))", code):
        code = "RuntimeError"
    payload = dict(agent_id=agent_id, cron_id=cron_id, thread_id=str(session.thread_id),
                   observed_at=datetime.now(timezone.utc).isoformat(), error_code=code)
    try:
        await asyncio.to_thread(_post_native_notice, payload)
    except Exception as exc:
        logging.getLogger(__name__).warning("Stock native notice failed: %s", type(exc).__name__)


async def deliver_native_round(service, command, session):
    from octop.infra.cron.delivery import (
        AIMessage, TurnHistoryTracker, UsageTracker, log_failed_tool_results,
        message_inputs, new_ulid, record_turn_usage, strip_thinking,
    )
    from octop.infra.connectors.gateway.adapters.agently_cli import write_scope

    usage, tracker = UsageTracker(), None
    parts, emitted_errors = [], set()
    saw_tool_call = interaction_required = False
    stream_failure = None
    try:
        request = await service._build_agent_request(command, session)
        tracker = TurnHistoryTracker.from_request(request)
        with write_scope(allowed=False):
            async for chunk in service._agent_manager.stream(command.agent_id, request):
                tracker.observe(chunk)
                usage.observe(chunk)
                kind = chunk.get("type")
                if kind == "tool_call_chunk":
                    saw_tool_call = True
                elif kind == "tool_result":
                    log_failed_tool_results(chunk, agent_id=command.agent_id, thread_id=session.thread_id,
                                            seen=emitted_errors, live=saw_tool_call)
                if kind in ("token", "delta"):
                    parts.append(str(chunk.get("content") or chunk.get("text") or ""))
                elif kind == "hitl_required":
                    interaction_required = True
                elif kind == "error":
                    error_text = str(chunk.get("message") or chunk.get("content") or "")
                    stage = re.search(r"STOCK_NATIVE_(PREPARE|ANALYZE|PUBLISH)_FAILED", error_text)
                    stream_failure = stage.group(0) if stage else "STOCK_NATIVE_STREAM_FAILED"
        if stream_failure:
            raise RuntimeError(stream_failure)
        if interaction_required:
            raise RuntimeError("STOCK_NATIVE_INTERACTION_REQUIRED")
        outbound = strip_thinking("".join(parts)).strip()
        if not outbound:
            raise RuntimeError("STOCK_NATIVE_EMPTY_RESPONSE")
    except Exception as exc:
        tag = str(exc)
        if not re.fullmatch(r"STOCK_NATIVE_[A-Z_]+", tag):
            tag = type(exc).__name__
        feedback = f"【{command.cron_name}｜执行失败】\n原因：{tag}\n已收到的模型用量和执行轨迹保留在本任务记录。"
        try:
            if tracker is not None:
                service._project_best_effort(session.thread_id, tracker.inputs)
            service._remember_thread_title(session, command)
            harness = service._agent_manager.get_agent(command.agent_id)
            appended = await harness.aappend_messages(session.thread_id, [
                AIMessage(content=feedback, id=f"cron:{new_ulid()}:failure")])
            service._project_best_effort(session.thread_id, message_inputs(appended, dedupe_missing_ids=True))
            await service._gateway.push_session_text(session, feedback, title_source=command.prompt)
            await service._notify_best_effort(session, command.agent_id, feedback)
        except Exception:
            logging.getLogger(__name__).exception("Stock native failure feedback could not be projected")
        await notify_native_failure(command, session, tag)
        raise
    finally:
        if tracker is not None:
            service._project_best_effort(session.thread_id, tracker.inputs)
            service._remember_thread_title(session, command)
        if usage.usage is not None:
            record_turn_usage(service._repos.usage_repo, agent_id=command.agent_id, user_id=session.user_id,
                              thread_id=session.thread_id, usage=usage.usage, source="cron")
    await service._gateway.push_session_text(session, outbound, title_source=command.prompt)
    await service._notify_best_effort(session, command.agent_id, outbound)
