"""Replay 042 page-length Count and 046 whole-list Count contracts."""
from contextlib import nullcontext
from datetime import date
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from modules.strategy_hermes import left_right_backfill_collectors as collector


DAY = "2026-11-13"
DAYS = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", DAY]


def boards(count):
    return [[str(800000 + index), "板块", "100", "1.25"] for index in range(count)]


class HistoricalPageTests(TestCase):
    def read_page(self, endpoint, offset, rows, count):
        raw = dict(errcode=0, Day=[DAY], Count=count, list=rows)
        scope = dict(Date=DAY, Index=str(offset), st="60" if endpoint == "042" else "1000")
        with patch.object(collector, "history_request", return_value=("https://test.invalid", scope)), \
             patch.object(collector, "get_json", return_value=raw), \
             patch.object(collector, "save_market", side_effect=lambda *args: dict(rows=args[5], batch="saved")) as save:
            value = collector._page(object(), object(), endpoint, DAY, offset, "801027" if endpoint == "046" else None)
        return value, save.call_args.args[4]

    def test_042_later_page_count_is_page_length_not_offset_plus_length(self):
        value, body = self.read_page("042", 240, boards(3), "3")
        self.assertEqual((value["rows"], value["total"]), (3, 3))
        self.assertEqual(body["count_basis"], "PAGE_ROWS")
        self.assertEqual(body["raw"]["Count"], "3")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.read_page("042", 60, boards(3), 63)

    def test_042_empty_tail_is_saved_but_an_empty_first_page_is_not_success(self):
        value, body = self.read_page("042", 120, [], 0)
        self.assertEqual((value["rows"], value["ids"]), (0, []))
        self.assertEqual(body["reported_total"], 0)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.read_page("042", 0, [], 0)

    def test_046_retains_total_and_nonempty_member_validation(self):
        value, body = self.read_page("046", 1000, [["600000", "测试股"]], 1001)
        self.assertEqual(value["total"], 1001)
        self.assertEqual(body["count_basis"], "TOTAL_ROWS")
        for rows, count in (([["600000", "测试股"]], 1), ([], 1000)):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "mismatch"):
                self.read_page("046", 1000, rows, count)

    def test_046_count_accepts_real_2292_total_and_third_page_request_scope(self):
        rows = [[str(600000+i), "测试股"] for i in range(292)]
        value, body = self.read_page("046", 2000, rows, 2292)
        self.assertEqual((value["rows"], value["total"]), (292, 2292))
        self.assertEqual(body["reported_total"], 2292)
        capture, root = Mock(), Mock()
        root.is_absolute.return_value = True
        root.glob.return_value = [capture]
        capture.is_symlink.return_value = False
        capture.stat.return_value.st_size = 200
        capture.read_text.return_value = json.dumps({"专属接口链接":
            "https://apphis.longhuvip.com/w1/api/index.php?a=ZhiShuStockList_W8&c=ZhiShuRanking"})
        with patch.object(collector, "Path", return_value=root):
            _, scope = collector.history_request("/private-captures", "046", DAY, 2000, "801572")
            self.assertEqual((scope["Index"], scope["st"]), ("2000", "1000"))
            _, scope = collector.history_request("/private-captures", "046", DAY, 9000, "801572")
            self.assertEqual(scope["Index"], "9000")
            with self.assertRaisesRegex(ValueError, "invalid historical request scope"):
                collector.history_request("/private-captures", "046", DAY, 10000, "801572")

    def collect_members(self, total, *, fail_second=False):
        requests, issues = [], []
        def page(store, captures, endpoint, day, offset, plate):
            if endpoint == "042":
                return dict(rows=1, total=1, ids=["801572"])
            count = min(1000, total-offset)
            return dict(rows=count, total=total, ids=[str(600000+i) for i in range(offset, offset+count)])
        def request(source, scope, operation):
            requests.append((source, scope[2]))
            if fail_second and source == "kaipanla:046" and scope[2] == 1000:
                return dict(status="FAILED", error_code="ProviderHTTPError")
            return dict(status="SAVED", receipt=operation())
        with patch.object(collector, "_calendar", return_value={day: True for day in DAYS}), \
             patch.object(collector, "_existing_page", return_value=None), \
             patch.object(collector, "_page", side_effect=page):
            collector.collect_boards(object(), dict(date_from=DAY, date_to=DAY),
                date(2026, 2, 1), date(2026, 11, 15), object(), request, lambda: None, issues)
        return [offset for source, offset in requests if source == "kaipanla:046"], issues

    def test_046_2292_members_finish_three_pages_with_the_existing_task_budget(self):
        from modules.strategy_hermes.left_right_backfill import MAX_REQUESTS
        offsets, issues = self.collect_members(2292)
        self.assertEqual(offsets, [0, 1000, 2000])
        self.assertEqual(issues, [])
        self.assertEqual((collector.MEMBER_PAGES, collector.MEMBER_PAGE_SIZE, MAX_REQUESTS), (10, 1000, 12000))

    def test_046_second_page_failure_stops_without_claiming_complete_members(self):
        offsets, issues = self.collect_members(2292, fail_second=True)
        self.assertEqual(offsets, [0, 1000])
        self.assertEqual([(row["source"], row["reason"]) for row in issues],
                         [("kaipanla:046", "REQUEST_FAILED")])

    def test_046_more_than_10000_members_are_partial_after_the_original_ten_page_bound(self):
        value, body = self.read_page("046", 0, [["600000", "测试股"]], 10001)
        self.assertEqual(body["reported_total"], 10001)
        offsets, issues = self.collect_members(10001)
        self.assertEqual(offsets, list(range(0, 10000, 1000)))
        self.assertEqual([(row["source"], row["reason"]) for row in issues],
                         [("kaipanla:046", "PAGE_LIMIT_INCOMPLETE")])

    def test_cached_042_full_page_and_empty_tail_preserve_their_page_scope(self):
        conn, store = Mock(), Mock()
        store._connect.return_value = nullcontext(conn)
        for offset, rows in ((0, boards(60)), (120, [])):
            conn.execute.return_value.fetchone.return_value = (
                "old-batch", json.dumps(dict(rows=rows, trade_days=[DAY], reported_total=len(rows))))
            value = collector._existing_page(store, "042", DAY, offset, None)
            self.assertEqual(value["rows"], len(rows))
            self.assertEqual(value["batch"], "old-batch")

    def collect(self, count, *, reuse_first=False, repeated=False):
        rows, requests, member_ids = boards(count), [], []
        def existing(store, endpoint, day, offset, plate):
            if reuse_first and endpoint == "042" and offset == 0:
                # Exact old persisted shape, without any new completion marker.
                return dict(batch="old-first", rows=60, total=60,
                            ids=[row[0] for row in rows[:60]])
            return None
        def page(store, captures, endpoint, day, offset, plate):
            requests.append((endpoint, day, offset))
            if endpoint == "046":
                member_ids.append(plate)
                return dict(rows=1, total=1, ids=["600000"])
            selected = rows[:60] if repeated and offset else rows[offset:offset+60]
            return dict(rows=len(selected), total=len(selected), ids=[row[0] for row in selected])
        issues = []
        with patch.object(collector, "_calendar", return_value={day: True for day in DAYS}), \
             patch.object(collector, "_existing_page", side_effect=existing), \
             patch.object(collector, "_page", side_effect=page):
            collector.collect_boards(object(), dict(date_from=DAY, date_to=DAY),
                date(2026, 2, 1), date(2026, 11, 15), object(),
                lambda source, scope, operation: dict(status="SAVED", receipt=operation()), lambda: None, issues)
        return requests, member_ids, issues

    def test_243_boards_survive_old_full_first_page_cache_and_all_are_linked(self):
        requests, members, issues = self.collect(243, reuse_first=True)
        self.assertEqual(issues, [])
        for day in DAYS:
            self.assertEqual([offset for endpoint, when, offset in requests if endpoint == "042" and when == day],
                             [60, 120, 180, 240])
        self.assertEqual(set(members), {row[0] for row in boards(243)})
        self.assertEqual(len(members), 243)

    def test_exact_page_multiple_reads_empty_tail_and_repeated_pages_are_reported(self):
        requests, members, issues = self.collect(120)
        self.assertEqual(issues, [])
        self.assertEqual([offset for endpoint, day, offset in requests if endpoint == "042" and day == DAY],
                         [0, 60, 120])
        self.assertEqual(len(members), 120)
        _, members, issues = self.collect(120, repeated=True)
        self.assertEqual({row["reason"] for row in issues}, {"INCONSISTENT_PAGES"})
        self.assertEqual(len(issues), len(DAYS))
        self.assertEqual(len(members), 60)

    def test_page_budget_exhaustion_is_not_marked_complete(self):
        requests, members, issues = self.collect(360)
        self.assertEqual({row["reason"] for row in issues}, {"PAGE_LIMIT_INCOMPLETE"})
        self.assertEqual(len(issues), len(DAYS))
        self.assertEqual(len([row for row in requests if row[0] == "042"]), len(DAYS)*collector.BOARD_PAGES)
        self.assertEqual(len(members), 300)
