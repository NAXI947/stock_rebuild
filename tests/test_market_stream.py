"""Exercise the shared stream through the same HTTP and WebSocket listener."""

from contextlib import ExitStack
from dataclasses import replace
import json
import os
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from modules.strategy_hermes.market_stream import BASE, make_workspace_server
from modules.strategy_hermes.stage2.build_sources import publish_snapshot
from modules.strategy_hermes.stage2.inputs import cache_client
from modules.strategy_hermes.stage2.market_notices import stream_key
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings
from modules.strategy_hermes.workspace_web import application

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")
TOKEN = "q" * 40
AT = "2026-11-13T09:35:00+08:00"
CODE = "512480.SH"


@unittest.skipUnless(ENV, "set STAGE2_TEST_ENV_FILE to verify PostgreSQL and Redis")
class MarketStreamTest(unittest.TestCase):
    def setUp(self):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, "test")
        self.ns = settings.redis_prefix + "ws-test:" + uuid4().hex
        self.ledger = PostgresLedger(replace(settings, namespace=self.ns), "FROZEN")
        self.ledger.migrate()
        self.cache = cache_client(settings)
        self.addCleanup(self.cleanup_data)
        self.server = make_workspace_server("127.0.0.1", 0, application(self.ledger, TOKEN))
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.start()
        self.addCleanup(self.close_server)
        self.address = f"127.0.0.1:{self.server.server_port}"

    def cleanup_data(self):
        with self.ledger._connect() as connection:
            connection.execute("DELETE FROM snapshot_state WHERE ns=%s", (self.ns,))
        keys = list(self.cache.scan_iter(self.ns + ":*"))
        if keys:
            self.cache.delete(*keys)
        self.cache.close()

    def close_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, path, *, method="GET", auth=True):
        headers = {"Authorization": "Bearer " + TOKEN} if auth else {}
        request = Request("http://" + self.address + path, method=method, headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def connect(self, origin=None):
        return connect("ws://" + self.address + BASE + "/ws",
                       origin=origin or "http://" + self.address, proxy=None, open_timeout=3, close_timeout=1)

    def ticket(self):
        status, payload = self.request(BASE + "/ws-ticket", method="POST")
        self.assertEqual((status, payload["expires_in"]), (200, 30))
        return payload["ticket"]

    def publish(self, revision, *, at=AT):
        snapshot = dict(schema_version="hotspot_market_snapshot_v1", snapshot_id=revision, observed_at=at,
                        trade_date=at[:10], source="frozen:test", quotes={CODE: dict(observed_at=at, price_cny="0.81")},
                        quality={"etf_quote_count": 1})
        key = self.ns + ":source:market"
        publish_snapshot(self.ledger, self.cache, snapshot, key=key)

    def test_auth_origin_ticket_reuse_and_snapshot_contract(self):
        self.publish("original")
        self.assertEqual(self.request(BASE + "/snapshot", auth=False)[0], 401)
        self.assertEqual(self.request(BASE + "/ws-ticket", method="POST", auth=False)[0], 401)
        self.assertEqual(self.request(BASE + "/snapshot?codes=bogus")[0], 422)
        status, payload = self.request(BASE + "/snapshot?codes=512480.SH,830799.BJ")
        self.assertEqual((status, payload["schema_version"], payload["revision"]), (200, "market_read_v1", "original"))
        self.assertEqual(payload["quotes"][CODE]["observed_at"], AT)
        self.assertEqual(self.request(BASE + "/ws")[0], 426)
        with self.assertRaises(InvalidStatus) as rejected:
            self.connect(origin="http://unrelated.invalid")
        self.assertEqual(rejected.exception.response.status_code, 403)
        ticket = self.ticket()
        with self.connect() as first:
            first.send(json.dumps({"ticket": ticket, "topics": ["market"]}))
            snapshot = json.loads(first.recv(timeout=5))
            self.assertEqual(snapshot["data"]["observed_at"], AT)
            self.assertEqual(self.request("/health")[0], 200)
            with self.connect() as second:
                second.send(json.dumps({"ticket": ticket}))
                with self.assertRaises(ConnectionClosed) as closed:
                    second.recv(timeout=5)
                self.assertEqual(closed.exception.rcvd.code, 1008)

    def test_notices_are_deduplicated_resumable_and_reset_from_database(self):
        self.publish("original")
        with self.connect() as ws:
            ws.send(json.dumps({"ticket": self.ticket(), "topics": ["market"]}))
            first = json.loads(ws.recv(timeout=5))
            self.assertEqual(first["type"], "snapshot")
            self.publish("revised")
            self.publish("revised")
            self.publish("old", at="2026-11-12T15:00:00+08:00")
            updated = json.loads(ws.recv(timeout=5))
            self.assertEqual((updated["type"], updated["data"]["topic"], updated["data"]["revision"]),
                             ("update", "market", "revised"))
        self.assertEqual(self.cache.xlen(stream_key(self.ns)), 2)
        with self.connect() as ws:
            ws.send(json.dumps({"ticket": self.ticket(), "after_cursor": first["cursor"]}))
            initial, replay = (json.loads(ws.recv(timeout=5)) for _ in range(2))
            self.assertEqual((initial["cursor"], initial["reset"]), (first["cursor"], False))
            self.assertEqual(replay, updated)
        self.cache.delete(stream_key(self.ns))
        with self.connect() as ws:
            ws.send(json.dumps({"ticket": self.ticket(), "after_cursor": updated["cursor"]}))
            reset = json.loads(ws.recv(timeout=5))
        self.assertTrue(reset["reset"])
        self.assertEqual((reset["cursor"], reset["data"]["revision"], reset["data"]["revision_seq"]),
                         ("0-0", "revised", 1))

    def test_capacity_keeps_http_available_and_expired_ticket_is_rejected(self):
        expired = self.ticket()
        self.cache.zadd(self.server.get_app().market_stream.tickets, {expired: 0}, xx=True)
        with self.connect() as ws:
            ws.send(json.dumps({"ticket": expired}))
            with self.assertRaises(ConnectionClosed) as closed:
                ws.recv(timeout=5)
            self.assertEqual(closed.exception.rcvd.code, 1008)
        with ExitStack() as sockets:
            for _ in range(4):
                connection = sockets.enter_context(self.connect())
                connection.send(json.dumps({"ticket": self.ticket()}))
                self.assertEqual(json.loads(connection.recv(timeout=5))["type"], "snapshot")
            with self.assertRaises(InvalidStatus) as full:
                self.connect()
            self.assertEqual(full.exception.response.status_code, 503)
            self.assertEqual(self.request("/health")[0], 200)
