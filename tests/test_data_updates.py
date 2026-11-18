"""Retained five-period updates: plans, API, queue, receipts and worker fences."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from io import BytesIO
import json
import json
import os
from time import monotonic, sleep
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from psycopg import sql

from modules.strategy_hermes import data_updates
from modules.strategy_hermes.data_updates import UpdateError, UpdateSchedule, execute_job, jobs, plan, snapshot, submit
from modules.strategy_hermes.left_right_backfill import BackfillExecutor, BackfillQueue, QueueFull, RequestConflict
from modules.strategy_hermes.left_right_backfill_collectors import collect_boards
from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings
from modules.strategy_hermes.workspace_web import API, application

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")
NOW = datetime(2026, 11, 14, 10, tzinfo=CHINA)
FIRST = date(2026, 10, 1)
CALENDAR = [(FIRST + timedelta(days=index)).isoformat() for index in range(61)
            if (FIRST + timedelta(days=index)).weekday() < 5]
SOURCE_AT = "2026-11-13T15:00:00+08:00"


def payload(mode="daily", request_id="one", **changes):
    scope = {"daily": dict(date_from="2026-11-12", date_to="2026-11-13"),
             "weekly": dict(date_from="2026-11-09", date_to="2026-11-13"),
             "basic": {}, "monthly": dict(month="2026-11"), "yearly": dict(year=2025)}[mode]
    return dict(mode=mode, request_id=request_id, **scope) | changes


class UpdatePlanTest(unittest.TestCase):
    def test_five_modes_freeze_only_the_original_datasets_and_dates(self):
        expected = {
            "daily": [(dataset, day) for day in ("2026-11-12", "2026-11-13")
                      for dataset in ("daily", "adj_factor", "daily_basic", "cyq_perf", "daily_assessment")],
            "weekly": [("weekly", "2026-11-13")],
            "basic": [(dataset, "2026-11-13") for dataset in ("stock_basic", "trade_cal", "index_member_all")],
            "monthly": [("dc_member", "2026-11-13"), ("share_float", "2026-11-01")],
            "yearly": [("fina_indicator", "2025-12-31"), ("daily_assessment", "2026-11-13")],
        }
        for mode, units in expected.items():
            with self.subTest(mode=mode):
                body = plan(payload(mode), CALENDAR, NOW)
                self.assertCountEqual([(unit["dataset"], unit["business_date"]) for unit in body["units"]], units)
                self.assertEqual(body["task"], "data_update")
                self.assertEqual(body["latest_business_date"], "2026-11-13")
                self.assertEqual(len({unit["unit_key"] for unit in body["units"]}), len(units))

    def test_range_is_inclusive_31_days_and_must_be_completed(self):
        accepted = plan(payload(date_from="2026-10-14"), CALENDAR, NOW)
        self.assertEqual((accepted["trade_dates"][0], accepted["trade_dates"][-1]), ("2026-10-14", "2026-11-13"))
        for changes in (dict(date_from="2026-10-13"), dict(date_from="2026-11-14"),
                        dict(date_to="2026-11-14"), dict(date_from="20261112"), dict(date_to=None)):
            with self.subTest(changes=changes), self.assertRaisesRegex(UpdateError, "USE_COMPLETED_DATES_MAX_31_DAYS"):
                plan(payload(**changes), CALENDAR, NOW)
        with self.assertRaisesRegex(UpdateError, "USE_COMPLETED_DATES_MAX_31_DAYS"):
            plan(payload(), CALENDAR, datetime(2026, 11, 13, 15, 34, tzinfo=CHINA))
        self.assertEqual(plan(payload(), CALENDAR, datetime(2026, 11, 13, 15, 35, tzinfo=CHINA))["trade_dates"][-1], "2026-11-13")

    def test_weekly_requires_a_completed_week_and_calendar_coverage(self):
        with self.assertRaisesRegex(UpdateError, "NO_COMPLETED_TRADING_DAY_IN_RANGE"):
            plan(payload("weekly", date_to="2026-11-12"), CALENDAR, NOW)
        with self.assertRaisesRegex(UpdateError, "TRADE_CALENDAR_RANGE_MISSING"):
            plan(payload("weekly"), [day for day in CALENDAR if day <= "2026-11-13"], NOW)
        # A Friday holiday makes Thursday the final trading day of the completed week.
        holiday = [day for day in CALENDAR if day != "2026-11-13"]
        self.assertEqual(plan(payload("weekly", date_to="2026-11-12"), holiday, NOW)["weekly_dates"], ["2026-11-12"])

    def test_month_accepts_current_closed_days_but_rejects_invalid_or_future_months(self):
        body = plan(payload("monthly"), CALENDAR, NOW)
        self.assertEqual(body["snapshot_date"], "2026-11-13")
        for value in ("2026-12", "2026-13", "2026-1", "0000-01", "wrong", None, 202611):
            with self.subTest(month=value), self.assertRaisesRegex(UpdateError, "INVALID_MONTH"):
                plan(payload("monthly", month=value), CALENDAR, NOW)

    def test_year_must_be_a_completed_integer_report_year(self):
        for value in (2026, 2027, 1999, "2025", True, None):
            with self.subTest(year=value), self.assertRaisesRegex(UpdateError, "USE_COMPLETED_REPORT_YEAR"):
                plan(payload("yearly", year=value), CALENDAR, NOW)

    def test_no_trading_day_and_missing_calendar_are_explicit(self):
        with self.assertRaisesRegex(UpdateError, "NO_COMPLETED_TRADING_DAY_IN_RANGE"):
            plan(payload(date_from="2026-11-07", date_to="2026-11-08"), CALENDAR, NOW)
        with self.assertRaisesRegex(UpdateError, "TRADE_CALENDAR_RANGE_MISSING"):
            plan(payload(), [], NOW)
        self.assertEqual(len(plan(payload("basic"), [], NOW)["units"]), 3)

    def test_nonoriginal_controls_and_invalid_ids_are_rejected(self):
        for changes in (dict(codes=["600000.SH"]), dict(scope="ALL"), dict(request_id="bad/id"), dict(extra=True)):
            with self.subTest(changes=changes), self.assertRaisesRegex(UpdateError, "INVALID_UPDATE_REQUEST"):
                plan(payload(**changes), CALENDAR, NOW)
        with self.assertRaisesRegex(UpdateError, "INVALID_UPDATE_MODE"):
            plan(dict(mode="recompute", request_id="one"), CALENDAR, NOW)


class UpdateScheduleTest(unittest.TestCase):
    def test_original_morning_window_recovers_at_seven_but_never_after_eight_thirty(self):
        queue = Mock(ledger=object(), calendar_source=CALENDAR)
        queue.calendar.return_value = CALENDAR
        schedule = UpdateSchedule(queue, lambda: True)
        with patch.object(data_updates, "submit", return_value={"job_count": 5}) as enqueue:
            for hour in (0, 5, 10, 15, 23):
                self.assertIsNone(schedule(NOW.replace(hour=hour)))
            self.assertIsNone(schedule(NOW.replace(hour=8, minute=30)))
            enqueue.assert_not_called()
            schedule(NOW.replace(hour=6, minute=0))
            body = enqueue.call_args.args[2]
            self.assertEqual(body, dict(request_id="scheduled-eod-2026-11-13", mode="daily", date_from="2026-11-13", date_to="2026-11-13"))
            schedule(NOW.replace(hour=7, minute=0))
            self.assertEqual(enqueue.call_args.args[2], body)
            schedule(NOW.replace(hour=8, minute=29))
            self.assertEqual(enqueue.call_count, 3)
            schedule(NOW.replace(day=15, hour=6))
            self.assertEqual(enqueue.call_args.args[2], body)
            schedule(NOW.replace(day=16, hour=6))
            self.assertEqual(enqueue.call_args.args[2], body)

    def test_disabled_no_calendar_and_full_queue_are_quiet(self):
        queue = Mock(ledger=object(), calendar_source=[])
        queue.calendar.return_value = []
        with patch.object(data_updates, "submit", side_effect=QueueFull("full")) as enqueue:
            self.assertIsNone(UpdateSchedule(queue, lambda: False)(NOW.replace(hour=6)))
            self.assertIsNone(UpdateSchedule(queue, lambda: True)(NOW.replace(hour=6)))
            enqueue.assert_not_called()
            queue.calendar.return_value = CALENDAR
            self.assertIsNone(UpdateSchedule(queue, lambda: True)(NOW.replace(hour=6)))
            enqueue.assert_called_once()


@unittest.skipUnless(ENV, "set STAGE2_TEST_ENV_FILE for isolated original-update integration")
class UpdateQueueTest(unittest.TestCase):
    def setUp(self):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, "test")
        self.ns = settings.redis_prefix + "original-updates-test:" + uuid4().hex
        self.ledger = PostgresLedger(replace(settings, namespace=self.ns), "FROZEN")
        self.ledger.migrate()
        self.queue = BackfillQueue(self.ledger, CALENDAR)
        self.addCleanup(self.clean)

    def clean(self):
        with self.ledger._connect() as conn:
            conn.execute("DELETE FROM left_right_backfill_requests WHERE ns=%s", (self.ns,))
            conn.execute("DELETE FROM left_right_backfill_jobs WHERE ns=%s", (self.ns,))

    def enqueue(self, mode="basic", request_id="one", **changes):
        return submit(self.ledger, CALENDAR, payload(mode, request_id, **changes), now=NOW)

    def unit_rows(self):
        with self.ledger._connect() as conn:
            return conn.execute("SELECT dataset,status,row_count,source_at,error_code FROM data_update_units WHERE ns=%s ORDER BY dataset", (self.ns,)).fetchall()

    def executor(self, **changes):
        return BackfillExecutor(self.queue, external_enabled=lambda: True, **changes)

    def test_concurrent_idempotency_creates_one_group_and_exact_units(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.enqueue(), range(4)))
        self.assertEqual(results, [results[0]] * 4)
        self.assertEqual(results[0]["job_count"], 3)
        self.assertEqual(len(self.unit_rows()), 3)
        with self.assertRaises(RequestConflict):
            self.enqueue("yearly")
        self.enqueue(request_id="two")
        self.enqueue(request_id="three")
        with self.assertRaises(QueueFull):
            self.enqueue(request_id="four")

    def test_release_pause_and_stop_requeue_update_with_budget_and_saved_receipt(self):
        self.enqueue("weekly")
        claim = self.queue.claim()
        operation = Mock(return_value=dict(rows=2, batch="saved-week"))
        self.queue.request(claim, "tushare:weekly", ["same-week"], operation)
        with self.ledger._connect() as conn:
            conn.execute("UPDATE left_right_backfill_jobs SET elapsed_seconds=20 WHERE ns=%s", (self.ns,))
        executor = self.executor(paused=lambda: True)
        executor.stop.set()
        collector = Mock()
        self.assertEqual(execute_job(executor, claim, collector=collector), "QUEUED")
        collector.assert_not_called()
        saved = self.queue.get(claim.job_id)
        self.assertEqual((saved["error_code"], saved["requests_used"]), ("PAUSED", 1))
        with self.ledger._connect() as conn:
            elapsed = conn.execute("SELECT elapsed_seconds FROM left_right_backfill_jobs WHERE ns=%s", (self.ns,)).fetchone()[0]
        self.assertGreaterEqual(elapsed, 20)
        def collect_saved(ledger, body, config, captures, request, checkpoint, record, progress):
            receipt = request("tushare:weekly", ["same-week"], operation)
            self.assertEqual(receipt["receipt"]["batch"], "saved-week")
            for unit in body["units"]:
                record(unit["dataset"], unit["business_date"], dict(status="OK", row_count=2, observed_at=SOURCE_AT))
            return {}
        self.assertEqual(execute_job(self.executor(), self.queue.claim(), collector=collect_saved), "SUCCEEDED")
        operation.assert_called_once()
        final = self.queue.get(claim.job_id)
        self.assertEqual((final["requests_used"], final["request_budget"]), (1, saved["request_budget"]))
        with self.ledger._connect() as conn:
            final_elapsed = conn.execute("SELECT elapsed_seconds FROM left_right_backfill_jobs WHERE ns=%s", (self.ns,)).fetchone()[0]
        self.assertGreaterEqual(final_elapsed, elapsed)

    def test_cancel_wins_over_update_release_pause_and_stop(self):
        self.enqueue("weekly")
        claim = self.queue.claim()
        self.queue.cancel(claim.job_id)
        executor = self.executor(paused=lambda: True)
        executor.stop.set()
        collector = Mock()
        self.assertEqual(execute_job(executor, claim, collector=collector), "CANCELLED")
        collector.assert_not_called()

    def test_snapshot_has_original_fields_no_collection_and_zero_is_empty(self):
        self.enqueue()
        claim = self.queue.claim()
        def collector(ledger, body, config, captures, request, checkpoint, record, progress):
            for unit in body["units"]:
                record(unit["dataset"], unit["business_date"], dict(status="RUNNING"))
                zero = unit["dataset"] == "index_member_all"
                record(unit["dataset"], unit["business_date"], dict(status="EMPTY" if zero else "OK", row_count=0 if zero else 2,
                       observed_at=SOURCE_AT, details={"coverage_end": "2027-12-31"} if unit["dataset"] == "trade_cal" else {}))
            return {}
        self.assertEqual(execute_job(self.executor(), claim, collector=collector), "SUCCEEDED")
        with patch("modules.strategy_hermes.data_updates_collect.execute_update", side_effect=AssertionError("GET cannot collect")):
            view = snapshot(self.ledger, CALENDAR, now=NOW)
        self.assertEqual(set(view), {"latest_business_date", "datasets", "recent_jobs"})
        self.assertEqual(len(view["datasets"]), 11)
        rows = {row["dataset"]: row for row in view["datasets"]}
        empty = rows["index_member_all"]
        self.assertEqual((empty["status"], empty["row_count"], empty["business_date"]), ("EMPTY", 0, "2026-11-13"))
        self.assertEqual(datetime.fromisoformat(empty["last_success_at"]), datetime.fromisoformat(SOURCE_AT))
        self.assertEqual(rows["trade_cal"]["details"]["coverage_end"], "2027-12-31")
        self.assertTrue(all(row["status"] == "SUCCEEDED" for row in view["recent_jobs"]))
        self.assertEqual(set(empty), {"dataset", "label", "cadence", "status", "business_date", "last_success_at", "row_count", "details", "error_code"})

    def test_failed_receipt_retains_error_and_partial_parent(self):
        self.enqueue()
        def collector(ledger, body, config, captures, request, checkpoint, record, progress):
            for index, unit in enumerate(body["units"]):
                receipt = dict(status="FAILED", error_code="SOURCE_SCHEMA_INVALID") if index == 0 else dict(status="OK", row_count=1, observed_at=SOURCE_AT)
                record(unit["dataset"], unit["business_date"], receipt)
            return {}
        self.assertEqual(execute_job(self.executor(), self.queue.claim(), collector=collector), "PARTIAL")
        failed = next(row for row in snapshot(self.ledger, CALENDAR, now=NOW)["datasets"] if row["dataset"] == "stock_basic")
        self.assertEqual((failed["status"], failed["error_code"]), ("FAILED", "SOURCE_SCHEMA_INVALID"))
        self.assertIsNone(failed["row_count"])

    def test_yearly_recomputes_only_latest_saved_market_date_and_records_receipts(self):
        self.enqueue("yearly")
        def collector(ledger, body, config, captures, request, checkpoint, record, progress):
            record("fina_indicator", "2025-12-31", dict(status="OK", row_count=2, observed_at=SOURCE_AT))
            return dict(recompute_date="2026-11-12")
        builder = Mock(return_value=dict(archives_saved=1, expected_items=4, ready_items=3, missing_count=1))
        self.assertEqual(execute_job(self.executor(), self.queue.claim(), collector=collector, builder=builder), "SUCCEEDED")
        self.assertEqual(builder.call_args.args[1:3], (["2026-11-12"], None))
        self.assertTrue(builder.call_args.kwargs["persist"])
        rows = {row["task"]: row for row in jobs(self.ledger)["items"]}
        self.assertEqual((rows["daily_assessment"]["status"], rows["daily_assessment"]["business_date"]), ("SUCCEEDED", "2026-11-12"))
        self.assertIsNone(rows["daily_assessment"]["dataset"])

    def test_yearly_without_saved_closes_marks_recompute_failed(self):
        self.enqueue("yearly")
        def collector(ledger, body, config, captures, request, checkpoint, record, progress):
            record("fina_indicator", "2025-12-31", dict(status="EMPTY", row_count=0, observed_at=SOURCE_AT))
            return dict(recompute_date=None)
        builder = Mock(side_effect=AssertionError("no closes means no rebuild"))
        self.assertEqual(execute_job(self.executor(), self.queue.claim(), collector=collector, builder=builder), "PARTIAL")
        assessment = next(row for row in jobs(self.ledger)["items"] if row["task"] == "daily_assessment")
        self.assertEqual(assessment["error_code"], "NO_SAVED_MARKET_CLOSES")
        builder.assert_not_called()

    def test_daily_builds_one_saved_lr_archive_for_each_requested_date(self):
        result = self.enqueue("daily")
        self.assertEqual(result["job_count"], 10)
        def collector(ledger, body, config, captures, request, checkpoint, record, progress):
            for unit in body["units"]:
                if unit["dataset"] != "daily_assessment":
                    record(unit["dataset"], unit["business_date"], dict(status="OK", row_count=2, observed_at=SOURCE_AT))
            return {}
        builder = Mock(return_value=dict(archives_saved=1, expected_items=4, ready_items=4, missing_count=0))
        self.assertEqual(execute_job(self.executor(), self.queue.claim(), collector=collector, builder=builder), "SUCCEEDED")
        self.assertEqual([call.args[1] for call in builder.call_args_list], [["2026-11-12"], ["2026-11-13"]])
        self.assertTrue(all(call.kwargs["persist"] for call in builder.call_args_list))
        assessed = [row for row in jobs(self.ledger)["items"] if row["task"] == "daily_assessment"]
        self.assertEqual(len(assessed), 2)
        self.assertTrue(all(row["status"] == "SUCCEEDED" for row in assessed))

    def test_daily_chips_failure_keeps_original_builder_and_partial_source_result(self):
        self.enqueue("daily")
        def collector(ledger, body, config, captures, request, checkpoint, record, progress):
            for unit in body["units"]:
                if unit["dataset"] == "daily_assessment":
                    continue
                failed = unit["dataset"] == "cyq_perf" and unit["business_date"] == "2026-11-12"
                record(unit["dataset"], unit["business_date"], dict(status="FAILED", error_code="SOURCE_SCHEMA_INVALID") if failed else dict(status="OK", row_count=2, observed_at=SOURCE_AT))
            return {}
        builder = Mock(return_value=dict(archives_saved=1, expected_items=4, ready_items=4, missing_count=0))
        self.assertEqual(execute_job(self.executor(), self.queue.claim(), collector=collector, builder=builder), "PARTIAL")
        self.assertEqual([call.args[1] for call in builder.call_args_list], [["2026-11-12"], ["2026-11-13"]])
        assessed = {row["business_date"]: row for row in jobs(self.ledger)["items"] if row["task"] == "daily_assessment"}
        self.assertEqual(assessed["2026-11-12"]["status"], "SUCCEEDED")
        self.assertEqual(assessed["2026-11-13"]["status"], "SUCCEEDED")
        chips = next(row for row in jobs(self.ledger)["items"] if row["task"] == "cyq_perf" and row["business_date"] == "2026-11-12")
        self.assertEqual(chips["status"], "FAILED")

    def test_daily_all_missing_archive_is_reported_without_stopping_other_dates(self):
        self.enqueue("daily")
        def collector(ledger, body, config, captures, request, checkpoint, record, progress):
            for unit in body["units"]:
                if unit["dataset"] != "daily_assessment":
                    record(unit["dataset"], unit["business_date"],
                           dict(status="OK", row_count=2, observed_at=SOURCE_AT))
            return {}
        builder = Mock(side_effect=[
            dict(archives_saved=1, expected_items=4, ready_items=0, missing_count=4),
            dict(archives_saved=1, expected_items=4, ready_items=3, missing_count=1)])
        self.assertEqual(execute_job(self.executor(), self.queue.claim(), collector=collector,
                                    builder=builder, board_collector=Mock()), "PARTIAL")
        self.assertEqual([call.args[1] for call in builder.call_args_list],
                         [["2026-11-12"], ["2026-11-13"]])
        rows = jobs(self.ledger)["items"]
        first = next(row for row in rows if row["task"] == "daily_assessment"
                     and row["business_date"] == "2026-11-12")
        second = next(row for row in rows if row["task"] == "daily_assessment"
                      and row["business_date"] == "2026-11-13")
        self.assertEqual(first["status"], "FAILED")
        self.assertEqual(first["error_code"], "ASSESSMENT_INPUT_MISSING")
        with self.ledger._connect() as conn:
            count, details = conn.execute("""SELECT row_count,details FROM data_update_units
                WHERE ns=%s AND dataset='daily_assessment' AND business_date='2026-11-12'""",
                (self.ns,)).fetchone()
        self.assertEqual(count, 4)
        self.assertEqual(json.loads(details) if isinstance(details, str) else details,
                         dict(ready_items=0, missing_count=4))
        self.assertEqual(second["status"], "SUCCEEDED")
        self.assertTrue(all(row["status"] == "SUCCEEDED" for row in rows
                            if row["task"] != "daily_assessment"))

    def test_daily_actual_board_helper_runs_between_sources_and_builder_with_shared_budget(self):
        self.enqueue("daily")
        claim, order = self.queue.claim(), []
        def collector(ledger, body, config, captures, request, checkpoint, record, progress):
            order.append("sources")
            for unit in body["units"]:
                if unit["dataset"] != "daily_assessment":
                    record(unit["dataset"], unit["business_date"], dict(status="OK", row_count=2, observed_at=SOURCE_AT))
            return {}
        def boards(*args):
            order.append("boards")
            return collect_boards(*args)
        def page(store, captures, endpoint, day, offset, plate):
            order.append((endpoint, day, plate, offset))
            return dict(rows=1, total=1, ids=["899998" if endpoint == "042" else "600000.SH"])
        def builder(store, days, codes, as_of, **kwargs):
            order.append(("build", days[0]))
            return dict(archives_saved=1, expected_items=4, ready_items=4, missing_count=0)
        module = "modules.strategy_hermes.left_right_backfill_collectors"
        with patch(module + "._calendar", return_value={day: True for day in CALENDAR}), \
             patch(module + "._existing_page", return_value=None), patch(module + "._page", side_effect=page):
            result = execute_job(self.executor(captures=object()), claim, collector=collector, builder=builder, board_collector=boards)
        self.assertEqual(result, "SUCCEEDED")
        self.assertEqual(order[:2], ["sources", "boards"])
        self.assertEqual(order[-2:], [("build", "2026-11-12"), ("build", "2026-11-13")])
        rankings = [item[1] for item in order if isinstance(item, tuple) and item[0] == "042"]
        self.assertEqual(rankings, ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"])
        self.assertEqual([item for item in order if isinstance(item, tuple) and item[0] == "046"], [("046", "2026-11-11", "899998", 0)])
        saved = self.queue.get(claim.job_id)
        self.assertEqual(saved["requests_used"], 7)
        self.assertEqual(saved["result"]["sector_issue_count"], 0)
        with self.ledger._connect() as conn:
            requests = conn.execute("SELECT source,status FROM left_right_backfill_requests WHERE ns=%s", (self.ns,)).fetchall()
        self.assertEqual(len(requests), 7)
        self.assertTrue(all(source.startswith("kaipanla:") and status == "SAVED" for source, status in requests))

    def test_daily_without_any_saved_closes_skips_boards_and_records_builder_missing(self):
        old_days = ["2004-11-08", "2004-11-09", "2004-11-10", "2004-11-11", "2004-11-12", "2004-11-15"]
        submit(self.ledger, old_days, payload("daily", date_from="2004-11-11", date_to="2004-11-12"), now=NOW)
        with self.ledger._connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM supplier_daily_bars WHERE trade_date BETWEEN '2004-11-11' AND '2004-11-12'").fetchone()[0], 0)
        def collector(ledger, body, config, captures, request, checkpoint, record, progress):
            for unit in body["units"]:
                if unit["dataset"] != "daily_assessment":
                    record(unit["dataset"], unit["business_date"], dict(status="FAILED", error_code="SOURCE_REQUEST_FAILED"))
            return {}
        boards = Mock(side_effect=AssertionError("no daily closes means no board requests"))
        builder = Mock(return_value=dict(archives_saved=0, expected_items=4, ready_items=0, missing_count=4))
        self.assertEqual(execute_job(self.executor(captures=object()), self.queue.claim(), collector=collector, builder=builder, board_collector=boards), "PARTIAL")
        boards.assert_not_called()
        self.assertEqual([call.args[1] for call in builder.call_args_list], [["2004-11-11"], ["2004-11-12"]])
        assessed = [row for row in jobs(self.ledger)["items"] if row["task"] == "daily_assessment"]
        self.assertEqual(len(assessed), 2)
        self.assertTrue(all(row["status"] == "FAILED" and row["error_code"] == "ASSESSMENT_INPUT_MISSING" for row in assessed))

    def test_cancellation_or_expired_lease_after_checkpoint_cannot_publish_unit_success(self):
        for reason in ("cancel", "expire"):
            with self.subTest(reason=reason):
                self.enqueue(request_id=reason)
                claim, armed = self.queue.claim(), [False]
                original_pulse = self.queue.pulse
                def pulse(current):
                    active = original_pulse(current)
                    if armed[0]:
                        armed[0] = False
                        with self.ledger._connect() as conn:
                            change = "cancel_requested=true" if reason == "cancel" else "lease_until=clock_timestamp()-interval '1 second'"
                            conn.execute("UPDATE left_right_backfill_jobs SET " + change + " WHERE ns=%s AND job_id=%s", (self.ns, claim.job_id))
                    return active
                def collector(ledger, body, config, captures, request, checkpoint, record, progress):
                    armed[0] = True
                    unit = body["units"][0]
                    record(unit["dataset"], unit["business_date"], dict(status="OK", row_count=99, observed_at=SOURCE_AT))
                    return {}
                with patch.object(self.queue, "pulse", side_effect=pulse):
                    result = execute_job(self.executor(), claim, collector=collector)
                self.assertEqual(result, "CANCELLED" if reason == "cancel" else "LEASE_LOST")
                for _, status, count, source, _ in self.unit_rows():
                    self.assertNotIn(status, ("OK", "EMPTY"))
                    self.assertIsNone(count)
                    self.assertIsNone(source)
                self.queue.claim()  # Sweep the expired parent so no next test inherits a lease.

    def test_record_holds_parent_lock_until_unit_commit_against_real_concurrent_cancel(self):
        self.enqueue()
        claim = self.queue.claim()
        unit = claim.body["units"][0]
        suffix, lock_key = uuid4().hex, int(uuid4().hex[:7], 16)
        function, trigger = "pause_update_" + suffix, "pause_update_" + suffix
        # The trigger pauses the physical unit UPDATE after the parent SELECT.
        # A competing connection must remain blocked on that parent's row lock.
        with self.ledger._connect() as conn:
            conn.execute(sql.SQL("""CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.ns={} THEN PERFORM pg_advisory_xact_lock({}); END IF;
                RETURN NEW; END $$""").format(sql.Identifier(function), sql.Literal(self.ns), sql.Literal(lock_key)))
            conn.execute(sql.SQL("CREATE TRIGGER {} BEFORE UPDATE ON data_update_units FOR EACH ROW EXECUTE FUNCTION {}()")
                         .format(sql.Identifier(trigger), sql.Identifier(function)))
        def collector(ledger, body, config, captures, request, checkpoint, record, progress):
            record(unit["dataset"], unit["business_date"], dict(status="OK", row_count=99, observed_at=SOURCE_AT))
            return {}
        def cancel_and_read():
            cancelled = self.queue.cancel(claim.job_id)
            with self.ledger._connect() as conn:
                saved = conn.execute("SELECT status,row_count FROM data_update_units WHERE ns=%s AND job_id=%s AND unit_key=%s",
                    (self.ns, claim.job_id, unit["unit_key"])).fetchone()
            return cancelled, saved
        try:
            with self.ledger._connect() as control, ThreadPoolExecutor(max_workers=2) as pool:
                control.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
                running = pool.submit(execute_job, self.executor(), claim, collector=collector)
                cancelling = None
                try:
                    worker_pid, deadline = None, monotonic() + 5
                    while worker_pid is None and monotonic() < deadline:
                        row = control.execute("""SELECT pid FROM pg_locks WHERE locktype='advisory'
                            AND classid=0 AND objid::bigint=%s AND NOT granted""", (lock_key,)).fetchone()
                        worker_pid = row[0] if row else None
                        if worker_pid is None:
                            sleep(.01)
                    self.assertIsNotNone(worker_pid, "the physical unit UPDATE must reach the held advisory lock")
                    cancelling = pool.submit(cancel_and_read)
                    blocked, deadline = [], monotonic() + 5
                    while not blocked and not cancelling.done() and monotonic() < deadline:
                        blocked = control.execute("SELECT pid FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                                                  (worker_pid,)).fetchall()
                        if not blocked:
                            sleep(.01)
                    self.assertFalse(cancelling.done(), "cancel committed between the parent lock and the unit receipt")
                    self.assertTrue(blocked, "PostgreSQL must show cancel waiting on the record transaction")
                    pending = control.execute("SELECT status,row_count FROM data_update_units WHERE ns=%s AND job_id=%s AND unit_key=%s",
                        (self.ns, claim.job_id, unit["unit_key"])).fetchone()
                    self.assertEqual(pending, ("PENDING", None))
                finally:
                    control.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                    result = running.result(timeout=10)
                    cancelled = cancelling.result(timeout=10) if cancelling else None
                self.assertEqual(result, "CANCELLED")
                self.assertTrue(cancelled[0]["cancel_requested"])
                self.assertEqual(cancelled[1], ("OK", 99), "cancel may commit only after the earlier unit receipt commits")
        finally:
            with self.ledger._connect() as conn:
                conn.execute(sql.SQL("DROP TRIGGER IF EXISTS {} ON data_update_units").format(sql.Identifier(trigger)))
                conn.execute(sql.SQL("DROP FUNCTION IF EXISTS {}()").format(sql.Identifier(function)))

    def test_schedule_repeated_morning_and_weekend_ticks_reuse_one_business_day_job(self):
        schedule = UpdateSchedule(self.queue, lambda: True)
        first = schedule(NOW.replace(hour=6, minute=0))
        self.assertEqual(schedule(NOW.replace(hour=6, minute=30)), first)
        self.assertEqual(schedule(NOW.replace(hour=7, minute=0)), first)
        self.assertEqual(schedule(NOW.replace(day=15, hour=6)), first)
        self.assertEqual(schedule(NOW.replace(day=16, hour=6)), first)
        self.assertEqual(first["job_count"], 5)
        self.assertEqual(len(self.unit_rows()), 5)

    def test_original_api_contract_auth_queue_only_post_and_jobs_pagination(self):
        token = "q" * 40
        app = application(self.ledger, token, calendar=CALENDAR)
        def request(method, suffix, body=None, auth=True):
            raw = json.dumps(body).encode() if body is not None else b""
            path, _, query = (API + suffix).partition("?")
            environ = dict(REQUEST_METHOD=method, PATH_INFO=path, QUERY_STRING=query, CONTENT_LENGTH=str(len(raw)),
                           CONTENT_TYPE="application/json", **{"wsgi.input": BytesIO(raw)})
            if auth:
                environ["HTTP_AUTHORIZATION"] = "Bearer " + token
            statuses = []
            response = b"".join(app(environ, lambda status, headers: statuses.append(int(status.split()[0]))))
            return statuses[0], json.loads(response)
        with patch.object(data_updates, "datetime", wraps=datetime) as clock, \
             patch("modules.strategy_hermes.data_updates_collect.execute_update", side_effect=AssertionError("HTTP never collects")):
            clock.now.return_value = NOW
            self.assertEqual(request("GET", "/data-updates", auth=False)[0], 401)
            self.assertEqual(request("POST", "/data-updates", payload("basic"), auth=False)[0], 401)
            status, view = request("GET", "/data-updates")
            self.assertEqual(status, 200)
            self.assertEqual(view["recent_jobs"], [])
            status, result = request("POST", "/data-updates", payload("basic"))
            self.assertEqual(status, 202)
            self.assertEqual(set(result), {"job_count", "job_ids"})
            self.assertEqual(result["job_count"], 3)
            self.assertEqual(request("POST", "/data-updates", payload("basic")), (202, result))
            self.assertEqual(request("POST", "/data-updates", payload("yearly"))[0], 409)
            code, problem = request("POST", "/data-updates", payload("monthly", month="2026-13"))
            self.assertEqual((code, problem["error"]), (400, "INVALID_MONTH"))
            status, page = request("GET", "/data-update-jobs?page=2&page_size=2")
            self.assertEqual(status, 200)
            self.assertEqual((page["total"], page["page"], page["page_size"], page["pages"], len(page["items"])), (3, 2, 2, 2, 1))
            self.assertEqual(page["items"][0]["status"], "PENDING")
            self.assertEqual(request("GET", "/data-update-jobs", auth=False)[0], 401)
            for suffix in ("/data-update-jobs?page=0", "/data-update-jobs?page_size=101", "/data-update-jobs?page=1&page=2", "/data-updates?unexpected=1"):
                self.assertEqual(request("GET", suffix)[0], 400)
        self.assertEqual(len(self.unit_rows()), 3)
