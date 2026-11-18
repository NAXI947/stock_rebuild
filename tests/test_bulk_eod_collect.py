"""Bulk collector contracts against PostgreSQL; HTTP is always replaced by fixtures."""
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg

from modules.strategy_hermes.bulk_eod_collect import (
    BulkTushare, collect_all, collect_day, PAGE_SIZE, MAX_PAGES, DAILY_APIS, _collection_plan,
)
from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")
HTTP = "modules.strategy_hermes.bulk_eod_collect.post_json"


def client():
    value = BulkTushare.__new__(BulkTushare)
    value.config = dict(token="mock-" + uuid4().hex, base_url="https://fixtures.invalid", timeout_seconds=1)
    return value


def stock_rows(api, day, start=0, count=1):
    result = []
    for i in range(start, start + count):
        row = dict(ts_code=f"{600000+i:06d}.SH", trade_date=day.strftime("%Y%m%d"))
        row.update({"daily": dict(open="10", high="12", low="9", close="11", pre_close="10",
                                 vol="123", amount="45", pct_chg="10"),
                    "adj_factor": dict(adj_factor="1.25"),
                    "daily_basic": dict(turnover_rate="2", volume_ratio="1.5", circ_mv="123"),
                    "cyq_perf": dict(cost_85pct="9.8"),
                    "weekly": dict(close="11", vol="123", amount="45")}[api])
        result.append(row)
    return result


def response(api, rows, fields=None):
    columns = list(fields or client().fields(api))
    return dict(code=0, data=dict(fields=columns, items=[[row.get(key) for key in columns] for row in rows]))


class BulkEodPlanTest(unittest.TestCase):
    def calendar(self, first, last):
        start = first-timedelta(days=280)
        end = last+timedelta(days=6-last.weekday())
        return {(start+timedelta(days=i)).isoformat(): (start+timedelta(days=i)).weekday() < 5
                for i in range((end-start).days+1)}

    def test_single_score_day_keeps_full_280_day_warmup_and_40_completed_weeks(self):
        day = date(2001, 11, 16)
        planned = _collection_plan(self.calendar(day, day), day, day)
        expected = [day-timedelta(days=i) for i in reversed(range(281)) if (day-timedelta(days=i)).weekday() < 5]
        for api in DAILY_APIS:
            self.assertEqual([when for name, when in planned if name == api], expected)
        weekly = [when for name, when in planned if name == "weekly"]
        self.assertEqual(weekly, [day-timedelta(weeks=i) for i in reversed(range(40))])
        self.assertEqual(len(expected), 201)
        self.assertEqual(len(planned), 201*4+40)

    def test_suspended_stock_has_eighty_actual_bars_inside_full_warmup(self):
        day = date(2001, 11, 16)
        planned = _collection_plan(self.calendar(day, day), day, day)
        market_days = [when for api, when in planned if api == "daily"]
        suspended = {date(2001, 10, 30), date(2001, 11, 13)}
        actual_bars = [when for when in market_days if when not in suspended][-80:]
        self.assertEqual(len(actual_bars), 80)
        self.assertEqual(len([when for when in market_days[-80:] if when not in suspended]), 78)
        self.assertLess(actual_bars[0], market_days[-80])
        for api in DAILY_APIS:
            self.assertTrue(set(actual_bars).issubset({when for name, when in planned if name == api}))

    def test_plan_excludes_dates_outside_the_configured_warmup_and_target_range(self):
        day = date(2001, 11, 16)
        calendar = self.calendar(day, day)
        calendar[str(day-timedelta(days=281))] = True
        calendar[str(day+timedelta(days=3))] = True
        planned = _collection_plan(calendar, day, day)
        self.assertTrue(all(day-timedelta(days=280) <= when <= day for _, when in planned))
        self.assertEqual(len(planned), 201*4+40)

    def test_multiple_targets_extend_the_first_window_and_respect_holiday_calendar(self):
        first, last = date(2026, 11, 9), date(2026, 11, 13)
        calendar = self.calendar(first, last)
        calendar["2026-11-11"] = False
        calendar["2026-11-13"] = False
        planned = _collection_plan(calendar, first, last)
        daily = [when for api, when in planned if api == "daily"]
        self.assertEqual(len([when for when in daily if when <= first]), 201)
        self.assertEqual(daily[-3:], [first, date(2026, 11, 10), date(2026, 11, 12)])
        self.assertEqual(len(daily), 203)
        self.assertEqual([when for api, when in planned if api == "weekly"][-1], date(2026, 11, 12))
        self.assertTrue(all(calendar[when.isoformat()] for _, when in planned))

    def test_partial_calendar_does_not_invent_sessions_or_completed_weeks(self):
        first, last = date(2026, 11, 9), date(2026, 11, 12)
        calendar = self.calendar(first, last)
        del calendar["2026-11-08"]
        planned = _collection_plan(calendar, first, last)
        weekly = [when for api, when in planned if api == "weekly"]
        self.assertNotIn(date(2026, 11, 6), weekly)
        self.assertNotIn(date(2026, 11, 13), weekly)
        self.assertEqual(_collection_plan({}, first, last), [])

    def test_collection_progress_counts_only_planned_dates_and_units(self):
        first = last = date(2001, 11, 16)
        calendar = self.calendar(first, last)
        checkpoints, progress = Mock(), []
        module = "modules.strategy_hermes.bulk_eod_collect"
        with patch(module+".BulkTushare", return_value=object()), \
             patch(module+"._collect_calendar", return_value=calendar), \
             patch(module+".collect_day", return_value=dict(cached_pages=1)) as collect, \
             patch("modules.strategy_hermes.left_right_backfill_collectors.collect_boards"):
            result = collect_all(object(), dict(date_from=str(first), date_to=str(last)), object(), object(),
                                 Mock(), checkpoints, lambda **fields: progress.append(fields))
        self.assertEqual(result, [])
        self.assertEqual(collect.call_count, 844)
        actual_days = {call.args[3] for call in collect.call_args_list}
        final = progress[-1]
        self.assertEqual((final["completed_days"], final["total_days"]), (len(actual_days), len(actual_days)))
        self.assertEqual((final["completed_units"], final["total_units"], final["cached_pages"]), (844, 844, 844))
        self.assertEqual((final["daily_days"], final["weekly_days"]), (201, 40))
        self.assertEqual(final["daily_date_to"], str(last))
        self.assertEqual(final["daily_date_from"], min(calendar))


@unittest.skipUnless(ENV, "requires isolated PostgreSQL STAGE2_TEST_ENV_FILE")
class BulkEodStorageTest(unittest.TestCase):
    def setUp(self):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, "test")
        self.store = PostgresLedger(replace(settings, namespace=settings.redis_prefix + "bulk-test:" + uuid4().hex), "FROZEN")
        self.store.migrate()
        self.client, self.calls = client(), []
        self.day = date(2001, 1, 5)
        monday = self.day - timedelta(days=self.day.weekday())
        self.calendar = {(monday + timedelta(days=i)).isoformat(): i < 5 for i in range(7)}
        self.addCleanup(self.clean)

    def clean(self):
        keys = list({self.client.identity(api) for api in ("trade_cal", "daily", "adj_factor", "daily_basic", "cyq_perf", "weekly")})
        with self.store._connect() as conn, conn.transaction():
            batches = [row[0] for row in conn.execute("SELECT batch_id FROM supplier_tushare_eod_runs WHERE source_key=ANY(%s)", (keys,))]
            batches += [row[0] for row in conn.execute("SELECT batch_id FROM supplier_batches WHERE requested_scope->>'source_key'=ANY(%s)", (keys,))]
            conn.execute("DELETE FROM supplier_daily_bars WHERE batch_id=ANY(%s)", (batches,))
            conn.execute("DELETE FROM supplier_trade_calendar WHERE batch_id=ANY(%s)", (batches,))
            conn.execute("DELETE FROM supplier_lr_features WHERE batch_id=ANY(%s)", (batches,))
            conn.execute("DELETE FROM supplier_tushare_eod_runs WHERE source_key=ANY(%s)", (keys,))
            conn.execute("DELETE FROM supplier_tushare_pages WHERE source_key=ANY(%s)", (keys,))
            conn.execute("DELETE FROM supplier_batches WHERE batch_id=ANY(%s)", (batches,))
            conn.execute("DELETE FROM supplier_market_batches WHERE batch_id=ANY(%s)", (batches,))

    def request(self, source, scope, operation):
        self.calls.append((source, scope))
        try:
            return dict(status="SAVED", receipt=operation())
        except (ValueError, OSError) as exc:
            return dict(status="FAILED", error_code=type(exc).__name__)

    def collect(self, api="daily"):
        return collect_day(self.store, self.client, api, self.day, self.calendar, self.request, lambda: None)

    def count(self, table, api="daily"):
        with self.store._connect() as conn:
            return conn.execute(f"SELECT count(*) FROM {table} WHERE source_key=%s AND api=%s",
                                (self.client.identity(api), api)).fetchone()[0]

    def test_more_than_3000_rows_publish_atomically_and_complete_range_reuses_database(self):
        self.assertEqual((PAGE_SIZE, MAX_PAGES), (3000, 10))
        def post(_url, payload, **_kwargs):
            scope = payload["params"]
            self.assertEqual(set(scope), {"trade_date", "limit", "offset"})
            offset = scope["offset"]
            return response("daily", stock_rows("daily", self.day, offset, min(3001-offset, PAGE_SIZE)))
        with patch(HTTP, side_effect=post) as http:
            receipt = self.collect()
        self.assertEqual((receipt["rows"], http.call_count), (3001, 2))
        with self.store._connect() as conn:
            row = conn.execute("SELECT count(*),min(volume_shares),min(amount_cny),min(return_ratio) FROM supplier_daily_bars WHERE batch_id=%s",
                               (receipt["batch"],)).fetchone()
            scope, raw, normalized = conn.execute("SELECT requested_scope,raw_rows,normalized FROM supplier_batches WHERE batch_id=%s",
                                                (receipt["batch"],)).fetchone()
        self.assertEqual(tuple(str(v) for v in row), ("3001", "12300", "45000", "0.1"))
        self.assertEqual(json.loads(scope)["universe"], "ALL")
        self.assertEqual((len(json.loads(raw)), len(json.loads(normalized))), (3001, 3001))
        with patch(HTTP, side_effect=AssertionError("complete data must be reused")):
            self.assertEqual(self.collect()["status"], "ALREADY_STORED")
        self.assertEqual(len(self.calls), 2)

    def test_failed_second_page_next_job_only_requests_failed_page(self):
        def first(_url, payload, **_kwargs):
            if payload["params"]["offset"]:
                raise OSError("mock failed page")
            return response("daily", stock_rows("daily", self.day, count=3000))
        with patch(HTTP, side_effect=first), self.assertRaisesRegex(ValueError, "request failed"):
            self.collect()
        self.assertEqual(self.count("supplier_tushare_pages"), 1)
        self.assertEqual(self.count("supplier_tushare_eod_runs"), 0)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM supplier_daily_bars WHERE trade_date=%s", (self.day,)).fetchone()[0], 0)
            cached_at = conn.execute("""UPDATE supplier_tushare_pages
                SET collected_at=clock_timestamp()-interval '2 hours'
                WHERE source_key=%s AND api='daily' RETURNING collected_at""",
                (self.client.identity("daily"),)).fetchone()[0]
        def retry(_url, payload, **_kwargs):
            self.assertEqual(payload["params"]["offset"], 3000)
            return response("daily", stock_rows("daily", self.day, 3000, 2))
        self.calls.clear()
        with patch(HTTP, side_effect=retry) as http:
            receipt = self.collect()
            self.assertEqual(receipt["rows"], 3002)
        self.assertEqual((http.call_count, len(self.calls)), (1, 1))
        with self.store._connect() as conn:
            page_times = [row[0] for row in conn.execute("""SELECT collected_at FROM supplier_tushare_pages
                WHERE source_key=%s AND api='daily' ORDER BY (scope->>'offset')::integer""",
                (self.client.identity("daily"),))]
            batch_at = conn.execute("SELECT collected_at FROM supplier_batches WHERE batch_id=%s",
                                    (receipt["batch"],)).fetchone()[0]
        self.assertEqual(page_times[0], cached_at)
        self.assertGreater(page_times[1], cached_at)
        self.assertEqual(batch_at, max(page_times))

    def test_business_rollback_preserves_successful_pages(self):
        with patch(HTTP, return_value=response("daily", stock_rows("daily", self.day))), \
                patch("modules.strategy_hermes.bulk_eod_collect._publish", side_effect=RuntimeError("business rollback")), \
                self.assertRaises(RuntimeError):
            self.collect()
        self.assertEqual(self.count("supplier_tushare_pages"), 1)
        with patch(HTTP, side_effect=AssertionError("page should survive business rollback")):
            self.assertEqual(self.collect()["rows"], 1)

    def test_database_constraint_rollback_retains_pages_without_partial_business_rows(self):
        from modules.strategy_hermes.bulk_eod_collect import _normalized
        raw = stock_rows("daily", self.day)
        invalid = _normalized("daily", raw, self.day, self.calendar)
        invalid[0]["volume_shares"] = "-1"
        with patch(HTTP, return_value=response("daily", raw)), \
                patch("modules.strategy_hermes.bulk_eod_collect._normalized", return_value=invalid), \
                self.assertRaises(psycopg.errors.CheckViolation):
            self.collect()
        self.assertEqual(self.count("supplier_tushare_pages"), 1)
        self.assertEqual(self.count("supplier_tushare_eod_runs"), 0)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM supplier_batches WHERE requested_scope->>'source_key'=%s",
                                          (self.client.identity("daily"),)).fetchone()[0], 0)
        with patch(HTTP, side_effect=AssertionError("database rollback must preserve the successful page")):
            self.assertEqual(self.collect()["rows"], 1)

    def test_field_changes_repeated_pages_and_wrong_date_never_publish(self):
        for kind in ("fields", "repeat", "date"):
            with self.subTest(kind=kind):
                self.clean()
                def post(_url, payload, **_kwargs):
                    if payload["params"]["offset"] == 0:
                        return response("daily", stock_rows("daily", self.day, count=3000))
                    if kind == "repeat":
                        return response("daily", stock_rows("daily", self.day, count=3000))
                    if kind == "date":
                        return response("daily", stock_rows("daily", self.day + timedelta(days=1), 3000))
                    return response("daily", stock_rows("daily", self.day, 3000), list(reversed(self.client.fields("daily"))))
                with patch(HTTP, side_effect=post), self.assertRaises(ValueError):
                    self.collect()
                self.assertEqual(self.count("supplier_tushare_eod_runs"), 0)
                self.assertEqual(self.count("supplier_tushare_pages"), 1)

    def test_full_last_page_is_truncation_not_success(self):
        def post(_url, payload, **_kwargs):
            return response("daily", stock_rows("daily", self.day, payload["params"]["offset"], 3000))
        with patch(HTTP, side_effect=post), patch("modules.strategy_hermes.bulk_eod_collect.MAX_PAGES", 2), \
                self.assertRaisesRegex(ValueError, "pagination limit"):
            self.collect()
        self.assertEqual(self.count("supplier_tushare_eod_runs"), 0)

    def test_empty_final_page_is_cached_and_nonempty_dataset_published(self):
        def post(_url, payload, **_kwargs):
            offset = payload["params"]["offset"]
            return response("daily", stock_rows("daily", self.day, count=3000) if offset == 0 else [])
        with patch(HTTP, side_effect=post):
            self.assertEqual(self.collect()["rows"], 3000)
        self.assertEqual(self.count("supplier_tushare_pages"), 2)

    def test_features_keep_original_units_and_weekly_completion(self):
        expected = {"adj_factor": {"adj_factor": "1.25"},
                    "daily_basic": {"turnover_ratio": "0.02", "volume_ratio": "1.5", "circ_mv_cny": "1230000"},
                    "cyq_perf": {"cost85": "9.8"},
                    "weekly": {"trade_date": self.day.isoformat(), "close": "11", "volume_shares": "123",
                               "amount_cny": "45", "complete": True}}
        for api, wanted in expected.items():
            with patch(HTTP, return_value=response(api, stock_rows(api, self.day))):
                receipt = self.collect(api)
            with self.store._connect() as conn:
                row = conn.execute("SELECT body FROM supplier_lr_features WHERE batch_id=%s", (receipt["batch"],)).fetchone()
                source = conn.execute("SELECT source_clock FROM supplier_market_batches WHERE batch_id=%s", (receipt["batch"],)).fetchone()[0]
            self.assertEqual(json.loads(row[0]), wanted)
            self.assertEqual(source.astimezone(CHINA).isoformat(), self.day.isoformat() + "T15:00:00+08:00")

    def test_expired_unfinished_pages_refresh_but_complete_runs_do_not(self):
        with patch(HTTP, return_value=response("daily", stock_rows("daily", self.day))), \
                patch("modules.strategy_hermes.bulk_eod_collect._publish", side_effect=RuntimeError), self.assertRaises(RuntimeError):
            self.collect()
        with self.store._connect() as conn:
            conn.execute("UPDATE supplier_tushare_pages SET collected_at=clock_timestamp()-interval '7 hours' WHERE source_key=%s",
                         (self.client.identity("daily"),))
        with patch(HTTP, return_value=response("daily", stock_rows("daily", self.day))) as http:
            self.collect()
            self.assertEqual(http.call_count, 1)

    def test_all_market_uses_calendar_trade_dates_and_only_completed_weekly(self):
        first, last = date(2001, 1, 8), date(2001, 1, 9)
        def post(_url, payload, **_kwargs):
            api, scope = payload["api_name"], payload["params"]
            self.assertNotIn("ts_code", scope)
            if api == "trade_cal":
                start, end = date.fromisoformat(scope["start_date"]), date.fromisoformat(scope["end_date"])
                rows = [dict(exchange="SSE", cal_date=(start + timedelta(days=i)).isoformat(),
                             is_open=int((start + timedelta(days=i)).weekday() < 5)) for i in range((end-start).days+1)]
                return response(api, rows)
            day = date.fromisoformat(scope["trade_date"])
            return response(api, stock_rows(api, day))
        progress = []
        with patch("modules.strategy_hermes.bulk_eod_collect.BulkTushare", return_value=self.client), \
                patch("modules.strategy_hermes.left_right_backfill_collectors.WARMUP_DAYS", 7), \
                patch("modules.strategy_hermes.left_right_backfill_collectors.collect_boards") as boards, \
                patch(HTTP, side_effect=post):
            result = collect_all(self.store, dict(scope="ALL", date_from=first.isoformat(), date_to=last.isoformat()),
                                 "mock-config", "mock-captures", self.request, lambda: None,
                                 lambda **fields: progress.append(fields))
        self.assertEqual(result, [])
        weekly = [scope["trade_date"] for source, scope in self.calls if source == "tushare:weekly"]
        self.assertEqual(weekly, ["20010105"])
        self.assertEqual(len(progress), 29)  # Seven trading dates x four APIs, one completed week.
        boards.assert_called_once()


if __name__ == "__main__":
    unittest.main()
