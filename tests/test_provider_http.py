"""Exercise provider transport against local HTTP servers without external egress."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from time import monotonic, sleep
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes.stage2.http_json import MAX_BYTES, USER_AGENT, ProviderHTTPError, post_json, get_json


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.path, dict(self.headers), payload))
        try:
            if self.path.startswith("/redirect/"):
                self.send_response(int(self.path.rsplit("/", 1)[1]))
                self.send_header("Location", self.server.redirect_target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/error":
                self.send_response(503)
                self.end_headers()
                return
            if self.path == "/slow":
                sleep(.3)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if self.path == "/large-length":
                self.send_header("Content-Length", str(MAX_BYTES + 1))
                self.end_headers()
                return
            if self.path == "/large-stream":
                self.end_headers()
                self.wfile.write(b" " * (MAX_BYTES + 1))
                return
            if self.path == "/truncated":
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return
            if self.path == "/drip":
                self.end_headers()
                for value in b'{"ok":true}':
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    sleep(.035)
                return
            body = (b"not json" if self.path == "/invalid" else b"[]" if self.path == "/array"
                    else b"\xff" if self.path == "/encoding" else json.dumps({
                        "received": json.loads(payload or b"{}"), "api_key": self.headers.get("x-api-key"),
                        "ua": self.headers.get("User-Agent"), "accept": self.headers.get("Accept"),
                        "content_type": self.headers.get("Content-Type"),
                    }, ensure_ascii=False).encode("utf-8"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST


@contextmanager
def server():
    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    instance.daemon_threads = True
    instance.requests = []
    instance.redirect_target = ""
    thread = Thread(target=instance.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield instance, f"http://127.0.0.1:{instance.server_port}"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


class ProviderHTTP(TestCase):
    def test_get_preserves_query_and_key_with_one_bounded_no_redirect_request(self):
        from http.client import HTTPConnection
        with server() as (site, url), patch("http.client.HTTPSConnection", HTTPConnection):
            secure = url.replace("http://", "https://")
            result = get_json(secure + "/ok?period=1m", headers=(("x-api-key", "fixture-key"),))
            self.assertEqual(result["api_key"], "fixture-key")
            self.assertEqual(result["ua"], USER_AGENT)
            self.assertEqual(site.requests[0][0], "/ok?period=1m")
            self.assertEqual(site.requests[0][2], b"")
            self.assertEqual(len(site.requests), 1)
            with self.assertRaisesRegex(ProviderHTTPError, "HTTP status 302"):
                get_json(secure + "/redirect/302")

    def test_one_post_preserves_utf8_payload_credentials_and_headers(self):
        with server() as (site, url), patch("subprocess.run", side_effect=AssertionError("no command")):
            payload = {"token": "fixture-secret", "value": "银行"}
            result = post_json(url + "/ok", payload, headers=(("x-api-key", "fixture-key"),), protocols="=http,https")
            self.assertEqual(result, dict(received=payload, api_key="fixture-key", ua=USER_AGENT,
                                          accept="application/json", content_type="application/json"))
            self.assertEqual(len(site.requests), 1)

    def test_redirects_never_forward_credentials_or_change_method(self):
        with server() as (target, target_url), server() as (origin, url):
            origin.redirect_target = target_url + "/ok"
            for status in (301, 302, 303, 307, 308):
                with self.subTest(status=status), self.assertRaisesRegex(ProviderHTTPError, f"HTTP status {status}"):
                    post_json(url + f"/redirect/{status}", {"token": "fixture-secret"},
                              headers=(("x-api-key", "fixture-key"),), protocols="=http")
            self.assertEqual(len(origin.requests), 5)
            self.assertEqual(target.requests, [])

    def test_response_size_is_bounded_with_and_without_content_length(self):
        with server() as (_, url):
            for path in ("/large-length", "/large-stream"):
                with self.subTest(path=path), self.assertRaisesRegex(ProviderHTTPError, "exceeded size bound"):
                    post_json(url + path, {}, protocols="=http")

    def test_status_json_encoding_and_truncation_errors_are_sanitized(self):
        with server() as (site, url):
            for path, message in (("/error", "HTTP status 503"), ("/invalid", "was not JSON"),
                                  ("/array", "was not JSON"), ("/encoding", "was not JSON"),
                                  ("/truncated", "was incomplete")):
                with self.subTest(path=path), self.assertRaisesRegex(ProviderHTTPError, message):
                    post_json(url + path, {"token": "fixture-secret"}, protocols="=http")
            self.assertEqual(len(site.requests), 5)

    def test_timeout_is_bounded_for_headers_and_a_slow_drip_body(self):
        with server() as (site, url):
            for path in ("/slow", "/drip"):
                started = monotonic()
                with self.subTest(path=path), self.assertRaisesRegex(ProviderHTTPError, "connection or timeout"):
                    post_json(url + path, {}, timeout=.09, protocols="=http")
                self.assertLess(monotonic() - started, .6)
            self.assertEqual(len(site.requests), 2)

    def test_protocol_and_header_validation_happen_before_network(self):
        with server() as (site, url):
            for call in (
                lambda: post_json(url + "/ok", {}),
                lambda: post_json("file:///tmp/key", {}, protocols="=http,https"),
                lambda: post_json(url + "/ok", {}, protocols="=http,file"),
                lambda: post_json(url + "/ok", {}, protocols="=http", timeout=0),
                lambda: post_json(url + "/ok", {}, protocols="=http", headers=(("x-api-key", "a\r\nb"),)),
                lambda: post_json(url + "/ok", {}, protocols="=http", headers=(("bad name", "a"),)),
            ):
                with self.assertRaises(ValueError):
                    call()
            self.assertEqual(site.requests, [])
