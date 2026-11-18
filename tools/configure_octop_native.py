"""Create/update the scoped native OCTOP expert and five disabled cron jobs.

Uses only the authenticated OCTOP HTTP API. Default mode is a redacted plan;
--apply writes configuration. --enable-cron is a separate final cutover step.
Never runs a job, sends a message, prints credentials, or modifies my-stock.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.strategy_hermes.stage2.octop_bridge import PROMPT, RESEARCH_PROMPT

SERVER = "stock-v4-rounds"
MCP_URL = "http://stock-v4-backend.internal:8001/mcp"
NAME = "板块轮动与ETF穿透"
MARKER = "stock-v4-native-rounds-v1"
WORKSPACE = "/data/.octop/stock-native-workspace"
SLOTS = ("09:35", "10:35", "13:05", "14:05", "14:30")
MAX_INPUT_LENGTH = 128000
MAX_OUTPUT_TOKENS = 28000

WORKFLOW = """你是受控板块轮动研判专家。只执行本轮一次研判，用三个 stock_hermes 工具完成流程。
定时指令指定 slot。先调用 prepare(slot)，服务端决定真实交易日、可用快照、轮次和run_id。
若 state 为 QUEUED、PREPARING、RUNNING 等处理中状态，调用 status(run_id)取得进展，最多三次等待。
READY 时读取返回的全部板块证据及研究范围，逐板块研判，然后调用 submit(run_id, decisions)。decisions 参数是一个JSON数组字符串，包含每个输入板块且每个恰好一次。
submit 返回仍在处理中时用 status 等待。PUBLISHED 表示报告已保存，可在最终回复引用报告编号和QQ实际投递状态；SKIPPED 直接简述原因；FAILED/接口报错则如实报告失败，不声称完成。
这三种工具之外不调用任何工具，不委派子代理，不自行采集或发送消息，不读写文件，不填写或推测现价/权重/成交/来源时间。
本轮最多8次模型调用，单次输出上限28000 token（包含模型推理预算），单次模型时限120秒。不要重复推演或输出思考过程。先保证每板块基础字段完整，再给有界研究解释。
以下技能规范与结构化判断格式适用于submit的decisions；其中历史“只返回JSON/排程/QQ桥接”的文字仅定义判断格式与系统职责，本轮以本段的工具提交顺序为准。
最终回复直接带出PUBLISHED工具回执中的feedback研判简报，并附QQ实际投递状态。不得将模型自己的判断当成已落库回执。
"""


def system_prompt():
    skill = (ROOT / "skills/a-share-hotspot-scout/SKILL.md").read_text(encoding="utf-8")
    return WORKFLOW + "\n\n" + skill + "\n\n" + PROMPT + "\n\n" + RESEARCH_PROMPT + "\n\n" + WORKFLOW


def cron_body(slot, enabled):
    hour, minute = slot.split(":")
    prompt = (f"执行北京时间 {slot} 板块轮动与ETF穿透本轮任务。调用 stock_hermes_prepare(slot='{slot}')，"
              "根据服务端state按系统流程等待、研判并提交。服务端负责交易日核验与幂等；SKIPPED直接报告原因。"
              "只使用stock-v4-rounds的三个工具，以PUBLISHED或SKIPPED真实回执为完成条件，失败如实反馈。"
              "decisions按JSON数组字符串提交，不在最终文字中替代提交。最终直接带出PUBLISHED.feedback研判简报，附QQ实际投递状态。")
    return dict(name=f"板块轮动·ETF穿透 {slot}", trigger=f"cron:{int(minute)} {int(hour)} * * mon-fri",
                prompt=prompt, fresh_thread=True, enabled=enabled, model=None, task_type="agent",
                mcp_servers=[SERVER])


def agent_body(model_ref, prompt):
    config = dict(stock_native_rounds=True, workspace_dir=WORKSPACE,
                  backend=dict(type="filesystem", root_dir=WORKSPACE, virtual_mode=True),
                  conversation_mode="agent", skills_disabled=[], acp={"tool_enabled": False},
                  memory={"memory_enabled": False, "extract_on_session_end": False})
    return dict(name=NAME, description=MARKER, system_prompt=prompt, default_model=model_ref,
                config=config, skill_package_ids=[], knowledge_base_ids=[], mcp_servers=[SERVER],
                # OCTOP maps max_iters to LangGraph node steps, including all
                # middleware hooks. Model calls have a separate hard cap of 8.
                max_iters=256, max_input_length=MAX_INPUT_LENGTH, max_tokens=MAX_OUTPUT_TOKENS, is_shared=False)


class Client:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method, path, data=None):
        raw = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        request = Request(self.base + path, data=raw, method=method,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=90) as response:
                payload = response.read(4 * 1024 * 1024)
                return json.loads(payload) if payload else None
        except HTTPError as exc:
            # Error bodies may echo headers/configuration. Keep them private.
            raise RuntimeError(f"OCTOP_API_{method}_{path}_{exc.code}") from None


def secret_file(path):
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("Credential file is empty or invalid")
    return value


def configure(args):
    prompt = system_prompt()
    plan = dict(expert=NAME, marker=MARKER, mcp_server=SERVER, mcp_url=MCP_URL,
                model=args.model_ref, workspace=WORKSPACE, cron_enabled=args.enable_cron,
                slots=list(SLOTS), prompt_sha256=sha256(prompt.encode()).hexdigest(),
                graph_recursion_limit=256, model_calls_per_round=8, model_timeout_seconds=120,
                max_input_length=MAX_INPUT_LENGTH, max_tokens=MAX_OUTPUT_TOKENS)
    if args.bundle_file:
        bundle = dict(schema=MARKER, server=SERVER, mcp_url=MCP_URL,
                      agent=agent_body(args.model_ref, prompt), jobs=[cron_body(slot, False) for slot in SLOTS])
        args.bundle_file.parent.mkdir(parents=True, exist_ok=True)
        args.bundle_file.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.apply:
        return {"mode": "plan", **plan}
    api_token = secret_file(args.api_token_file) if args.api_token_file else os.environ.get("OCTOP_API_TOKEN", "")
    if not api_token:
        raise ValueError("OCTOP_API_TOKEN or --api-token-file is required")
    token = secret_file(args.mcp_token_file)
    client = Client(args.api_base, api_token)
    if client.call("GET", "/api/cron/settings").get("timezone") != "Asia/Shanghai":
        raise ValueError("OCTOP cron timezone must be Asia/Shanghai before creating the schedule")
    existing = client.call("GET", "/api/connectors/custom-mcp")["servers"]
    desired = dict(transport="streamable_http", url=MCP_URL,
                   headers={"Authorization": "Bearer " + token}, enabled=True, default_open=False, shared=False,
                   display_name="板块轮动受控接口")
    if existing.get(SERVER) != desired:
        client.call("PUT", "/api/connectors/custom-mcp", {"servers": {**existing, SERVER: desired}})
    probe = client.call("POST", "/api/connectors/custom-mcp/test", {"name": SERVER})
    if probe.get("ok") is not True and probe.get("status") not in ("ok", "connected", "success"):
        raise RuntimeError("OCTOP_MCP_PROBE_NOT_READY")
    agents = client.call("GET", "/api/agents")
    if isinstance(agents, dict):
        agents = agents.get("agents", agents.get("items", []))
    matches = [row for row in agents if row.get("description") == MARKER]
    if len(matches) > 1:
        raise ValueError("Multiple managed native experts found")
    body = agent_body(args.model_ref, prompt)
    if matches:
        agent_id = matches[0]["agent_id"]
        current = client.call("GET", f"/api/agents/{agent_id}")
        if current.get("config", {}).get("stock_native_rounds") is not True:
            raise ValueError("Managed expert marker found without native scope")
        client.call("PATCH", f"/api/agents/{agent_id}", body)
    else:
        agent_id = client.call("POST", "/api/agents", body)["agent_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
        raise ValueError("Invalid created expert id")
    jobs = client.call("GET", f"/api/agents/{agent_id}/cron")
    records = []
    for slot in SLOTS:
        body = cron_body(slot, args.enable_cron)
        found = [job for job in jobs if job.get("name") == body["name"]]
        if len(found) > 1:
            raise ValueError("Duplicate managed cron slot")
        if found:
            cron_id = found[0]["id"]
            row = client.call("PATCH", f"/api/agents/{agent_id}/cron/{cron_id}", body)
        else:
            row = client.call("POST", f"/api/agents/{agent_id}/cron", body)
        records.append({key: row.get(key) for key in ("id", "name", "trigger", "enabled", "task_type")})
    client.call("POST", f"/api/agents/{agent_id}/start")
    result = {"mode": "applied", **plan, "agent_id": agent_id, "jobs": records}
    if args.result_file:
        args.result_file.parent.mkdir(parents=True, exist_ok=True)
        args.result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:37437")
    parser.add_argument("--api-token-file", type=Path)
    parser.add_argument("--mcp-token-file", type=Path,
                        default=Path("/opt/1panel/apps/local/stock-system/data/octop-native.token"))
    parser.add_argument("--model-ref", required=True, help="Verified my-stock resolved provider/model reference")
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--bundle-file", type=Path, help="Write credential-free bundle for the official local CLI runtime")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--enable-cron", action="store_true", help="Enable only after disabling the old five-round clock")
    args = parser.parse_args()
    try:
        print(json.dumps(configure(args), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"native configuration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
