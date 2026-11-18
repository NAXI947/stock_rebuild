"""Saved LR history, exact missing-value semantics and dated sector evidence."""
from contextlib import contextmanager, nullcontext
import copy
from datetime import date, timedelta
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes import left_right_info as info

BANK = dict(id="kaipanla:801027", name="银行", membership_date="2026-11-12",
            source="开盘啦 046 已保存成员", source_batch="saved-046-bank-20261112")


def archive(day="2026-11-12", published="2026-11-13T15:53:03+08:00", items=None):
    return dict(schema_version=info.EOD_SCHEMA, trade_date=day,
        observed_at=day+"T15:00:00+08:00", input_as_of=published, source="Tushare 已保存盘后资料",
        model_versions={"LEFT": "left_v3.1.2_adj", "RIGHT": "right_v3.1.2_adj"},
        evidence_sha256="saved-evidence", source_batches=["daily", "adj", "basic", "chips"],
        items=items if items is not None else [
            dict(code="000001.SZ", name="平安银行", side="RIGHT", score=76.706, status="READY",
                 note="收盘价 11.79 元 · 评分 76.7060", parts={"trend": 82, "memory": 0},
                 reasons=[], sectors=[BANK]),
            dict(code="000001.SZ", name="平安银行", side="LEFT", score=None, status="MISSING",
                 note="评分缺失（缺完整周线、板块五日收益）", reasons=["weekly", "relative_strength"])])


class Store:
    ns = "stock-v4:test:lr-history"

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries = []

    @contextmanager
    def _connect(self):
        yield self

    def transaction(self):
        return nullcontext()

    def execute(self, sql, args=()):
        assert sql.strip().startswith(("SELECT", "SET TRANSACTION")), sql
        self.queries.append((sql, args))
        if sql.startswith("SET"):
            self.selected = []
        elif "SELECT day FROM" in sql:
            self.selected = [(day,) for day in sorted({row.get("trade_date") for row in self.rows if row.get("trade_date")}, reverse=True)[:120]]
        elif "FROM left_right_assessments" in sql:
            self.selected = [(0,)]
        else:
            assert "FROM snapshot_state" in sql and "ns=%s" in sql and "LIMIT 121" in sql
            self.selected = [(row,) for row in self.rows if len(args) == 1 or row.get("trade_date") in args[1]]
        return self

    def fetchall(self):
        return self.selected

    def fetchone(self):
        return self.selected[0] if self.selected else None


class LeftRightInfoTests(TestCase):
    def setUp(self):
        self.store = Store([archive()])

    def read(self, query="", **kwargs):
        return info.read_info(self.store, query, **kwargs)

    def test_defaults_keep_source_times_names_and_unknown_structure_fields(self):
        value = self.read()
        self.assertEqual((value["family"], value["available_dates"], value["selected_trade_date"]),
                         ("RIGHT", ["2026-11-12"], "2026-11-12"))
        row = value["items"][0]
        self.assertEqual(row["name"], "平安银行")
        self.assertEqual(row["setup_score"], "76.706")
        self.assertEqual(row["observed_at"], "2026-11-12T15:00:00+08:00")
        self.assertEqual(row["published_at"], "2026-11-13T15:53:03+08:00")
        self.assertEqual(row["version_id"], "right_v3.1.2_adj")
        self.assertEqual(row["subscores"]["parts"]["memory"], "0")
        self.assertEqual(row["subscores"]["weights"]["trend"], "0.2")
        for key in ("close", "return_ratio", "volume_ratio", "turnover_ratio", "quality"):
            self.assertIsNone(row[key], key)
        self.assertEqual(value["summary"], dict(total=1, valid_score_count=1, max_score="76.706",
                         valid_return_count=0, up_count=0, high_return_count=0))

    def test_left_missing_score_is_visible_and_not_generated(self):
        value = self.read("family=LEFT")
        self.assertEqual(value["total"], 1)
        self.assertIsNone(value["items"][0]["setup_score"])
        self.assertEqual(value["items"][0]["status"], "MISSING")
        self.assertEqual(value["items"][0]["reasons"], ["weekly", "relative_strength"])
        self.assertEqual(value["summary"]["valid_score_count"], 0)
        self.assertIsNone(value["summary"]["max_score"])

    def test_parameters_rejected_before_database_read(self):
        for query in ("family=left", "family=ALL", "family=", "page=0", "page=-1", "page=1.5",
                      "page_size=0", "page_size=101", "page=１２", "page=1000000000",
                      "trade_date=2026-02-30", "trade_date=20261112", "trade_date=2026-1-2",
                      "q="+"x"*81, "family=LEFT&family=RIGHT", "unexpected=x", "page"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.read(query)
        self.assertEqual(self.store.queries, [])

    def test_score_sort_pagination_and_search_do_not_change_summary(self):
        rows = [dict(code=code, side="RIGHT", score=score, name=name, return_ratio=change)
                for code, score, name, change in (
                    ("000004.SZ", None, None, None), ("000003.SZ", "0", "零分股", "0"),
                    ("000002.SZ", "80", "第二股", "0.095"), ("000001.SZ", "80", "第一股", "0.0949"))]
        self.store.rows = [archive(items=rows)]
        first = self.read("page_size=2")
        self.assertEqual([r["ts_code"] for r in first["items"]], ["000001.SZ", "000002.SZ"])
        second = self.read("page=2&page_size=2")
        self.assertEqual([r["ts_code"] for r in second["items"]], ["000003.SZ", "000004.SZ"])
        self.assertEqual((first["total"], first["pages"]), (4, 2))
        self.assertEqual(self.read("page=999&page_size=100")["items"], [])
        self.assertEqual(self.read("q=零分")["items"][0]["setup_score"], "0")
        self.assertEqual(self.read("q=000002.sz")["total"], 1)
        self.assertEqual(self.read("q=第一股")["summary"], first["summary"])
        self.assertEqual(first["summary"]["valid_return_count"], 3)
        self.assertEqual((first["summary"]["up_count"], first["summary"]["high_return_count"]), (2, 1))

    def test_nonfinite_money_ratios_and_booleans_are_missing_but_zero_survives(self):
        rows = [dict(code="000001.SZ", side="RIGHT", score=True, close="NaN", return_ratio=float("inf")),
                dict(code="000002.SZ", side="RIGHT", score=0, close="0", return_ratio=0.0,
                     volume_ratio=0, turnover_ratio="0"),
                dict(code="000003.SZ", side="RIGHT", score="Infinity", close="-1", turnover_ratio=False)]
        self.store.rows = [archive(items=rows)]
        items = self.read()["items"]
        self.assertEqual(items[0]["ts_code"], "000002.SZ")
        self.assertEqual(items[0]["return_ratio"], "0.0")
        self.assertEqual(items[0]["volume_ratio"], "0")
        self.assertIsNone(items[1]["setup_score"])
        self.assertIsNone(items[1]["close"])
        self.assertIsNone(items[1]["return_ratio"])
        self.assertIsNone(items[2]["close"])

    def test_two_saved_dates_latest_first_and_missing_requested_day_stays_empty(self):
        self.store.rows.append(archive("2026-11-11", "2026-11-13T15:42:05+08:00"))
        result = self.read("trade_date=2026-11-11")
        self.assertEqual(result["available_dates"], ["2026-11-12", "2026-11-11"])
        self.assertEqual(result["selected_trade_date"], "2026-11-11")
        missing = self.read("trade_date=2026-11-10")
        self.assertEqual(missing["selected_trade_date"], "2026-11-10")
        self.assertEqual(missing["items"], [])
        self.assertIsNone(missing["batch"]["published_at"])

    def test_duplicate_dates_use_actual_input_as_of_across_timezones(self):
        old = archive(published="2026-11-13T11:00:00+08:00")
        latest = archive(published="2026-11-13T04:00:00+00:00")
        latest["items"][0]["score"] = "81.5"
        self.store.rows = [latest, old]
        with patch.object(info, "read_file", return_value=old):
            value = self.read(eod_file="old.json")
        self.assertEqual(value["items"][0]["setup_score"], "81.5")
        self.assertEqual(value["batch"]["published_at"], "2026-11-13T12:00:00+08:00")

    def test_same_revision_enriched_history_retains_verified_links_and_stable_id(self):
        enriched = archive()
        del self.store.rows[0]["items"][0]["sectors"]
        before = self.read()["items"][0]["assessment_id"]
        with patch.object(info, "read_file", return_value=dict(schema_version=info.HISTORY_SCHEMA, archives=[enriched])):
            result = self.read(history_file="history.json")
        self.assertEqual(result["items"][0]["sectors"], [BANK])
        self.assertEqual(result["items"][0]["assessment_id"], before)

    def test_sector_links_require_saved_id_date_source_and_batch(self):
        row = self.store.rows[0]["items"][0]
        row["sectors"] = [BANK, dict(BANK, name="银行", id="银行"), dict(BANK, source_batch=None),
                          dict(BANK, source=None), dict(BANK, membership_date="2026-11-13"),
                          dict(BANK, membership_date="not-a-date")]
        result = self.read()
        self.assertEqual(result["items"][0]["sectors"], [BANK])
        self.assertIn("LR_SECTOR_LINK_INVALID", result["issues"])
        self.assertEqual(self.read("family=LEFT")["items"][0]["sectors"], [])

    def test_partial_same_revision_enrichment_preserves_other_stock_family_and_score(self):
        self.store.rows[0]["items"].append(dict(code="000002.SZ", side="RIGHT", score="80", status="READY"))
        del self.store.rows[0]["items"][0]["sectors"]
        partial = archive(items=[dict(code="000001.SZ", side="RIGHT", sectors=[BANK])])
        with patch.object(info, "read_file", return_value=dict(schema_version=info.HISTORY_SCHEMA, archives=[partial])):
            right = self.read(history_file="history.json")
            left = self.read("family=LEFT", history_file="history.json")
        self.assertEqual([row["ts_code"] for row in right["items"]], ["000002.SZ", "000001.SZ"])
        self.assertEqual(right["items"][1]["setup_score"], "76.706")
        self.assertEqual(right["items"][1]["sectors"], [BANK])
        self.assertEqual(left["total"], 1)
        self.assertEqual(left["items"][0]["status"], "MISSING")

    def test_same_time_conflicting_evidence_is_not_silently_merged(self):
        other = archive(items=[dict(code="000002.SZ", side="RIGHT", score="99")])
        other["evidence_sha256"] = "different-evidence"
        with patch.object(info, "read_file", return_value=other):
            result = self.read(eod_file="other.json")
        self.assertEqual([row["ts_code"] for row in result["items"]], ["000001.SZ"])
        self.assertIn("LR_ARCHIVE_REVISION_CONFLICT", result["issues"])

    def test_subset_recompute_does_not_change_untouched_row_identity_or_time(self):
        row = self.store.rows[0]["items"][0]
        row.update(published_at="2026-11-13T15:53:03+08:00", evidence_sha256="original-row-evidence")
        before = self.read()["items"][0]
        self.store.rows[0]["input_as_of"] = "2026-11-14T16:00:00+08:00"
        self.store.rows[0]["evidence_sha256"] = "other-stock-updated"
        self.store.rows[0]["items"].append(dict(code="000002.SZ", side="RIGHT", score="20",
            published_at="2026-11-14T16:00:00+08:00", evidence_sha256="other-stock-evidence"))
        after = self.read()["items"][0]
        self.assertEqual(after["assessment_id"], before["assessment_id"])
        self.assertEqual(after["published_at"], before["published_at"])

    def test_read_is_select_only_and_preserves_original_archives(self):
        original = copy.deepcopy(self.store.rows)
        self.read()
        info.load_history(self.store)
        self.assertEqual(self.store.rows, original)
        self.assertTrue(all(sql.strip().startswith(("SELECT", "SET TRANSACTION")) for sql, _ in self.store.queries))
        self.assertEqual(len([sql for sql, _ in self.store.queries if "SELECT body FROM snapshot_state" in sql]), 2)

    def test_invalid_file_is_explicit_and_does_not_hide_valid_database_archive(self):
        for document in ({"schema_version": "wrong", "archives": []},
                         {"schema_version": info.HISTORY_SCHEMA, "archives": [archive()]*121}):
            with self.subTest(document=document["schema_version"]), patch.object(info, "read_file", return_value=document):
                result = self.read(history_file="history.json")
            self.assertEqual(result["total"], 1)
            self.assertIn("LR_HISTORY_FILE_INVALID", result["issues"])
        with patch.object(info, "read_file", side_effect=FileNotFoundError):
            self.assertIn("LR_HISTORY_FILE_MISSING", self.read(history_file="missing.json")["issues"])

    def test_bad_archive_and_item_are_locally_reported(self):
        self.store.rows += [{"schema_version": "wrong", "trade_date": "2026-11-12"}]
        self.store.rows[0]["items"] += [None, {"code": "bad", "side": "RIGHT"}]
        result = self.read()
        self.assertEqual(result["total"], 1)
        self.assertIn("LR_ARCHIVE_INVALID", result["issues"])
        self.assertIn("LR_ITEM_INVALID", result["issues"])

    def test_unknown_metadata_remains_null(self):
        self.store.rows = [dict(schema_version=info.EOD_SCHEMA, trade_date="2026-11-12",
                               items=[dict(code="000001.SZ", side="RIGHT")])]
        row = self.read()["items"][0]
        for key in ("name", "status", "version_id", "observed_at", "published_at", "source", "reasons"):
            self.assertIsNone(row[key], key)
        self.assertEqual(row["subscores"], dict(parts=None, weights=None))

    def test_history_dates_are_bounded_to_latest_120(self):
        first = date(2026, 1, 1)
        self.store.rows = [archive(str(first+timedelta(days=i)), items=[]) for i in range(121)]
        result = info.load_history(self.store)
        self.assertEqual(len(result["archives"]), 120)
        self.assertEqual(result["archives"][0]["trade_date"], str(first+timedelta(days=120)))
        self.assertEqual(result["archives"][-1]["trade_date"], str(first+timedelta(days=1)))
