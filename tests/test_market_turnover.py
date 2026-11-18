"""Frozen turnover arithmetic and bounded worker collection; no provider calls."""
import copy
from datetime import date, datetime, timedelta
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import market_turnover as market


DAY = date(2026, 11, 13)
RECEIVED = datetime(2026, 11, 14, 10, tzinfo=CHINA)
DAYS = ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]


def minutes(day=DAY, slot="14:05", amount="100.25"):
    stamps = [int(stamp.timestamp() * 1000) for stamp in market.minute_labels(day, slot)]
    return {"data": {symbol: {"timestamp": stamps[:], "amount": [amount] * len(stamps)} for symbol in market.SYMBOLS}}


def daily(days=DAYS, amount="100000000"):
    stamps = [int(market.cutoff(date.fromisoformat(day), "00:00").timestamp() * 1000) for day in days]
    return {"data": {symbol: {"timestamp": stamps[:], "amount": [amount] * len(stamps)} for symbol in market.SYMBOLS}}


class NormalizeTests(unittest.TestCase):
    def test_minutes_keep_cny_and_opening_point(self):
        value = market.normalize_minutes(minutes(), DAY, RECEIVED)
        self.assertEqual(value["slots"]["09:35"]["amount_cny"], "1203.00")
        self.assertEqual(value["slots"]["10:35"]["amount_cny"], "13233.00")
        self.assertEqual(len(market.minute_labels(DAY, "13:05")), 126)
        self.assertEqual(value["slots"]["14:05"]["observed_at"], "2026-11-13T14:05:00+08:00")

    def test_lunch_slot_sums_121_minutes_and_keeps_1130_source_time(self):
        labels = market.minute_labels(DAY, "11:35")
        self.assertEqual(len(labels), 121)
        self.assertEqual(labels[-1], market.cutoff(DAY, "11:30"))
        value = market.normalize_minutes(minutes(slot="11:30"), DAY, market.cutoff(DAY, "11:35"))
        lunch = value["slots"]["11:35"]
        self.assertEqual(lunch["amount_cny"], "24260.50")
        self.assertEqual(lunch["observed_at"], "2026-11-13T11:30:00+08:00")
        self.assertIsNone(lunch["reason"])
        self.assertIsNone(value["slots"]["13:05"]["amount_cny"])

    def test_lunch_slot_is_not_available_before_1135(self):
        body = minutes(slot="11:30")
        before = market.cutoff(DAY, "11:35") - timedelta(seconds=1)
        value = market.normalize_minutes(body, DAY, before)["slots"]["11:35"]
        self.assertIsNone(value["amount_cny"])
        self.assertIsNone(value["observed_at"])
        self.assertEqual(value["reason"], "未到采样时点")
        record = market.normalize_minutes(body, DAY, market.cutoff(DAY, "11:35"))
        records = {(DAY.isoformat(), "1m"): record}
        hidden = market.presentation(DAYS, records, before)
        lunch = next(slot for slot in hidden["slots"] if slot["time"] == "11:35")
        self.assertIsNone(lunch["current_amount_cny"])
        shown = market.presentation(DAYS, records, market.cutoff(DAY, "11:35"))
        self.assertEqual(shown["default_slot"], "11:35")
        self.assertEqual(next(slot for slot in shown["slots"] if slot["time"] == "11:35")["current_amount_cny"], "24260.50")

    def test_missing_1130_minute_does_not_invent_lunch_total(self):
        body = minutes(slot="11:30")
        series = body["data"][market.SYMBOLS[1]]
        series["timestamp"].pop()
        series["amount"].pop()
        slots = market.normalize_minutes(body, DAY, market.cutoff(DAY, "11:35"))["slots"]
        self.assertEqual(slots["10:35"]["amount_cny"], "13233.00")
        self.assertIsNone(slots["11:35"]["amount_cny"])
        self.assertIsNone(slots["11:35"]["observed_at"])
        self.assertEqual(slots["11:35"]["reason"], "两市指数分钟数据未齐全")

    def test_missing_minute_affects_only_later_slots(self):
        body = minutes()
        series = body["data"][market.SYMBOLS[0]]
        del series["timestamp"][10]
        del series["amount"][10]
        value = market.normalize_minutes(body, DAY, RECEIVED)["slots"]
        self.assertIsNotNone(value["09:35"]["amount_cny"])
        self.assertIsNone(value["10:35"]["amount_cny"])
        self.assertIsNone(value["14:05"]["amount_cny"])

    def test_last_minute_never_stands_for_complete_series(self):
        body = minutes()
        for series in body["data"].values():
            series["timestamp"] = series["timestamp"][-1:]
            series["amount"] = series["amount"][-1:]
        self.assertTrue(all(p["amount_cny"] is None for p in market.normalize_minutes(body, DAY, RECEIVED)["slots"].values()))

    def test_opening_point_required_and_no_zero_fill(self):
        body = minutes()
        for series in body["data"].values():
            series["timestamp"] = series["timestamp"][1:]
            series["amount"] = series["amount"][1:]
        self.assertIsNone(market.normalize_minutes(body, DAY, RECEIVED)["slots"]["09:35"]["amount_cny"])

    def test_future_cutoffs_and_future_timestamp_rejected(self):
        received = market.cutoff(DAY, "09:35")
        value = market.normalize_minutes(minutes(slot="09:35"), DAY, received)["slots"]
        self.assertIsNotNone(value["09:35"]["amount_cny"])
        self.assertEqual(value["10:35"]["reason"], "未到采样时点")
        value = market.normalize_minutes(minutes(slot="09:36"), DAY, received)["slots"]
        self.assertIsNotNone(value["09:35"]["amount_cny"])
        self.assertIsNone(value["10:35"]["amount_cny"])

    def test_duplicate_missing_symbol_and_negative_value_are_missing(self):
        bodies = []
        body = minutes()
        del body["data"][market.SYMBOLS[1]]
        bodies.append(body)
        body = minutes()
        body["data"][market.SYMBOLS[0]]["amount"][0] = -1
        bodies.append(body)
        body = minutes()
        series = body["data"][market.SYMBOLS[0]]
        series["timestamp"].append(series["timestamp"][0])
        series["amount"].append(series["amount"][0])
        bodies.append(body)
        for body in bodies:
            with self.subTest(body=body):
                self.assertIsNone(market.normalize_minutes(body, DAY, RECEIVED)["slots"]["09:35"]["amount_cny"])

    def test_empty_bool_nan_and_seconds_are_never_money(self):
        for bad in (None, True, "", "NaN", "Infinity"):
            with self.subTest(value=bad):
                body = minutes()
                body["data"][market.SYMBOLS[0]]["amount"][0] = bad
                self.assertIsNone(market.normalize_minutes(body, DAY, RECEIVED)["slots"]["09:35"]["amount_cny"])
        body = minutes()
        for series in body["data"].values():
            series["timestamp"] = [stamp // 1000 for stamp in series["timestamp"]]
        self.assertIsNone(market.normalize_minutes(body, DAY, RECEIVED)["slots"]["09:35"]["amount_cny"])

    def test_zero_is_valid(self):
        self.assertEqual(market.normalize_minutes(minutes(amount=0), DAY, RECEIVED)["slots"]["09:35"]["amount_cny"], "0")

    def test_outside_symbols_and_oversized_range_rejected(self):
        body = minutes()
        body["data"]["000001.SH"] = {}
        with self.assertRaises(market.ProbeError):
            market.normalize_minutes(body, DAY, RECEIVED)
        with self.assertRaises(ValueError):
            market.normalize_daily(daily(), DAYS + ["2026-11-14"], RECEIVED)

    def test_daily_amount_is_cny_and_keeps_midnight_source_time(self):
        body = daily()
        body["data"][market.SYMBOLS[0]]["amount"][-1] = "770333016500"
        body["data"][market.SYMBOLS[1]]["amount"][-1] = "885006883700"
        value = market.normalize_daily(body, DAYS, RECEIVED)[-1]["slots"]["15:00"]
        self.assertEqual(value["amount_cny"], "1655339900200")
        self.assertEqual(value["observed_at"], "2026-11-13T00:00:00+08:00")

    def test_daily_missing_day_is_local_and_unclosed_day_hidden(self):
        body = daily()
        body["data"][market.SYMBOLS[0]]["amount"][1] = None
        values = market.normalize_daily(body, DAYS, market.cutoff(DAY, "14:05"))
        self.assertIsNotNone(values[0]["slots"]["15:00"]["amount_cny"])
        self.assertIsNone(values[1]["slots"]["15:00"]["amount_cny"])
        self.assertEqual(values[-1]["slots"]["15:00"]["reason"], "尚未收盘")


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.records = {}
        for index, day in enumerate(DAYS):
            snapshot = market.normalize_daily(daily([day], str((index + 1) * 100)), [day], RECEIVED)[0]
            self.records[(day, "1d")] = snapshot

    def test_fixed_five_day_baseline_and_decimal_ratios(self):
        value = market.presentation(DAYS, self.records, RECEIVED)
        close = value["slots"][-1]
        self.assertEqual(value["default_slot"], "15:00")
        self.assertEqual(close["current_amount_cny"], "1200")
        self.assertEqual(close["average5_amount_cny"], "600")
        self.assertEqual(close["average5_ratio"], "1")
        self.assertEqual(close["previous_ratio"], "0.2")
        self.assertEqual([p["trade_date"] for p in close["points"]], DAYS[-5:])

    def test_missing_baseline_not_replaced_with_older_day(self):
        del self.records[(DAYS[1], "1d")]
        close = market.presentation(DAYS, self.records, RECEIVED)["slots"][-1]
        self.assertEqual(close["baseline_count"], 4)
        self.assertIsNone(close["average5_amount_cny"])
        self.assertIsNone(close["average5_ratio"])
        self.assertEqual(close["previous_ratio"], "0.2")

    def test_zero_denominator_has_no_ratio(self):
        self.records[(DAYS[-2], "1d")]["slots"]["15:00"]["amount_cny"] = "0"
        self.assertIsNone(market.presentation(DAYS, self.records, RECEIVED)["slots"][-1]["previous_ratio"])

    def test_missing_current_data_defaults_to_latest_due_cutoff(self):
        value = market.presentation(DAYS, {}, market.cutoff(DAY, "10:35"))
        self.assertEqual(value["default_slot"], "10:35")


class SavedArchiveTests(unittest.TestCase):
    def setUp(self):
        self.saved = []
        for day in DAYS:
            raw = minutes(date.fromisoformat(day))
            record = market.normalize_minutes(raw, day, RECEIVED)
            del record["slots"]["11:35"]
            record.update(raw_response=raw, source_sha256=market.fingerprint(raw))
            for point in record["slots"].values():
                point.update(collected_at=record["collected_at"], source_sha256=record["source_sha256"])
            self.saved.append(record)
            self.saved.append(market.normalize_daily(daily([day]), [day], RECEIVED)[0])
        self.ledger = MagicMock()
        self.connection = self.ledger._connect.return_value.__enter__.return_value

        def execute(sql, params=None):
            result = MagicMock()
            if "scope->>'kind'='calendar'" in sql:
                result.fetchone.return_value = ({"days": DAYS[:]},)
            elif "SELECT DISTINCT ON" in sql:
                self.assertEqual(params, (DAYS,))
                result.fetchall.return_value = [(record,) for record in self.saved]
            else:
                raise AssertionError("unexpected archive SQL: " + sql)
            return result

        self.connection.execute.side_effect = execute

    def test_read_derives_lunch_from_old_raw_without_mutating_source(self):
        # An earlier point may already have its own provenance after a merge.
        self.saved[0]["slots"]["09:35"]["collected_at"] = "2026-11-06T09:36:00+08:00"
        self.saved[0]["slots"]["09:35"]["source_sha256"] = "older-point-source"
        original = copy.deepcopy(self.saved)
        with patch.object(market, "_now", return_value=RECEIVED + timedelta(days=1)), \
                patch.object(market, "get_json") as fetch, patch.object(market, "save") as save:
            days, records = market._read(self.ledger)
            value = market.load(self.ledger)
        self.assertEqual(days, DAYS)
        self.assertEqual(self.saved, original)
        self.assertEqual(len(value["slots"]), 6)
        lunch = next(slot for slot in value["slots"] if slot["time"] == "11:35")
        self.assertEqual(lunch["baseline_count"], 5)
        self.assertEqual(lunch["current_amount_cny"], "24260.50")
        self.assertEqual(lunch["average5_amount_cny"], "24260.50")
        self.assertEqual(lunch["average5_ratio"], "0")
        for old in original[::2]:
            derived = records[(old["trade_date"], "1m")]
            self.assertEqual(derived["collected_at"], old["collected_at"])
            self.assertEqual(derived["raw_response"], old["raw_response"])
            for slot, point in old["slots"].items():
                self.assertEqual(derived["slots"][slot], point)
            point = derived["slots"]["11:35"]
            self.assertEqual(point["observed_at"], old["trade_date"] + "T11:30:00+08:00")
            self.assertEqual(point["collected_at"], old["collected_at"])
            self.assertEqual(point["source_sha256"], old["source_sha256"])
        fetch.assert_not_called()
        save.assert_not_called()

    def test_worker_uses_derived_archive_without_collecting_or_writing(self):
        original = copy.deepcopy(self.saved)
        with patch.object(market, "_now", return_value=RECEIVED), \
                patch.object(market, "trading_days", return_value=DAYS), \
                patch.object(market.TurnoverRefresh, "_attempts", return_value=set()), \
                patch.object(market.TurnoverRefresh, "_last_current_attempt", return_value=None), \
                patch.object(market.TurnoverRefresh, "_history_attempt_times", return_value={}), \
                patch.object(market, "get_json") as fetch, \
                patch.object(market, "RequestLog") as budget, patch.object(market, "save") as save:
            worker = market.TurnoverRefresh(self.ledger, "private.json", "calendar.json")
            self.assertEqual(worker.tick(0)["state"], "UP_TO_DATE")
        self.assertEqual(self.saved, original)
        fetch.assert_not_called()
        budget.assert_not_called()
        save.assert_not_called()

    def test_old_raw_uses_original_collection_time_not_read_time(self):
        record = self.saved[-2]
        record["collected_at"] = "2026-11-13T11:34:59+08:00"
        original = copy.deepcopy(self.saved)
        with patch.object(market, "_now", return_value=RECEIVED):
            value = market.load(self.ledger)
        lunch = next(slot for slot in value["slots"] if slot["time"] == "11:35")
        self.assertIsNone(lunch["current_amount_cny"])
        self.assertIsNone(lunch["observed_at"])
        self.assertEqual(lunch["collected_at"], "2026-11-13T11:34:59+08:00")
        self.assertEqual(self.saved, original)

    def test_old_raw_with_mismatched_source_hash_is_not_derived(self):
        self.saved[-2]["source_sha256"] = "different-original-response"
        original = copy.deepcopy(self.saved)
        days, records = market._read(self.ledger)
        self.assertNotIn("11:35", records[(DAYS[-1], "1m")]["slots"])
        lunch = next(slot for slot in market.presentation(days, records, RECEIVED)["slots"]
                     if slot["time"] == "11:35")
        self.assertIsNone(lunch["current_amount_cny"])
        self.assertIsNone(lunch["observed_at"])
        self.assertEqual(lunch["baseline_count"], 5)
        self.assertEqual(self.saved, original)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = object()
        self.days = []
        self.records = {}
        self.attempts = set()
        self.calls = []
        self.current_attempt_at = None
        self.history_attempt_times = {}
        self.now = RECEIVED
        owner = self

        class Budget:
            def __init__(self, ledger, run):
                self.run, self.new_calls = run, 0

            def call(self, source, scope, operation, *, limit):
                identity = (self.run, source, market.fingerprint(scope))
                if identity in owner.attempts:
                    raise market.ProbeError("duplicate request")
                if sum(run == self.run and old_source == source for run, old_source, _ in owner.attempts) >= limit:
                    raise market.ProbeError("budget exhausted")
                owner.attempts.add(identity)
                self.new_calls = 1
                owner.calls.append((source, scope, limit))
                if source.endswith(":current"):
                    owner.current_attempt_at = owner.now
                if source.endswith(":history"):
                    owner.history_attempt_times[market.fingerprint(scope)] = owner.now
                return operation()

        def save(ledger, provider, endpoint, scope, body, count, source, received):
            self.assertEqual((provider, endpoint), ("tickflow", "market-turnover"))
            self.assertEqual(scope["purpose"], "market-turnover")
            self.assertGreater(count, 0)
            if scope["kind"] == "calendar":
                self.days = body["days"][:]
            else:
                self.records[(body["trade_date"], body["period"])] = copy.deepcopy(body)

        def get_json(url, *, headers, timeout):
            self.assertEqual(timeout, 8)
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query["symbols"], [",".join(market.SYMBOLS)])
            if query["period"] == ["1d"]:
                return daily()
            day = datetime.fromtimestamp(int(query["start_time"][0]) / 1000, CHINA).date()
            return minutes(day)

        self.patches = [patch.object(market, "_now", side_effect=lambda: self.now),
                        patch.object(market, "trading_days", return_value=DAYS),
                        patch.object(market, "_read", side_effect=lambda ledger: (self.days[:], copy.deepcopy(self.records))),
                        patch.object(market, "save", side_effect=save),
                        patch.object(market, "RequestLog", Budget),
                        patch.object(market.TurnoverRefresh, "_attempts", side_effect=lambda run: {(source, key) for r, source, key in self.attempts if r == run}),
                        patch.object(market.TurnoverRefresh, "_last_current_attempt", side_effect=lambda run: self.current_attempt_at),
                        patch.object(market.TurnoverRefresh, "_history_attempt_times", side_effect=lambda run: self.history_attempt_times.copy()),
                        patch.object(market, "tickflow_config", return_value="frozen-key"),
                        patch.object(market, "get_json", side_effect=get_json)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def worker(self):
        return market.TurnoverRefresh(self.ledger, "private.json", "calendar.json")

    def test_one_request_per_tick_and_historical_cache_survives_restart(self):
        worker = self.worker()
        for tick in range(7):
            before = len(self.calls)
            self.assertEqual(worker.tick(tick)["state"], "STORED")
            self.assertEqual(len(self.calls) - before, 1)
        self.assertEqual(worker.tick(7)["state"], "UP_TO_DATE")
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(self.worker().tick(0)["state"], "UP_TO_DATE")
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(len(self.records), 12)

    def test_failure_is_persisted_and_next_tick_moves_on(self):
        with patch.object(market, "get_json", side_effect=market.ProviderHTTPError("failure")):
            first = self.worker()
            self.assertEqual(first.tick(0)["state"], "FAILED")
            self.assertEqual(first.tick(1)["state"], "FAILED")
            self.assertEqual(len(self.calls), 2)
            restarted = self.worker()
            restarted.tick(0)
            self.assertEqual(len(self.calls), 3)
            self.assertEqual(len(set((source, market.fingerprint(scope)) for source, scope, _ in self.calls)), 3)

    def test_current_day_missing_cutoff_at_most_once_per_fifteen_minutes(self):
        self.now = market.cutoff(DAY, "10:35")
        self.days = DAYS[:]
        # Historical complete minutes remove unrelated bootstrap work.
        for day in DAYS[:-1]:
            self.records[(day, "1m")] = market.normalize_minutes(minutes(date.fromisoformat(day)), day, RECEIVED)
        with patch.object(market, "get_json", side_effect=market.ProviderHTTPError("failure")):
            self.worker().tick(0)
            self.worker().tick(0)
            self.assertEqual(len(self.calls), 1)
            self.now += timedelta(minutes=15)
            self.worker().tick(0)
            self.assertEqual(len(self.calls), 2)

    def test_load_never_fetches_provider(self):
        self.days = DAYS[:]
        with patch.object(market, "get_json", side_effect=AssertionError("GET must not collect")):
            value = market.load(self.ledger)
        self.assertEqual(value["trade_date"], DAYS[-1])
        self.assertEqual(len(value["slots"]), 6)

    def test_restart_across_quarter_hour_still_waits_full_fifteen_minutes(self):
        self.now = market.cutoff(DAY, "10:44") + timedelta(seconds=59)
        self.days = DAYS[:]
        for day in DAYS[:-1]:
            self.records[(day, "1m")] = market.normalize_minutes(minutes(date.fromisoformat(day)), day, RECEIVED)
        with patch.object(market, "get_json", side_effect=market.ProviderHTTPError("failure")):
            self.worker().tick(0)
            self.now += timedelta(seconds=2)
            self.worker().tick(0)
            self.assertEqual(len(self.calls), 1)
            self.now += timedelta(minutes=15)
            self.worker().tick(0)
            self.assertEqual(len(self.calls), 2)

    def test_real_trial_last_history_timeout_gets_one_delayed_repair(self):
        worker = self.worker()
        for tick in range(6):
            worker.tick(tick)
        # Mirrors the live trial: daily and five minute dates saved, 11/06 timed out.
        with patch.object(market, "get_json", side_effect=market.ProviderHTTPError("provider connection or timeout")):
            self.assertEqual(worker.tick(6)["state"], "FAILED")
        self.assertEqual(len(self.calls), 7)
        self.now += timedelta(seconds=899)
        self.assertEqual(self.worker().tick(0)["state"], "WAITING_RETRY")
        self.assertEqual(len(self.calls), 7)
        self.now += timedelta(seconds=1)
        self.assertEqual(self.worker().tick(0)["state"], "STORED")
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(self.calls[-1], ("tickflow:turnover:history", {"day": "2026-11-06", "retry": 1}, 12))
        self.assertEqual(self.worker().tick(0)["state"], "UP_TO_DATE")
        self.assertEqual(len(self.calls), 8)

    def test_history_repair_failure_stops_at_two_attempts_per_date(self):
        self.days = DAYS[:]
        for day in DAYS:
            self.records[(day, "1d")] = market.normalize_daily(daily([day]), [day], RECEIVED)[0]
        for day in DAYS[1:]:
            self.records[(day, "1m")] = market.normalize_minutes(minutes(date.fromisoformat(day)), day, RECEIVED)
        with patch.object(market, "get_json", side_effect=market.ProviderHTTPError("failure")):
            self.worker().tick(0)
            self.now += timedelta(seconds=900)
            self.worker().tick(0)
            self.now += timedelta(seconds=900)
            self.assertEqual(self.worker().tick(0)["state"], "WAITING_RETRY")
            self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
