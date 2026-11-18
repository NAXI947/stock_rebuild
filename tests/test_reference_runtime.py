"""Reference compatibility and dated calendar fallback, without provider I/O."""
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2.build_sources import stock_identity
from modules.strategy_hermes.stage2.export_calendar import export_calendar
from modules.strategy_hermes.stage2.reference_runtime import calendar_days, calendar_status, runtime_calendar

AT = datetime(2026, 11, 16, 8, 45, tzinfo=CHINA)


def fallback(**changes):
    return dict(schema_version="hotspot_trade_calendar_v1", source="quant.trade_calendar", exchange="SSE",
                generated_at=(AT-timedelta(days=3)).isoformat(), range_start="2026-11-13", range_end="2026-11-17",
                days={"2026-11-13": True, "2026-11-16": True, "2026-11-17": True}) | changes


def row(day, flag, exchange="SSE", wrapped=False):
    body = dict(exchange=exchange, cal_date=day, is_open=flag)
    return dict(entity_key=exchange+"|"+day, body=body) if wrapped else body


class Connection:
    def __init__(self, batches=(), error=None):
        self.batches, self.error, self.calls = batches, error, []

    def __enter__(self):
        if self.error:
            raise self.error
        return self

    def __exit__(self, *_):
        pass

    def transaction(self):
        return self

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        if "collected_at<=%s" in sql:
            result = sorted((item for item in self.batches if item[1] <= args[0]),
                            key=lambda item: (item[1], item[2]), reverse=True)[:args[1]]
            return SimpleNamespace(fetchall=lambda: result)
        if "batch_id = %s" in sql:
            result = next((item[:2] for item in self.batches if item[2] == args[0]), None)
            return SimpleNamespace(fetchone=lambda: result)
        return SimpleNamespace(fetchall=lambda: [])


def ledger(connection):
    return SimpleNamespace(_connect=lambda: connection)


class ReferenceRuntime(TestCase):
    def test_stock_identity_accepts_both_formats_and_unknown_market(self):
        listed = dict(symbol="600000", ts_code="600000.SH", market="主板", list_status="L")
        unknown = dict(symbol="600001", ts_code="600001.SH", list_status="L")
        retired = dict(symbol="600000", ts_code="600000.SZ", market="主板", list_status="D")
        data = [listed, dict(body=listed), dict(body=unknown), dict(body=retired), dict(body={"symbol": "bad"})]
        for encoded in (data, json.dumps(data)):
            conn = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: (encoded,)))
            identities, markets = stock_identity(conn, "fixture")
            self.assertEqual(dict(identities), {"600000": ["600000.SH"], "600001": ["600001.SH"]})
            self.assertEqual(markets, {"600000.SH": "主板"})

    def test_stored_observations_extend_file_and_close_previously_open_day(self):
        older = ([row("2026-11-16", True)], AT-timedelta(days=2), "old")
        newer = ([row("2026-11-16", False, wrapped=True), row("2027-01-04", True, wrapped=True)],
                 AT-timedelta(minutes=5), "new")
        conn = Connection([older, newer])
        with patch("modules.strategy_hermes.stage2.reference_runtime.read_file", return_value=fallback()):
            result = runtime_calendar(ledger(conn), "calendar.json", AT)
        self.assertEqual(result, ["2026-11-13", "2026-11-17", "2027-01-04"])
        self.assertEqual(conn.calls[0][0], "SET TRANSACTION READ ONLY")
        with patch("modules.strategy_hermes.stage2.reference_runtime.read_file", return_value=fallback()):
            status = calendar_status(ledger(conn), "calendar.json", AT)
        self.assertEqual(status["range_end"], "2027-01-04")
        self.assertEqual(status["open_day_count"], 3)
        self.assertEqual(status["source_batches"], ["old", "new"])

    def test_future_batch_and_future_file_do_not_leak_into_replay(self):
        old = ([row("2026-11-13", True)], AT-timedelta(days=4), "old")
        future = ([row("2026-11-13", False), row("2027-01-04", True)], AT+timedelta(minutes=1), "future")
        with patch("modules.strategy_hermes.stage2.reference_runtime.read_file",
                   return_value=fallback(generated_at=(AT+timedelta(hours=1)).isoformat())):
            result = runtime_calendar(ledger(Connection([old, future])), "calendar.json", AT)
        self.assertEqual(result, ["2026-11-13"])

    def test_older_database_record_cannot_override_newer_deployed_observation(self):
        batch = ([row("2026-11-16", False)], AT-timedelta(days=4), "old")
        with patch("modules.strategy_hermes.stage2.reference_runtime.read_file", return_value=fallback()):
            self.assertIn("2026-11-16", runtime_calendar(ledger(Connection([batch])), "calendar.json", AT))

    def test_database_failure_preserves_file_and_partial_rows_do_not_block(self):
        with patch("modules.strategy_hermes.stage2.reference_runtime.read_file", return_value=fallback()):
            self.assertEqual(runtime_calendar(ledger(Connection(error=OSError())), "calendar.json", AT),
                             ["2026-11-13", "2026-11-16", "2026-11-17"])
        records = [row("2027-01-04", True), row("2027-01-05", True, exchange="SZSE"),
                   row("broken", True), row("2027-01-06", "true"), row("2027-01-07", True, wrapped=True)]
        self.assertEqual(calendar_days(records), {"2027-01-04": True, "2027-01-07": True})
        with patch("modules.strategy_hermes.stage2.reference_runtime.read_file", side_effect=OSError()):
            result = runtime_calendar(ledger(Connection([(records, AT, "partial")])), "calendar.json", AT)
        self.assertEqual(result, ["2027-01-04", "2027-01-07"])

    def test_export_accepts_envelopes_and_reports_gaps_without_fabricating_days(self):
        records = [row("2027-01-04", True, wrapped=True), row("2027-01-07", True, wrapped=True),
                   row("2027-01-08", False, exchange="SZSE", wrapped=True)]
        result = export_calendar(ledger(Connection([(json.dumps(records), AT, "batch")])), "batch")
        self.assertEqual(result["days"], {"2027-01-04": True, "2027-01-07": True})
        self.assertEqual(result["missing_day_count"], 2)
        self.assertFalse(result["coverage_complete"])

