"""Bounded worker heartbeat decoding retains clock and boolean validation."""
from contextlib import contextmanager
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes import workspace_readiness as readiness

NOW = moment("2026-11-14T17:43:20+08:00")


def heartbeat():
    return {"observed_at": "2026-11-14T17:43:00+08:00", "hermes_schedule": True,
            "official_collection": True, "qq_push": True, "stock_research": True, "findesk_iopv": True}


def encode(body):
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class WorkerReadinessTest(TestCase):
    def read(self, raw):
        ledger = SimpleNamespace(settings=object(), ns="stock-v4:test:readiness")
        class Cache:
            def get(self, key):
                if key != readiness.worker_status_key(ledger.ns):
                    raise AssertionError("wrong heartbeat namespace")
                return raw
        @contextmanager
        def cache(settings):
            if settings is not ledger.settings:
                raise AssertionError("wrong cache environment")
            yield Cache()
        with patch.object(readiness, "cache_client", side_effect=cache):
            return readiness._read_worker_status(ledger, NOW)

    def test_realistic_extended_heartbeat_over_one_kib_remains_available(self):
        body = heartbeat()
        body.update(
            etf_statistics={"evaluated_products": 1, "history_as_of": "2026-11-13", "items": [
                {"api": api, "code": "512800.SH", "error": "ProbeError",
                 "reason": "request already attempted in this logical batch", "status": "UNAVAILABLE"}
                for api in ("fund_daily", "fund_adj")], "new_requests": 0, "omitted_products": 0, "status": "PARTIAL"},
            macro_background={"batch": "a" * 64, "collected_at": "2026-11-14T17:38:54+08:00", "endpoint": "018",
                              "parts": {endpoint: {"batch": endpoint + "b" * 61,
                                                    "collected_at": "2026-11-14T17:38:54+08:00",
                                                    "rows": rows, "state": "STORED"}
                                        for endpoint, rows in (("018", 1), ("072", 18))}, "rows": 1, "state": "STORED"},
            market_turnover={"state": "UP_TO_DATE", "trade_date": "2026-11-13"},
            temporary={"managed_bytes": 4096, "managed_entries": 2, "usage_error": None,
                       "last_cleanup": {"removed": 0}, "unlisted_detail": "not public"})
        raw = encode(body)
        self.assertGreater(len(raw), 1024)
        self.assertLess(len(raw), readiness.WORKER_STATUS_MAX_BYTES)
        result = self.read(raw)
        self.assertIsNotNone(result)
        self.assertTrue(all(result[key] is True for key in (*readiness.WORKER_SWITCHES, "stock_research", "findesk_iopv")))
        self.assertEqual(result["observed_at"], body["observed_at"])
        self.assertEqual(set(result["temporary"]), {"managed_bytes", "managed_entries", "usage_error", "last_cleanup"})
        self.assertNotIn("macro_background", result)
        self.assertNotIn("etf_statistics", result)

    def test_size_boundary_is_accepted_and_next_byte_is_rejected_before_json(self):
        raw = encode(heartbeat())
        raw += b" " * (readiness.WORKER_STATUS_MAX_BYTES - len(raw))
        self.assertIsNotNone(self.read(raw))
        with patch.object(readiness.json, "loads", side_effect=AssertionError("oversized JSON must not be parsed")):
            self.assertIsNone(self.read(raw + b" "))

    def test_utf8_byte_bound_is_enforced(self):
        body = heartbeat() | {"extra": "数" * 6000}
        raw = encode(body)
        self.assertLess(len(raw.decode("utf-8")), readiness.WORKER_STATUS_MAX_BYTES)
        self.assertGreater(len(raw), readiness.WORKER_STATUS_MAX_BYTES)
        self.assertIsNone(self.read(raw))

    def test_old_and_future_heartbeats_still_fail(self):
        for stamp in (NOW - timedelta(seconds=readiness.WORKER_STATUS_TTL + 1), NOW + timedelta(seconds=1)):
            with self.subTest(stamp=stamp):
                self.assertIsNone(self.read(encode(heartbeat() | {"observed_at": stamp.isoformat()})))

    def test_required_switches_still_require_actual_booleans(self):
        for key in readiness.WORKER_SWITCHES:
            for value in ("true", 1, None):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(self.read(encode(heartbeat() | {key: value})))
            body = heartbeat()
            del body[key]
            self.assertIsNone(self.read(encode(body)))

    def test_malformed_or_missing_heartbeat_still_fails(self):
        for raw in (None, b"", b"not-json", b"[]", b"{}", encode(heartbeat() | {"observed_at": "2026-11-14T17:43:00"})):
            with self.subTest(raw=raw):
                self.assertIsNone(self.read(raw))
