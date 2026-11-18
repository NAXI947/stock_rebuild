"""Clock-driven Stage 2 worker entry with a supervised OCTOP connection."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
import json
import signal
import socket
import subprocess
import sys
from threading import Event
from time import monotonic

import psycopg
from redis.exceptions import RedisError

from ..ledger import encode
from ..simulator import CHINA, moment
from .inputs import mapping_file, shared_market, trading_days
from .reference_runtime import runtime_calendar
from .maintenance_store import live_mapping
from .notifications import deliver_qq
from .octop_analyst import OctopAnalyst
from .octop_client import OctopClient
from .worker import due_round, planned_at, scheduled_tick
from .outbox import dispatch, redis_delivery


def dispatch_saved(ledger, cache, qq):
    """Deliver already committed events independently of collection/ledger errors."""
    result = {}
    deliveries = [('redis', lambda event: redis_delivery(ledger, cache, event))]
    if qq:
        deliveries.append(('octop', qq))
    for target, deliver in deliveries:
        key = 'qq_delivery' if target == 'octop' else 'redis_delivery'
        try:
            result[key] = dispatch(ledger, target, deliver)
        except (psycopg.Error, RedisError, OSError, ValueError, KeyError, TypeError) as exc:
            result[key] = 'UNAVAILABLE'
            result[key+'_error'] = type(exc).__name__
    return result


@contextmanager
def connection(args):
    if args.octop_stdio:
        yield OctopClient(sys.stdin.fileno(), sys.stdout.fileno())
        return
    path = getattr(args, "octop_socket", None)
    if path:
        client = SocketClient(path)
        try:
            yield client
        finally:
            client.close()
        return
    argv = json.loads(args.octop_command_json)
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("OCTOP command must be a nonempty JSON argument list")
    client = CommandClient(argv)
    try:
        yield client
    finally:
        client.close()


class CommandClient:
    """Restart the operator-configured bridge after a closed pipe."""
    def __init__(self, argv):
        self.argv = argv
        self.child = self.client = None

    def call(self, operation, payload, *, timeout):
        if self.child is None or self.child.poll() is not None:
            self.close()
            self.child = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
            self.client = OctopClient(self.child.stdout.fileno(), self.child.stdin.fileno())
        try:
            return self.client.call(operation, payload, timeout=timeout)
        except ConnectionError:
            self.close()
            raise

    def close(self):
        if self.child is None:
            return
        child, self.child, self.client = self.child, None, None
        child.stdin.close()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        finally:
            child.stdout.close()


class SocketClient:
    """Use the worker-only socket without a Docker API or SSH credential."""
    def __init__(self, path):
        self.path = path
        self.socket = self.client = None

    def call(self, operation, payload, *, timeout):
        deadline = monotonic() + timeout
        try:
            if self.socket is None:
                self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.socket.settimeout(min(3, timeout))
                self.socket.connect(str(self.path))
                self.socket.setblocking(True)
                fd = self.socket.fileno()
                self.client = OctopClient(fd, fd)
            return self.client.call(operation, payload, timeout=deadline - monotonic())
        except TimeoutError:
            self.close()
            raise
        except OSError:
            self.close()
            raise ConnectionError("OCTOP socket unavailable") from None

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = self.client = None


def serve(ledger, cache, args, *, paused=lambda: False, pulse=lambda: None):
    if args.received_at and (ledger.origin != "FROZEN" or not args.once):
        raise ValueError("explicit worker time requires --once and frozen inputs")
    if not 1 <= args.poll_seconds <= 60:
        raise ValueError("worker poll interval must be within 1-60 seconds")
    if args.qq and ledger.origin != "LIVE":
        raise ValueError("QQ delivery requires the live worker mode")
    trial = args.trial_day
    intraday = bool(args.tickflow_config or args.kaipanla_captures)
    if trial and (ledger.settings.env != "development" or ledger.origin != "LIVE" or args.qq):
        raise ValueError("live trial requires the isolated development worker")
    official = ledger.settings.env == "production" and ledger.origin == "LIVE"
    if intraday and (not (trial or official) or not args.snapshot_key
                     or not args.tickflow_config or not args.kaipanla_captures):
        raise ValueError("intraday collection requires a live environment, shared key and both source configurations")
    stock_research = getattr(args, "stock_research", False)
    if stock_research and not intraday:
        raise ValueError("stock research requires an active shared collector")
    findesk_config = getattr(args, "findesk_config", None)
    if findesk_config is not None and not intraday:
        raise ValueError("Findesk requires an active shared collector")
    if args.manual_queue and args.once:
        raise ValueError("manual queue requires the continuous worker")
    executor = nullcontext()
    if args.manual_queue:
        from ..manual_executor import ManualExecutor
        from ..manual_queue import ManualQueue
        executor = ManualExecutor(ManualQueue(ledger), paused=paused)
    clock = moment(args.received_at) if args.received_at else None
    output = sys.stderr if args.octop_stdio else sys.stdout
    stop = Event()
    refreshed_slot = None
    trigger_mode = getattr(args, "trigger_mode", "internal")
    if trigger_mode not in ("internal", "octop"):
        raise ValueError("invalid Hermes trigger mode")
    native_queue = None
    if trigger_mode == "octop":
        from .native_rounds import NativeRounds
        native_queue = NativeRounds(ledger, args.calendar, paused=paused)

    def shutdown(*_):
        stop.set()

    previous_handlers = {sig: signal.signal(sig, shutdown) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with connection(args) as client, executor:
            qq = (lambda event: deliver_qq(ledger, client, event)) if args.qq else None
            while not stop.is_set():
                pulse()
                if paused():
                    stop.wait(args.poll_seconds)
                    continue
                if args.octop_stdio and not client.peer_open():
                    print("OCTOP_STDIO_CLOSED", file=output, flush=True)
                    return 1
                now = clock if clock is not None else datetime.now(CHINA)
                try:
                    if native_queue is not None:
                        from .native_rounds import native_tick
                        from .worker import apply_market
                        update = native_tick(native_queue, cache, args, client)
                        days = runtime_calendar(ledger,args.calendar,now)
                        mapping = live_mapping(ledger,args.mapping,now)
                        market = shared_market(ledger, cache, key=args.snapshot_key, path=args.snapshot_file)
                        result = apply_market(ledger, datetime.now(CHINA), days, mapping, market)
                        if intraday and not paused() and not stop.is_set() and update.get("state") in ("IDLE", "EMPTY", "PUBLISHED"):
                            from .pending_quotes import provider_enabled, tick as pending_quote_tick
                            try:
                                followup = pending_quote_tick(ledger, cache, args.snapshot_key, days,
                                    args.tickflow_config, paused=lambda: paused() or stop.is_set()) if provider_enabled() else None
                                if followup is not None:
                                    result["pending_quotes"] = followup
                                    if followup["status"] == "PUBLISHED":
                                        market = shared_market(ledger, cache, key=args.snapshot_key)
                                        result.update(apply_market(ledger, datetime.now(CHINA), days, mapping, market))
                            except (psycopg.Error, RedisError, OSError, ValueError, KeyError, TypeError) as exc:
                                result["pending_quotes"] = {"status": "UNAVAILABLE", "error_type": type(exc).__name__}
                        result["round"] = "NO_DUE_ROUND" if update.get("state") in ("IDLE", "EMPTY", None) else "NATIVE_" + update["state"]
                        if result["round"] != "NO_DUE_ROUND":
                            result["native"] = {key: update[key] for key in
                                                ("state", "run_id", "report_id", "slot", "error", "stage")
                                                if key in update}
                    else:
                        days = runtime_calendar(ledger,args.calendar,now)
                        if trial:
                            if trial not in days:
                                raise ValueError("trial date is outside the trading calendar")
                            if now.date().isoformat() < trial:
                                stop.wait(args.poll_seconds)
                                continue
                            if now >= planned_at(trial, "14:30") + timedelta(minutes=8):
                                return 0
                        mapping = live_mapping(ledger,args.mapping,now)
                        market = shared_market(ledger, cache, key=args.snapshot_key, path=args.snapshot_file)
                        due = due_round(now, days)
                        collection = None
                        if intraday and due is not None and due[:2] != refreshed_slot:
                            from .intraday import refresh
                            options = {}
                            if stock_research:
                                options["stock_mapping"] = mapping
                            if findesk_config is not None:
                                options["findesk"] = (findesk_config, mapping)
                            collection = refresh(ledger, cache, args.snapshot_key, due, market,
                                                 args.tickflow_config, args.kaipanla_captures, **options)
                            if collection is None:
                                stop.wait(args.poll_seconds)
                                continue
                            refreshed_slot = due[:2]
                            market = shared_market(ledger, cache, key=args.snapshot_key)
                            now = datetime.now(CHINA)
                        if paused() or stop.is_set():
                            continue
                        if due is not None:
                            from .etf_statistics import overlay_mapping
                            mapping = overlay_mapping(ledger, mapping, days, now)
                        result = scheduled_tick(ledger, cache, now, days, mapping, market,
                                                OctopAnalyst(ledger, mapping, market, client, now), qq=qq)
                        if collection is not None:
                            result["collection"] = collection
                except (psycopg.Error, RedisError, OSError, ValueError, KeyError, TypeError) as exc:
                    result = {"round": "WORKER_ERROR", "error_type": type(exc).__name__}
                if native_queue is not None and not paused() and not stop.is_set():
                    result.update(dispatch_saved(ledger, cache, qq))
                if args.once or result.get("pending_quotes") or result["round"] not in ("NO_DUE_ROUND", "ROUND_ALREADY_CLAIMED"):
                    print(encode(result), file=output, flush=True)
                if args.once:
                    return int(result["round"] in ("FAILED", "WORKER_ERROR"))
                stop.wait(2 if native_queue is not None else args.poll_seconds)
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return 0
