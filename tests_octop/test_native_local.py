"""Validate official OCTOP signatures and paused config lifecycle without live DB."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, create_autospec, patch

from tools.octop_native_local import configure, MCP_URL, MAINTENANCE_SCHEMA, SCHEMA, SERVER, WORKSPACE


class NativeLocalTests(unittest.TestCase):
    def test_private_docker_alias_passes_official_url_policy(self):
        from octop.infra.connectors.custom_mcp import normalize_server_spec, validate_mcp_http_url
        from octop.infra.errors import OctopError
        self.assertEqual(MCP_URL, "http://stock-v4-backend.internal:8001/mcp")
        self.assertEqual(validate_mcp_http_url(MCP_URL), MCP_URL)
        normalized = normalize_server_spec(SERVER, dict(transport="streamable_http", url=MCP_URL))
        self.assertEqual(normalized["url"], MCP_URL)
        with self.assertRaises(OctopError):
            validate_mcp_http_url("http://stock-v4-backend:8001/mcp")

    def test_official_api_configuration_with_paused_scheduler(self):
        self._official_configuration()

    def test_maintenance_configuration_keeps_research_expert_separate(self):
        self._official_configuration(maintenance=True)

    def test_maintenance_upgrade_reuses_ids_and_disables_old_preopen(self):
        self._official_configuration(maintenance=True,legacy=True)

    def test_partial_maintenance_upgrade_keeps_existing_timers_disabled(self):
        self._official_configuration(maintenance=True,legacy=True,fail_update=True)

    def _official_configuration(self, maintenance=False, legacy=False, fail_update=False):
        from apscheduler.schedulers.base import STATE_PAUSED
        from octop.infra.agents.manager import AgentManager
        from octop.infra.connectors.service import ConnectorService
        from octop.infra.cron.manager import CronManager
        from octop.infra.proactive.scheduler import ProactiveCareScheduler

        source = SimpleNamespace(user_id=1)
        native = SimpleNamespace(agent_id="native-test", name="native", default_model="provider/model")
        registry = create_autospec(AgentManager, instance=True)
        registry.create.return_value = native
        registry.update.return_value = native
        manager = create_autospec(CronManager, instance=True)
        manager.list_by_agent.return_value = []
        if legacy:
            manager.list_by_agent.return_value=[SimpleNamespace(cron_id=key,name='ETF资料维护 '+slot)
                for key,slot in [('close-id','18:00'),('review-id','21:00'),('preopen-id','08:45')]]

        async def create_cron(spec):
            self.assertEqual(spec.agent_id, native.agent_id)
            self.assertFalse(spec.enabled)
            self.assertEqual(spec.mcp_servers, [SERVER])
            public = dict(id=spec.cron_id, name=spec.name, trigger=spec.trigger, enabled=False, task_type="agent")
            return SimpleNamespace(cron_id=spec.cron_id,to_public_dict=lambda **kwargs: public)

        manager.create.side_effect = create_cron
        async def update_cron(cron_id, **changes):
            if fail_update and changes.get('name')=='ETF资料维护 14:15':
                raise RuntimeError('frozen update failure')
            return SimpleNamespace(cron_id=cron_id,to_public_dict=lambda **kw: dict(id=cron_id,**changes))
        manager.update.side_effect = update_cron
        services = SimpleNamespace(
            config=SimpleNamespace(default_timezone="Asia/Shanghai"),
            repos=SimpleNamespace(agent_repo=SimpleNamespace(get=Mock(return_value=source),
                list_by_user=Mock(return_value=[])), connector_repo=Mock()),
            user_repo=SimpleNamespace(get=Mock(return_value=SimpleNamespace(role="admin", username="test-admin"))),
            secret_repo=Mock(), settings_repo=Mock(), audit_repo=Mock())
        server = SimpleNamespace(services=services, app_runtime=SimpleNamespace(agent_registry=registry, cron_manager=manager))
        timer = CronManager(gateway=Mock(), delivery_service=Mock(), repos=Mock())

        @asynccontextmanager
        async def isolated_runtime():
            # Calls the actual CronManager class, under the script's scoped
            # pause patch. No server, production DB, listeners, or model starts.
            await timer.boot()
            self.assertEqual(timer._scheduler.state, STATE_PAUSED)
            await ProactiveCareScheduler.start_all(None)
            try:
                yield server
            finally:
                await timer.shutdown()

        connector = create_autospec(ConnectorService, instance=True)
        connector.get_custom_servers.return_value = {"existing": {"url": "preserved"}}
        tools = [{"name": f"stock_hermes_{op}"} for op in ("prepare", "submit", "status")]
        marker = MAINTENANCE_SCHEMA if maintenance else SCHEMA
        budget, tokens = (768, 2048) if maintenance else (256, 8192)
        if maintenance:
            tools.extend({"name": f"stock_etf_maintenance_{op}"} for op in ("prepare", "status"))
            tools.extend({"name": f"stock_etf_verification_{op}"} for op in ("context", "submit"))
            services.repos.agent_repo.list_by_user.return_value = [SimpleNamespace(description=SCHEMA, agent_id="research-untouched")]
        body = dict(name="native", default_model="provider/model", system_prompt="frozen rules", description=marker,
                    config=dict(stock_native_rounds=True, workspace_dir=WORKSPACE,
                                backend=dict(type="filesystem", root_dir=WORKSPACE, virtual_mode=True)),
                    max_iters=budget, max_input_length=64000, max_tokens=tokens)
        jobs = [dict(name=f"slot-{slot}", trigger=f"cron:35 {slot} * * mon-fri", prompt="test") for slot in range(9, 14)]
        if maintenance:
            jobs = [dict(name=f"maintenance-{mode}", trigger=trigger, prompt=f"执行ETF资料维护 {mode}。")
                    for mode, trigger in (("market", "cron:0 10 * * mon-fri"), ("retry", "cron:15 14 * * mon-fri"))]
            if legacy:
                for job,slot in zip(jobs,("10:00","14:15"),strict=True):job["name"]="ETF资料维护 "+slot
        bundle = dict(schema=marker, agent=body, jobs=jobs)
        with (patch("octop.cli.repl.embedded_session.embedded_runtime", isolated_runtime),
              patch("octop.infra.connectors.service.ConnectorService", return_value=connector),
              patch("octop.infra.connectors.probe.probe_custom_mcp_server", AsyncMock(return_value=dict(ok=True, tools=tools)))):
            if fail_update:
                with self.assertRaisesRegex(RuntimeError,'frozen update failure'):
                    asyncio.run(configure(SimpleNamespace(enable_cron=True),bundle,"test-only-credential"))
                self.assertFalse(any(call.kwargs.get('enabled') for call in manager.update.call_args_list))
                self.assertEqual([call.args[0] for call in manager.update.call_args_list[-3:]],
                                 ['close-id','review-id','preopen-id'])
                return
            result = asyncio.run(configure(SimpleNamespace(enable_cron=False), bundle, "test-only-credential"))
            if legacy:
                self.assertEqual([job['id'] for job in result['jobs']],['close-id','review-id'])
                self.assertEqual(result['retired_jobs'],['preopen-id'])
                manager.create.assert_not_called()
        self.assertEqual(len(result["jobs"]), 2 if maintenance else 5)
        self.assertTrue(all(row["id"] for row in result["jobs"]))
        self.assertEqual(result["agent_id"], native.agent_id)
        registry.create.assert_awaited_once()
        registry.update.assert_not_called()
        spec = registry.create.call_args.args[0]
        self.assertEqual(spec.description, marker)
        self.assertEqual(spec.config["max_tokens"], tokens)
        self.assertEqual(spec.config["max_iters"], budget)
        self.assertEqual(spec.skill_package_ids, [])
        self.assertEqual(connector.put_custom_servers.call_args.args[1]["existing"], {"url": "preserved"})
        self.assertNotIn("test-only-credential", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
