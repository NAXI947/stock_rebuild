"""Authenticated WSGI command endpoints for the manual backtest queue."""
import argparse
from contextlib import ExitStack
from dataclasses import replace
from http import HTTPStatus
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import sys
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import psycopg

from shared_kernel.temp_manager import BudgetError, CHUNK

from .adapter import unique_object
from .ledger import encode
from .manual_executor import MAX_OUTPUT_BYTES
from .manual_queue import DraftNotFound, ManualQueue, MAX_INPUT_BYTES, QueueFull
from .temp_maintenance import TempMaintenance
from .stage2.postgres import PostgresLedger
from .stage2.settings import Settings

BASE = "/api/v2/backtests"
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


def read_token(path):
    path = Path(path)
    if (not path.is_absolute() or path.resolve() != path or not path.is_file()
            or path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid()):
        raise ValueError("API token requires a private file owned by the service")
    with path.open(encoding="ascii") as file:
        token = file.read(130).strip()
    if not TOKEN.fullmatch(token):
        raise ValueError("invalid API token format")
    return token


class APIError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def _body(env, *, allowed):
    if env.get("HTTP_TRANSFER_ENCODING"):
        raise APIError(400,"UNSUPPORTED_TRANSFER_ENCODING")
    length = env.get("CONTENT_LENGTH", "")
    if length == "0" or not length:
        return {}
    if not re.fullmatch(r"[0-9]{1,12}", length):
        raise APIError(400,"INVALID_LENGTH")
    size = int(length)
    if size > MAX_INPUT_BYTES:
        raise APIError(413,"BODY_TOO_LARGE")
    if env.get("CONTENT_TYPE","").split(";",1)[0].strip().lower() != "application/json":
        raise APIError(415,"JSON_REQUIRED")
    raw = env["wsgi.input"].read(size)
    if len(raw) != size:
        raise APIError(400,"INCOMPLETE_BODY")
    value = json.loads(raw,object_pairs_hook=unique_object)
    if not isinstance(value,dict) or set(value)-allowed:
        raise APIError(400,"INVALID_FIELDS")
    return value


def _route(queue, env):
    method, path = env["REQUEST_METHOD"], env["PATH_INFO"]
    if path == BASE + "/manual" and method == "POST":
        body = _body(env,allowed={"bundle","parent_id"})
        identifier = queue.create(body["bundle"],body.get("parent_id"))
        return 201,{"id":identifier}
    if not path.startswith(BASE + "/"):
        raise APIError(404,"NOT_FOUND")
    parts = path[len(BASE)+1:].split("/")
    identifier = parts[0]
    if len(parts) == 1 and method == "GET":
        return 200,queue.get(identifier)
    if len(parts) != 2 or method != "POST":
        raise APIError(404,"NOT_FOUND")
    action = parts[1]
    if action == "run":
        body = _body(env,allowed={"retry"})
        job = queue.submit(identifier,retry=body.get("retry",False))
        return (202 if job["status"] in ("QUEUED","RUNNING") else 200),{"id":identifier,"job":job}
    if action == "cancel":
        _body(env,allowed=set())
        job = queue.cancel(identifier)
        return (202 if job and job["status"] == "RUNNING" else 200),{"id":identifier,"job":job}
    if action == "clone":
        body = _body(env,allowed={"request_id"})
        return 201,{"id":queue.clone(identifier,body["request_id"])}
    raise APIError(404,"NOT_FOUND")


def authorized(env, expected):
    supplied = env.get("HTTP_AUTHORIZATION","").encode("utf-8")
    return len(supplied) <= 135 and secrets.compare_digest(supplied,expected)


def _download(queue, identifier, manager, headers, start_response):
    saved = queue.get(identifier)
    result = (saved["job"] or {}).get("result")
    if result is None:
        raise APIError(409, "BACKTEST_RESULT_UNAVAILABLE")
    raw = encode({"id":identifier, "result":result}).encode()
    if len(raw) > MAX_OUTPUT_BYTES:
        raise APIError(413, "BACKTEST_EXPORT_TOO_LARGE")

    class Download:
        def __iter__(self):
            return iter(lambda:stream.read(CHUNK),b"")

        def close(self):
            resources.close()

    with ExitStack() as stack:
        work = stack.enter_context(manager.task())
        path = work.stage("result.json",BytesIO(raw))
        stream = stack.enter_context(path.open("rb"))
        start_response("200 OK",headers+[
            ("Content-Length",str(len(raw))),
            ("Content-Disposition",f'attachment; filename="{identifier}.json"'),
        ])
        resources = stack.pop_all()
    return Download()


def application(queue, token, *, paused=lambda: False, export_manager=None):
    if not TOKEN.fullmatch(token):
        raise ValueError("invalid API token format")
    expected = ("Bearer "+token).encode("ascii")

    def handle(env, start_response):
        headers = [("Content-Type","application/json; charset=utf-8"),("Cache-Control","no-store")]
        try:
            if not authorized(env,expected):
                raise APIError(401,"AUTH_REQUIRED")
            if env["REQUEST_METHOD"] == "GET" and env["PATH_INFO"].endswith("/download"):
                parts = env["PATH_INFO"][len(BASE)+1:].split("/")
                if len(parts) != 2 or parts[1] != "download":
                    raise APIError(404,"NOT_FOUND")
                identifier = parts[0]
                if export_manager is None:
                    raise APIError(503,"BACKTEST_EXPORT_UNAVAILABLE")
                return _download(queue,identifier,export_manager,headers,start_response)
            if env["REQUEST_METHOD"] == "POST" and paused():
                raise APIError(503,"RELEASE_PAUSED")
            status, body = _route(queue,env)
        except APIError as exc:
            status, body = exc.status, {"error":exc.code}
        except DraftNotFound:
            status, body = 404, {"error":"NOT_FOUND"}
        except BudgetError:
            status, body = 507, {"error":"TEMP_STORAGE_LIMIT"}
        except QueueFull:
            status, body = 429, {"error":"QUEUE_FULL"}
            headers.append(("Retry-After","10"))
        except (KeyError, ValueError, TypeError, UnicodeError, RecursionError):
            status, body = 400, {"error":"INVALID_REQUEST"}
        except (psycopg.Error, OSError) as exc:
            from shared_kernel.diagnostics import record
            status, body = 503, record("backtest", "BACKTEST_UNAVAILABLE", exc)
        if status == 401:
            headers.append(("WWW-Authenticate","Bearer"))
        raw = encode(body).encode()
        start_response(f"{status} {HTTPStatus(status).phrase}",headers+[("Content-Length",str(len(raw)))])
        return [raw]

    return handle


class LocalServer(WSGIServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--namespace")
    parser.add_argument("--port",type=int,default=18083)
    args = parser.parse_args(argv)
    try:
        settings = Settings.read(args.env_file)
        if args.namespace:
            if not args.namespace.startswith(settings.redis_prefix) or len(args.namespace) > 120:
                raise ValueError("manual API namespace outside the configured environment")
            settings = replace(settings,namespace=args.namespace)
        origin = "FROZEN" if settings.env == "test" else "LIVE"
        queue = ManualQueue(PostgresLedger(settings,origin))
        maintenance = TempMaintenance.from_environment()
        app = application(queue,read_token(args.token_file),export_manager=maintenance.manager if maintenance else None)
        with make_server("127.0.0.1",args.port,app,server_class=LocalServer,handler_class=QuietHandler) as server:
            server.timeout = 0.5
            while True:
                if maintenance:
                    maintenance.tick()
                server.handle_request()
    except KeyboardInterrupt:
        return 0
    except (ValueError, KeyError, OSError) as exc:
        print(f"manual API: {type(exc).__name__}",file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
