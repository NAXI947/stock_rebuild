"""Manual LR admission, durable fences, provider bounds and evidence contracts."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit

from modules.strategy_hermes.left_right_backfill import (
    BackfillExecutor, BackfillQueue, Interrupted, QueueFull, RequestConflict, normalize_request)
from modules.strategy_hermes.left_right_backfill_collectors import (
    HistoricalTushare, _existing_tushare, _existing_page, normalize_features, collect)
from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings
from modules.strategy_hermes.stage2.market_bootstrap import save as save_market

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")
NOW = datetime(2026, 11, 14, 10, tzinfo=CHINA)
DAYS = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]


def payload(request_id="one", **changes):
    return dict(request_id=request_id, mode="recompute", date_from="2026-11-12",
                date_to="2026-11-13", codes=["600000.SH"]) | changes


class BackfillValidationTest(unittest.TestCase):
    def test_all_market_admission_has_no_stock_limit_and_allows_calendar_bootstrap(self):
        body = dict(request_id="bulk", mode="collect", scope="ALL",
                    date_from="2026-08-01", date_to="2026-08-31")
        self.assertEqual(normalize_request(body, DAYS, NOW), body)
        for changes in (dict(scope="unknown"), dict(codes=["600000.SH"]),
                        dict(date_to="2026-09-01"), dict(mode="recompute")):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_request(body | changes, DAYS, NOW)

    def test_admission_rejects_open_day_and_unbounded_inputs(self):
        self.assertEqual(normalize_request(payload(), DAYS, NOW)["codes"], ["600000.SH"])
        for changes in ({"codes": []}, {"codes": ["600000.SH"] * 2},
                        {"codes": [f"{i:06d}.SH" for i in range(11)]}, {"date_to": "2026-11-14"},
                        {"date_from": "2026-10-01"}, {"mode": "realtime"}, {"request_id": "bad/id"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_request(payload(**changes), DAYS, NOW)
        with self.assertRaises(ValueError):
            normalize_request(payload(), DAYS, datetime(2026, 11, 13, 15, 34, tzinfo=CHINA))
        self.assertEqual(normalize_request(payload(), DAYS, datetime(2026, 11, 13, 15, 35, tzinfo=CHINA))["date_to"], DAYS[-1])

    def test_feature_units_and_completed_week_keep_original_close(self):
        first, last = date(2026, 11, 9), date(2026, 11, 13)
        row = dict(ts_code="600000.SH", trade_date="20261113", turnover_rate="1.5", volume_ratio="2", circ_mv="123")
        result = normalize_features("daily_basic", [row], "600000.SH", first, last, {})
        self.assertEqual(result["features"][DAYS[-1]], {"turnover_ratio": "0.015", "volume_ratio": "2", "circ_mv_cny": "1230000"})
        chips = normalize_features("cyq_perf", [dict(ts_code="600000.SH", trade_date="20261113", cost_85pct="10.2")],
                                   "600000.SH", first, last, {})
        self.assertEqual(chips["features"][DAYS[-1]]["cost85"], "10.2")
        week = {(first + timedelta(days=i)).isoformat(): i < 5 for i in range(7)}
        weekly = [dict(ts_code="600000.SH", trade_date="20261113", close="10", vol="123", amount="456")]
        result = normalize_features("weekly", weekly, "600000.SH", first, last, week)["weekly"][0]
        self.assertEqual((result["close"], result["complete"], result["volume_shares"]), ("10", True, "123"))
        week.pop("2026-11-15")
        self.assertFalse(normalize_features("weekly", weekly, "600000.SH", first, last, week)["weekly"][0]["complete"])

    def test_feature_transport_is_one_request_with_known_fields_and_no_retry(self):
        client = HistoricalTushare.__new__(HistoricalTushare)
        client.config, client.used = {"token": "fake", "timeout_seconds": 999}, set()
        answer = {"code": 0, "data": {"fields": ["ts_code", "trade_date", "cost_85pct"],
                                      "items": [["600000.SH", "20261113", "10.2"]]}}
        with patch("modules.strategy_hermes.left_right_backfill_collectors.post_json", return_value=answer) as send:
            self.assertEqual(client.fetch("cyq_perf", {"ts_code": "600000.SH"})[0]["cost_85pct"], "10.2")
            self.assertEqual(send.call_args.kwargs["timeout"], 30)
            self.assertEqual(send.call_args.args[1]["fields"], "ts_code,trade_date,cost_85pct")
            with self.assertRaises(ValueError):
                client.fetch("cyq_perf", {})
            self.assertEqual(send.call_count, 1)

    def test_three_failures_stop_collection_and_leave_partial_reason(self):
        calls = []
        def request(source, scope, operation):
            calls.append(source)
            return dict(status="FAILED")
        with patch("modules.strategy_hermes.left_right_backfill_collectors._existing_tushare", return_value=None):
            issues = collect(Mock(), payload(mode="collect"), "/no-real-secret", None, request, lambda: None)
        self.assertEqual(len(calls), 3)
        self.assertEqual(issues[-1]["reason"], "CONSECUTIVE_PROVIDER_FAILURES")


@unittest.skipUnless(ENV, "set STAGE2_TEST_ENV_FILE for isolated backfill integration")
class BackfillQueueTest(unittest.TestCase):
    def setUp(self):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, "test")
        self.ns = settings.redis_prefix + "lr-backfill-test:" + uuid4().hex
        self.ledger = PostgresLedger(replace(settings, namespace=self.ns), "FROZEN")
        self.ledger.migrate()
        self.queue = BackfillQueue(self.ledger, DAYS)
        self.addCleanup(self.clean)

    def clean(self):
        with self.ledger._connect() as conn, conn.transaction():
            conn.execute("DELETE FROM left_right_backfill_requests WHERE ns=%s", (self.ns,))
            conn.execute("DELETE FROM left_right_backfill_jobs WHERE ns=%s", (self.ns,))
            conn.execute("DELETE FROM snapshot_state WHERE ns=%s", (self.ns,))

    def submit(self, request_id="one", **changes):
        return self.queue.submit(payload(request_id, **changes), now=NOW)

    def executor(self, **kwargs):
        return BackfillExecutor(self.queue, **kwargs)

    def test_concurrent_idempotency_capacity_and_namespace_cancel(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(lambda _: self.submit(), range(4)))
        self.assertEqual(len({job["job_id"] for job in jobs}), 1)
        with self.assertRaises(RequestConflict):
            self.submit(codes=["000001.SZ"])
        self.submit("two")
        self.submit("three")
        with self.assertRaises(QueueFull):
            self.submit("four")
        first = self.queue.claim()
        self.assertIsNone(self.queue.claim())
        self.queue.cancel(first.job_id)
        self.assertFalse(self.queue.pulse(first))
        self.assertEqual(self.queue.finish(first, status="SUCCEEDED"), "CANCELLED")
        self.assertEqual(self.queue.cancel(jobs[0]["job_id"])["status"], "CANCELLED")
        listing = self.queue.listing(now=NOW)
        self.assertEqual((len(listing["jobs"]), listing["limits"]["latest_closed_trade_date"]), (3, "2026-11-13"))

    def test_persisted_request_budget_includes_failures_no_duplicate_physical_call(self):
        self.submit(mode="collect")
        claim = self.queue.claim()
        with self.ledger._connect() as conn:
            conn.execute("UPDATE left_right_backfill_jobs SET request_budget=1 WHERE ns=%s", (self.ns,))
        operation = Mock(side_effect=ValueError("private-secret-not-recorded"))
        first = self.queue.request(claim, "tushare:daily", ["one"], operation)
        self.assertEqual(first["error_code"], "ValueError")
        second = self.queue.request(claim, "tushare:daily", ["one"], operation)
        self.assertEqual(first, second)
        self.assertEqual(operation.call_count, 1)
        with self.assertRaises(Interrupted):
            self.queue.request(claim, "tushare:weekly", ["two"], operation)
        self.assertEqual(self.queue.get(claim.job_id)["requests_used"], 1)
        self.assertNotIn("private-secret", str(self.queue.get(claim.job_id)))

    def test_provider_diagnostics_persist_safely_and_failure_still_deduplicates(self):
        from modules.strategy_hermes.stage2.market_bootstrap import ProbeError
        self.submit(mode='collect')
        claim = self.queue.claim()
        operation = Mock(side_effect=ProbeError('https://private/?token=never-save-this',
                         kind='HTTP_STATUS', details={'http_status': 403, 'url': 'never-save-this'}))
        first = self.queue.request(claim, 'maintenance:members', ['one'], operation)
        second = self.queue.request(claim, 'maintenance:members', ['one'], operation)
        self.assertEqual(first, second)
        self.assertEqual(first, dict(source='maintenance:members', status='FAILED', error_code='ProbeError',
                                    error_kind='HTTP_STATUS', error_details={'http_status': 403}))
        self.assertEqual(operation.call_count, 1)
        self.assertEqual(self.queue.get(claim.job_id)['requests_used'], 1)
        with self.ledger._connect() as conn:
            stored = conn.execute('SELECT result FROM left_right_backfill_requests WHERE ns=%s AND job_id=%s',
                                  (self.ns, claim.job_id)).fetchone()[0]
        self.assertNotIn('never-save-this', stored)
        self.assertEqual(json.loads(stored), first)

    def test_restart_lease_fences_late_completion_and_preserves_budget(self):
        self.submit(mode="collect")
        claim = self.queue.claim()
        self.queue.request(claim, "tushare:daily", ["one"], lambda: dict(rows=1))
        with self.ledger._connect() as conn:
            conn.execute("UPDATE left_right_backfill_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE ns=%s", (self.ns,))
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.get(claim.job_id)["error_code"], "WORKER_LEASE_EXPIRED")
        self.assertEqual(self.queue.finish(claim, status="SUCCEEDED"), "LEASE_LOST")
        with self.assertRaises(Interrupted):
            self.queue.request(claim, "tushare:daily", ["two"], Mock())
        self.assertEqual(self.queue.get(claim.job_id)["requests_used"], 1)

    def test_empty_queue_never_loads_provider_and_disabled_collect_never_calls(self):
        with patch("modules.strategy_hermes.left_right_backfill_collectors.HistoricalTushare") as provider:
            with self.executor() as executor:
                executor.stop.wait(.1)
            self.assertFalse(provider.called)
            self.submit(mode="collect")
            self.assertEqual(self.executor().execute(self.queue.claim()), "FAILED")
            self.assertFalse(provider.called)
        job = self.queue.listing(now=NOW)["jobs"][0]
        self.assertEqual((job["error_code"], job["requests_used"]), ("EXTERNAL_DISABLED", 0))

    def test_pause_requeues_without_resetting_used_budget(self):
        self.submit(mode="collect")
        claim = self.queue.claim()
        self.queue.request(claim, "tushare:daily", ["one"], lambda: {})
        executor = self.executor(paused=lambda: True, external_enabled=lambda: True)
        self.assertEqual(executor.execute(claim), "QUEUED")
        resumed = self.queue.claim()
        operation = Mock()
        self.queue.request(resumed, "tushare:daily", ["one"], operation)
        self.assertFalse(operation.called)
        self.assertEqual(self.queue.get(resumed.job_id)["requests_used"], 1)

    def test_release_pause_during_request_wins_over_stop_and_resumes_saved_receipt(self):
        self.submit(mode="collect")
        claim = self.queue.claim()
        with self.ledger._connect() as conn:
            conn.execute("UPDATE left_right_backfill_jobs SET elapsed_seconds=30 WHERE ns=%s", (self.ns,))
        entered, release, paused = Event(), Event(), Event()
        def first_operation():
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release bounded request")
            return dict(batch="saved-before-release", rows=1)
        operation = Mock(side_effect=first_operation)
        second = Mock(return_value=dict(batch="next-source", rows=1))
        receipts = []
        def collector(ledger, body, config, captures, request, checkpoint):
            receipts.append(request("tushare:daily", ["one"], operation))
            request("tushare:weekly", ["two"], second)
            return []
        builder = Mock(return_value=dict(archives=[], missing=[], expected_items=0))
        executor = self.executor(paused=paused.is_set, external_enabled=lambda: True,
                                 collector=collector, builder=builder)
        states = []
        thread = Thread(target=lambda: states.append(executor.execute(claim)))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            paused.set()
            executor.stop.set()
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(states, ["QUEUED"])
        saved = self.queue.get(claim.job_id)
        self.assertEqual((saved["error_code"], saved["requests_used"]), ("PAUSED", 1))
        self.assertIsNone(saved["finished_at"])
        second.assert_not_called()
        builder.assert_not_called()
        with self.ledger._connect() as conn:
            elapsed = conn.execute("SELECT elapsed_seconds FROM left_right_backfill_jobs WHERE ns=%s", (self.ns,)).fetchone()[0]
            cached = conn.execute("SELECT status,result FROM left_right_backfill_requests WHERE ns=%s", (self.ns,)).fetchall()
        self.assertGreaterEqual(elapsed, 30)
        self.assertEqual(len(cached), 1)
        self.assertEqual((cached[0][0], json.loads(cached[0][1])["receipt"]["batch"]),
                         ("SAVED", "saved-before-release"))
        paused.clear()
        resumed = self.queue.claim()
        self.assertEqual(resumed.job_id, claim.job_id)
        self.assertEqual(self.executor(paused=paused.is_set, external_enabled=lambda: True,
            collector=collector, builder=builder).execute(resumed), "PARTIAL")
        self.assertEqual(operation.call_count, 1)
        second.assert_called_once()
        self.assertEqual(receipts[0]["receipt"]["batch"], "saved-before-release")
        final = self.queue.get(claim.job_id)
        self.assertEqual((final["requests_used"], final["request_budget"]), (2, saved["request_budget"]))
        with self.ledger._connect() as conn:
            final_elapsed = conn.execute("SELECT elapsed_seconds FROM left_right_backfill_jobs WHERE ns=%s", (self.ns,)).fetchone()[0]
        self.assertGreaterEqual(final_elapsed, elapsed)

    def test_cancel_remains_terminal_when_release_pause_and_stop_arrive_together(self):
        self.submit(mode="collect")
        claim = self.queue.claim()
        self.queue.cancel(claim.job_id)
        collector = Mock()
        executor = self.executor(paused=lambda: True, external_enabled=lambda: True, collector=collector)
        executor.stop.set()
        self.assertEqual(executor.execute(claim), "CANCELLED")
        collector.assert_not_called()
        self.assertEqual(self.queue.get(claim.job_id)["requests_used"], 0)

    def test_pause_resume_preserves_total_active_time_budget(self):
        self.submit()
        claim = self.queue.claim()
        with self.ledger._connect() as conn:
            conn.execute("UPDATE left_right_backfill_jobs SET elapsed_seconds=5399 WHERE ns=%s", (self.ns,))
        self.assertEqual(self.executor(paused=lambda: True).execute(claim), "QUEUED")
        resumed = self.queue.claim()
        with self.ledger._connect() as conn:
            conn.execute("UPDATE left_right_backfill_jobs SET last_active_at=clock_timestamp()-interval '2 seconds' WHERE ns=%s",
                         (self.ns,))
        builder = Mock()
        self.assertEqual(self.executor(builder=builder).execute(resumed), "FAILED")
        self.assertFalse(builder.called)
        self.assertEqual(self.queue.get(claim.job_id)["error_code"], "TIME_BUDGET_EXHAUSTED")

    def test_recompute_only_saved_data_and_missing_are_partial(self):
        self.submit()
        builder = Mock(return_value={"archives": [], "missing": [{"trade_date": "2026-11-12", "code": "600000.SH", "reason": "DAILY_MISSING"}]})
        collector, persist = Mock(), Mock()
        self.assertEqual(self.executor(builder=builder, collector=collector, persist=persist).execute(self.queue.claim()), "PARTIAL")
        self.assertFalse(collector.called)
        self.assertFalse(persist.called)
        self.assertEqual(builder.call_count, 2)
        self.assertEqual(builder.call_args.args[1:3], (["2026-11-13"], ["600000.SH"]))

    def test_all_market_executor_persists_bounded_progress_and_uses_bulk_counts(self):
        body = dict(request_id="bulk", mode="recompute", scope="ALL",
                    date_from="2026-11-12", date_to="2026-11-13")
        job = self.queue.submit(body, now=NOW)
        def build(ledger, dates, codes, as_of, *, persist, checkpoint, progress):
            self.assertIsNone(codes)
            self.assertTrue(persist)
            checkpoint()
            progress(processed_stocks=6000, ready_items=12000)
            running = self.queue.get(job["job_id"])
            self.assertEqual(running["result"]["progress"]["processed_stocks"], 6000)
            return dict(archives=[], archives_saved=1, expected_items=12000, ready_items=12000,
                        missing=[], missing_count=0)
        self.assertEqual(self.executor(builder=build).execute(self.queue.claim()), "SUCCEEDED")
        result = self.queue.get(job["job_id"])["result"]
        self.assertEqual((result["ready_items"], result["expected_items"], result["archives_saved"]), (24000,24000,2))
        self.assertEqual(result["progress"]["completed_days"], 2)
        self.assertEqual(result["progress"]["phase"], "complete")

    def test_all_market_empty_universe_and_many_missing_are_partial(self):
        body = dict(request_id="bulk-empty", mode="recompute", scope="ALL",
                    date_from="2026-11-12", date_to="2026-11-13")
        self.queue.submit(body, now=NOW)
        builder = Mock(return_value=dict(archives=[], archives_saved=0, expected_items=12000, ready_items=0,
                                        missing_count=6000, missing=[dict(reason="MISSING")] * 100))
        self.assertEqual(self.executor(builder=builder).execute(self.queue.claim()), "PARTIAL")
        result = self.queue.listing(now=NOW)["jobs"][0]["result"]
        self.assertEqual((result["missing_count"], len(result["missing"])), (12000,100))

    def test_slow_collection_does_not_block_status_and_cancel_fences_archives(self):
        self.submit(mode="collect")
        claim = self.queue.claim()
        started, release = Event(), Event()
        builder, persist = Mock(), Mock()
        def collector(store, body, config, captures, request, checkpoint):
            def slow():
                started.set()
                release.wait(4)
                return dict(rows=1)
            request("tushare:daily", ["one"], slow)
        executor = self.executor(collector=collector, builder=builder, persist=persist, external_enabled=lambda: True)
        outcome = []
        thread = Thread(target=lambda: outcome.append(executor.execute(claim)))
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertEqual(self.queue.listing(now=NOW)["jobs"][0]["status"], "RUNNING")
            self.queue.cancel(claim.job_id)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["CANCELLED"])
        self.assertFalse(builder.called)
        self.assertFalse(persist.called)

    def test_existing_reference_lookup_uses_persisted_scope_column(self):
        self.assertIsNone(_existing_tushare(self.ledger, "daily", dict(ts_code="999999.SH", start_date="20200101", end_date="20200102")))

    def test_board_cache_requires_identical_close_window_and_ranking(self):
        batches = []
        try:
            for overrides in (dict(Type="5"), dict(REnd="1430")):
                scope = dict(Date="2026-11-12", Index="0", st="60", Type="1", Order="1", RStart="0925", REnd="1500") | overrides
                receipt = save_market(self.ledger, "kaipanla", "042", scope,
                    dict(rows=[["899998", "缓存测试", "10", "2"]], trade_days=["2026-11-12"], reported_total=1),
                    1, None, NOW)
                batches.append(receipt["batch"])
            self.assertIsNone(_existing_page(self.ledger, "042", "2026-11-12", 0, None))
        finally:
            with self.ledger._connect() as conn:
                conn.execute("DELETE FROM supplier_market_batches WHERE batch_id=ANY(%s)", (batches,))

    def test_cancel_between_days_prevents_remaining_archive_builds(self):
        self.submit()
        claim = self.queue.claim()
        def build(*args, **kwargs):
            self.queue.cancel(claim.job_id)
            return dict(archives=[], missing=[])
        builder = Mock(side_effect=build)
        self.assertEqual(self.executor(builder=builder).execute(claim), "CANCELLED")
        self.assertEqual(builder.call_count, 1)

    def test_external_disable_during_request_stops_next_call_and_archive(self):
        self.submit(mode="collect")
        claim = self.queue.claim()
        allowed, calls = [True], []
        def collector(store, body, config, captures, request, checkpoint):
            def operation():
                calls.append("one")
                allowed[0] = False
                return dict(rows=1)
            request("tushare:daily", ["one"], operation)
            request("tushare:weekly", ["two"], operation)
        builder = Mock()
        executor = self.executor(collector=collector, builder=builder, external_enabled=lambda: allowed[0])
        self.assertEqual(executor.execute(claim), "FAILED")
        self.assertEqual(self.queue.get(claim.job_id)["error_code"], "EXTERNAL_DISABLED")
        self.assertEqual(calls, ["one"])
        self.assertFalse(builder.called)

    def test_full_collect_real_storage_and_archive_builder_with_mocked_http(self):
        code = "995123.SH"
        with self.ledger._connect() as conn:
            old_reference = {row[0] for row in conn.execute("SELECT batch_id FROM supplier_batches")}
            old_market = {row[0] for row in conn.execute("SELECT batch_id FROM supplier_market_batches")}
        def clean_sources():
            with self.ledger._connect() as conn, conn.transaction():
                refs = [row[0] for row in conn.execute("SELECT batch_id FROM supplier_batches") if row[0] not in old_reference]
                market = [row[0] for row in conn.execute("SELECT batch_id FROM supplier_market_batches") if row[0] not in old_market]
                conn.execute("DELETE FROM supplier_daily_bars WHERE batch_id=ANY(%s)", (refs,))
                conn.execute("DELETE FROM supplier_trade_calendar WHERE batch_id=ANY(%s)", (refs,))
                conn.execute("DELETE FROM supplier_batches WHERE batch_id=ANY(%s)", (refs,))
                conn.execute("DELETE FROM supplier_lr_features WHERE batch_id=ANY(%s)", (market,))
                conn.execute("DELETE FROM supplier_market_batches WHERE batch_id=ANY(%s)", (market,))
        self.addCleanup(clean_sources)
        calls = []
        def post(_url, data, **kwargs):
            api, scope = data["api_name"], data["params"]
            calls.append(api)
            start, end = date.fromisoformat(scope["start_date"]), date.fromisoformat(scope["end_date"])
            rows = []
            for i in range((end - start).days + 1):
                day = start + timedelta(days=i)
                if api == "trade_cal":
                    row = dict(exchange="SSE", cal_date=day.strftime("%Y%m%d"), is_open=int(day.weekday() < 5))
                elif day.weekday() >= 5 or api == "weekly" and day.weekday() != 4:
                    continue
                else:
                    close = 10 + i / 1000
                    row = dict(ts_code=code, trade_date=day.strftime("%Y%m%d"), open=close, close=close,
                        high=close + .2, low=close - .2, pre_close=close - .001, pct_chg=.01,
                        vol=10000 + i, amount=100000 + i, adj_factor=1, turnover_rate=1.5,
                        volume_ratio=1.1, circ_mv=12345, cost_85pct=10)
                # Real post_json parses JSON floating numbers as decimal text.
                rows.append([format(row[field], ".4f") if isinstance(row[field], float) else row[field]
                             for field in data["fields"].split(",")])
            return dict(code=0, data=dict(fields=data["fields"].split(","), items=rows))
        def get(url, **kwargs):
            args = parse_qs(urlsplit(url).query)
            action = args["a"][0]
            if action == "RealRankingInfo":
                self.assertEqual({key: args[key][0] for key in ("Type", "Order", "RStart", "REnd")},
                                 dict(Type="1", Order="1", RStart="0925", REnd="1500"))
            calls.append(action)
            values = ["899999", "历史测试板块", "10", "2"] if action == "RealRankingInfo" else [code[:6], "历史测试股"]
            return dict(errcode=0, Day=[args["Date"][0]], Count=1, list=[values])
        with TemporaryDirectory(dir=os.environ.get("STAGE1_TEST_ROOT")) as directory:
            root = Path(directory)
            config = root / "private.json"
            config.write_text(json.dumps({"token": "frozen-test-token"}))
            config.chmod(0o600)
            for endpoint, action in (("042", "RealRankingInfo"), ("046", "ZhiShuStockList_W8")):
                (root / (endpoint + "_frozen.json")).write_text(json.dumps({"专属接口链接":
                    "https://apphis.longhuvip.com/w1/api/index.php?a=" + action + "&c=ZhiShuRanking"}), encoding="utf-8")
            with patch("modules.strategy_hermes.left_right_backfill_collectors.post_json", side_effect=post), \
                 patch("modules.strategy_hermes.stage2.tushare_bootstrap.post_json", side_effect=post), \
                 patch("modules.strategy_hermes.left_right_backfill_collectors.get_json", side_effect=get):
                job = self.submit("full", mode="collect", date_from="2026-11-13", codes=[code])
                state = self.executor(config=config, captures=root, external_enabled=lambda: True).execute(self.queue.claim())
                saved = self.queue.get(job["job_id"])
                self.assertEqual(state, "SUCCEEDED", saved)
                self.assertEqual(saved["result"]["ready_items"], 2, saved)
                self.assertEqual(saved["result"]["archives_saved"], 1, saved)
                self.assertEqual(saved["result"]["collect_issues"], [], saved)
                self.assertTrue(all(item["status"] == "SAVED" for item in saved["result"]["requests"]), saved)
                self.assertEqual(saved["requests_used"], 12)  # six Tushare, five history days, one member page
                with self.ledger._connect() as conn:
                    archive = json.loads(conn.execute("SELECT body FROM snapshot_state WHERE ns=%s AND dataset='left_right_eod:2026-11-13'", (self.ns,)).fetchone()[0])
                    projected = conn.execute("SELECT api,body FROM supplier_lr_features WHERE code=%s AND trade_date='2026-11-13' ORDER BY api",(code,)).fetchall()
                    self.assertEqual([row[0] for row in projected],['adj_factor','cyq_perf','daily_basic','weekly'])
                    self.assertEqual(json.loads(projected[2][1])['turnover_ratio'],'0.015000')
                    self.assertTrue(json.loads(projected[3][1])['complete'])
                    coverage_before = conn.execute("""SELECT dataset,row_count,source_at,updated_at FROM data_coverage
                        WHERE scope='' AND business_date='2026-11-13' AND dataset IN ('adj_factor','cyq_perf','daily_basic','weekly')
                        ORDER BY dataset""").fetchall()
                    self.assertEqual([(row[0],row[1]) for row in coverage_before],
                        conn.execute("SELECT api,count(*) FROM supplier_lr_features WHERE trade_date='2026-11-13' GROUP BY api ORDER BY api").fetchall())
                self.assertEqual({item["code"] for item in archive["items"]}, {code})
                self.assertEqual({item["status"] for item in archive["items"]}, {"READY"})
                before = len(calls)
                second = self.submit("cached", mode="collect", date_from="2026-11-13", codes=[code])
                self.executor(config=config, captures=root, external_enabled=lambda: True).execute(self.queue.claim())
                self.assertEqual(len(calls), before)
                self.assertEqual(self.queue.get(second["job_id"])["requests_used"], 0)
                with self.ledger._connect() as conn:
                    self.assertEqual(conn.execute("""SELECT dataset,row_count,source_at,updated_at FROM data_coverage
                        WHERE scope='' AND business_date='2026-11-13' AND dataset IN ('adj_factor','cyq_perf','daily_basic','weekly')
                        ORDER BY dataset""").fetchall(),coverage_before)


if __name__ == "__main__":
    unittest.main()
