"""Verify the preserved daily LEFT/RIGHT computation and dated archive boundary."""
from contextlib import nullcontext
import copy
from datetime import date, timedelta
from decimal import Decimal
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from modules.strategy_hermes.simulator import decimal, moment
from modules.strategy_hermes.stage2.left_right_eod import (
    INPUT_SCHEMA, produce, _weekly_inputs, _membership_links, _sector_inputs, _database_input, _number,
)
from modules.strategy_hermes.stage2.left_right_refresh import (
    LeftRightRefresh, refresh, persist_archive, build_manual_archives,
)

DAY = date(2026, 11, 13)
AS_OF = moment("2026-11-13T15:35:00+08:00")
CODE = "000001.SZ"


def fixture():
    start = DAY - timedelta(days=109)
    dates = [start + timedelta(days=i) for i in range(110)
             if (start + timedelta(days=i)).weekday() < 5]
    bars = []
    for index, day in enumerate(dates):
        close = 100 + index
        bars.append(dict(trade_date=day.isoformat(), available_at=f"{day}T15:20:00+08:00",
            open=str(close), high=str(close + 1), low=str(close - 1), close=str(close),
            volume_shares="100000", turnover_ratio=".015", adj_factor="1", circ_mv_cny="100000000000"))
    weekly = []
    for index in range(26):
        end = DAY - timedelta(weeks=25 - index)
        weekly.append(dict(week_end=end.isoformat(), available_at=f"{end}T15:25:00+08:00",
                           complete=True, close=str(120 + index)))
    evidence = dict(schema_version=INPUT_SCHEMA, source="frozen fixture",
        symbols={CODE: dict(daily=bars, weekly=weekly, cost85={
            "trade_date": str(DAY), "available_at": f"{DAY}T15:28:00+08:00", "value": "160"},
            sector_return_5d={"trade_date": str(DAY), "available_at": f"{DAY}T15:28:00+08:00", "value": "0.01"})})
    return [str(day) for day in dates], evidence


class LeftRightArchive(TestCase):
    def test_model_numbers_keep_finite_boundary_without_ledger_scale_limit(self):
        value = "0.0632716482160120274049"
        self.assertEqual(_number(value), float(value))
        self.assertEqual(_number(Decimal(value)), float(value))
        self.assertIsNone(_number(None))
        for bad in (True, 0.1, "not-a-number", "NaN", "sNaN", "Infinity", "-Infinity", "1e9999"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                _number(bad)
        # Cash/accounting validation retains its original precision boundary.
        with self.assertRaisesRegex(ValueError, "decimal outside supported precision"):
            decimal(value)

    def test_complete_frozen_daily_inputs_produce_legacy_scores(self):
        calendar, evidence = fixture()
        saved = produce(DAY, calendar, AS_OF, evidence)
        self.assertEqual(saved["observed_at"], "2026-11-13T15:00:00+08:00")
        self.assertEqual(saved["input_as_of"], AS_OF.isoformat())
        self.assertEqual(saved["model_versions"], {"LEFT": "left_v3.1.2_adj", "RIGHT": "right_v3.1.2_adj"})
        left, right = saved["items"]
        self.assertEqual([row["status"] for row in (left, right)], ["READY", "READY"])
        self.assertEqual((left["score"], right["score"]), (51.1215, 83.5593))
        self.assertEqual(right["parts"]["memory"], 0.0)

    def test_structural_fields_and_row_provenance_come_from_saved_evidence(self):
        calendar, evidence = fixture()
        asset = evidence["symbols"][CODE]
        asset["name"] = "平安银行"
        asset["daily"][-1].update(return_ratio="0.095", volume_ratio="0", turnover_ratio="0.015")
        asset["sectors"] = [dict(id="kaipanla:801027", name="银行", membership_date="2026-11-12",
                                  source="开盘啦046", source_batch="saved-members")]
        saved = produce(DAY, calendar, AS_OF, evidence)
        for row in saved["items"]:
            self.assertEqual(row["name"], "平安银行")
            self.assertEqual(row["close"], asset["daily"][-1]["close"])
            self.assertEqual((row["return_ratio"], row["volume_ratio"], row["turnover_ratio"]), ("0.095", "0", "0.015"))
            self.assertEqual(row["sectors"], asset["sectors"])
            self.assertEqual(row["evidence_sha256"], saved["evidence_sha256"])
            self.assertEqual(row["published_at"], AS_OF.isoformat())
            self.assertEqual(row["observed_at"], "2026-11-13T15:00:00+08:00")
            self.assertEqual(row["quality"]["completed_weeks"], 26)

    def test_old_sector_return_is_not_reused_as_the_target_five_day_window(self):
        calendar, evidence = fixture()
        evidence["symbols"][CODE]["sector_return_5d"]["trade_date"] = "2026-11-12"
        left, right = produce(DAY, calendar, AS_OF, evidence)["items"]
        self.assertIsNone(left["score"])
        self.assertIsNone(left["parts"]["relative_strength"])
        self.assertEqual(right["status"], "READY")

    def test_missing_reasons_are_limited_to_the_assessment_family(self):
        calendar, evidence = fixture()
        asset = evidence["symbols"][CODE]
        asset.pop("weekly")
        asset.pop("sector_return_5d")
        left, right = produce(DAY, calendar, AS_OF, evidence)["items"]
        self.assertEqual(left["status"], "MISSING")
        self.assertIn("WEEKLY_INSUFFICIENT_COMPLETED_WEEKS", left["reasons"])
        self.assertIn("SECTOR_RETURN_UNAVAILABLE", left["reasons"])
        self.assertEqual(right["status"], "READY")
        self.assertEqual(right["reasons"], [])

        calendar, evidence = fixture()
        for row in evidence["symbols"][CODE]["daily"]:
            row["turnover_ratio"] = None
        left, right = produce(DAY, calendar, AS_OF, evidence)["items"]
        self.assertEqual(left["status"], "READY")
        self.assertNotIn("TURNOVER_UNAVAILABLE", left["reasons"])
        self.assertEqual(right["status"], "MISSING")
        self.assertIn("TURNOVER_UNAVAILABLE", right["reasons"])

    def test_missing_factors_are_not_replaced_with_zero_and_no_close_keeps_old_archive(self):
        calendar, evidence = fixture()
        bars = evidence["symbols"][CODE]["daily"]
        bars[-1]["adj_factor"] = None
        saved = produce(DAY, calendar, AS_OF, evidence)
        self.assertTrue(all(row["score"] is None for row in saved["items"]))
        self.assertTrue(all("缺复权因子" in row["note"] for row in saved["items"]))
        bars[-1]["available_at"] = "2026-11-13T15:36:00+08:00"
        self.assertIsNone(produce(DAY, calendar, AS_OF, evidence))

    def test_trading_calendar_and_post_close_availability(self):
        calendar, evidence = fixture()
        with self.assertRaisesRegex(ValueError, "completed trading day"):
            produce(DAY, calendar, AS_OF.replace(minute=34), evidence)
        with self.assertRaisesRegex(ValueError, "completed trading day"):
            produce(DAY, calendar[:-1], AS_OF, evidence)
        evidence["symbols"][CODE]["daily"][-1]["available_at"] = "2026-11-13T14:59:00+08:00"
        with self.assertRaisesRegex(ValueError, "before its close"):
            produce(DAY, calendar, AS_OF, evidence)


class SavedInputTests(TestCase):
    def test_database_reader_checks_cancellation_between_sql_queries(self):
        ledger, conn = Mock(), Mock()
        ledger._connect.return_value = nullcontext(conn)
        conn.transaction.return_value = nullcontext()
        conn.execute.return_value.fetchall.return_value = []
        checkpoint = Mock(side_effect=[None, RuntimeError("cancelled")])
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            _database_input(ledger, DAY, [CODE], AS_OF, checkpoint=checkpoint)
        self.assertEqual(checkpoint.call_count, 2)
        self.assertEqual(conn.execute.call_count, 1)  # Calendar read; stopped before sector reads.

    def calendar(self, count=21):
        start = date(2026, 11, 2)
        return {start+timedelta(days=i): (start+timedelta(days=i)).weekday() < 5 for i in range(count)}

    def daily(self, calendar):
        return [dict(trade_date=str(day), close="50", adj_factor="2",
                     available_at=f"{day}T15:10:00+08:00") for day, opened in calendar.items() if opened]

    def test_completed_week_keeps_legacy_raw_close_and_excludes_open_week(self):
        calendar = self.calendar()
        daily = self.daily(calendar)
        weekly = _weekly_inputs(daily, [], calendar, date(2026, 11, 12))
        self.assertEqual([row["week_end"] for row in weekly], ["2026-11-06"])
        self.assertEqual(weekly[0]["close"], "50")
        saved = [dict(trade_date="2026-11-06", close="49", complete=True,
                      available_at="2026-11-13T15:20:00+08:00", source_batch="weekly-source")]
        weekly = _weekly_inputs(daily, saved, calendar, date(2026, 11, 12))
        self.assertEqual(weekly[0]["close"], "49")
        self.assertEqual(weekly[0]["price_basis"], "LEGACY_UNADJUSTED_CLOSE")
        self.assertEqual(weekly[0]["available_at"], saved[0]["available_at"])

    def test_holiday_week_uses_real_calendar_and_missing_week_is_not_compressed(self):
        calendar = self.calendar()
        calendar[date(2026, 11, 6)] = False
        daily = self.daily(calendar)
        weekly = _weekly_inputs(daily, [], calendar, date(2026, 11, 5))
        self.assertEqual([row["week_end"] for row in weekly], ["2026-11-05"])
        daily = [row for row in daily if not "2026-11-09" <= row["trade_date"] <= "2026-11-13"]
        weekly = _weekly_inputs(daily, [], calendar, date(2026, 11, 20))
        self.assertEqual([row["week_end"] for row in weekly], ["2026-11-20"])
        del calendar[date(2026, 11, 22)]
        self.assertEqual(_weekly_inputs(daily, [], calendar, date(2026, 11, 20)), [])

    def pages(self):
        return [(dict(Date="2026-11-12", PlateID="801027", Index="0"),
                 dict(trade_days=["2026-11-12"], reported_total=3,
                      codes=["000001", "600036"], matches=[["000001", "平安银行"]]), AS_OF, "page-one"),
                (dict(Date="2026-11-12", PlateID="801027", Index="2"),
                 dict(trade_days=["2026-11-12"], reported_total=3,
                      codes=["601398"], matches=[]), AS_OF, "page-two")]

    def test_membership_requires_complete_pages_and_preserves_real_date(self):
        pages = self.pages()
        links, names = _membership_links(pages, [CODE], DAY)
        self.assertEqual(names[CODE], "平安银行")
        link = links[CODE][0]
        self.assertEqual(link["id"], "kaipanla:801027")
        self.assertEqual(link["membership_date"], "2026-11-12")
        self.assertEqual(link["source_batch"], "page-one")
        self.assertEqual(link["source_batches"], ["page-one", "page-two"])
        self.assertEqual(dict(_membership_links(pages[:1], [CODE], DAY)[0]), {})
        self.assertEqual(dict(_membership_links(pages, [CODE], date(2026, 11, 11))[0]), {})
        bad = copy.deepcopy(pages)
        bad[1][1]["codes"] = ["000001"]
        self.assertEqual(dict(_membership_links(bad, [CODE], DAY)[0]), {})

    def test_2292_members_include_third_page_and_missing_second_page_stays_unlinked(self):
        pages = []
        for offset, count in ((0, 1000), (1000, 1000), (2000, 292)):
            codes = [str(600000+i) for i in range(offset, offset+count)]
            matches = [["602291", "第三页测试股"]] if offset == 2000 else []
            pages.append((dict(Date="2026-11-12", PlateID="801572", Index=str(offset)),
                dict(trade_days=["2026-11-12"], reported_total=2292, codes=codes, matches=matches),
                AS_OF, "member-page-"+str(offset)))
        links, names = _membership_links(pages, ["602291.SH"], DAY)
        self.assertEqual(names["602291.SH"], "第三页测试股")
        self.assertEqual(links["602291.SH"][0]["source_batches"],
                         ["member-page-0", "member-page-1000", "member-page-2000"])
        self.assertEqual(links["602291.SH"][0]["membership_date"], "2026-11-12")
        self.assertEqual(dict(_membership_links([pages[0], pages[2]], ["602291.SH"], DAY)[0]), {})
        oversized = []
        for offset in range(0, 10001, 1000):
            codes = [str(600000+i) for i in range(offset, min(offset+1000, 10001))]
            oversized.append((dict(Date="2026-11-12", PlateID="801572", Index=str(offset)),
                dict(trade_days=["2026-11-12"], reported_total=10001, codes=codes,
                     matches=[["610000", "越界页测试股"]] if offset == 10000 else []),
                AS_OF, "member-page-"+str(offset)))
        self.assertEqual(dict(_membership_links(oversized, ["610000.SH"], DAY)[0]), {})

    def test_five_day_sector_returns_compound_and_choose_previous_day_strength(self):
        days = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
        links, _ = _membership_links(self.pages(), [CODE], DAY)
        rows = [("042", dict(Date=day, RStart="0925", REnd="1500"),
                 dict(trade_days=[day], rows=[["801027", "银行", 100, "1"]]), AS_OF, AS_OF, "source-"+day)
                for day in days]
        values, names = _sector_inputs(rows, links, days, days[-2])
        self.assertEqual(values[CODE]["value"], "0.0510100501")
        self.assertEqual(values[CODE]["trade_date"], str(DAY))
        self.assertEqual(names["kaipanla:801027"], "银行")
        self.assertEqual(len(values[CODE]["source_batches"]), 7)
        self.assertEqual(_sector_inputs(rows[:-1], links, days, days[-2])[0], {})
        intraday = [("041", {}, body, at, AS_OF.replace(hour=14), batch)
                    for _, _, body, at, _, batch in rows]
        self.assertEqual(_sector_inputs(intraday, links, days, days[-2])[0], {})
        bad = copy.deepcopy(rows)
        bad[-1][1]["REnd"] = "1430"
        self.assertEqual(_sector_inputs(bad, links, days, days[-2])[0], {})
        bad = copy.deepcopy(rows)
        bad[-1][1]["RStart"] = "1300"
        self.assertEqual(_sector_inputs(bad, links, days, days[-2])[0], {})

    def test_fractional_sector_compound_is_accepted_by_the_actual_score_builder(self):
        days = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
        links, _ = _membership_links(self.pages(), [CODE], DAY)
        changes = ("1.2345", "-0.9876", "0.4321", "2.3456", "-0.1234")
        rows = [("042", dict(Date=day, RStart="0925", REnd="1500"),
                 dict(trade_days=[day], rows=[["801027", "银行", 100, change]]),
                 AS_OF, AS_OF, "source-"+day) for day, change in zip(days, changes)]
        values, _ = _sector_inputs(rows, links, days, days[-2])
        expected = Decimal(1)
        for change in changes:
            expected *= 1+Decimal(change)/100
        self.assertEqual(values[CODE]["value"], str(expected-1))
        self.assertGreater(abs((expected-1).as_tuple().exponent), 12)
        calendar, evidence = fixture()
        evidence["symbols"][CODE]["sector_return_5d"] = values[CODE]
        archive = produce(DAY, calendar, AS_OF, evidence)
        self.assertEqual([row["status"] for row in archive["items"]], ["READY", "READY"])
        self.assertTrue(all(row["quality"]["sector_return_available"] for row in archive["items"]))


class ArchiveStore:
    ns = "stock-v4:test:fixture"

    def __init__(self):
        self.saved, self.calls, self.rows, self.indexed, self.evidence = {}, [], [], {}, {}
        self.calendar = [DAY]

    def _connect(self):
        return nullcontext(self)

    def transaction(self):
        return nullcontext(self)

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        if "pg_advisory_xact_lock" in sql:
            self.rows = []
        elif "SELECT dataset,body" in sql:
            self.rows = [(name, self.saved[name]) for name in args[1] if name in self.saved]
        elif "INSERT INTO snapshot_state" in sql:
            target = self.evidence if args[1].startswith("left_right_evidence:") else self.saved
            target[args[1]] = json.loads(args[3])
            self.rows = []
        elif "INSERT INTO left_right_assessments" in sql:
            self.rows = []
            for row in json.loads(args[1]):
                key = (row["trade_date"], row["code"], row["family"])
                prior = self.indexed.get(key)
                if prior is None or (prior["published_at"] < row["published_at"] and prior["evidence_sha256"] != row["evidence_sha256"]):
                    self.indexed[key] = row
                    self.rows.append((row["code"], row["family"]))
        elif "left_right_assessment_sectors" in sql:
            self.rows = []
        elif "FROM supplier_trade_calendar" in sql:
            self.rows = [(day,) for day in self.calendar if day in args[0]]
        else:
            raise AssertionError(sql)
        return self

    def fetchall(self):
        return self.rows


class ScheduledLeftRightArchive(TestCase):
    def test_before_close_and_incomplete_input_never_replace_prior_archive(self):
        calendar, evidence = fixture()
        evidence["symbols"][CODE]["daily"][-1]["adj_factor"] = None
        ledger = ArchiveStore()
        previous = produce(DAY, calendar, AS_OF, fixture()[1])
        ledger.saved["left_right_eod"] = previous
        with patch("modules.strategy_hermes.stage2.left_right_refresh._database_input", return_value=evidence) as reader:
            self.assertEqual(refresh(ledger, calendar, [CODE], AS_OF.replace(minute=34)), "OUTSIDE_WINDOW")
            reader.assert_not_called()
            self.assertEqual(refresh(ledger, calendar, [CODE], AS_OF), "ARCHIVED")
            reader.assert_called_once_with(ledger, DAY, [CODE], AS_OF)
        self.assertEqual(ledger.saved["left_right_eod"], previous)
        self.assertTrue(all(row["status"] == "MISSING" for row in ledger.saved["left_right_eod:2026-11-13"]["items"]))

    def test_good_evidence_is_upserted_and_worker_rate_limits_rechecks(self):
        calendar, evidence = fixture()
        ledger = ArchiveStore()
        checker = LeftRightRefresh(ledger, "/fixture/trading-calendar.json", CODE)
        with patch("modules.strategy_hermes.stage2.left_right_refresh.trading_days", return_value=calendar), \
             patch("modules.strategy_hermes.stage2.left_right_refresh._database_input", return_value=evidence) as reader:
            self.assertEqual(checker.tick(0, now=AS_OF), "PUBLISHED")
            self.assertIsNone(checker.tick(299, now=AS_OF))
            reader.assert_called_once()
        query, params = ledger.calls[-1]
        self.assertIn("ON CONFLICT", query)
        self.assertIn("evidence_sha256", params[3])
        self.assertEqual(params[:2], ("stock-v4:test:fixture", "left_right_eod"))
        self.assertEqual(params[2], moment("2026-11-13T15:00:00+08:00"))
        self.assertEqual(set(ledger.saved), {"left_right_eod", "left_right_eod:2026-11-13"})

    def test_subset_recompute_keeps_other_stock_and_per_row_provenance(self):
        calendar, evidence = fixture()
        evidence["symbols"]["000002.SZ"] = copy.deepcopy(evidence["symbols"][CODE])
        original = produce(DAY, calendar, AS_OF, evidence)
        ledger = ArchiveStore()
        self.assertEqual(persist_archive(ledger, original)["state"], "PUBLISHED")
        old_second = [row for row in ledger.saved["left_right_eod"]["items"] if row["code"] == "000002.SZ"]
        changed_evidence = fixture()[1]
        changed_evidence["symbols"][CODE]["name"] = "已保存名称"
        newer = produce(DAY, calendar, AS_OF+timedelta(minutes=5), changed_evidence)
        self.assertEqual(persist_archive(ledger, newer)["state"], "PUBLISHED")
        merged = ledger.saved["left_right_eod:2026-11-13"]
        self.assertEqual(len(merged["items"]), 4)
        self.assertEqual([row for row in merged["items"] if row["code"] == "000002.SZ"], old_second)
        self.assertTrue(all(row["published_at"] == (AS_OF+timedelta(minutes=5)).isoformat()
                            for row in merged["items"] if row["code"] == CODE))
        self.assertEqual(persist_archive(ledger, newer)["state"], "UNCHANGED")
        self.assertEqual(persist_archive(ledger, original)["state"], "UNCHANGED")

    def test_historical_manual_build_is_read_only_and_reports_missing_inputs(self):
        ledger = ArchiveStore()
        evidence = fixture()[1]
        evidence["symbols"][CODE].pop("weekly")
        with patch("modules.strategy_hermes.stage2.left_right_refresh._database_input", return_value=evidence):
            result = build_manual_archives(ledger, [str(DAY), "2026-11-14"], [CODE], AS_OF+timedelta(days=1))
        self.assertEqual(len(result["archives"]), 1)
        self.assertEqual({row["reason"] for row in result["missing"]},
                         {"ASSESSMENT_INPUT_MISSING", "TRADING_CALENDAR_UNAVAILABLE"})
        self.assertEqual(ledger.saved, {})
        self.assertTrue(all(sql.strip().startswith("SELECT") for sql, _ in ledger.calls))
