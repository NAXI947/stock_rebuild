"""Run the packaged workspace, worker, migration and release checks."""
import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import signal
import socket
import sys
from threading import Event, Thread
from time import monotonic

import psycopg
from redis.exceptions import RedisError

from .ledger import encode, fingerprint
from .manual_api import read_token
from .manual_executor import ManualExecutor
from .manual_portfolio import replay
from .manual_queue import ManualQueue
from .market_stream import make_workspace_server
from .simulator import CHINA
from .temp_maintenance import TempMaintenance
from .stage2.inputs import cache_client, mapping_file, trading_days
from .stage2.reference_runtime import runtime_calendar
from .stage2.maintenance_store import live_mapping
from .stage2.market_bootstrap import ProbeError
from .stage2.outbox import dispatch, redis_delivery
from .stage2.postgres import MIGRATIONS, PostgresLedger
from .stage2.settings import Settings
from .workspace_readiness import WORKER_STATUS_TTL, worker_status_key
from .workspace_web import ASSETS, WEB, application

ROOT = Path(__file__).resolve().parents[2]
LOG = logging.getLogger(__name__)
RUNTIME_ERRORS = (OSError, ValueError, KeyError, TypeError, psycopg.Error, RedisError, ProbeError)


def _file(name, default=None):
    value = os.environ.get(name, default)
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} requires an absolute path")
    return path


def _enabled(name):
    value = os.environ.get(name, "false").lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} requires true or false")
    return value == "true"


def _hermes_trigger():
    mode = os.environ.get("HERMES_TRIGGER_MODE", "internal")
    if mode not in ("internal", "octop"):
        raise ValueError("HERMES_TRIGGER_MODE requires internal or octop")
    return mode


def _paused():
    path = _file("APP_PAUSE_FILE")
    return (lambda: path.exists()) if path else (lambda: False)


def _ledger():
    settings = Settings.from_values(os.environ)
    return PostgresLedger(settings, "FROZEN" if settings.env == "test" else "LIVE")


def _expected_revisions():
    migrations = sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9].sql"))
    if not migrations:
        raise ValueError("no packaged migrations")
    return {file.stem: fingerprint(file.read_text(encoding="utf-8")) for file in migrations}


def _schema_ready(ledger):
    with ledger._connect() as connection:
        applied = dict(connection.execute("SELECT revision, digest FROM schema_revisions").fetchall())
    for revision, digest in _expected_revisions().items():
        if applied.get(revision) != digest:
            raise ValueError(f"required schema revision {revision} is missing or changed")


def _smoke():
    file = ROOT / "tests" / "fixtures" / "stage3_manual_v14.json"
    result = replay(json.loads(file.read_text(encoding="utf-8")))
    if (result["return_pct"] != "9.5000" or result["profit"] != "950.00"
            or len(result["fills"]) != 4 or result["price_mode"] != "ASSUMPTION"):
        raise ValueError("frozen portfolio smoke failed")
    if not all((WEB / name).stat().st_size > 0 for name, _ in ASSETS.values()):
        raise ValueError("workspace static files are missing")
    return {"status": "OK", "frozen_return_pct": result["return_pct"],
            "revisions": sorted(_expected_revisions())}


@contextmanager
def _shutdown_event():
    stop = Event()

    def shutdown(*_):
        stop.set()

    signals = (signal.SIGTERM, signal.SIGINT)
    previous = {sig: signal.signal(sig, shutdown) for sig in signals}
    try:
        yield stop
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _api(ledger):
    maintenance = TempMaintenance.from_environment()
    token = read_token(_file("APP_WORKSPACE_TOKEN_FILE", "/app/data/workspace.token"))
    calendar = _file("APP_CALENDAR_FILE")
    app = application(ledger, token, calendar=runtime_calendar(ledger,calendar) if calendar else (),
                      fallback=_file("APP_MARKET_FILE"), touch_file=_file("APP_TOUCH_FILE"),
                      eod_file=_file("APP_LEFT_RIGHT_EOD_FILE"),
                      macro_file=_file("APP_MARKET_MACRO_FILE"),
                      mapping_file=_file("APP_ETF_INFO_MAPPING_FILE", "/app/data/etf-mapping.json"),
                      lr_history_file=_file("APP_LEFT_RIGHT_HISTORY_FILE", "/app/data/left-right-history.json"),
                      hot_history_file=_file("APP_HOT_SECTOR_HISTORY_FILE", "/app/data/hot-sector-history.json"),
                      paused=_paused(), maintenance=maintenance)
    port = int(os.environ.get("APP_API_PORT", "8000"))
    if not 1024 <= port <= 65535:
        raise ValueError("invalid API port")
    native = nullcontext()
    if _hermes_trigger() == "octop":
        from .native_mcp import listener
        from .stage2.native_rounds import NativeRounds
        from .etf_maintenance import MaintenanceQueue
        native_token = read_token(_file("APP_OCTOP_NATIVE_TOKEN_FILE", "/app/data/octop-native.token"))
        native = listener(NativeRounds(ledger, calendar, paused=_paused()), native_token,
                          int(os.environ.get("APP_OCTOP_MCP_PORT", "8001")),
                          maintenance=MaintenanceQueue(ledger,calendar,paused=_paused()))
    with _shutdown_event() as stop, native, make_workspace_server("0.0.0.0", port, app) as server:
        server.timeout = 0.5
        while not stop.is_set():
            if maintenance:
                maintenance.tick()
            server.handle_request()


def _worker_socket(ledger):
    # Linux abstract sockets are scoped to the network namespace and create no files.
    namespace = sha256(ledger.ns.encode()).hexdigest()[:24]
    return "\0stock-v4-worker-" + namespace


@contextmanager
def _worker_heartbeat(ledger):
    last_tick = monotonic()

    def pulse():
        nonlocal last_tick
        last_tick = monotonic()

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(_worker_socket(ledger))
        listener.listen(4)
        listener.settimeout(0.5)
        stop = Event()

        def answer():
            while not stop.is_set():
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    continue
                with client:
                    client.settimeout(0.5)
                    try:
                        client.sendall(b"1" if monotonic() - last_tick <= 180 else b"0")
                    except OSError:
                        pass

        thread = Thread(target=answer, name="worker-health", daemon=True)
        thread.start()
        try:
            yield pulse
        finally:
            stop.set()
            thread.join(timeout=2)


def _worker_health(ledger):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(_worker_socket(ledger))
        if connection.recv(1) != b"1":
            raise ValueError("worker heartbeat is stale")


def _schedule_args(ledger, qq, collect, *, stock_research=False, findesk=False):
    if stock_research and not collect:
        raise ValueError("stock research requires official collection")
    if findesk and not collect:
        raise ValueError("Findesk requires official collection")
    calendar = _file("APP_CALENDAR_FILE")
    mapping = _file("APP_MAPPING_FILE")
    snapshot_key = os.environ.get("APP_SNAPSHOT_KEY")
    snapshot_file = _file("APP_MARKET_FILE")
    command = os.environ.get("OCTOP_COMMAND_JSON")
    octop_socket = _file("APP_OCTOP_SOCKET")
    if not calendar or not mapping or bool(snapshot_key) == bool(snapshot_file):
        raise ValueError("Hermes schedule requires calendar, mapping and one shared snapshot source")
    if bool(command) == bool(octop_socket):
        raise ValueError("Hermes schedule requires exactly one OCTOP transport")
    if octop_socket and not octop_socket.is_socket():
        raise ValueError("OCTOP socket is unavailable")
    if snapshot_key and not snapshot_key.startswith(ledger.settings.redis_prefix):
        raise ValueError("snapshot key is outside the configured Redis environment")
    tickflow = _file("APP_TICKFLOW_CONFIG") if collect else None
    kaipanla = _file("APP_KAIPANLA_CAPTURES") if collect else None
    findesk_config = Path("/app/automation/findesk.json") if findesk else None
    if collect:
        from .stage2.market_bootstrap import capture_request, tickflow_config

        if not snapshot_key or not tickflow or not kaipanla:
            raise ValueError("official collection requires shared snapshot key and both source configurations")
        tickflow_config(tickflow)
        capture_request(kaipanla, "041", {"Index": "0"})
    runtime_calendar(ledger,calendar)
    live_mapping(ledger,mapping)
    return argparse.Namespace(
        calendar=calendar, mapping=mapping, snapshot_key=snapshot_key, snapshot_file=snapshot_file,
        octop_stdio=False, octop_command_json=command, octop_socket=octop_socket, poll_seconds=15,
        received_at=None, once=False, manual_queue=True, qq=qq, trial_day=None,
        tickflow_config=tickflow, kaipanla_captures=kaipanla, stock_research=stock_research,
        findesk_config=findesk_config,
        trigger_mode=_hermes_trigger(),
    )


def _worker(ledger, cache):
    from .left_right_backfill import BackfillExecutor, BackfillQueue
    from .data_updates import UpdateSchedule
    collect = _enabled("ENABLE_OFFICIAL_COLLECTION")
    schedule = _enabled("ENABLE_HERMES_SCHEDULE")
    qq = _enabled("ENABLE_QQ_PUSH")
    stock_research = _enabled("ENABLE_STOCK_RESEARCH")
    findesk = _enabled("ENABLE_FINDESK_IOPV")
    trigger_mode = _hermes_trigger()
    if trigger_mode == "octop" and not schedule:
        raise ValueError("OCTOP triggering requires the Hermes worker service")
    if (collect or qq) and (not schedule or ledger.settings.env != "production"):
        raise ValueError("official collection and QQ require an enabled production Hermes schedule")
    if stock_research and not collect:
        raise ValueError("stock research requires official collection")
    if findesk and not collect:
        raise ValueError("Findesk requires official collection")
    paused = _paused()
    lr_scope = os.environ.get("APP_LEFT_RIGHT_EOD_CODES", "").strip()
    lr_refresh = None
    if lr_scope:
        from .stage2.left_right_refresh import LeftRightRefresh
        lr_refresh = LeftRightRefresh(ledger, _file("APP_CALENDAR_FILE"), lr_scope)
    args = _schedule_args(ledger, qq, collect, stock_research=stock_research,
                          findesk=findesk) if schedule else None
    statistics_status = dict(status='MAINTENANCE_QUEUE',modes=['market','retry'])
    macro = None
    turnover = None
    if collect:
        from .stage2.macro_refresh import MacroRefresh
        from .stage2.market_turnover import TurnoverRefresh
        macro = MacroRefresh(ledger, _file("APP_KAIPANLA_CAPTURES"))
        turnover = TurnoverRefresh(ledger, _file("APP_TICKFLOW_CONFIG"), _file("APP_CALENDAR_FILE"))
    maintenance = TempMaintenance.from_environment()
    update_queue = BackfillQueue(ledger, _file("APP_CALENDAR_FILE"))
    external_enabled = lambda: ledger.settings.env == "production" and not _enabled("DISABLE_EXTERNAL_PROVIDER_HTTP")
    backfill = BackfillExecutor(update_queue,
        config=_file("APP_TUSHARE_CONFIG", "/app/automation/tushare-eod.json"),
        captures=_file("APP_KAIPANLA_CAPTURES"), paused=paused,
        external_enabled=external_enabled,
        scheduler=UpdateSchedule(update_queue, enabled=lambda: collect and external_enabled()))
    # Keep signal handling installed until every background executor has joined.
    # serve() temporarily replaces these handlers and restores them on return.
    with _shutdown_event() as stop, _worker_heartbeat(ledger) as heartbeat, backfill:
        status_key = worker_status_key(ledger.ns)
        status_due = 0

        def pulse():
            nonlocal status_due
            heartbeat()
            tick = monotonic()
            if maintenance:
                maintenance.tick(now=tick)
            if lr_refresh and not paused():
                lr_refresh.tick(tick)
            if macro and not paused():
                macro.tick(tick)
                heartbeat()
            if turnover and not paused():
                turnover.tick(tick)
                heartbeat()
            if tick < status_due:
                return
            status_due = tick + 30
            status = dict(observed_at=datetime.now(CHINA).isoformat(), hermes_schedule=schedule,
                          hermes_trigger=trigger_mode,
                          official_collection=collect, qq_push=qq, stock_research=stock_research,
                          findesk_iopv=findesk, etf_statistics=statistics_status,
                          macro_background=macro.status if macro else None,
                          market_turnover=turnover.status if turnover else None)
            if maintenance:
                status["temporary"] = maintenance.status()
            try:
                cache.set(status_key, encode(status), ex=WORKER_STATUS_TTL)
            except (OSError, RedisError) as exc:
                LOG.warning("Worker status unavailable: %s", type(exc).__name__)

        if args:
            from .stage2.schedule import serve
            return serve(ledger, cache, args, paused=paused, pulse=pulse)
        with ManualExecutor(ManualQueue(ledger), paused=paused):
            while not stop.is_set():
                pulse()
                if not paused():
                    try:
                        dispatch(ledger, "redis", lambda event: redis_delivery(ledger, cache, event))
                    except (psycopg.Error, RedisError) as exc:
                        LOG.warning("Redis outbox poll failed: %s", type(exc).__name__)
                stop.wait(5)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("api", "worker", "migrate", "worker-health"):
        sub.add_parser(name)
    sub.add_parser("smoke").add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "smoke" and args.offline:
            print(json.dumps(_smoke()), flush=True)
            return 0
        ledger = _ledger()
        if args.command == "worker-health":
            _worker_health(ledger)
            return 0
        if args.command == "migrate":
            ledger.migrate()
            _schema_ready(ledger)
            print(json.dumps({"status": "OK", "revisions": sorted(_expected_revisions())}), flush=True)
            return 0
        _schema_ready(ledger)
        with cache_client(ledger.settings) as cache:
            if not cache.ping():
                raise ValueError("Redis ping failed")
            if args.command == "api":
                _api(ledger)
            elif args.command == "worker":
                return _worker(ledger, cache)
            else:
                print(json.dumps(_smoke()), flush=True)
    except RUNTIME_ERRORS as exc:
        detail = ": " + str(exc) if type(exc) is ValueError and exc.args and (
            str(exc).startswith(("required schema revision ", "production collection requires "))
        ) else ""
        print(f"runtime {args.command}: {type(exc).__name__}{detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
