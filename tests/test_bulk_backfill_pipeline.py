"""Real all-market queue -> paged collection -> legacy LR scoring -> SQL archive."""
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from modules.strategy_hermes.bulk_eod_collect import BulkTushare
from modules.strategy_hermes.left_right_backfill import BackfillExecutor, BackfillQueue
from modules.strategy_hermes.left_right_info import read_info
from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2.left_right_refresh import build_manual_archives
from modules.strategy_hermes.stage2.left_right_eod import _database_input, _points
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")


@unittest.skipUnless(ENV, "requires isolated PostgreSQL STAGE2_TEST_ENV_FILE")
class BulkBackfillPipelineTest(unittest.TestCase):
    def test_all_market_collect_score_archive_and_zero_http_second_job(self):
        self.exercise_pipeline(suspended_days=set())

    def test_suspended_stock_keeps_eighty_actual_bars_and_reuses_complete_warmup(self):
        self.exercise_pipeline(suspended_days={date(2001, 10, 30), date(2001, 11, 13)})

    def exercise_pipeline(self, *, suspended_days):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, "test")
        ns = settings.redis_prefix + "bulk-pipeline:" + uuid4().hex
        ledger = PostgresLedger(replace(settings, namespace=ns), "FROZEN")
        ledger.migrate()
        day = date(2001, 11, 16)
        warmup, code = day - timedelta(days=280), "995124.SH"
        queue = BackfillQueue(ledger, [day.isoformat()])
        calls, source_keys = [], []
        with ledger._connect() as conn:
            previous_market = {row[0] for row in conn.execute("SELECT batch_id FROM supplier_market_batches")}

        def cleanup():
            with ledger._connect() as conn, conn.transaction():
                batches = [row[0] for row in conn.execute("SELECT batch_id FROM supplier_tushare_eod_runs WHERE source_key=ANY(%s)", (source_keys,))]
                refs = [row[0] for row in conn.execute("SELECT batch_id FROM supplier_batches WHERE requested_scope->>'source_key'=ANY(%s)", (source_keys,))]
                market = [row[0] for row in conn.execute("SELECT batch_id FROM supplier_market_batches WHERE provider='kaipanla'")
                          if row[0] not in previous_market]
                conn.execute("DELETE FROM left_right_backfill_requests WHERE ns=%s", (ns,))
                conn.execute("DELETE FROM left_right_backfill_jobs WHERE ns=%s", (ns,))
                conn.execute("DELETE FROM left_right_assessments WHERE ns=%s", (ns,))
                conn.execute("DELETE FROM snapshot_state WHERE ns=%s", (ns,))
                conn.execute("DELETE FROM supplier_daily_bars WHERE batch_id=ANY(%s)", (batches,))
                conn.execute("DELETE FROM supplier_lr_features WHERE batch_id=ANY(%s)", (batches,))
                conn.execute("DELETE FROM supplier_trade_calendar WHERE batch_id=ANY(%s)", (refs,))
                conn.execute("DELETE FROM supplier_tushare_eod_runs WHERE source_key=ANY(%s)", (source_keys,))
                conn.execute("DELETE FROM supplier_tushare_pages WHERE source_key=ANY(%s)", (source_keys,))
                conn.execute("DELETE FROM supplier_batches WHERE batch_id=ANY(%s)", (refs,))
                conn.execute("DELETE FROM supplier_market_batches WHERE batch_id=ANY(%s)", (batches + market,))
        self.addCleanup(cleanup)

        def post(_url, payload, **_kwargs):
            api, scope = payload["api_name"], payload["params"]
            calls.append((api, scope))
            self.assertNotIn("ts_code", scope)
            if api == "trade_cal":
                start, end = date.fromisoformat(scope["start_date"]), date.fromisoformat(scope["end_date"])
                dates = [start + timedelta(days=i) for i in range((end-start).days+1)]
            else:
                self.assertEqual(set(scope), {"trade_date", "limit", "offset"})
                self.assertEqual((scope["limit"], scope["offset"]), (3000, 0))
                dates = [date.fromisoformat(scope["trade_date"])]
            rows = []
            for current in dates:
                i = (current-warmup).days
                if api == "trade_cal":
                    row = dict(exchange="SSE", cal_date=current.strftime("%Y%m%d"), is_open=int(current.weekday()<5))
                else:
                    self.assertLess(current.weekday(), 5)
                    close = Decimal(10) + Decimal(i) / 1000
                    # Another market stock keeps the all-market day nonempty
                    # while the assessed stock is suspended on two sessions.
                    observed_code = "995125.SH" if current in suspended_days and api != "weekly" else code
                    row = dict(ts_code=observed_code, trade_date=current.strftime("%Y%m%d"), open=close, close=close,
                        high=close + Decimal(".2"), low=close - Decimal(".2"), pre_close=close - Decimal(".001"),
                        pct_chg=".01", vol=10000+i, amount=100000+i, adj_factor=1, turnover_rate="1.5",
                        volume_ratio="1.1", circ_mv=12345, cost_85pct=10)
                rows.append([str(row[field]) if isinstance(row[field], Decimal) else row[field]
                             for field in payload["fields"].split(",")])
            return dict(code=0, data=dict(fields=payload["fields"].split(","), items=rows))

        def get(url, **_kwargs):
            args = parse_qs(urlsplit(url).query)
            action = args["a"][0]
            calls.append((action, args))
            values = ["899997", "批量历史板块", "10", "2"] if action == "RealRankingInfo" else [code[:6], "批量历史股"]
            return dict(errcode=0, Day=[args["Date"][0]], Count=1, list=[values])

        with TemporaryDirectory(dir=os.environ.get("STAGE1_TEST_ROOT")) as directory:
            root = Path(directory)
            config = root / "private.json"
            config.write_text(json.dumps(dict(token="bulk-pipeline-fixture-" + uuid4().hex)))
            config.chmod(0o600)
            client = BulkTushare(config)
            source_keys.extend({client.identity(api) for api in ("trade_cal", "daily", "adj_factor", "daily_basic", "cyq_perf", "weekly")})
            for endpoint, action in (("042", "RealRankingInfo"), ("046", "ZhiShuStockList_W8")):
                (root / (endpoint + "_fixture.json")).write_text(json.dumps({"专属接口链接":
                    "https://apphis.longhuvip.com/w1/api/index.php?a=" + action + "&c=ZhiShuRanking"}), encoding="utf-8")
            body = dict(scope="ALL", mode="collect", date_from=day.isoformat(), date_to=day.isoformat())
            executor = BackfillExecutor(queue, config=config, captures=root, external_enabled=lambda: True)
            with patch("modules.strategy_hermes.bulk_eod_collect.post_json", side_effect=post), \
                    patch("modules.strategy_hermes.left_right_backfill_collectors.get_json", side_effect=get):
                job = queue.submit(dict(body, request_id="bulk-first"))
                state = executor.execute(queue.claim())
                saved = queue.get(job["job_id"])
            self.assertEqual(state, "SUCCEEDED", saved)
            self.assertEqual((saved["result"]["ready_items"], saved["result"]["expected_items"],
                              saved["result"]["archives_saved"]), (2, 2, 1), saved)
            self.assertEqual(saved["result"]["collect_issues"], [], saved)
            # The original 280-day scope includes 201 exchange sessions here.
            # Suspended stocks need earlier sessions to retain 80 actual bars.
            self.assertEqual(saved["requests_used"], 1 + 201*4 + 40 + 5 + 1)
            self.assertEqual(saved["requests_used"], len(calls))
            for api in ("daily", "adj_factor", "daily_basic", "cyq_perf"):
                requested = [date.fromisoformat(scope["trade_date"]) for name, scope in calls if name == api]
                self.assertEqual(len(requested), 201)
                self.assertEqual(requested[-1], day)
                self.assertEqual(requested[0], warmup)
            self.assertEqual(len([name for name, _ in calls if name == "weekly"]), 40)
            self.assertLessEqual(len(saved["result"]["requests"]), 100)
            with ledger._connect() as conn:
                rows = conn.execute("SELECT family,status,name,setup_score FROM left_right_assessments WHERE ns=%s ORDER BY family", (ns,)).fetchall()
                snapshots = conn.execute("SELECT count(*) FROM snapshot_state WHERE ns=%s AND dataset LIKE 'left_right_eod%%'", (ns,)).fetchone()[0]
            self.assertEqual({(family, status, name) for family, status, name, score in rows},
                             {("LEFT", "READY", "批量历史股"), ("RIGHT", "READY", "批量历史股")})
            self.assertEqual(snapshots, 0)  # Bulk results use the indexed SQL archive.
            explicit = build_manual_archives(ledger, [day.isoformat()], [code], datetime.now(CHINA))
            self.assertEqual({row["side"]: Decimal(str(row["score"])) for row in explicit["archives"][0]["items"]},
                             {family: score for family, status, name, score in rows})
            observed_at = datetime.now(CHINA)
            evidence = _database_input(ledger, day, [code], observed_at)
            actual = _points(code, evidence["symbols"][code]["daily"], day, observed_at)
            expected_days = [warmup+timedelta(days=i) for i in range(281)
                             if (warmup+timedelta(days=i)).weekday()<5
                             and warmup+timedelta(days=i) not in suspended_days][-80:]
            self.assertEqual([point.trade_date for point in actual], expected_days)
            self.assertEqual(len(actual), 80)
            for family in ("LEFT", "RIGHT"):
                view = read_info(ledger, f"family={family}&trade_date={day.isoformat()}&page_size=1")
                self.assertEqual((view["total"], view["pages"], view["items"][0]["ts_code"]), (1, 1, code))
                self.assertEqual(read_info(ledger, f"family={family}&trade_date={day.isoformat()}&page=2&page_size=1")["items"], [])
            with patch("modules.strategy_hermes.bulk_eod_collect.post_json", side_effect=AssertionError("complete bulk days must be reused")), \
                    patch("modules.strategy_hermes.left_right_backfill_collectors.get_json", side_effect=AssertionError("board pages must be reused")):
                second = queue.submit(dict(body, request_id="bulk-cached"))
                self.assertEqual(executor.execute(queue.claim()), "SUCCEEDED")
                self.assertEqual(queue.get(second["job_id"])["requests_used"], 0)


if __name__ == "__main__":
    unittest.main()
