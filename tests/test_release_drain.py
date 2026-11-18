"""Release waits for durable jobs before stopping the old worker image."""
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import signal
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from modules.strategy_hermes import runtime

RELEASE = (Path(__file__).resolve().parents[1] / "deploy" / "release.sh").read_text()
READER = RELEASE.split("<<'DRAIN_PY'\n", 1)[1].split("\nDRAIN_PY", 1)[0]
FUNCTIONS = RELEASE.split("wait_background_jobs() {", 1)[1].split("\nensure_images() {", 1)[0]
FUNCTIONS = "wait_background_jobs() {" + FUNCTIONS


class ReleaseReaderTest(unittest.TestCase):
    def run_reader(self, counts, *, paused=True):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        states = iter(counts)
        queries = []
        def execute(query, values=None):
            queries.append((query, values))
            return Mock(fetchone=lambda: next(states)) if query.startswith("SELECT") else Mock()
        connection.execute.side_effect = execute
        ledger = Mock(ns="test:one")
        ledger._connect.return_value = connection
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(runtime, "_ledger", return_value=ledger), patch.object(
            runtime, "_paused", return_value=lambda: paused
        ), patch("time.monotonic", side_effect=lambda: clock[0]), patch("time.sleep", side_effect=sleep), redirect_stdout(StringIO()):
            exec(compile(READER, "deploy/release.sh:release-reader", "exec"), {})
        self.assertTrue(all(query.startswith(("SELECT", "SET")) for query, _ in queries))
        self.assertTrue(all(values == ("test:one", "test:one") for query, values in queries if query.startswith("SELECT")))
        return clock[0], queries

    def test_reader_waits_for_both_queues_and_stable_quiet_period(self):
        elapsed, queries = self.run_reader([(1, 0), (0, 1), (0, 0), (1, 0), (0, 0), (0, 0), (0, 0)])
        self.assertEqual(elapsed, 3)
        self.assertIn("left_right_backfill_jobs", queries[2][0])
        self.assertIn("manual_jobs", queries[2][0])
        self.assertEqual(queries[0][0], "SET TRANSACTION READ ONLY")

    def test_reader_fails_when_pause_missing_or_running_work_exceeds_deadline(self):
        with self.assertRaisesRegex(SystemExit, "pause is missing"):
            self.run_reader([], paused=False)
        with self.assertRaisesRegex(SystemExit, "worker stays running"):
            self.run_reader([(1, 0)] * 281)


@unittest.skipUnless(shutil.which("bash"), "release runs in Linux bash")
class ReleaseShellTest(unittest.TestCase):
    def run_drain(self, wait_status=0):
        script = FUNCTIONS + """
compose_bundle() {
    case "$2" in
        ps) printf 'worker-id\\n' ;;
        exec) cat >/dev/null; printf 'WAIT\\n'; return WAIT_STATUS ;;
        stop) printf 'STOP\\n' ;;
        *) return 90 ;;
    esac
}
docker() {
    if [[ "$3" == '{{.State.Running}}' ]]; then printf 'true\\n';
    else printf '0 false\\n'; fi
}
drain_worker /bundle
""".replace("WAIT_STATUS", str(wait_status))
        return subprocess.run([shutil.which("bash")], input=script, text=True,
                              capture_output=True, timeout=10)

    def test_worker_stops_only_after_drain_succeeds(self):
        result = self.run_drain()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["WAIT", "STOP"])

    def test_failed_or_timed_out_drain_does_not_stop_worker(self):
        result = self.run_drain(23)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["WAIT"])


class WorkerSignalLifetimeTest(unittest.TestCase):
    def test_signal_handler_survives_backfill_join_in_both_worker_modes(self):
        for scheduled in (False, True):
            with self.subTest(scheduled=scheduled):
                previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
                seen = []
                class Backfill:
                    def __init__(self, *args, **kwargs):
                        pass
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        for sig in previous:
                            handler = signal.getsignal(sig)
                            seen.append(callable(handler) and handler != previous[sig])
                            if callable(handler):
                                handler(sig, None)
                class Manual:
                    def __init__(self, *args, **kwargs):
                        pass
                    def __enter__(self):
                        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                    def __exit__(self, *args):
                        pass
                def serve(*args, **kwargs):
                    # The actual schedule installs its own handlers and restores
                    # the previous ones before returning to _worker.
                    with runtime._shutdown_event() as stop:
                        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                        self.assertTrue(stop.is_set())
                    return 0
                ledger = SimpleNamespace(settings=SimpleNamespace(env="test"), ns="test:signal")
                with patch.dict("os.environ", {}, clear=True), patch.object(
                    runtime, "_enabled", side_effect=lambda name: scheduled and name == "ENABLE_HERMES_SCHEDULE"
                ), patch.object(runtime, "_schedule_args", return_value=object()), patch.object(
                    runtime.TempMaintenance, "from_environment", return_value=None
                ), patch.object(runtime, "_worker_heartbeat", return_value=nullcontext(lambda: None)), patch(
                    "modules.strategy_hermes.left_right_backfill.BackfillExecutor", Backfill
                ), patch("modules.strategy_hermes.stage2.schedule.serve", side_effect=serve), patch.object(
                    runtime, "ManualExecutor", Manual
                ):
                    self.assertEqual(runtime._worker(ledger, Mock()), 0)
                self.assertEqual(seen, [True, True])
                self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)
