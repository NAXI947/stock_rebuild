"""Local authenticated market reads and bounded resumable WebSocket sessions."""

from datetime import datetime, timezone
from hashlib import sha256
from http import HTTPStatus
import json
import re
from secrets import token_hex
from select import select
from socket import SHUT_RDWR
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from wsgiref.simple_server import ServerHandler, make_server

import psycopg
from redis.exceptions import RedisError
from websockets.datastructures import Headers
from websockets.frames import BINARY, CONT, TEXT
from websockets.http11 import Request
from websockets.protocol import OPEN
from websockets.server import ServerProtocol

from .ledger import encode
from .manual_api import APIError, LocalServer, QuietHandler, authorized
from .simulator import moment
from .stage2.inputs import cache_client, read_file
from .stage2.market_notices import stream_key

BASE = "/api/v2/market"
SNAPSHOT = BASE + "/snapshot"
TICKET = BASE + "/ws-ticket"
WS = BASE + "/ws"
TOPICS = {"market", "candidates", "skill", "report", "datasets"}
DEFAULT_TOPICS = ("market", "candidates", "skill", "report")
MARKET_CODE = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z")
CURSOR = re.compile(r"[0-9]{1,18}-[0-9]{1,18}\Z")
TICKET_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
TICKET_TTL = 30
HEARTBEAT = 15
MAX_FRAME = 8192

# Keep at most 64 live tickets per workspace. Expiry and consumption are atomic.
TICKETS = """
local now=tonumber(redis.call('TIME')[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if ARGV[1] == 'take' then
    return redis.call('ZREM', KEYS[1], ARGV[2])
end
if redis.call('ZCARD', KEYS[1]) >= 64 then return 0 end
redis.call('ZADD', KEYS[1], 'NX', now + tonumber(ARGV[3]), ARGV[2])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""


def read_market(ledger, fallback):
    with ledger._connect() as connection:
        row = connection.execute(
            "SELECT body FROM snapshot_state WHERE ns=%s AND dataset='market'", (ledger.ns,)
        ).fetchone()
    if row:
        return json.loads(row[0])
    if fallback:
        try:
            return read_file(fallback)
        except FileNotFoundError:
            pass
    return None


def public_snapshot(market, codes=()):
    source = market or {}
    now = datetime.now(timezone.utc)
    observed = source.get("observed_at")
    age = (now - moment(observed)).total_seconds() if observed else None
    if age is None:
        freshness = "UNAVAILABLE"
    elif 0 <= age <= 90:
        freshness = "FRESH"
    elif 90 < age <= 900:
        freshness = "DELAYED"
    else:
        freshness = "EXPIRED"
    rows = source.get("quotes") or {}
    quotes = {code: rows[code] for code in codes if code in rows}
    ages = {code: max(0, (now - moment(quote["observed_at"])).total_seconds())
            for code, quote in quotes.items() if quote.get("observed_at")}
    return dict(schema_version="market_read_v1", revision=source.get("snapshot_id") or source.get("revision"),
                revision_seq=source.get("revision_seq"), trade_date=source.get("trade_date"),
                observed_at=observed, source_time_precision=source.get("source_time_precision", "DAY"),
                time_basis=source.get("time_basis", "SYSTEM_OBSERVATION"), source=source.get("source"),
                mode=source.get("mode"), freshness=freshness, age_seconds=age, quality=source.get("quality") or {},
                quotes=quotes, quote_ages_seconds=ages)


def _codes(query):
    if len(query) > 20000:
        raise APIError(422, "INVALID_MARKET_CODES")
    try:
        params = parse_qs(query, max_num_fields=4)
    except ValueError as error:
        raise APIError(422, "INVALID_MARKET_CODES") from error
    value = params.get("codes", [""])
    if len(value) != 1 or len(value[0]) > 6000:
        raise APIError(422, "INVALID_MARKET_CODES")
    codes = list(dict.fromkeys(filter(None, value[0].split(","))))
    if len(codes) > 500 or any(not MARKET_CODE.fullmatch(code) for code in codes):
        raise APIError(422, "INVALID_MARKET_CODES")
    return codes


def _cursor(value):
    return tuple(map(int, value.split("-")))


def _hello(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid hello")
    ticket = value.get("ticket")
    cursor = value.get("after_cursor", "0-0")
    topics = value.get("topics", DEFAULT_TOPICS)
    if (not isinstance(ticket, str) or not TICKET_PATTERN.fullmatch(ticket)
            or not isinstance(cursor, str) or not CURSOR.fullmatch(cursor)
            or not isinstance(topics, (list, tuple)) or not 1 <= len(topics) <= len(TOPICS)
            or any(not isinstance(topic, str) or topic not in TOPICS for topic in topics)):
        raise ValueError("invalid hello")
    return ticket, cursor, set(topics)


class MarketStream:
    def __init__(self, ledger, fallback, expected):
        self.ledger = ledger
        self.fallback = fallback
        self.stream = stream_key(ledger.ns)
        self.tickets = ledger.ns + ":market:ws-tickets:" + sha256(expected).hexdigest()

    def snapshot(self, codes=()):
        return public_snapshot(read_market(self.ledger, self.fallback), codes)

    def ticket(self):
        ticket = token_hex(32)
        with cache_client(self.ledger.settings) as cache:
            if not cache.eval(TICKETS, 1, self.tickets, "issue", ticket, TICKET_TTL):
                raise APIError(429, "MARKET_TICKETS_FULL")
        return {"ticket": ticket, "expires_in": TICKET_TTL}

    def _snapshot(self, cache, ws, cursor, *, force=False):
        with cache.pipeline(transaction=True) as pipe:
            first, last = pipe.xrange(self.stream, count=1).xrevrange(self.stream, count=1).execute()
        earliest = first[0][0].decode("ascii") if first else "0-0"
        latest = last[0][0].decode("ascii") if last else "0-0"
        reset = force or (cursor != "0-0" and not _cursor(earliest) <= _cursor(cursor) <= _cursor(latest))
        if reset or cursor == "0-0":
            cursor = latest
        ws.send({"type": "snapshot", "cursor": cursor, "reset": reset, "data": self.snapshot()})
        return cursor

    def serve(self, ws, stop):
        try:
            raw = ws.hello(stop)
            if raw is None:
                return
            try:
                ticket, cursor, topics = _hello(raw)
            except (ValueError, RecursionError):
                ws.close(1008)
                return
            with cache_client(self.ledger.settings) as cache:
                if not cache.eval(TICKETS, 1, self.tickets, "take", ticket, TICKET_TTL):
                    ws.close(1008)
                    return
                cursor = self._snapshot(cache, ws, cursor)
                last_sent = monotonic()
                expires = last_sent + 21600
                while not stop.is_set() and monotonic() < expires and ws.poll():
                    entries = cache.xread({self.stream: cursor}, count=50, block=1000)
                    if entries:
                        first = cache.xrange(self.stream, count=1)
                        if first and cursor != "0-0" and _cursor(cursor) < _cursor(first[0][0].decode("ascii")):
                            cursor = self._snapshot(cache, ws, cursor, force=True)
                            last_sent = monotonic()
                            continue
                    for _, messages in entries:
                        for identifier, fields in messages:
                            cursor = identifier.decode("ascii")
                            event = json.loads(fields[b"data"])
                            if event.get("topic") in topics:
                                ws.send({"type": "update", "cursor": cursor, "data": event})
                                last_sent = monotonic()
                    if monotonic() - last_sent >= HEARTBEAT:
                        last = cache.xrevrange(self.stream, count=1)
                        latest = last[0][0].decode("ascii") if last else "0-0"
                        if _cursor(cursor) > _cursor(latest):
                            cursor = self._snapshot(cache, ws, cursor, force=True)
                        else:
                            ws.send({"type": "heartbeat", "cursor": cursor})
                        last_sent = monotonic()
                ws.close(1001 if stop.is_set() else 1000)
        except (OSError, TimeoutError):
            pass
        except (RedisError, psycopg.Error, ValueError, KeyError, TypeError):
            ws.close(1011)


class MarketSocket:
    """Use the library's WebSocket parser with the existing HTTP socket."""

    def __init__(self, connection, protocol):
        self.connection = connection
        self.protocol = protocol

    def flush(self):
        for data in self.protocol.data_to_send():
            if data:
                self.connection.sendall(data)

    def send(self, message):
        if self.protocol.state == OPEN:
            self.protocol.send_text(encode(message).encode())
            self.flush()

    def close(self, code):
        if self.protocol.state == OPEN:
            self.protocol.send_close(code)
            try:
                self.flush()
            except OSError:
                pass

    def receive(self, timeout):
        if not select([self.connection], [], [], timeout)[0]:
            return []
        data = self.connection.recv(4096)
        if not data:
            raise ConnectionError("WebSocket peer disconnected")
        self.protocol.receive_data(data)
        self.flush()
        return self.protocol.events_received()

    def hello(self, stop):
        until = monotonic() + 10
        payload = bytearray()
        while not stop.is_set() and self.protocol.state == OPEN and monotonic() < until:
            for frame in self.receive(0.5):
                if frame.opcode == BINARY:
                    self.close(1008)
                    return None
                if frame.opcode in (TEXT, CONT):
                    payload.extend(frame.data)
                    if frame.fin:
                        return payload
        self.close(1008)
        return None

    def poll(self):
        if any(frame.opcode in (TEXT, BINARY, CONT) for frame in self.receive(0)):
            self.close(1008)
        return self.protocol.state == OPEN


def _reply(start_response, status, data):
    raw = encode(data).encode()
    headers = [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store"),
               ("Content-Length", str(len(raw)))]
    if status == 401:
        headers.append(("WWW-Authenticate", "Bearer"))
    start_response(f"{status} {HTTPStatus(status).phrase}", headers)
    return [raw]


def market_application(app, ledger, fallback, expected):
    stream = MarketStream(ledger, fallback, expected)

    def handle(env, start_response):
        method, path = env["REQUEST_METHOD"], env["PATH_INFO"]
        if path not in (SNAPSHOT, TICKET, WS):
            return app(env, start_response)
        try:
            if path == WS:
                raise APIError(426, "WEBSOCKET_UPGRADE_REQUIRED")
            if not authorized(env, expected):
                raise APIError(401, "AUTH_REQUIRED")
            if method == "GET" and path == SNAPSHOT:
                data = stream.snapshot(_codes(env.get("QUERY_STRING", "")))
                return _reply(start_response, 200, {**data, "producer": None})
            if method == "POST" and path == TICKET:
                return _reply(start_response, 200, stream.ticket())
            raise APIError(405, "METHOD_NOT_ALLOWED")
        except APIError as error:
            return _reply(start_response, error.status, {"error": error.code})
        except (OSError, ValueError, psycopg.Error, RedisError):
            return _reply(start_response, 503, {"error": "MARKET_UNAVAILABLE"})

    handle.market_stream = stream
    return handle


class WorkspaceServer(LocalServer):
    """Reserve the main HTTP request loop while limiting upgraded connections."""

    def __init__(self, *args, **kwargs):
        self.ws_slots = BoundedSemaphore(4)
        self.ws_stop = Event()
        self.ws_lock = Lock()
        self.ws_active = {}
        self.ws_handed_off = set()
        super().__init__(*args, **kwargs)

    def hand_off(self, connection, protocol):
        if self.ws_stop.is_set() or not self.ws_slots.acquire(blocking=False):
            return False
        thread = Thread(target=self._stream, args=(connection, protocol), name="market-websocket")
        with self.ws_lock:
            self.ws_active[connection] = thread
        self.ws_handed_off.add(connection)
        try:
            thread.start()
        except RuntimeError:
            self.ws_handed_off.remove(connection)
            with self.ws_lock:
                del self.ws_active[connection]
            self.ws_slots.release()
            return False
        return True

    def _stream(self, connection, protocol):
        try:
            connection.settimeout(3)
            ws = MarketSocket(connection, protocol)
            ws.flush()
            self.get_app().market_stream.serve(ws, self.ws_stop)
        except OSError:
            pass
        finally:
            super().shutdown_request(connection)
            with self.ws_lock:
                del self.ws_active[connection]
            self.ws_slots.release()

    def shutdown_request(self, request):
        if request in self.ws_handed_off:
            self.ws_handed_off.remove(request)
        else:
            super().shutdown_request(request)

    def server_close(self):
        self.ws_stop.set()
        super().server_close()
        with self.ws_lock:
            active = list(self.ws_active.items())
        for connection, _ in active:
            try:
                connection.shutdown(SHUT_RDWR)
            except OSError:
                pass
        for _, thread in active:
            thread.join()


class WorkspaceHandler(QuietHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        # wsgiref's default handler owns parsing and immediately invokes WSGI.
        # The upgrade must happen between those steps so HTTP keeps its normal lifecycle.
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline = self.request_version = self.command = ""
            self.send_error(414)
            return
        if not self.parse_request():
            return
        if self.path.partition("?")[0] == WS and self.headers.get("Upgrade"):
            self.upgrade()
            return
        handler = ServerHandler(self.rfile, self.wfile, self.get_stderr(), self.get_environ(), multithread=False)
        handler.request_handler = self
        handler.run(self.server.get_app())

    def upgrade(self):
        if self.path != WS or self.command != "GET" or self.request_version != "HTTP/1.1":
            self.send_error(400)
            return
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if len(hosts) != 1 or len(origins) > 1:
            self.send_error(400)
            return
        if origins:
            try:
                url = urlsplit(origins[0])
            except ValueError:
                self.send_error(403)
                return
            if (url.scheme not in ("http", "https") or url.netloc.lower() != hosts[0].lower()
                    or url.path or url.query or url.fragment):
                self.send_error(403)
                return
        protocol = ServerProtocol(max_size=MAX_FRAME)
        request = Request(self.path, Headers(self.headers.raw_items()))
        protocol.receive_data(request.serialize())
        events = protocol.events_received()
        if len(events) != 1 or not isinstance(events[0], Request):
            self.send_error(400)
            return
        response = protocol.accept(events[0])
        protocol.send_response(response)
        if response.status_code != 101:
            MarketSocket(self.connection, protocol).flush()
            return
        if not self.server.hand_off(self.connection, protocol):
            self.send_error(503)


def make_workspace_server(host, port, app):
    return make_server(host, port, app, server_class=WorkspaceServer, handler_class=WorkspaceHandler)
