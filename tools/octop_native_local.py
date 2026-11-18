"""Provision through OCTOP's official embedded CLI runtime during maintenance.

Run only in a one-shot container with the existing server stopped, the same
data volume and verified native overlays. Supply a recent host docker-inspect
proof; no JWT, browser cookie, or password is required or generated.
"""
import argparse
import asyncio
from hashlib import sha256
import json
import logging
from pathlib import Path
import re
import time
from unittest.mock import patch

SCHEMA = "stock-v4-native-rounds-v1"
MAINTENANCE_SCHEMA = "stock-v4-etf-maintenance-v1"
SERVER = "stock-v4-rounds"
MCP_URL = "http://stock-v4-backend.internal:8001/mcp"
WORKSPACE = "/data/.octop/stock-native-workspace"
IMAGE_ID = "sha256:c15e792f405a5b5aca8f3625adcc11f3421e13f6d01da7217ea18e6f0b98553f"


def validate_inputs(args):
    proof = json.loads(args.stopped_proof.read_text())
    if (proof.get("running") is not False or proof.get("name") != "1Panel-octop-6ivR"
            or proof.get("image_id") != IMAGE_ID or not 0 <= time.time() - proof.get("checked_at", 0) <= 300):
        raise ValueError("Recent stopped-server Docker proof is required")
    manifest = json.loads(args.manifest.read_text())
    if manifest.get("image_id") != IMAGE_ID:
        raise ValueError("Unreviewed image manifest")
    for target_key, digest_key in (("manager_target", "manager_sha256"), ("guard_target", "guard_sha256"),
                                   ("delivery_target", "delivery_sha256"), ("delivery_helper_target", "delivery_helper_sha256")):
        if sha256(Path(manifest[target_key]).read_bytes()).hexdigest() != manifest[digest_key]:
            raise ValueError("OCTOP native overlay does not match manifest")
    bundle = json.loads(args.bundle.read_text())
    marker = bundle.get("schema")
    if (marker not in (SCHEMA, MAINTENANCE_SCHEMA) or bundle.get("server") != SERVER or bundle.get("mcp_url") != MCP_URL
            or bundle["agent"]["description"] != marker
            or bundle["agent"]["config"].get("stock_native_rounds") is not True
            or bundle["agent"]["config"]["backend"] != dict(type="filesystem", root_dir=WORKSPACE, virtual_mode=True)):
        raise ValueError("Invalid native configuration bundle")
    jobs = bundle["jobs"]
    expected = 2 if marker == MAINTENANCE_SCHEMA else 5
    if len(jobs) != expected or len({j["name"] for j in jobs}) != expected:
        raise ValueError("Unexpected managed cron definitions")
    if marker == MAINTENANCE_SCHEMA:
        if {(j['name'],j['trigger']) for j in jobs} != {
                ('ETF资料维护 10:00','cron:0 10 * * mon-fri'),
                ('ETF资料维护 14:15','cron:15 14 * * mon-fri')}:
            raise ValueError("Unexpected maintenance schedule")
    token = args.mcp_token_file.read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("Invalid MCP bearer file")
    return bundle, token


def cron_migration_plan(existing, jobs, *, maintenance):
    """Reuse the two old IDs so authenticated failure notices keep their identity."""
    expected = {job['name'] for job in jobs}
    legacy = {'ETF资料维护 18:00':'ETF资料维护 10:00',
              'ETF资料维护 21:00':'ETF资料维护 14:15',
              'ETF资料维护 08:45':None} if maintenance else {}
    if any(job.name not in expected | set(legacy) for job in existing):
        raise ValueError("Dedicated expert has unexpected cron definitions")
    if len({job.name for job in existing}) != len(existing):
        raise ValueError("Duplicate native cron slot")
    selected = {}
    for job in jobs:
        candidates = [row for row in existing if row.name == job['name']
                      or legacy.get(row.name) == job['name']]
        if len(candidates) > 1:
            raise ValueError("Both legacy and current maintenance slots exist")
        selected[job['name']] = candidates[0] if candidates else None
    used = {row.cron_id for row in selected.values() if row is not None}
    return selected, [row for row in existing if row.cron_id not in used]


async def configure(args, bundle, token):
    marker = bundle["schema"]
    from octop.cli.repl.embedded_session import embedded_runtime
    from octop.infra.agents.manager import AgentCreateSpec
    from octop.infra.connectors.service import ConnectorService
    from octop.infra.connectors.probe import probe_custom_mcp_server
    from octop.infra.cron.manager import CronCreateSpec, CronManager
    from octop.infra.proactive.scheduler import ProactiveCareScheduler
    from octop.infra.utils.ulid import new_cron_id

    async def paused_boot(manager):
        # Official APScheduler pause: no old or newly created timer can execute
        # in this one-shot configuration process.
        manager._scheduler.start(paused=True)

    async def no_proactive_jobs(_scheduler):
        return None

    with (patch.object(CronManager, "boot", paused_boot),
          patch.object(ProactiveCareScheduler, "start_all", no_proactive_jobs)):
        async with embedded_runtime() as server:
            services, runtime = server.services, server.app_runtime
            if services.config.default_timezone != "Asia/Shanghai":
                raise ValueError("OCTOP timezone must be Asia/Shanghai")
            source = services.repos.agent_repo.get("my-stock")
            if source is None or source.user_id is None:
                raise ValueError("Source expert owner is unavailable")
            owner = services.user_repo.get(source.user_id)
            if owner is None or owner.role != "admin":
                raise ValueError("Local configuration requires the source expert admin owner")
            connector = ConnectorService(repo=services.repos.connector_repo, secret_repo=services.secret_repo,
                                         settings_repo=services.settings_repo, config=services.config)
            spec = dict(transport="streamable_http", url=MCP_URL,
                        headers={"Authorization": "Bearer " + token}, enabled=True,
                        default_open=False, shared=False, display_name="板块轮动受控接口")
            probe = await probe_custom_mcp_server(spec)
            if probe.get("ok") is not True:
                code = str(probe.get("error_code") or "UNKNOWN")
                safe_code = code if re.fullmatch(r"[A-Z_]{1,80}", code) else "UNKNOWN"
                raise RuntimeError("STOCK_NATIVE_MCP_PROBE_FAILED:" + safe_code)
            names = {item.get("name") for item in probe.get("tools", [])}
            research = {f"stock_hermes_{op}" for op in ("prepare", "submit", "status")}
            combined = research | {"stock_etf_maintenance_prepare", "stock_etf_maintenance_status",
                                   "stock_etf_verification_context", "stock_etf_verification_submit"}
            if (names != combined if marker == MAINTENANCE_SCHEMA else names not in (research, combined)):
                raise RuntimeError("STOCK_NATIVE_MCP_TOOL_SET_MISMATCH")
            old = connector.get_custom_servers(source.user_id)
            connector.put_custom_servers(source.user_id, {**old, SERVER: spec})
            registry = runtime.agent_registry
            candidates = [row for row in services.repos.agent_repo.list_by_user(source.user_id)
                          if row.description == marker]
            if len(candidates) > 1:
                raise ValueError("Multiple managed native experts")
            expected_agent=getattr(args,'maintenance_agent_id',None)
            if expected_agent and (marker!=MAINTENANCE_SCHEMA or len(candidates)!=1
                                   or candidates[0].agent_id!=expected_agent):
                raise ValueError("Maintenance cutover expert identity mismatch")
            body = bundle["agent"]
            native_config = {**body["config"], **{k: body[k] for k in ("max_iters", "max_input_length", "max_tokens")}}
            if candidates:
                row = candidates[0]
                old_cfg = json.loads(row.config_json or "{}")
                if old_cfg.get("stock_native_rounds") is not True or old_cfg.get("workspace_dir") != WORKSPACE:
                    raise ValueError("Existing marker is not the scoped native expert")
                row = await registry.update(row.agent_id, name=body["name"], description=marker,
                                            default_model=body["default_model"], system_prompt=body["system_prompt"])
                await registry.update_config_json(row.agent_id, json.dumps(native_config))
                registry.persist_mcp_servers(row.agent_id, [SERVER])
            else:
                row = await registry.create(AgentCreateSpec(name=body["name"], user_id=source.user_id,
                    description=marker, default_model=body["default_model"], system_prompt=body["system_prompt"],
                    config=native_config, skill_package_ids=[], knowledge_base_ids=[], mcp_servers=[SERVER]))
            manager = runtime.cron_manager
            existing = manager.list_by_agent(row.agent_id)
            expected_crons=getattr(args,'maintenance_cron_ids',None)
            if expected_crons and (marker!=MAINTENANCE_SCHEMA
                                   or {item.cron_id for item in existing}!=set(expected_crons.split(','))):
                raise ValueError("Maintenance cutover cron identities mismatch")
            selected, retired = cron_migration_plan(existing,bundle['jobs'],
                                                       maintenance=marker==MAINTENANCE_SCHEMA)
            # The embedded scheduler is paused; persist all timers disabled until
            # every replacement has been saved. Retired rows keep their history.
            for item in existing:
                await manager.update(item.cron_id,enabled=0)
            configured=[]
            try:
                for job in bundle["jobs"]:
                    matching = selected[job['name']]
                    if matching:
                        result = await manager.update(matching.cron_id, name=job["name"], trigger=job["trigger"],
                            prompt=job["prompt"], fresh_thread=True, task_type="agent", mcp_servers=[SERVER],
                            enabled=0, model=None)
                    else:
                        result = await manager.create(CronCreateSpec(cron_id=new_cron_id(), agent_id=row.agent_id,
                            user_id=source.user_id, username=owner.username, name=job["name"], trigger=job["trigger"],
                            prompt=job["prompt"], fresh_thread=True, task_type="agent", mcp_servers=[SERVER],
                            enabled=False, model=None))
                    configured.append(result)
                results=[]
                for item in configured:
                    result=await manager.update(item.cron_id,enabled=int(args.enable_cron))
                    public=result.to_public_dict(include_agent=True)
                    results.append({k:public.get(k) for k in ('id','name','trigger','enabled','task_type')})
            except Exception:
                for item in manager.list_by_agent(row.agent_id):
                    await manager.update(item.cron_id,enabled=0)
                raise
            services.audit_repo.write(actor=owner.username, action="stock.native.configure", target=row.agent_id,
                                      payload=json.dumps({"cron_enabled": args.enable_cron, "jobs": len(results)}))
            return dict(agent_id=row.agent_id, name=row.name, model=row.default_model, mcp_server=SERVER,
                        cron_enabled=args.enable_cron, jobs=results, retired_jobs=[item.cron_id for item in retired])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "manifest", "stopped-proof", "mcp-token-file"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--enable-cron", action="store_true")
    parser.add_argument("--maintenance-agent-id", help="Require the existing maintenance expert identity at cutover")
    parser.add_argument("--maintenance-cron-ids", help="Require the exact comma-separated old cron IDs, including the retired slot")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    bundle, token = validate_inputs(args)
    result = asyncio.run(configure(args, bundle, token))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
