"""Exercise bounded post-signal collection and deterministic simulated execution."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import os
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

from modules.strategy_hermes.signal_cycle import apply_report, create_cycle, fill
from modules.strategy_hermes.simulator import Calendar, Rules, buy, moment, new_book
from modules.strategy_hermes.stage2.inputs import SharedMarket
from modules.strategy_hermes.stage2 import pending_quotes as followup

DAY = "2026-11-17"
CODE = "512800.SH"
DAYS = ["2026-11-13", "2026-11-16", DAY, "2026-11-18"]
CALENDAR = Calendar(DAYS)
KEY = "stock-v4:test:source:market"


def at(clock, day=DAY):
    return moment(f"{day}T{clock}+08:00")


def cycle(signal="13:06:16", *, sellable="2026-11-16"):
    rule = Rules()
    book = new_book(rule)
    buy(book, rule, 11100, Decimal("0.899"), at("14:30:05", "2026-11-13").date(),
        moment(sellable + "T09:30:00+08:00").date())
    c = dict(etf=CODE, primary_sector="bank", status="OPEN", unit=100,
             rule=asdict(rule), book=book, orders=[], reduced=False)
    apply_report(c, dict(id="report-1305", at=at(signal).isoformat(),
                         sectors={"bank": {"state": "WEAK"}}))
    return c


def market(clock):
    return SharedMarket.read(dict(schema_version="hotspot_market_snapshot_v1", snapshot_id="sample-"+clock.replace(":", ""),
        trade_date=DAY, observed_at=at(clock).isoformat(), mode="PARTIAL_REALTIME", source="tickflow",
        quotes={CODE: {"price_cny": "0.913", "observed_at": at(clock).isoformat()}}, sectors=[]))


class QuoteFollowup(TestCase):
    def setUp(self):
        gate = patch.dict(os.environ, {"DISABLE_EXTERNAL_PROVIDER_HTTP": "false"})
        gate.start()
        self.addCleanup(gate.stop)
        self.ledger = Mock(ns="stock-v4:test:followup", origin="LIVE")
        self.conn = Mock()
        self.conn.execute.return_value.fetchone.return_value = (True,)
        self.ledger._connect.side_effect = lambda: nullcontext(self.conn)
        self.cache = Mock()
        self.keys = followup.run_keys(self.ledger.ns, DAY, "13:05")
        self.anchor = dict(slot="13:05", at=at("13:06:16").isoformat(), report_id="report-1305", keys=self.keys)
        self.records = {}
        self.old = market("13:05:09")

    def perform(self, now="13:06:46", *, cycles=None, fresh=None, failure=None, paused=False, anchors=None):
        current = at(now)
        saved = []
        with patch.object(followup, "read_state", return_value=(anchors or [self.anchor], cycles or [cycle()], self.records)), \
             patch.object(followup, "shared_market", return_value=self.old), \
             patch.object(followup, "RequestLog") as budget, \
             patch.object(followup, "quote_pool", return_value={"batch": "batch-1"}) as provider, \
             patch.object(followup, "merge_quotes", return_value=(fresh or market("13:06:40")).body), \
             patch.object(followup, "publish_snapshot", return_value=(KEY, True, "new")) as publish, \
             patch.object(followup, "save_outcome", side_effect=lambda ledger, key, result: saved.append((key, result))) as checkpoint:
            def call(source, scope, operation, *, limit):
                record = self.records.get(budget.call_args.args[1], {}).get(source)
                if record and record["status"] == "SAVED":
                    return record["result"]
                if failure:
                    raise failure
                return operation()
            budget.return_value.call.side_effect = call
            result = followup.tick(self.ledger, self.cache, KEY, DAYS, "/fixture/tickflow", 
                                   paused=lambda: paused, clock=lambda: current)
        return result, provider, publish, budget, saved

    def test_next_real_source_quote_sells_5500_once(self):
        c = cycle()
        requested = at("13:06:16")
        self.assertEqual(c["orders"][0]["target"], 5500)
        self.assertEqual(followup.missing_prices({CODE: requested}, self.old, CALENDAR, at("13:06:46")), [CODE])
        result, provider, publish, budget, saved = self.perform(cycles=[c])
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(result["covered"], [CODE])
        self.assertEqual(result["physical_requests"], 1)
        provider.assert_called_once_with(self.ledger, "/fixture/tickflow")
        self.assertEqual(budget.return_value.call.call_args.kwargs["limit"], 1)
        quote, = market("13:06:40").quote_events({CODE: 100}, at("13:06:46"))[0]
        fill(c, quote, CALENDAR)
        fill(c, quote, CALENDAR)
        self.assertEqual(c["orders"][0]["status"], "FILLED")
        self.assertEqual(c["book"]["lots"][0]["remaining"], 5600)
        self.assertEqual(len(c["book"]["fills"]), 1)
        self.assertEqual(c["book"]["fills"][0]["filled_at"], at("13:06:40").isoformat())
        self.assertEqual(len(saved), 1)

    def failed(self, slot="14:05", planned="14:05:00"):
        return dict(slot=slot, at=at(planned).isoformat(), report_id=None, sells_only=True,
                    keys=followup.run_keys(self.ledger.ns, DAY, slot))

    def test_failed_round_prices_an_existing_sell_from_its_own_budget(self):
        failed = self.failed()
        self.old = market("14:05:02")
        for anchors in ([failed, self.anchor], [failed]):
            result, provider, publish, budget, saved = self.perform("14:15:04", anchors=anchors, fresh=market("14:15:03"),
                                                                    cycles=[cycle("14:05:03")])
            self.assertEqual((result["status"], result["slot"], result["report_id"], result["failed_round"], result["covered"]),
                             ("PUBLISHED", "14:05", None, True, [CODE]))
            self.assertEqual((result["run_key"], budget.call_args.args[1]), (failed["keys"][0], failed["keys"][0]))
            provider.assert_called_once()
        # The first attempt is timed from the planned start of the failed round.
        self.assertIsNone(self.perform("14:05:29", anchors=[failed], cycles=[cycle("14:05:03")])[0])

    def test_failed_round_sample_refreshes_only_the_quotes_of_its_sells(self):
        other = "510001.SH"
        def sample(clock, price):
            body = deepcopy(market(clock).body)
            body["quotes"][other] = {"price_cny": price, "observed_at": at(clock).isoformat()}
            return SharedMarket.read(body)
        self.old = sample("14:05:02", "10")
        _, _, publish, _, _ = self.perform("14:15:04", anchors=[self.failed()], cycles=[cycle("14:05:03")],
                                           fresh=sample("14:15:03", "11"))
        quotes = publish.call_args.args[2]["quotes"]
        self.assertEqual((quotes[CODE]["observed_at"], quotes[other]),
                         (at("14:15:03").isoformat(), {"price_cny": "10", "observed_at": at("14:05:02").isoformat()}))
        partial = publish.call_args.args[2]["snapshot_id"]
        # A published round still shares the whole sample, under an identity of its own.
        _, _, publish, _, _ = self.perform("14:15:04", cycles=[cycle("14:05:03")], fresh=sample("14:15:03", "11"))
        self.assertEqual(publish.call_args.args[2]["quotes"][other]["price_cny"], "11")
        self.assertNotEqual(publish.call_args.args[2]["snapshot_id"], partial)

    def test_failed_round_never_prices_a_buy_and_falls_back_when_spent(self):
        waiting = dict(etf=CODE, primary_sector="bank", status="WAIT_BUY", unit=100, rule=asdict(Rules()),
                       book=new_book(Rules()), reduced=False,
                       orders=[dict(id="buy", action="BUY", status="PENDING", requested_at=at("13:06:16").isoformat(),
                                    eligible_at=at("13:06:16").isoformat(), remaining=None, target=None)])
        failed = self.failed()
        self.old = market("13:05:09")
        result, provider, _, _, _ = self.perform("14:15:04", cycles=[waiting], anchors=[failed])
        self.assertIsNone(result)
        provider.assert_not_called()
        # With a published round behind it the buy keeps the follow-up it always had.
        result, _, _, budget, _ = self.perform("14:15:04", cycles=[waiting], anchors=[failed, self.anchor],
                                               fresh=market("14:15:03"))
        self.assertEqual((result["slot"], result["report_id"], "failed_round" in result), ("13:05", "report-1305", False))
        # A sell whose failed round has used its three attempts falls back the same way.
        for key in failed["keys"]:
            self.records[key] = {followup.SOURCE: dict(status="FAILED", started_at=at("14:10:00").isoformat())}
        result, _, _, budget, _ = self.perform("14:15:04", anchors=[failed, self.anchor], fresh=market("14:15:03"))
        self.assertEqual((result["slot"], result["run_key"]), ("13:05", self.keys[0]))
        self.assertIsNone(self.perform("14:15:04", anchors=[failed])[0])

    def test_last_round_spreads_its_three_calls_to_1450_while_a_sell_waits(self):
        keys = followup.run_keys(self.ledger.ns, DAY, "14:30")
        late = dict(slot="14:30", at=at("14:33:00").isoformat(), report_id="report-1430", keys=keys)
        sell, stale = [cycle("14:33:00")], market("14:30:09")
        self.old = stale
        used = []
        for now, key in (("14:33:59", None), ("14:34:00", keys[0]), ("14:37:59", None), ("14:38:00", keys[1]),
                         ("14:49:59", None), ("14:50:00", keys[2])):
            fresh = market("14:49:58") if now == "14:50:00" else stale
            result, provider, publish, _, _ = self.perform(now, anchors=[late], cycles=sell, fresh=fresh)
            self.assertEqual(result and result["run_key"], key, now)
            if key:
                # The 14:34 and 14:38 samples bring no price after the signal; the one at 14:50 does.
                self.assertEqual(result["status"], "PUBLISHED" if key == keys[2] else "NO_NEW_PENDING_PRICE")
                self.records[key] = {followup.SOURCE: dict(status="SAVED", started_at=at(now).isoformat()),
                                     followup.OUTCOME: dict(status="SAVED")}
                used.append(provider.call_count)
        self.assertEqual((used, publish.call_count), ([1, 1, 1], 1))
        self.assertIsNone(self.perform("14:55:00", anchors=[late], cycles=sell)[0])
        filled = sell[0]
        fill(filled, dict(id="q-1450", etf=CODE, observed_at=at("14:49:58").isoformat(), received_at=at("14:50:01").isoformat(),
                          price_cny="0.913", source="tickflow", tradeable=True, revision=0), CALENDAR)
        self.assertEqual(filled["orders"][-1]["status"], "FILLED")

    def test_last_round_schedule_holds_for_failed_rounds_late_restarts_and_nothing_else(self):
        keys = followup.run_keys(self.ledger.ns, DAY, "14:30")
        failed = self.failed("14:30", "14:30:00")
        self.old = market("14:05:02")
        self.assertIsNone(self.perform("14:30:59", anchors=[failed], cycles=[cycle("14:05:03")])[0])
        result = self.perform("14:31:00", anchors=[failed], cycles=[cycle("14:05:03")], fresh=market("14:30:58"))[0]
        self.assertEqual((result["run_key"], result["failed_round"]), (keys[0], True))
        # A worker that was down until 14:49:30 still leaves real time before the last call.
        published = at("14:33:00")
        records = {keys[0]: {followup.SOURCE: dict(status="FAILED", started_at=at("14:34:00").isoformat())},
                   keys[1]: {followup.SOURCE: dict(status="FAILED", started_at=at("14:49:30").isoformat())}}
        self.assertIsNone(followup.choose_attempt(published, records, keys, at("14:50:29"), True))
        self.assertEqual(followup.choose_attempt(published, records, keys, at("14:50:30"), True), keys[2])
        self.assertEqual([due.time().isoformat() for due, _ in followup.schedule(at("14:47:00"), True)],
                         ["14:48:00", "14:52:00", "14:53:00"])
        # A buy alone in the 14:30 round, and a sell in any other round, keep 30, 120 and 300 seconds.
        waiting = dict(etf=CODE, primary_sector="bank", status="WAIT_BUY", unit=100, rule=asdict(Rules()),
                       book=new_book(Rules()), reduced=False,
                       orders=[dict(id="buy", action="BUY", status="PENDING", requested_at=at("14:33:00").isoformat(),
                                    eligible_at=at("14:33:00").isoformat(), remaining=None, target=None)])
        late = dict(slot="14:30", at=at("14:33:00").isoformat(), report_id="report-1430", keys=keys)
        self.old = market("14:30:09")
        self.assertIsNone(self.perform("14:33:29", anchors=[late], cycles=[waiting])[0])
        self.assertEqual(self.perform("14:33:30", anchors=[late], cycles=[waiting], fresh=market("14:33:28"))[0]["run_key"], keys[0])
        early = dict(self.anchor, slot="14:05", at=at("14:08:00").isoformat(), keys=followup.run_keys(self.ledger.ns, DAY, "14:05"))
        self.old = market("14:05:09")
        self.assertEqual(self.perform("14:08:30", anchors=[early], cycles=[cycle("14:08:00")],
                                      fresh=market("14:08:28"))[0]["run_key"], early["keys"][0])

    def test_http_disabled_skips_provider_budget_and_database(self):
        with patch.dict(os.environ, {"DISABLE_EXTERNAL_PROVIDER_HTTP": "true"}):
            result, provider, publish, budget, saved = self.perform()
        self.assertIsNone(result)
        self.ledger._connect.assert_not_called()
        provider.assert_not_called()
        publish.assert_not_called()
        budget.assert_not_called()
        self.assertEqual(saved, [])

    def test_http_disabled_immediately_before_request_never_calls_provider(self):
        with patch.object(followup, "provider_enabled", side_effect=[True, False]):
            result, provider, publish, budget, saved = self.perform()
        self.assertEqual((result["status"], result["physical_requests"]), ("COLLECTION_FAILED", 0))
        provider.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(saved, [])

    def test_closed_paused_and_pre_signal_prices_never_collect_or_fill(self):
        for clock in ("09:29:59", "11:31:00", "12:59:59", "15:00:01"):
            result, provider, publish, _, _ = self.perform(clock)
            self.assertIsNone(result)
            provider.assert_not_called()
            publish.assert_not_called()
        result, provider, _, _, _ = self.perform(paused=True)
        self.assertIsNone(result)
        provider.assert_not_called()
        result, _, publish, _, saved = self.perform(fresh=self.old)
        self.assertEqual(result["status"], "NO_NEW_PENDING_PRICE")
        publish.assert_not_called()
        self.assertEqual(saved[0][1]["covered"], [])

    def test_t1_locked_skips_and_old_order_unlocks_on_current_round(self):
        result, provider, _, _, _ = self.perform(cycles=[cycle(sellable="2026-11-18")])
        self.assertIsNone(result)
        provider.assert_not_called()
        c = cycle()
        c["orders"][0]["requested_at"] = at("14:31:00", "2026-11-16").isoformat()
        self.old = None
        self.assertEqual(followup.eligible_orders([c], CALENDAR, at("13:06:46")),
                         {CODE: at("14:31:00", "2026-11-16")})
        result, provider, _, _, _ = self.perform(cycles=[c])
        self.assertEqual(result["status"], "PUBLISHED")
        provider.assert_called_once()

    def test_actual_attempt_spacing_and_three_call_bound_survive_restart(self):
        published = at("13:06:16")
        self.assertIsNone(followup.choose_attempt(published, {}, self.keys, at("13:06:45")))
        self.assertEqual(followup.choose_attempt(published, {}, self.keys, at("13:49:00")), self.keys[0])
        self.records[self.keys[0]] = {followup.SOURCE: dict(status="FAILED", started_at=at("13:49:00").isoformat())}
        self.assertIsNone(followup.choose_attempt(published, self.records, self.keys, at("13:50:29")))
        self.assertEqual(followup.choose_attempt(published, self.records, self.keys, at("13:50:30")), self.keys[1])
        self.records[self.keys[1]] = {followup.SOURCE: dict(status="STARTED", started_at=at("13:50:30").isoformat())}
        self.assertIsNone(followup.choose_attempt(published, self.records, self.keys, at("13:53:29")))
        self.assertEqual(followup.choose_attempt(published, self.records, self.keys, at("13:53:30")), self.keys[2])
        self.records[self.keys[2]] = {followup.SOURCE: dict(status="FAILED", started_at=at("13:53:30").isoformat())}
        self.assertIsNone(followup.choose_attempt(published, self.records, self.keys, at("14:00:00")))
        self.assertEqual(len(set(sum([followup.run_keys(self.ledger.ns, DAY, slot)
            for slot in ("09:35", "10:35", "13:05", "14:05", "14:30")], []))), 15)

    def test_restart_replays_saved_provider_batch_without_calling_provider(self):
        self.records[self.keys[0]] = {followup.SOURCE: dict(status="SAVED",
            started_at=at("13:06:46").isoformat(), result={"batch": "batch-before-crash"})}
        result, provider, publish, _, saved = self.perform("13:07:00")
        self.assertEqual((result["status"], result["physical_requests"]), ("PUBLISHED", 0))
        self.assertEqual(result["supplier_batch"], "batch-before-crash")
        provider.assert_not_called()
        publish.assert_called_once()
        self.records[self.keys[0]][followup.OUTCOME] = {"status": "SAVED"}
        result, provider, publish, _, saved = self.perform("13:07:01")
        self.assertIsNone(result)
        provider.assert_not_called()

    def test_provider_failure_is_local_and_existing_source_sample_is_not_republished(self):
        result, provider, publish, _, saved = self.perform(failure=followup.ProbeError("provider timeout"))
        self.assertEqual(result["status"], "COLLECTION_FAILED")
        self.assertEqual(result["error_type"], "ProbeError")
        publish.assert_not_called()
        self.assertEqual(saved, [])
        self.old = market("13:06:40")
        result, provider, publish, _, _ = self.perform()
        self.assertIsNone(result)
        provider.assert_not_called()

    def test_quote_only_refresh_preserves_other_fact_values_and_source_clocks(self):
        previous = market("13:05:09")
        previous.body.update(stock_research={"observed_at": at("13:05:42").isoformat(), "quotes": {"600000.SH": {"last": "10"}}},
                             iopv={"readings": {CODE: {"at": "13:05:10", "iopv": "0.913"}}})
        previous.body["sectors"] = [{"sector_id": "bank", "observed_at": at("13:05:40").isoformat()}]
        previous.body["quality"] = {"stock_quotes": "AVAILABLE", "iopv": "AVAILABLE", "board_reported_total": 243}
        before = deepcopy(previous.body)
        refreshed = market("13:06:40").body
        refreshed["quality"] = {"etf_quote_count": 1633, "fresh_etf_count": 1633}
        with patch.object(followup, "assemble", return_value=refreshed):
            merged = followup.merge_quotes(self.ledger, DAY, previous, {"batch": "quote-batch"})
        for name in ("stock_research", "iopv", "sectors"):
            self.assertEqual(merged[name], before[name])
        self.assertEqual(merged["quality"]["stock_quotes"], "AVAILABLE")
        self.assertEqual(merged["provenance"]["pending_quotes"], "quote-batch")
        self.assertEqual(previous.body, before)

    def test_future_source_price_is_not_used_and_buy_with_insufficient_funds_does_not_poll(self):
        result, _, publish, _, _ = self.perform(fresh=market("13:10:00"))
        self.assertEqual(result["status"], "NO_NEW_PENDING_PRICE")
        publish.assert_not_called()
        c = cycle()
        c["orders"][0]["last_block"] = "INSUFFICIENT_CASH_FOR_SELL_FEE"
        self.assertEqual(followup.eligible_orders([c], CALENDAR, at("13:06:46")), {})

    def test_cash_block_skips_extra_collection_but_next_shared_quote_can_fill(self):
        report = dict(id="new-buy", at=at("13:06:16").isoformat(), sectors={"bank": {"state": "UP"}})
        c = create_cycle(CODE, 100, Rules(), report, {"sector_id": "bank"}, [])
        expensive = market("13:06:40")
        expensive.body["quotes"][CODE]["price_cny"] = "200"
        quote, = expensive.quote_events({CODE: 100}, at("13:06:46"))[0]
        fill(c, quote, CALENDAR)
        self.assertEqual(c["orders"][0]["last_block"], "INSUFFICIENT_CASH_FOR_ONE_LOT")
        self.assertEqual(followup.eligible_orders([c], CALENDAR, at("13:06:46")), {})
        cheaper = market("14:05:40")
        cheaper.body["quotes"][CODE]["price_cny"] = "80"
        quote, = cheaper.quote_events({CODE: 100}, at("14:05:46"))[0]
        fill(c, quote, CALENDAR)
        self.assertEqual((c["status"], c["orders"][0]["status"]), ("OPEN", "FILLED"))
        self.assertEqual(c["book"]["fills"][0]["quantity"], 100)
        self.assertNotIn("last_block", c["orders"][0])

    def test_worker_consumes_published_quote_without_restarting_analysis(self):
        self.run_worker_followup(disabled=False)

    def test_worker_does_not_invoke_followup_when_http_disabled(self):
        self.run_worker_followup(disabled=True)

    def run_worker_followup(self, *, disabled):
        from modules.strategy_hermes.stage2 import schedule
        ledger = SimpleNamespace(origin="LIVE", settings=SimpleNamespace(env="production"))
        args = SimpleNamespace(received_at=None, once=True, poll_seconds=2, qq=False, trial_day=None,
            tickflow_config="fixture", kaipanla_captures="fixture", stock_research=False, findesk_config=None,
            manual_queue=False, octop_stdio=False, trigger_mode="octop", calendar="fixture",
            mapping="fixture", snapshot_key=KEY, snapshot_file=None)
        with patch.dict(os.environ, {"DISABLE_EXTERNAL_PROVIDER_HTTP": str(disabled).lower()}), \
             patch.object(schedule, "connection", return_value=nullcontext(Mock())), \
             patch("modules.strategy_hermes.stage2.native_rounds.NativeRounds"), \
             patch("modules.strategy_hermes.stage2.native_rounds.native_tick", return_value={"state": "IDLE"}) as native, \
             patch.object(schedule, "runtime_calendar", return_value=DAYS), \
             patch.object(schedule, "live_mapping", return_value={}), \
             patch.object(schedule, "shared_market", side_effect=[self.old, market("13:06:40")]), \
             patch("modules.strategy_hermes.stage2.worker.apply_market", return_value={"quote_events": 1}) as apply, \
             patch.object(followup, "tick", return_value={"status": "PUBLISHED"}) as tick, \
             patch.object(schedule, "dispatch", return_value="EMPTY"), \
             patch.object(schedule, "scheduled_tick", side_effect=AssertionError("duplicate analysis")), \
             patch("builtins.print"):
            self.assertEqual(schedule.serve(ledger, Mock(), args), 0)
        native.assert_called_once()
        if disabled:
            tick.assert_not_called()
            apply.assert_called_once()
            return
        tick.assert_called_once()
        self.assertEqual(apply.call_count, 2)
        self.assertEqual(apply.call_args.args[4].observed_at, at("13:06:40").isoformat())


@skipUnless(os.environ.get("STAGE2_TEST_ENV_FILE"), "requires isolated PostgreSQL/Redis test environment")
class QuoteFollowupDatabase(TestCase):
    def setUp(self):
        gate = patch.dict(os.environ, {"DISABLE_EXTERNAL_PROVIDER_HTTP": "false"})
        gate.start()
        self.addCleanup(gate.stop)

    def test_saved_request_and_checkpoint_survive_restart_without_duplicate_fill(self):
        from test_stage2_postgres import StageTwo
        from test_stage1 import A, D1, D2, DAYS as FROZEN_DAYS, ETF, report, stamp
        from modules.strategy_hermes.stage2.worker import apply_market, planned_at
        helper = StageTwo()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        claim = helper.store.claim_round(D1, "09:35", planned_at(D1, "09:35"))
        helper.store.publish(claim, report("followup-entry", D1, "09:35", [(A, "新增")], [(ETF, A, 1)]),
                             FROZEN_DAYS, {ETF: 100}, {}, targets=("redis",))
        helper.quotes(D1, "09:36", "10", 1)
        helper.market(D2, "13:04", "11", 2)
        claim = helper.store.claim_round(D2, "13:05", planned_at(D2, "13:05"))
        helper.store.publish(claim, report("followup-weak", D2, "13:05", [(A, "减弱")]),
                             FROZEN_DAYS, {ETF: 100}, {}, targets=("redis",))
        now = moment(stamp(D2, "13:06"))
        # The execution follow-up only calls this connection facade; all source
        # facts and ledger writes remain in the isolated FROZEN test database.
        facade = SimpleNamespace(ns=helper.store.ns, origin="LIVE", settings=helper.settings,
                                 _connect=helper.store._connect)
        keys = followup.run_keys(facade.ns, D2, "13:05")
        self.addCleanup(self.clean_requests, helper.store, keys)
        budget = followup.RequestLog(facade, keys[0])
        budget.call(followup.SOURCE, followup.SCOPE, lambda: {"batch": "saved-before-restart"}, limit=1)
        (anchor,), cycles, records = followup.read_state(facade, D2)
        self.assertEqual(anchor["report_id"], "followup-weak")
        self.assertEqual(cycles[0]["orders"][-1]["target"], 500)
        self.assertEqual(records[keys[0]][followup.SOURCE]["status"], "SAVED")
        prior = followup.shared_market(helper.store, helper.cache, key=helper.key)
        fresh_body = deepcopy(prior.body)
        fresh_body.update(snapshot_id="followup-new-source", observed_at=now.isoformat())
        fresh_body["quotes"][ETF].update(observed_at=now.isoformat())
        with patch.object(followup, "quote_pool", side_effect=AssertionError("duplicate provider request")), \
             patch.object(followup, "merge_quotes", return_value=fresh_body):
            result = followup.tick(facade, helper.cache, helper.key, FROZEN_DAYS, "fixture", clock=lambda: now)
        self.assertEqual((result["status"], result["physical_requests"]), ("PUBLISHED", 0))
        fresh = followup.shared_market(helper.store, helper.cache, key=helper.key)
        apply_market(helper.store, now, FROZEN_DAYS, helper.map, fresh)
        apply_market(helper.store, now, FROZEN_DAYS, helper.map, fresh)
        saved_cycle, = helper.store.inspect()["cycles"]
        self.assertEqual([f["quantity"] for f in saved_cycle["book"]["fills"]], [1000, 500])
        self.assertEqual(saved_cycle["orders"][-1]["status"], "FILLED")
        _, _, records = followup.read_state(facade, D2)
        self.assertIn(followup.OUTCOME, records[keys[0]])
        with patch.object(followup, "quote_pool", side_effect=AssertionError("already filled")):
            self.assertIsNone(followup.tick(facade, helper.cache, helper.key, FROZEN_DAYS, "fixture", clock=lambda: now))
        # A later round that fails adds its own sell-only anchor in front; a published one after it removes it again.
        claim = helper.store.claim_round(D2, "14:05", planned_at(D2, "14:05"))
        helper.store.fail_round(claim, "NATIVE_ROUND_DEADLINE")
        anchors, _, records = followup.read_state(facade, D2)
        self.assertEqual([(a["slot"], a["report_id"], a.get("sells_only", False), a["keys"]) for a in anchors],
                         [("14:05", None, True, followup.run_keys(facade.ns, D2, "14:05")), ("13:05", "followup-weak", False, keys)])
        self.assertEqual(moment(anchors[0]["at"]), moment(stamp(D2, "14:05")))
        self.assertIn(followup.OUTCOME, records[keys[0]])
        # A newer round that is still running already retires the failed one.
        claim = helper.store.claim_round(D2, "14:30", planned_at(D2, "14:30"))
        anchors, _, _ = followup.read_state(facade, D2)
        self.assertEqual([(a["slot"], a["report_id"]) for a in anchors], [("13:05", "followup-weak")])
        helper.store.publish(claim, report("followup-late", D2, "14:30", [(A, "维持")]), FROZEN_DAYS, {ETF: 100}, {},
                             targets=("redis",))
        anchors, _, _ = followup.read_state(facade, D2)
        self.assertEqual([(a["slot"], a["report_id"]) for a in anchors], [("14:30", "followup-late")])

    def test_failed_first_round_still_fills_a_quote_triggered_v2_exit(self):
        from dataclasses import replace
        from test_stage2_postgres import StageTwo
        from test_stage1 import D1, D2, DAYS as FROZEN_DAYS, ETF, stamp
        from modules.strategy_hermes.stage2.worker import apply_market, planned_at
        helper = StageTwo()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.store.settings = replace(helper.settings, evaluation_rule="hermes_signal_cycle_v2", regime_mode="DUAL")
        helper.publish("v2-entry", D1, "09:35", "新增", helper.market(D1, "09:34", "10"))
        helper.quotes(D1, "09:36", "10", 1)
        # The opening sample of the next day's first round breaks the stop; then that round fails.
        helper.quotes(D2, "09:35", "9.60", 2)
        claim = helper.store.claim_round(D2, "09:35", planned_at(D2, "09:35"))
        helper.store.fail_round(claim, "NATIVE_ROUND_DEADLINE")
        waiting, = helper.store.inspect()["cycles"]
        self.assertEqual((waiting["status"], waiting["orders"][-1]["trigger_reason"]), ("WAIT_EXIT", "STOP_LOSS_HIT"))
        facade = SimpleNamespace(ns=helper.store.ns, origin="LIVE", settings=helper.settings, _connect=helper.store._connect)
        keys = followup.run_keys(facade.ns, D2, "09:35")
        self.addCleanup(self.clean_requests, helper.store, keys)
        now = moment(stamp(D2, "09:46"))
        fresh_body = deepcopy(followup.shared_market(helper.store, helper.cache, key=helper.key).body)
        fresh_body.update(snapshot_id="after-failed-round", observed_at=now.isoformat())
        fresh_body["quotes"][ETF].update(observed_at=now.isoformat(), price_cny="9.55", revision_seq=3)
        with patch.object(followup, "quote_pool", return_value={"batch": "after-failed-round"}) as provider, \
             patch.object(followup, "merge_quotes", return_value=fresh_body):
            result = followup.tick(facade, helper.cache, helper.key, FROZEN_DAYS, "fixture", clock=lambda: now)
            self.assertEqual((result["status"], result["slot"], result["report_id"], result["failed_round"], result["run_key"]),
                             ("PUBLISHED", "09:35", None, True, keys[0]))
            apply_market(helper.store, now, FROZEN_DAYS, helper.map,
                         followup.shared_market(helper.store, helper.cache, key=helper.key))
            closed, = helper.store.inspect()["cycles"]
            self.assertEqual((closed["status"], closed["orders"][-1]["status"], closed["book"]["fills"][-1]["price"]),
                             ("CLOSED", "FILLED", "9.55"))
            self.assertIsNone(followup.tick(facade, helper.cache, helper.key, FROZEN_DAYS, "fixture", clock=lambda: now))
        provider.assert_called_once()

    @staticmethod
    def clean_requests(store, keys):
        with store._connect() as conn:
            conn.execute("DELETE FROM collection_requests WHERE run_key=ANY(%s::text[])", (keys,))
