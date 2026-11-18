"""Saved observations survive UNKNOWN signals without creating trade decisions."""
from contextlib import nullcontext
import json
import sqlite3
from types import SimpleNamespace
from unittest import TestCase

from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes.stage2.octop_analyst import OctopAnalyst
from modules.strategy_hermes.stage2.octop_decision import render


SID = "kaipanla:801001"
CUTOFF = "2026-11-16T14:30:30+08:00"


def observation(clock="14:05:00", change="-.0222", *, day="2026-11-16", ref=None):
    at = f"{day}T{clock}+08:00"
    return dict(ref=ref or f"{SID}@{at}#batch", observed_at=at,
                board_return_ratio=change, relative_strength="100")


class ObservationHistory(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE events(ns TEXT,kind TEXT,id TEXT,at TEXT,payload TEXT)")
        self.queries = []

        def execute(sql, args):
            self.queries.append((sql, args))
            return self.db.execute(sql.replace("%s", "?"), args)

        self.ledger = SimpleNamespace(ns="test:history", origin="FROZEN",
                                      _connect=lambda: nullcontext(SimpleNamespace(execute=execute)))
        self.current = observation("14:30:00", "-.0249")
        board = dict(sector_id=SID, sector_name="锂电池", source_batch="batch",
                     return_ratio=self.current["board_return_ratio"],
                     observed_at=self.current["observed_at"], strength="100")
        self.market = SimpleNamespace(observed_at=board["observed_at"], snapshot_id="current",
                                      body=dict(sectors=[board], quality={}))
        self.analyst = OctopAnalyst(self.ledger, {"sectors": {}, "version": "test"},
                                    self.market, None, moment(CUTOFF))
        self.request = dict(active_directions={}, tracked_sectors=[], round_slot="14:30",
                            trade_date="2026-11-16")

    def save(self, observed, *, published="2026-11-16T14:06:00+08:00", ns="test:history", sid=SID):
        report = dict(input_meta=dict(octop=dict(market_evidence={sid: observed})))
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                        (ns, "report", str(self.db.total_changes), published, json.dumps(report)))

    def prepare(self):
        return self.analyst.prepare(self.request, None)["evidence"][SID]

    def test_unconfirmed_unmapped_sector_retains_previous_price_and_unknown_signal(self):
        before = observation()
        self.save(before)
        evidence = self.prepare()
        self.assertEqual(evidence["previous"], [before])
        self.assertFalse(evidence["prior_active"])
        self.assertIn("SECTOR_MEMBERS_UNAVAILABLE", evidence["facts"]["quality"])
        self.assertEqual(evidence["facts"]["board_return_change_ratio"], "-0.0027")
        self.assertEqual(evidence["facts"]["comparison_ref"], before["ref"])
        result = render(dict(report_id="current-report", generated_at=CUTOFF, finish_reason="stop",
                             output=json.dumps(dict(sector_id=SID, phase="UNKNOWN",
                                                    evidence_refs=[evidence["current"][0]["ref"]]))), evidence)
        self.assertIn("较14:05日涨幅变化 -0.27个百分点", result["report"]["summary"])
        self.assertEqual(result["normalized"][SID]["phase"], "UNKNOWN")
        self.assertEqual(result["judgments"], [])

    def test_latest_observation_wins_even_when_older_data_was_published_later(self):
        recent = observation()
        self.save(recent)
        self.save(observation("13:05:00"), published="2026-11-16T14:20:00+08:00")
        self.assertEqual(self.prepare()["previous"], [recent])

    def test_repeated_same_time_and_newer_observations_do_not_count_as_comparison(self):
        before = observation()
        self.save(before)
        self.save(self.current, published="2026-11-16T14:30:10+08:00")
        self.save(observation("14:30:00", ref="different-batch"), published="2026-11-16T14:30:11+08:00")
        self.save(observation("14:30:02"), published="2026-11-16T14:30:12+08:00")
        self.save(observation("14:25:00", ref=self.current["ref"]), published="2026-11-16T14:30:13+08:00")
        self.assertEqual(self.prepare()["previous"], [before])

    def test_cutoff_namespace_and_publication_time_isolate_usable_evidence(self):
        before = observation("13:05:00")
        self.save(before)
        self.save(observation("14:20:00"), published=CUTOFF)
        self.save(observation("14:25:00"), published="2026-11-16T14:31:00+08:00")
        self.save(observation("14:15:00"), ns="production")
        self.save(observation("14:10:00"), published="2026-11-16T14:09:00+08:00")
        self.assertEqual(self.prepare()["previous"], [before])

    def test_only_eight_recent_reports_are_read(self):
        self.save(observation("13:05:00"), published="2026-11-16T13:06:00+08:00")
        for minute in range(8):
            self.save(observation(), published=f"2026-11-16T14:{10+minute}:00+08:00", sid="kaipanla:999999")
        evidence = self.prepare()
        self.assertEqual(evidence["previous"], [])
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(self.queries[0][1], ("test:history", CUTOFF, 8))

    def test_cross_day_preserves_timestamp_without_intraday_return_difference(self):
        before = observation("14:30:00", day="2026-11-13")
        self.save(before, published="2026-11-13T14:31:00+08:00")
        evidence = self.prepare()
        self.assertEqual(evidence["previous"], [before])
        self.assertNotIn("board_return_change_ratio", evidence["facts"])
        self.assertNotIn("comparison_observed_at", evidence["facts"])

    def test_malformed_observation_is_skipped_without_hiding_valid_history(self):
        before = observation("13:05:00")
        self.save(before)
        for changes in (dict(ref=None), dict(observed_at="bad"), dict(board_return_ratio="NaN")):
            self.save({**observation(), **changes})
        self.assertEqual(self.prepare()["previous"], [before])

    def test_previous_evidence_does_not_activate_an_inactive_trade_direction(self):
        self.save(observation())
        evidence = self.prepare()
        receipt = dict(report_id="current-report", generated_at=CUTOFF, finish_reason="stop")
        for phase in ("MAINTAIN", "STRENGTHEN", "WEAKEN", "RETREAT"):
            receipt["output"] = json.dumps(dict(sector_id=SID, phase=phase,
                                                fact_refs=["sector_return"], etf_choices=[],
                                                evidence_refs=[evidence["current"][0]["ref"]]))
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, "comparable active"):
                render(receipt, evidence)
