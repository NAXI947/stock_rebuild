"""Small authenticated, stateless MCP endpoint for the OCTOP research agent.

Runs on an internal listener in the API process. The single request thread and
bounded waits leave the public workspace HTTP loop and worker independent.
"""
from contextlib import contextmanager
from http import HTTPStatus
import json
import logging
import re
from threading import Event, Thread
from time import monotonic
from wsgiref.simple_server import make_server

import psycopg

from .adapter import unique_object
from .ledger import encode
from .manual_api import LocalServer, QuietHandler, TOKEN, authorized

MAX_BODY = 256 * 1024
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TERMINAL = {"READY", "PUBLISHED", "FAILED", "SKIPPED", "PAUSED", "NOT_DUE", "NOT_TRADING_DAY"}
# Errors that can pass on their own: a dropped or refused connection, a lock or statement timeout.
TRANSIENT = (OSError, psycopg.OperationalError)


def scope_list(value,field):
    """Normalize OCTOP's string transport before the unchanged service boundary."""
    pattern=r'pv-[0-9a-f]{40}' if field=='request_ids' else r'[0-9]{6}\.(?:SH|SZ)'
    if isinstance(value,str):
        if len(value)>2048:raise ValueError('INVALID_'+field.upper()+'_FORMAT')
        text=value.strip()
        if text.startswith('['):
            try:value=json.loads(text)
            except ValueError:raise ValueError('INVALID_'+field.upper()+'_FORMAT') from None
        else:value=[text]
    if (not isinstance(value,list) or not 1<=len(value)<=8
            or any(not isinstance(item,str) or re.fullmatch(pattern,item) is None for item in value)
            or len(set(value))!=len(value)):
        raise ValueError('INVALID_'+field.upper()+'_FORMAT')
    return value


def tool_error(name,error):
    """Tool validation is recoverable; never expose arbitrary exception text."""
    message=str(error)
    translated={'verification request not found':'VERIFICATION_REQUEST_NOT_FOUND',
        'invalid verification request IDs':'INVALID_REQUEST_IDS_FORMAT','unknown maintenance mode':'UNKNOWN_MAINTENANCE_MODE',
        'target scope requires materials mode':'TARGET_CODES_REQUIRE_MATERIALS',
        'verification scope requires verify mode':'REQUEST_IDS_REQUIRE_VERIFY'}
    code=translated.get(message,message)
    if re.fullmatch(r'[A-Z][A-Z0-9_]*(?::[A-Za-z0-9_.:-]{1,100})?',code) is None:
        code='TOOL_ARGUMENT_OR_BUSINESS_RULE_REJECTED'
    expected={
        'stock_etf_maintenance_prepare':'mode为market/retry/materials/verify；verify必须显式提供request_ids，target_codes仅materials，均用1至8项唯一值的JSON数组字符串。例如request_ids=\'["pv-0123456789abcdef0123456789abcdef01234567"]\'或target_codes=\'["512480.SH"]\'。ID须复制已有申请回执，ETF须有已保存真实关联。',
        'stock_etf_verification_submit':'target_id复制context回执；review为符合review_contract的JSON对象字符串，只引用已存官方文档定位和原文。',
        'stock_etf_verification_context':'target_id复制维护队列中已有的目标ID。',
        'stock_etf_maintenance_status':'run_id复制prepare返回的原任务ID。',
        'stock_hermes_prepare':'slot为09:35/10:35/13:05/14:05/14:30之一，须与本次指令一致。',
        'stock_hermes_submit':'run_id复制原prepare回执；decisions为完整JSON数组字符串，遵循该回执的instructions。',
        'stock_hermes_status':'run_id复制原prepare返回的轮次ID。'}
    result=dict(state='TOOL_ERROR',tool=name,error=code,expected_arguments=expected[name],
                next_action='在本会话剩余调用预算内按错误及已保存回执修正参数；不要编造ID、扩大范围或重建任务预算。')
    return {'content':[{'type':'text','text':encode(result)}],'isError':True}


def tools_schema(maintenance=False):
    research = [
        {"name": "stock_hermes_prepare", "description": "开始本轮或取得同一轮已冻结的研判证据。仅允许当前交易日的指定轮次，自动去重。READY后依据sectors研判并调用submit；PENDING时调用status。SKIPPED/FAILED时反馈原因。",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["slot"], "properties": {"slot": {"type": "string", "enum": ["09:35", "10:35", "13:05", "14:05", "14:30"]}}}},
        {"name": "stock_hermes_submit", "description": "提交本轮全部板块决策。程序核验引用、计算ETF穿透并保存报告和节点。仅PUBLISHED表示保存完成；重复提交相同内容幂等。提交后必须读取最终状态再反馈。",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["run_id", "decisions"], "properties": {"run_id": {"type": "string", "maxLength": 80}, "decisions": {"type": "string", "maxLength": 262144, "description": "完整JSON数组字符串，例如[{\"sector_id\":\"kaipanla:801027\",\"phase\":\"UNKNOWN\",\"evidence_refs\":[\"从current复制ref\"]}]。每个输入板块恰好一项；可加core_stock_codes/core_leader_codes/research，严格遵循prepare返回的instructions。"}}}},
        {"name": "stock_hermes_status", "description": "查询本轮准备/发布结果；正在处理时等待最多50秒。READY返回冻结证据，PUBLISHED返回已落库报告编号和反馈内容。QQ状态与报告保存状态分别展示。",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["run_id"], "properties": {"run_id": {"type": "string", "maxLength": 80}}}},
    ]
    if maintenance:
        research.extend([
            {"name":"stock_etf_maintenance_prepare", "description":"提交有界ETF资料维护任务。market/retry对应10:00主维护/14:15缺项复核。materials是手动官方静态材料采集，只读已保存关联，不采行情。verify是手动静态官方回核，仅处理已导入申请，可在盘外运行，不采行情。verify必须显式传request_ids，最多8项；重复请求复用原预算。QUEUED/RUNNING后用status等待。verification中AWAITING_REVIEW须context取得原文并审阅；满足逐字段证据才submit，再以返回request_id调用verify；证据不足保留待审阅。",
             "inputSchema":{"type":"object","additionalProperties":False,"required":["mode"],"properties":{"mode":{"type":"string","enum":["market","retry","materials","verify"]},"request_ids":{"type":"string","maxLength":2048,"description":"verify必填，1至8个已导入申请ID的JSON数组字符串，ID逐字复制回执；例如[\"pv-0123456789abcdef0123456789abcdef01234567\"]。单个合法ID字符串也兼容，不传逗号分隔文本或ALL。"},"target_codes":{"type":"string","maxLength":2048,"description":"仅materials使用，1至8个唯一ETF代码的JSON数组字符串，例如[\"512480.SH\",\"588100.SH\"]。单个合法代码字符串也兼容；服务端检查已保存的真实关联。"}}}},
            {"name":"stock_etf_maintenance_status", "description":"读取ETF资料维护任务进度、最终回执与QQ状态。处理时最多等待50秒；只转述已保存的结果。",
             "inputSchema":{"type":"object","additionalProperties":False,"required":["run_id"],"properties":{"run_id":{"type":"string","maxLength":80}}}},
            {"name":"stock_etf_verification_context", "description":"读取已登记目标的官方材料与逐字段审阅契约。只引用返回的文档标签、内容摘要和页码定位；身份、范围、指数、交易单位、实际涨跌幅和T+1须逐项有证据。未提供或有冲突的事实保持待核，不根据名称或目录自动批准。",
             "inputSchema":{"type":"object","additionalProperties":False,"required":["target_id"],"properties":{"target_id":{"type":"string","minLength":1,"maxLength":80}}}},
            {"name":"stock_etf_verification_submit", "description":"提交依据context已存官方原文完成的逐字段审阅JSON字符串。遵循context返回的review_contract并保留证据定位；只生成待回核申请，不授予交易资格。返回request_id后调用maintenance_prepare(mode=verify,request_ids=申请ID的JSON数组字符串)，再读取status确认实际发布或错误。",
             "inputSchema":{"type":"object","additionalProperties":False,"required":["target_id","review"],"properties":{"target_id":{"type":"string","minLength":1,"maxLength":80},"review":{"type":"string","maxLength":65536}}}},
        ])
    return research


def application(queue, token, *, stop=None, wait_seconds=50, maintenance=None):
    if not TOKEN.fullmatch(token) or not 0 <= wait_seconds <= 50:
        raise ValueError("invalid native MCP configuration")
    expected = ("Bearer " + token).encode("ascii")
    stop = stop or Event()
    known_tools={tool["name"] for tool in tools_schema(maintenance is not None)}

    def wait(call, service=None, terminal=None):
        """Run one queue call and follow its run until a terminal state or the wait limit.

        A lost connection or a lock or statement timeout is retried inside the same limit; every call
        routed here is a read or repeats safely with the same arguments.
        """
        service = service or queue
        terminal = terminal or TERMINAL
        deadline = monotonic() + wait_seconds
        delivery_deadline = None
        while True:
            try:
                result = call()
                break
            except TRANSIENT as exc:
                if monotonic() >= deadline or stop.wait(.5):
                    raise
                logging.getLogger(__name__).warning('native MCP call retried after %s', type(exc).__name__)
        while result.get("run_id") and monotonic() < deadline:
            if result.get("state") in terminal:
                if result.get("state") not in ("PUBLISHED","SUCCEEDED","PARTIAL") or result.get("qq", {}).get("state") not in ("PENDING", "SENDING"):
                    break
                if delivery_deadline is None:
                    delivery_deadline = min(deadline, monotonic() + 20)
                if monotonic() >= delivery_deadline:
                    break
            if stop.wait(.5):
                break
            try:
                result = service.status(result["run_id"])
            except TRANSIENT as exc:
                # Keep the last saved state; the next poll, or the agent's own status call, reads it again.
                logging.getLogger(__name__).warning('native MCP status poll failed: %s', type(exc).__name__)
        return result

    def invoke_tool(name,args):
        if name == "stock_hermes_prepare" and set(args) == {"slot"}:
            result = wait(lambda: queue.prepare(args["slot"]))
        elif name == "stock_hermes_submit" and set(args) == {"run_id", "decisions"}:
            if not isinstance(args["decisions"], str):
                raise ValueError("DECISIONS_JSON_STRING_REQUIRED")
            decisions = json.loads(args["decisions"], object_pairs_hook=unique_object)
            result = wait(lambda: queue.submit(args["run_id"], decisions))
        elif name == "stock_hermes_status" and set(args) == {"run_id"}:
            result = wait(lambda: queue.status(args["run_id"]))
        elif maintenance is not None and name == "stock_etf_maintenance_prepare" and set(args) in ({"mode"},{"mode","request_ids"},{"mode","target_codes"}):
            from .etf_maintenance import TERMINAL as MAINTENANCE_TERMINAL
            if args['mode']=='verify' and 'request_ids' not in args:
                raise ValueError('REQUEST_IDS_REQUIRED_FOR_VERIFY')
            options={key:scope_list(args[key],key) for key in ('request_ids','target_codes') if key in args}
            result = wait(lambda: maintenance.prepare(args['mode'],**options),maintenance,MAINTENANCE_TERMINAL)
        elif maintenance is not None and name == "stock_etf_maintenance_status" and set(args) == {"run_id"}:
            from .etf_maintenance import TERMINAL as MAINTENANCE_TERMINAL
            result = wait(lambda: maintenance.status(args['run_id']),maintenance,MAINTENANCE_TERMINAL)
        elif maintenance is not None and name == 'stock_etf_verification_context' and set(args)=={'target_id'}:
            result=maintenance.context(args['target_id'])
        elif maintenance is not None and name == 'stock_etf_verification_submit' and set(args)=={'target_id','review'}:
            if not isinstance(args['review'],str) or len(args['review'])>65536:
                raise ValueError('REVIEW_JSON_STRING_REQUIRED')
            review=json.loads(args['review'],object_pairs_hook=unique_object)
            result=maintenance.submit_review(args['target_id'],review)
        else:
            raise ValueError("UNKNOWN_TOOL_OR_ARGUMENTS")
        return result

    def dispatch(method, params):
        if method == "initialize":
            version = params.get("protocolVersion")
            return {"protocolVersion": version if version in VERSIONS else VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "stock-v4-hermes", "version": "1.0"},
                    "instructions": "研判使用stock_hermes工具；资料维护使用stock_etf_maintenance工具。保留源时间，依据已保存回执反馈结果。"}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tools_schema(maintenance is not None)}
        if method != "tools/call":
            raise LookupError("METHOD_NOT_FOUND")
        name, args = params.get("name"), params.get("arguments", {})
        if not isinstance(args, dict):
            raise ValueError("INVALID_ARGUMENTS")
        if name not in known_tools:
            raise ValueError("UNKNOWN_TOOL")
        try:
            result=invoke_tool(name,args)
        except ValueError as error:
            return tool_error(name,error)
        from .qq_task_notices import enqueue_status,enabled as notices_enabled
        if result.get('state') in ('FAILED','SKIPPED','PAUSED','CANCELLED') and notices_enabled(queue.ledger):
            kind='maintenance' if name.startswith('stock_etf_') else 'research'
            try:
                result['status_notification']=enqueue_status(queue.ledger,result,kind,
                    result.get('mode') or result.get('slot') or args.get('mode') or args.get('slot',''))
            except (OSError,ValueError,psycopg.Error) as exc:
                result['status_notification']=dict(state='UNAVAILABLE',error=type(exc).__name__)
                logging.getLogger(__name__).warning('QQ task status could not be queued: %s',type(exc).__name__)
        return {"content": [{"type": "text", "text": encode(result)}], "isError": result.get("state") == "FAILED"}

    def handle(env, start_response):
        status, result, request = 200, None, {}
        try:
            if not authorized(env, expected):
                status, result = 401, {"error": "AUTH_REQUIRED"}
            elif env.get("PATH_INFO") not in ("/mcp","/native-notice"):
                status, result = 404, {"error": "NOT_FOUND"}
            elif env.get("REQUEST_METHOD") != "POST":
                status, result = 405, {"error": "POST_REQUIRED"}
            elif env.get("HTTP_ORIGIN"):
                status, result = 403, {"error": "BROWSER_ORIGIN_NOT_ALLOWED"}
            else:
                length = env.get("CONTENT_LENGTH", "")
                if not length.isdecimal() or not 0 < int(length) <= MAX_BODY or env.get("HTTP_TRANSFER_ENCODING"):
                    raise ValueError("INVALID_BODY_LENGTH")
                if env.get("CONTENT_TYPE", "").split(";", 1)[0] != "application/json":
                    raise ValueError("JSON_REQUIRED")
                raw = env["wsgi.input"].read(int(length))
                if len(raw) != int(length):
                    raise ValueError("INCOMPLETE_BODY")
                request = json.loads(raw, object_pairs_hook=unique_object)
                if env.get('PATH_INFO')=='/native-notice':
                    from .qq_task_notices import receive_native_failure
                    result=receive_native_failure(queue,maintenance,request)
                    response=encode(result).encode('utf-8')
                    start_response('200 OK', [('Content-Type','application/json'),('Content-Length',str(len(response))),('Cache-Control','no-store')])
                    return [response]
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                    request = {}
                    raise ValueError("INVALID_JSON_RPC")
                params = request.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError("INVALID_PARAMS")
                if request["method"].startswith("notifications/") and "id" not in request:
                    status = 202
                else:
                    if "id" not in request or type(request["id"]) not in (str, int):
                        raise ValueError("REQUEST_ID_REQUIRED")
                    result = {"jsonrpc": "2.0", "id": request["id"], "result": dispatch(request["method"], params)}
        except LookupError:
            if env.get('PATH_INFO')=='/native-notice':
                status,result=404,{'error':'NOTICE_RUN_NOT_FOUND'}
            else:
                result = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request,dict) else None, "error": {"code": -32601, "message": "METHOD_OR_RUN_NOT_FOUND"}}
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            if env.get('PATH_INFO')=='/native-notice':
                status,result=400,{'error':'INVALID_NOTICE'}
            else:
                result = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request,dict) else None, "error": {"code": -32602, "message": "INVALID_REQUEST"}}
        except (OSError, psycopg.Error):
            status, result = 503, {"error": "NATIVE_ROUND_SERVICE_UNAVAILABLE"}
        raw = b"" if result is None else encode(result).encode("utf-8")
        headers = [("Content-Type", "application/json"), ("Content-Length", str(len(raw))), ("Cache-Control", "no-store")]
        if status == 401:
            headers.append(("WWW-Authenticate", "Bearer"))
        if status == 405:
            headers.append(("Allow", "POST"))
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [raw]
    return handle


@contextmanager
def listener(queue, token, port=8001, *, maintenance=None):
    if not 1024 <= port <= 65535:
        raise ValueError("invalid native MCP port")
    stop = Event()
    with make_server("0.0.0.0", port, application(queue, token, stop=stop,maintenance=maintenance),
                     server_class=LocalServer, handler_class=QuietHandler) as server:
        server.timeout = .5
        def serve():
            while not stop.is_set():
                server.handle_request()
        thread = Thread(target=serve, name="octop-native-mcp", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=6)
            if thread.is_alive():
                raise RuntimeError("native MCP listener did not stop")
