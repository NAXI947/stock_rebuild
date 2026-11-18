"""Run production automation wiring against local sockets and frozen inputs."""
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from modules.strategy_hermes.runtime import _schedule_args, _worker
from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import bulk_collect, intraday, schedule
from modules.strategy_hermes.stage2.octop_client import PROTOCOL

DAY = "2026-11-13"
AT = f"{DAY}T09:35:10+08:00"


class Automation(unittest.TestCase):
    def test_native_mode_never_starts_the_legacy_scheduled_analyst(self):
        from contextlib import nullcontext
        ledger = SimpleNamespace(origin="LIVE", settings=SimpleNamespace(env="production"))
        args = SimpleNamespace(received_at=None,once=True,poll_seconds=2,qq=False,trial_day=None,
            tickflow_config=None,kaipanla_captures=None,stock_research=False,findesk_config=None,
            manual_queue=False,octop_stdio=False,trigger_mode="octop",calendar=Path("/calendar"),
            mapping=Path("/mapping"),snapshot_key="fixture",snapshot_file=None)
        with patch.object(schedule,"connection",return_value=nullcontext(Mock())), \
             patch("modules.strategy_hermes.stage2.native_rounds.NativeRounds"), \
             patch("modules.strategy_hermes.stage2.native_rounds.native_tick",return_value={"state":"IDLE"}) as tick, \
             patch.object(schedule,"runtime_calendar",return_value=[DAY]), \
             patch.object(schedule,"live_mapping",return_value={}), \
             patch.object(schedule,"shared_market",return_value=None), \
             patch("modules.strategy_hermes.stage2.worker.apply_market",return_value={}), \
             patch.object(schedule,"dispatch",return_value="EMPTY") as delivery, \
             patch.object(schedule,"scheduled_tick",side_effect=AssertionError("duplicate internal clock")):
            self.assertEqual(schedule.serve(ledger,Mock(),args),0)
            tick.assert_called_once()
            delivery.assert_called_once()
            self.assertEqual(delivery.call_args.args[1],"redis")

    def test_socket_reconnects_to_fixed_rpc_protocol(self):
        with TemporaryDirectory() as work, socket.socket(socket.AF_UNIX) as listener:
            path = Path(work, "bridge.sock")
            listener.bind(str(path))
            listener.listen(1)
            listener.settimeout(3)
            received = []
            failures = []

            def bridge():
                try:
                    for _ in range(2):
                        peer, _ = listener.accept()
                        with peer, peer.makefile("rb") as source:
                            peer.settimeout(3)
                            call = json.loads(source.readline())
                            received.append(call)
                            reply = dict(protocol=PROTOCOL, id=call["id"], result={"status": "READY"})
                            peer.sendall((json.dumps(reply) + "\n").encode())
                except (OSError, ValueError, KeyError) as exc:
                    failures.append(exc)

            thread = Thread(target=bridge)
            thread.start()
            args = SimpleNamespace(octop_stdio=False, octop_socket=path)
            with schedule.connection(args) as client:
                self.assertEqual(client.call("mx", {"query": "fixture"}, timeout=2), {"status": "READY"})
                with self.assertRaises(ConnectionError):
                    client.call("analyze", {"sectors": []}, timeout=2)
                self.assertEqual(client.call("analyze", {"sectors": []}, timeout=2), {"status": "READY"})
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual([call["operation"] for call in received], ["mx", "analyze"])

    def test_runtime_requires_collection_config_and_a_private_socket(self):
        with TemporaryDirectory() as work, socket.socket(socket.AF_UNIX) as listener:
            path = Path(work, "bridge.sock")
            listener.bind(str(path))
            config = {"APP_CALENDAR_FILE": "/fixture/calendar.json",
                      "APP_MAPPING_FILE": "/fixture/mapping.json",
                      "APP_SNAPSHOT_KEY": "stock-v4:prod:source:market",
                      "APP_OCTOP_SOCKET": str(path)}
            ledger = SimpleNamespace(settings=SimpleNamespace(redis_prefix="stock-v4:prod:"))
            with patch.dict("os.environ", config, clear=True), patch(
                "modules.strategy_hermes.runtime.runtime_calendar"
            ), patch("modules.strategy_hermes.runtime.live_mapping"):
                args = _schedule_args(ledger, False, False)
                self.assertEqual(args.octop_socket, path)
                self.assertIsNone(args.tickflow_config)
                self.assertFalse(args.stock_research)
                with self.assertRaisesRegex(ValueError, "stock research requires official collection"):
                    _schedule_args(ledger, False, False, stock_research=True)
                stock_config = {"APP_TICKFLOW_CONFIG":"/fixture/tickflow.json",
                                "APP_KAIPANLA_CAPTURES":"/fixture/kaipanla"}
                with patch.dict("os.environ", stock_config), patch(
                    "modules.strategy_hermes.stage2.market_bootstrap.tickflow_config"
                ), patch("modules.strategy_hermes.stage2.market_bootstrap.capture_request"):
                    self.assertTrue(_schedule_args(ledger, False, True, stock_research=True).stock_research)
                with self.assertRaisesRegex(ValueError, "both source configurations"):
                    _schedule_args(ledger, False, True)
                with patch.dict("os.environ", {"OCTOP_COMMAND_JSON": '["legacy"]'}):
                    with self.assertRaisesRegex(ValueError, "exactly one OCTOP transport"):
                        _schedule_args(ledger, False, False)

    def test_worker_rejects_stock_research_without_official_collection(self):
        ledger = SimpleNamespace(settings=SimpleNamespace(env="production"))
        with patch.dict("os.environ", {"ENABLE_STOCK_RESEARCH":"true"}, clear=True):
            with self.assertRaisesRegex(ValueError, "stock research requires official collection"):
                _worker(ledger, object())

    def test_tickflow_quotes_send_one_whole_universe_request(self):
        expected = int(datetime.fromisoformat(AT).timestamp() * 1000)
        with patch.object(bulk_collect, "tickflow_config", return_value="fixture"), patch.object(
            bulk_collect, "fetch", return_value={"data": [
                {"symbol": "512480.SH", "timestamp": expected, "last_price": "1.15"},
                {"symbol": "159915.SZ", "timestamp": expected, "last_price": "2.00"},
            ]}
        ) as fetch, patch.object(bulk_collect, "save", return_value={"batch": "fixture"}) as save:
            result = bulk_collect.quote_pool(object(), "/fixture/tickflow.json")
        fetch.assert_called_once()
        request = fetch.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("User-agent"), "stock-v4-tickflow-client/1.0")
        self.assertEqual(fetch.call_args.kwargs, {"timeout": 15})
        self.assertEqual(json.loads(request.data), {"universes": ["CN_ETF"]})
        self.assertEqual(len(save.call_args.kwargs["quotes"]), 2)
        self.assertEqual(result["batch"], "fixture")

    def test_official_feed_is_claimed_once_and_consumed_by_same_worker(self):
        due = (DAY, "09:35", datetime.fromisoformat(AT))
        args = SimpleNamespace(received_at=None, once=True, poll_seconds=1, qq=False, trial_day=None,
                               snapshot_key="stock-v4:prod:source:market", snapshot_file=None,
                               tickflow_config=Path("/fixture/tickflow.json"),
                               kaipanla_captures=Path("/fixture/kaipanla"),
                               manual_queue=False, octop_stdio=False, octop_command_json='["fixture"]',
                               calendar=Path("/fixture/calendar.json"), mapping=Path("/fixture/map.json"))
        ledger = SimpleNamespace(origin="LIVE", settings=SimpleNamespace(env="production"))
        first = object()
        fresh = object()
        with patch.object(schedule, "runtime_calendar", return_value=[DAY]), patch.object(
            schedule, "live_mapping", return_value={}
        ), patch.object(schedule, "due_round", return_value=due), patch.object(
            schedule, "shared_market", side_effect=[first, fresh]
        ), patch.object(intraday, "refresh", return_value={"snapshot_id": "fixture"}) as refresh, patch.object(
            schedule, "scheduled_tick", return_value={"round": "PUBLISHED"}
        ) as tick, patch("modules.strategy_hermes.stage2.etf_statistics.overlay_mapping", return_value={}) as statistics, patch("builtins.print"):
            self.assertEqual(schedule.serve(ledger, None, args), 0)
            statistics.assert_called_once()
        refresh.assert_called_once_with(ledger, None, args.snapshot_key, due, first,
                                        args.tickflow_config, args.kaipanla_captures)
        self.assertIs(tick.call_args.args[5], fresh)

    def test_partial_refresh_publishes_within_one_slot_budget(self):
        ledger = SimpleNamespace(ns="stock-v4:prod:" + "n" * 100,
                                 settings=SimpleNamespace(env="production"))
        now = datetime.fromisoformat(AT).astimezone(CHINA)
        due = (DAY, "09:35", now)
        with patch("modules.strategy_hermes.stage2.inputs.shared_market", return_value=None), patch.object(
            intraday, "RequestLog"
        ) as request_log, patch.object(intraday, "collect_boards", return_value=[{"batch": "fixture"}]), patch.object(
            intraday, "assemble", return_value={"snapshot_id": "fixture"}
        ) as assemble, patch.object(intraday, "publish_snapshot") as publish:
            request_log.return_value.call.side_effect = intraday.ProbeError("fixture")
            request_log.return_value.new_calls = 1
            result = intraday._refresh(ledger, None, "stock-v4:prod:source:market", due, None, None, None)
        self.assertLessEqual(len(request_log.call_args.args[1]), 100)
        request_log.return_value.call.assert_called_once()
        self.assertEqual(request_log.return_value.call.call_args.kwargs["limit"], 1)
        self.assertIsNone(assemble.call_args.args[3])
        publish.assert_called_once()
        self.assertEqual(result["snapshot_id"], "fixture")
        self.assertFalse(result["tickflow_available"])
        self.assertEqual(result["kaipanla_pages"], 1)
