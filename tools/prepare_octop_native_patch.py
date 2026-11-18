"""Prepare a reversible, image-pinned OCTOP native expert middleware overlay.

This command writes reviewable files only; it does not restart containers.
"""
import argparse
import ast
from hashlib import sha256
import json
from pathlib import Path

IMAGE_REF = "ghcr.io/tencentcloud/octop:1.0.2"
IMAGE_ID = "sha256:c15e792f405a5b5aca8f3625adcc11f3421e13f6d01da7217ea18e6f0b98553f"
SOURCE_SHA256 = "445669cd8286acd7533a18a0fc66e558d3b7bb65d7fe0cabe8a7cfa5ec52f8e0"
DELIVERY_SHA256 = "db237e17ae1212b935d3dd8b4fe6a123bc4152c171dd9c0fd0c73326ae8216a4"
WORKSPACE = "/data/.octop/stock-native-workspace"
ANCHOR = "        merged_tools: list[Any] = []\n"
INSERT = '''        # Stock V4: scoped native-round tool and completion boundary.
        if cfg.get("stock_native_rounds") is True:
            native_backend = cfg.get("backend") or {}
            if (not isinstance(native_backend, dict)
                    or native_backend.get("type") != "filesystem"
                    or native_backend.get("root_dir") != "/data/.octop/stock-native-workspace"
                    or native_backend.get("virtual_mode") is not True):
                raise ValueError("Stock native rounds require the isolated filesystem backend")
            from octop.infra.agents.middleware.stock_native_rounds import StockNativeRoundsMiddleware, native_filesystem_middleware
            agent_middleware.append(native_filesystem_middleware())
            agent_middleware.append(StockNativeRoundsMiddleware())

        if row.agent_id == "my-stock":
            from octop.infra.agents.middleware.stock_qq_daily import StockQQDailyMiddleware
            agent_middleware.append(StockQQDailyMiddleware())

'''
STREAM_ANCHOR = "    async def stream(self, agent_id: str, request: dict[str, Any]) -> AsyncIterator[Any]:\n"
STREAM_INSERT = '''    async def stream(self, agent_id: str, request: dict[str, Any]) -> AsyncIterator[Any]:
        from octop.infra.agents.middleware.stock_qq_daily import qq_daily_scope
        with qq_daily_scope(agent_id, request.get("source")):
            async for chunk in self._stream_qq_scoped(agent_id, request):
                yield chunk

    async def _stream_qq_scoped(self, agent_id: str, request: dict[str, Any]) -> AsyncIterator[Any]:
'''
CALL_ANCHOR = "    async def call(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:\n"
CALL_INSERT = '''    async def call(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        from octop.infra.agents.middleware.stock_qq_daily import qq_daily_scope
        with qq_daily_scope(agent_id, request.get("source")):
            return await self._call_qq_scoped(agent_id, request)

    async def _call_qq_scoped(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
'''
CONFIG_ANCHOR = "        if applied.model_retry_enabled:\n"
CONFIG_INSERT = '''        if cfg.get("stock_native_rounds") is True:
            applied.system_prompt = row.system_prompt
            applied.bootstrap_enabled = False
            applied.memory = ()
            applied.memory_enabled = False
            applied.memory_extract_on_session_end = False
            applied.subagents_auto_load = False
            applied.team_enabled = False
            applied.tool_search_mode = "eager"
            applied.decision = None
            applied.model_retry_enabled = False
            applied.tools_disabled = frozenset(set(applied.tools_disabled) | {"task"})

'''
DELIVERY_ANCHOR = "    async def _deliver_agent(\n"
DELIVERY_INSERT = '''    async def _deliver_agent(self, command, session):
        if self._agent_manager.get_config(command.agent_id).get("stock_native_rounds") is True:
            from octop.infra.cron.stock_native_delivery import deliver_native_round
            return await deliver_native_round(self, command, session)
        return await self._deliver_agent_unscoped(command, session)

    async def _deliver_agent_unscoped(
'''


def prepare(source, guard, output, image_ref, image_id, delivery_source=None):
    raw = source.read_bytes()
    if (image_ref, image_id, sha256(raw).hexdigest()) != (IMAGE_REF, IMAGE_ID, SOURCE_SHA256):
        raise ValueError("OCTOP image/source does not match the reviewed version")
    text = raw.decode("utf-8")
    if any(text.count(anchor) != 1 for anchor in (ANCHOR, CONFIG_ANCHOR, STREAM_ANCHOR, CALL_ANCHOR)):
        raise ValueError("OCTOP patch anchor is not unique")
    patched = text.replace(ANCHOR, INSERT + ANCHOR)
    patched = patched.replace(CONFIG_ANCHOR, CONFIG_INSERT + CONFIG_ANCHOR)
    patched = patched.replace(STREAM_ANCHOR, STREAM_INSERT).replace(CALL_ANCHOR, CALL_INSERT)
    ast.parse(patched)
    guard_bytes = guard.read_bytes()
    ast.parse(guard_bytes.decode("utf-8"))
    daily_bytes = guard.with_name("octop_qq_daily.py").read_bytes()
    ast.parse(daily_bytes.decode("utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    original = output / "manager.original.py"
    if original.exists() and original.read_bytes() != raw:
        raise ValueError("Existing OCTOP manager backup differs")
    original.write_bytes(raw)
    (output / "manager.py").write_bytes(patched.encode("utf-8"))
    (output / "stock_native_rounds.py").write_bytes(guard_bytes)
    (output / "stock_qq_daily.py").write_bytes(daily_bytes)
    manifest = dict(image_ref=image_ref, image_id=image_id, original_sha256=sha256(raw).hexdigest(),
                    manager_sha256=sha256(patched.encode()).hexdigest(),
                    guard_sha256=sha256(guard_bytes).hexdigest(),
                    daily_sha256=sha256(daily_bytes).hexdigest(),
                    manager_target="/app/src/octop/infra/agents/manager.py",
                    daily_target="/app/src/octop/infra/agents/middleware/stock_qq_daily.py",
                    guard_target="/app/src/octop/infra/agents/middleware/stock_native_rounds.py")
    if delivery_source is not None:
        delivery = delivery_source.read_bytes()
        if sha256(delivery).hexdigest() != DELIVERY_SHA256:
            raise ValueError("OCTOP cron delivery source does not match the reviewed version")
        text = delivery.decode("utf-8")
        if text.count(DELIVERY_ANCHOR) != 1:
            raise ValueError("OCTOP cron delivery patch anchor is not unique")
        patched_delivery = text.replace(DELIVERY_ANCHOR, DELIVERY_INSERT).encode()
        ast.parse(patched_delivery.decode())
        helper = guard.with_name("octop_native_delivery.py").read_bytes()
        ast.parse(helper.decode())
        original_delivery = output / "delivery.original.py"
        if original_delivery.exists() and original_delivery.read_bytes() != delivery:
            raise ValueError("Existing OCTOP delivery backup differs")
        original_delivery.write_bytes(delivery)
        (output / "delivery.py").write_bytes(patched_delivery)
        (output / "stock_native_delivery.py").write_bytes(helper)
        manifest.update(delivery_original_sha256=DELIVERY_SHA256,
                        delivery_sha256=sha256(patched_delivery).hexdigest(),
                        delivery_helper_sha256=sha256(helper).hexdigest(),
                        delivery_target="/app/src/octop/infra/cron/delivery.py",
                        delivery_helper_target="/app/src/octop/infra/cron/stock_native_delivery.py")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-source", type=Path, required=True)
    parser.add_argument("--delivery-source", type=Path)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    guard = Path(__file__).resolve().parents[1] / "deploy" / "octop_native_guard.py"
    print(json.dumps(prepare(args.manager_source, guard, args.output, args.image_ref, args.image_id,
                             args.delivery_source), indent=2))


if __name__ == "__main__":
    main()
