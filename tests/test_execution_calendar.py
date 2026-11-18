"""Wide reference calendars must not rewrite committed live execution history."""
import json
import os
from types import SimpleNamespace
import unittest

from modules.strategy_hermes.ledger import encode, fingerprint
from modules.strategy_hermes.simulator import Rules, moment
from modules.strategy_hermes.stage2.execution_calendar import execution_calendar
import test_stage2_postgres as fixtures
from test_stage1 import A, D1, D2, D3, D4, DAYS, ETF, quote, report, stamp


class Projection(unittest.TestCase):
    def connection(self, row):
        calls = []

        def execute(sql, args):
            calls.append((sql, args))
            return SimpleNamespace(fetchone=lambda: row)

        return SimpleNamespace(execute=execute), calls

    def test_only_future_additions_and_all_committed_dates_survive(self):
        old = [D1, D2, D3]
        reference = ["2026-10-15", D1, D3, D4]
        conn, calls = self.connection((encode(dict(calendar=old)), stamp(D1, "10:36")))
        self.assertEqual(execution_calendar(conn, "fixture", "LIVE", reference), DAYS)
        self.assertEqual(reference, ["2026-10-15", D1, D3, D4])
        self.assertIn("pg_advisory_xact_lock", calls[0][0])
        self.assertEqual(calls[0][1], ("fixture",))

    def test_new_live_and_frozen_runs_keep_original_input(self):
        for row in (None, (encode(dict(calendar=[D1])), None)):
            conn, _ = self.connection(row)
            self.assertIs(execution_calendar(conn, "fixture", "LIVE", DAYS), DAYS)
        conn, calls = self.connection(None)
        self.assertIs(execution_calendar(conn, "fixture", "FROZEN", DAYS), DAYS)
        self.assertEqual(calls, [])

    def test_projection_still_rejects_invalid_reference_calendar(self):
        conn, calls = self.connection((encode(dict(calendar=DAYS)), stamp(D1, "10:36")))
        with self.assertRaises(ValueError):
            execution_calendar(conn, "fixture", "LIVE", ["bad", *DAYS])
        self.assertEqual(calls, [])


@unittest.skipUnless(os.environ.get("STAGE2_TEST_ENV_FILE"), "requires isolated PostgreSQL/Redis")
class ExecutionCalendarIntegration(unittest.TestCase):
    def setUp(self):
        fixtures.StageTwo.setUp(self)
        self.reference = ["2026-10-15", *DAYS]
        self.initial = DAYS[:-1]

    def clean(self):
        fixtures.StageTwo.clean(self)
        with self.store._connect() as conn:
            conn.execute('DELETE FROM maintained_mappings WHERE ns=%s', (self.ns,))

    def seed(self, origin):
        from modules.strategy_hermes.stage2.postgres import BoundConnection
        # Exercise the LIVE branch using fixed fixtures in the isolated test DB.
        # No LIVE settings, provider, clock-triggered job or delivery is enabled.
        self.store.origin = origin
        if origin == "LIVE":
            from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
            publish_mapping(self.store, self.map, {}, at=moment(stamp(D1, "09:00")))
        events = [report("entry", D1, "09:35", [(A, "新增")], [(ETF, A, 1)]),
                  quote("entry-price", D1, "09:36", "10"),
                  report("weak", D1, "10:35", [(A, "减弱")]),
                  quote("same-day", D1, "10:36", "11")]
        with self.store._connect() as conn, conn.transaction():
            self.store._store(BoundConnection(conn), events, self.initial, {ETF: 100}, Rules())
        cycle, = self.store.inspect()["cycles"]
        self.assertEqual(cycle["pending_reason"], "WAIT_T1")
        self.assertEqual(cycle["orders"][-1]["eligible_at"], stamp(D2, "09:30"))
        return cycle

    def frozen_hashes(self):
        with self.store._connect() as conn:
            reports = conn.execute("""SELECT id,fingerprint,payload FROM events
                WHERE ns=%s AND kind='report' AND id IN ('entry','weak') ORDER BY id""",
                (self.ns,)).fetchall()
            fills = conn.execute("""SELECT evidence FROM simulation_fills
                WHERE ns=%s AND quote_id='entry-price'""", (self.ns,)).fetchall()
        self.assertEqual(len(reports), 2)
        self.assertEqual(len(fills), 1)
        return fingerprint(reports), fingerprint(fills)

    def market(self, day, hour, price):
        from modules.strategy_hermes.stage2.inputs import SharedMarket
        at = stamp(day, hour)
        return SharedMarket.read(dict(schema_version="hotspot_market_snapshot_v1",
            snapshot_id="calendar-fixture-"+day+hour.replace(":", ""), revision_seq=1, trade_date=day,
            observed_at=at, source="frozen:feed", mode="PARTIAL_REALTIME", quality={},
            quotes={ETF: dict(observed_at=at, price_cny=price, revision_seq=1)}))

    def test_live_publication_and_quotes_extend_calendar_without_rewriting_history(self):
        from modules.strategy_hermes.stage2.postgres import BoundConnection
        from modules.strategy_hermes.stage2.worker import apply_market, planned_at, publish_round
        before = self.seed("LIVE")
        hashes = self.frozen_hashes()
        with self.assertRaisesRegex(ValueError, "calendar update inserts an already processed trading day"):
            with self.store._connect() as conn, conn.transaction():
                self.store._store(BoundConnection(conn), [], self.reference, {ETF: 100}, Rules())
        self.assertEqual(self.frozen_hashes(), hashes)

        # Native publication and scheduled publication share this exact entry.
        claim = self.store.claim_round(D1, "13:05", planned_at(D1, "13:05"))
        source = report("later", D1, "13:05", [(A, "维持")])
        envelope = dict(input_kind="LIVE", published_at=stamp(D1, "13:05"),
                        report_json=source["report_json"], judgments=source["judgments"])
        published = publish_round(self.store, claim, envelope, self.reference, self.map, None)
        self.assertEqual(published["id"], "later")
        result = apply_market(self.store, moment(stamp(D1, "13:06")), self.reference,
                              self.map, self.market(D1, "13:06", "11"))
        self.assertEqual(result["quote_events"], 1)
        self.assertEqual(self.frozen_hashes(), hashes)
        cycle, = self.store.inspect()["cycles"]
        self.assertEqual(cycle["rule"], before["rule"])
        self.assertEqual(cycle["book"]["fills"], before["book"]["fills"])
        self.assertEqual(cycle["orders"][-1], before["orders"][-1])
        self.assertEqual(cycle["book"]["lots"][0]["sellable_from"], D2)
        with self.store._connect() as conn:
            config, = conn.execute("SELECT config FROM runs WHERE ns=%s", (self.ns,)).fetchone()
        self.assertEqual(json.loads(config)["calendar"], DAYS)
        self.assertEqual(self.reference, ["2026-10-15", *DAYS])

        apply_market(self.store, moment(stamp(D2, "09:30")), self.reference,
                     self.map, self.market(D2, "09:30", "11"))
        cycle, = self.store.inspect()["cycles"]
        self.assertEqual([fill["quantity"] for fill in cycle["book"]["fills"]], [1000, 500])
        self.assertEqual(cycle["book"]["fills"][-1]["filled_at"], stamp(D2, "09:30"))
        self.assertEqual(self.frozen_hashes(), hashes)

    def test_frozen_public_quote_intake_still_rejects_historical_expansion(self):
        self.seed("FROZEN")
        hashes = self.frozen_hashes()
        with self.assertRaisesRegex(ValueError, "calendar update inserts an already processed trading day"):
            self.store.apply_quotes([quote("later", D1, "13:06", "11")], self.reference, {ETF: 100})
        self.assertEqual(self.frozen_hashes(), hashes)
