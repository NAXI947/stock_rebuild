"""Relay one frozen Stage 2 worker slot to the installed OCTOP over operator SSH."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import shlex
import sys

PROTOCOL = "stock-v4-octop-rpc-v1"


def ssh(host, known_hosts):
    args = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
    if known_hosts:
        args.extend(["-o", f"UserKnownHostsFile={known_hosts}", "-o", "UpdateHostKeys=no",
                     "-o", "HostKeyAlgorithms=ssh-ed25519"])
    return [*args, host]


async def start(args, script):
    coded = base64.b64encode(script.encode()).decode()
    command = f"""bash -c "$(printf %s '{coded}' | base64 -d)" """
    return await asyncio.create_subprocess_exec(*args, command, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                limit=600000)


async def log_errors(label, stream):
    while line := await stream.readline():
        print(label, line.decode("utf-8", "replace").rstrip(), flush=True, file=sys.stderr)


async def relay(args):
    dev = ssh(args.dev_host, args.dev_known_hosts)
    execution = ssh(args.octop_host, args.octop_known_hosts)
    project = args.project_root.rstrip("/")
    bridge = project + "/modules/strategy_hermes/stage2/octop_bridge.py"
    skill = project + "/skills/a-share-hotspot-scout/SKILL.md"

    async def read_source(path, limit):
        source = await asyncio.create_subprocess_exec(*dev, "cat -- " + shlex.quote(path),
                                                       stdout=asyncio.subprocess.PIPE)
        content, _ = await asyncio.wait_for(source.communicate(), 12)
        if source.returncode or not content or len(content) > limit:
            raise RuntimeError("failed to read the Stage 2 bridge or reviewed skill")
        return content

    content, reviewed_skill = await asyncio.gather(read_source(bridge, 30000), read_source(skill, 36864))
    skill_env = "STOCK_V4_SKILL_B64=" + base64.b64encode(reviewed_skill).decode()
    octop_cmd = (
        "set -euo pipefail\n"
        "container=$(docker ps --filter name=1Panel-octop --format '{{.Names}}')\n"
        "test -n \"$container\"\n"
        "exec docker exec -i -e PYTHONDONTWRITEBYTECODE=1 -e LANGSMITH_TRACING=false "
        "-e LANGCHAIN_TRACING_V2=false -e " + shlex.quote(skill_env)
        + " \"$container\" python -c " + shlex.quote(content.decode())
    )
    rt = args.runtime_root.rstrip("/")
    worker_args = [rt + "/stage2-venv/bin/python", "-B", "-m", "modules.strategy_hermes.stage2"]
    sources = ["--calendar", args.calendar, "--mapping", args.mapping]
    if args.live_day:
        worker_args += [
            "--env-file", rt + "/.env.development", "--input-kind", "LIVE", "--dev-namespace", args.namespace,
            "serve", "--octop-stdio", "--trial-day", args.live_day,
            "--tickflow-config", rt + "/secrets/tickflow.json", "--kaipanla-captures", args.kaipanla_captures,
            "--snapshot-key", args.namespace + ":source:market", *sources,
        ]
    else:
        worker_args += [
            "--env-file", rt + "/.env.test", "--input-kind", "FROZEN", "--test-namespace", args.namespace,
            "serve", "--once", "--octop-stdio", "--received-at", args.at, "--snapshot-file", args.snapshot, *sources,
        ]
    worker_command = shlex.join(worker_args)
    dev_cmd = "set -euo pipefail\ncd " + shlex.quote(args.project_root) + "\n"
    if args.live_day:
        dev_cmd += "exec " + worker_command
    else:
        dev_cmd += worker_command + "\n" + worker_command
    worker, octop = await asyncio.gather(start(dev, dev_cmd), start(execution, octop_cmd))
    logs = [asyncio.create_task(log_errors("worker", worker.stderr)),
            asyncio.create_task(log_errors("octop", octop.stderr))]
    try:
        async with asyncio.timeout(7 * 3600 if args.live_day else 165):
            while line := await worker.stdout.readline():
                call = json.loads(line)
                if call.get("protocol") != PROTOCOL:
                    raise ValueError("unexpected worker message")
                octop.stdin.write(line)
                await octop.stdin.drain()
                reply = await octop.stdout.readline()
                received = json.loads(reply)
                if received.get("protocol") != PROTOCOL or received.get("id") != call["id"]:
                    raise ValueError("OCTOP response does not match the worker request")
                result = received["result"]
                print(json.dumps(dict(operation=call["operation"], status=result["status"],
                                      requests=result.get("requests"), http_status=result.get("http_status"))),
                      flush=True)
                worker.stdin.write(reply)
                await worker.stdin.drain()
            worker.stdin.close()
            octop.stdin.close()
            codes = await asyncio.gather(worker.wait(), octop.wait())
            await asyncio.gather(*logs)
            return int(any(codes))
    finally:
        for child in (worker, octop):
            if child.returncode is None:
                child.kill()
                await child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-host", default="tencent")
    parser.add_argument("--octop-host", default="telestock")
    parser.add_argument("--dev-known-hosts", type=Path)
    parser.add_argument("--octop-known-hosts", type=Path)
    parser.add_argument("--project-root", default="/opt/workspace/stock-v4")
    parser.add_argument("--runtime-root", default="/opt/workspace/stock-v4-runtime")
    for name in ("namespace", "calendar", "mapping"):
        parser.add_argument("--" + name, required=True)
    clock = parser.add_mutually_exclusive_group(required=True)
    clock.add_argument("--at")
    clock.add_argument("--live-day")
    parser.add_argument("--snapshot")
    parser.add_argument("--kaipanla-captures", default="/opt/workspace/legacy_backup/kaipanlajson")
    args = parser.parse_args()
    if args.at and not args.snapshot:
        parser.error("--at requires a frozen snapshot")
    return asyncio.run(relay(args))


if __name__ == "__main__":
    raise SystemExit(main())
