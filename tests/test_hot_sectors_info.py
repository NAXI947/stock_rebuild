"""Saved ranking, partial coverage and same-day historical stock boundaries."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modules.strategy_hermes import hot_sectors_info as hot
from modules.strategy_hermes.simulator import CHINA

NOW = datetime(2026, 11, 14, 18, tzinfo=CHINA)
SID = "kaipanla:801027"


def day(value, rank=37, amount="0"):
    return dict(trade_date=value, source="saved 041", source_observed_at=value+"T14:30:00+08:00",
                collected_at=value+"T14:31:00+08:00", source_batches=["a"*64], coverage=dict(loaded=1, reported_total=243),
                sectors=[dict(sector_id=SID, name="银行", rank=rank, strength_raw="0", net_cny=amount,
                              return_ratio="0", observed_at=value+"T14:30:00+08:00")])


def stock(value, *, score="76.706", family="RIGHT", code="000001.SZ", membership=None):
    return dict(ts_code=code, name="平安银行", family=family, setup_score=score, status="READY", trade_date=value,
                sectors=[dict(id=SID, membership_date=membership or value, source_batch="b"*64, source="saved 046")])


class HotSectorsTests(unittest.TestCase):
    def result(self, *, query="", saved=None, imported=None, archives=None, now=NOW):
        with patch.object(hot, "_saved_days", return_value=deepcopy(saved or {})), \
             patch.object(hot, "_saved_historical_days", return_value={}), \
             patch.object(hot, "_historical_days", return_value=deepcopy(imported or {})), \
             patch("modules.strategy_hermes.left_right_info.load_sector_groups", return_value=({}, set())), \
             patch("modules.strategy_hermes.left_right_info.load_history", return_value={"archives": archives or [], "issues": []}):
            return hot.read_info(object(), query, now=now)

    def test_latest_trading_day_has_no_stock_even_on_weekend(self):
        body=self.result(saved={d:day(d) for d in ("2026-11-12","2026-11-13")},
                         archives=[dict(trade_date=d,items=[stock(d)]) for d in ("2026-11-12","2026-11-13")])
        newest,older=body["days"]
        self.assertEqual(newest["stock_mode"],"TODAY_SECTORS_ONLY")
        self.assertEqual(newest["top_sectors"][0]["stock_groups"],{"LEFT":[],"RIGHT":[]})
        self.assertIsNone(newest["score_trade_date"])
        self.assertEqual(older["stock_mode"],"HISTORICAL_LR")
        self.assertEqual(older["top_sectors"][0]["stock_groups"]["RIGHT"][0]["ts_code"],"000001.SZ")

    def test_calendar_today_is_also_sector_only_and_future_is_hidden(self):
        body=self.result(saved={d:day(d) for d in ("2026-11-14","2026-11-15")},
                         archives=[dict(trade_date="2026-11-14",items=[stock("2026-11-14")])])
        self.assertEqual(body["available_dates"],["2026-11-14"])
        self.assertEqual(body["days"][0]["stock_mode"],"TODAY_SECTORS_ONLY")

    def test_history_requires_exact_score_day_and_keeps_prior_membership_date(self):
        rows=[stock("2026-11-11"),stock("2026-11-12",membership="2026-11-11"),
              stock("2026-11-12",membership="2026-11-13")]
        body=self.result(saved={d:day(d) for d in ("2026-11-12","2026-11-13")},
                         archives=[dict(trade_date="2026-11-12",items=rows)])
        sector = body["days"][1]["top_sectors"][0]
        self.assertEqual(sector["group_totals"],{"LEFT":0,"RIGHT":1})
        self.assertEqual(sector["stock_groups"]["RIGHT"][0]["sectors"][0]["membership_date"], "2026-11-11")

    def test_thresholds_missing_scores_and_group_limit(self):
        rows=[stock("2026-11-12",score=str(70+i),code=f"00000{i}.SZ") for i in range(7)]
        rows += [stock("2026-11-12",score="69.99"),stock("2026-11-12",score=None),
                 stock("2026-11-12",score="74.99",family="LEFT"),stock("2026-11-12",score="75",family="LEFT")]
        groups,totals=hot._groups(dict(items=rows),SID,"2026-11-12")
        self.assertEqual(totals,{"LEFT":1,"RIGHT":7})
        self.assertEqual(len(groups["RIGHT"]),5)
        self.assertEqual(groups["RIGHT"][0]["setup_score"],"76")
        self.assertEqual(groups["LEFT"][0]["setup_score"],"75")

    def test_search_keeps_original_rank_zero_and_signed_net(self):
        for query in ("q=银行","q=801027"):
            body=self.result(query=query,saved={"2026-11-13":day("2026-11-13",amount="-123.45")})
            row=body["days"][0]["top_sectors"][0]
            self.assertEqual((row["rank"],row["strength_raw"],row["return_ratio"],row["net_cny"]),(37,"0","0","-123.45"))
        self.assertEqual(self.result(query="q=AI",saved={"2026-11-13":day("2026-11-13")})["days"][0]["top_sectors"],[])

    def test_newer_live_round_wins_without_merging_imported_sectors(self):
        old=day("2026-11-13");old["source_observed_at"]="2026-11-13T10:35:00+08:00"
        old["sectors"].append(dict(old["sectors"][0],sector_id="kaipanla:801001",name="old"))
        body=self.result(saved={"2026-11-13":day("2026-11-13")},imported={"2026-11-13":old})
        self.assertEqual(len(body["days"][0]["top_sectors"]),1)

    def test_bad_queries_and_empty_state(self):
        for query in ("days=21","days=0","limit=51","limit=101","days=５","q="+"x"*81,
                      "q=a&q=b","unknown=1","days=no"):
            with self.subTest(query=query),self.assertRaises(ValueError):self.result(query=query)
        empty=self.result()
        self.assertEqual(empty["days"],[]);self.assertIsNone(empty["latest_trade_date"])

    def test_saved_pages_keep_rank_gaps_and_source_clocks(self):
        rows=[("2026-11-13","round",{"Index":"60"},{"reported_total":243,"rows":[["801027","银行",0,1.5,0,0,-50]]},
               "2026-11-13T14:30:00+08:00","2026-11-13T14:31:00+08:00","a"*64)]
        class Connection:
            def execute(self,*args):return SimpleNamespace(fetchall=lambda:rows)
        @contextmanager
        def connect():yield Connection()
        issues=set();result=hot._saved_days(SimpleNamespace(_connect=connect),issues)
        row=result["2026-11-13"]["sectors"][0]
        self.assertEqual((row["rank"],row["return_ratio"],row["net_cny"]),(61,"0.015","-50"))
        self.assertEqual(result["2026-11-13"]["coverage"],{"loaded":1,"reported_total":243})
        self.assertEqual(issues,set())
        rows[0]=(*rows[0][:4],"2026-11-12T14:30:00+08:00",*rows[0][5:])
        self.assertEqual(hot._saved_days(SimpleNamespace(_connect=connect),issues),{})
        self.assertIn("SAVED_BOARD_PAGE_INVALID",issues)

    def test_manual_historical_ranking_uses_closed_window_and_preserves_provider_rank(self):
        scope=dict(Date="2026-11-12",Index="60",Type="1",Order="1",RStart="0925",REnd="1500")
        body=dict(trade_days=["2026-11-12"],reported_total=243,rows=[["801027","银行",100,1.5,0,0,-50]])
        rows=[("2026-11-12","historical-close",scope,body,"2026-11-12T15:00:00+08:00",
               "2026-11-14T10:00:00+08:00","a"*64)]
        issues=set(); result=hot._parse_pages(rows,issues,historical=True)
        bank=result["2026-11-12"]["sectors"][0]
        self.assertEqual((bank["rank"],bank["return_ratio"],bank["net_cny"]),(61,"0.015","-50"))
        self.assertEqual(bank["observed_at"],"2026-11-12T15:00:00+08:00")
        self.assertEqual(result["2026-11-12"]["collected_at"],"2026-11-14T10:00:00+08:00")
        scope["Type"]="5"
        self.assertEqual(hot._parse_pages(rows,issues,historical=True),{})
        self.assertIn("SAVED_BOARD_PAGE_INVALID",issues)

    def test_history_file_validation_and_numeric_gaps(self):
        good=day("2026-11-12");good["sectors"][0]["net_cny"]="NaN"
        with patch.object(hot,"read_file",return_value={"schema_version":"stock-v4-hot-sector-history-v1","days":[good]}):
            result=hot._historical_days("history.json",set())
        self.assertIsNone(result["2026-11-12"]["sectors"][0]["net_cny"])
        good["sectors"][0]["observed_at"]="2026-11-13T14:30:00+08:00"
        issues=set()
        with patch.object(hot,"read_file",return_value={"schema_version":"stock-v4-hot-sector-history-v1","days":[good]}):
            self.assertEqual(hot._historical_days("history.json",issues),{})
        self.assertIn("HOT_HISTORY_DAY_INVALID",issues)

    def historical_pages(self, count, *, empty_tail=False):
        records = [[str(800000+i), "板块"+str(i), 100-i, 1.25] for i in range(count)]
        offsets = list(range(0, count, 60)) + ([count] if empty_tail else [])
        rows = []
        for index in offsets:
            selected = records[index:index+60]
            scope = dict(Date="2026-11-12", Index=str(index), st="60", Type="1", Order="1",
                         RStart="0925", REnd="1500")
            # The pre-fix cache used this name for 042's current-page Count.
            body = dict(trade_days=["2026-11-12"], reported_total=len(selected), rows=selected)
            rows.append(("2026-11-12", "historical-close", scope, body,
                "2026-11-12T15:00:00+08:00", "2026-11-14T10:00:00+08:00", str(index)))
        return rows

    def test_042_complete_pages_infer_243_sectors_without_count_conflict(self):
        issues = set()
        body = hot._parse_pages(self.historical_pages(243), issues, historical=True)["2026-11-12"]
        self.assertEqual(body["coverage"], dict(loaded=243, reported_total=243))
        self.assertEqual([row["rank"] for row in body["sectors"]], list(range(1,244)))
        self.assertEqual(issues, set())

    def test_042_full_page_alone_and_page_gaps_do_not_claim_complete_coverage(self):
        pages = self.historical_pages(243)
        for rows in (pages[:1], pages[:1]+pages[2:]):
            with self.subTest(pages=len(rows)):
                issues = set()
                body = hot._parse_pages(rows, issues, historical=True)["2026-11-12"]
                self.assertIsNone(body["coverage"]["reported_total"])
                self.assertIn("SAVED_BOARD_PAGES_INCOMPLETE", issues)
                self.assertNotIn("SAVED_BOARD_TOTAL_CONFLICT", issues)
        issues = set()
        body = hot._parse_pages(self.historical_pages(120, empty_tail=True), issues,
                                historical=True)["2026-11-12"]
        self.assertEqual(body["coverage"], dict(loaded=120, reported_total=120))
        self.assertEqual(issues, set())
