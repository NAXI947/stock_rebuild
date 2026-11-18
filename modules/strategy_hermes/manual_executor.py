"""Single subprocess executor for the existing scheduler's manual queue."""
import ctypes
import json
import logging
import os
import resource
import signal
import subprocess
import sys
from threading import Event, Thread, BoundedSemaphore
from time import monotonic

from .ledger import encode
from .manual_portfolio import replay
from .manual_queue import MAX_INPUT_BYTES

MAX_OUTPUT_BYTES = 24 * 1024 * 1024
MAX_SECONDS = 120
LOG = logging.getLogger(__name__)
PREVIEW_SLOT = BoundedSemaphore(1)


def preview(bundle):
    """Small frozen preview in a bounded, no-file-write child; never invokes providers."""
    if not isinstance(bundle, dict) or len(bundle.get("nodes", [])) > 64 or len(bundle.get("quotes", [])) > 4000:
        raise ValueError("preview supports 64 nodes and 4000 saved quotes")
    raw = encode(bundle).encode()
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("preview input too large")
    if not PREVIEW_SLOT.acquire(blocking=False):
        raise ValueError("preview already running")
    try:
        with subprocess.Popen([sys.executable, "-B", "-m", __name__, str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as child:
            try:
                output, _ = child.communicate(raw, timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise ValueError("preview deadline exceeded") from None
            if child.returncode or len(output) > MAX_OUTPUT_BYTES:
                raise ValueError("preview failed")
            result = json.loads(output)
            if result.get("error"):
                raise ValueError(result["error"])
            return result["result"]
    finally:
        PREVIEW_SLOT.release()


def _child(parent_pid):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        return 1
    os.nice(5)
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("input too large")
        output = encode({"result": replay(json.loads(raw))}).encode()
        if len(output) > MAX_OUTPUT_BYTES:
            output = b'{"error":"RESULT_TOO_LARGE"}'
    except (ArithmeticError, ValueError, TypeError, KeyError, AttributeError):
        output = b'{"error":"INVALID_SCENARIO"}'
    except MemoryError:
        output = b'{"error":"RESOURCE_LIMIT"}'
    sys.stdout.buffer.write(output)
    return 0


class ManualExecutor:
    def __init__(self, queue, *, paused=lambda: False):
        self.queue = queue
        self.paused = paused
        self.stop = Event()
        self.threads = [Thread(target=self._loop, name=f"manual-backtest-{index}", daemon=True)
                        for index in range(getattr(queue, "concurrency", 1))]
        self.thread = self.threads[0]

    def __enter__(self):
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=8)

    def _loop(self):
        while not self.stop.is_set():
            try:
                if not self.paused():
                    claim = self.queue.claim()
                    if claim:
                        self.execute(claim)
            except Exception as exc:
                LOG.warning("manual worker operation failed: %s", type(exc).__name__)
            self.stop.wait(0.5)

    def execute(self, claim):
        process = None
        try:
            command = [sys.executable,"-B","-m",__name__,str(os.getpid())]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
            started = last_pulse = monotonic()
            incoming = claim.bundle.encode()
            while True:
                if self.stop.is_set():
                    return self._interrupt(process,claim,"WORKER_STOPPED")
                if monotonic() - started > MAX_SECONDS:
                    return self._interrupt(process,claim,"EXECUTION_TIMEOUT")
                if monotonic() - last_pulse >= 2:
                    if not self.queue.pulse(claim):
                        return self._interrupt(process,claim,"LEASE_OR_CANCEL")
                    last_pulse = monotonic()
                try:
                    raw, _ = process.communicate(incoming,timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    incoming = None
            if process.returncode or len(raw) > MAX_OUTPUT_BYTES:
                return self.queue.finish(claim,error="CHILD_EXECUTION_FAILED")
            body = json.loads(raw)
            return self.queue.finish(claim, result=body.get("result"), error=body.get("error"))
        except (OSError, ValueError, TypeError) as exc:
            return self.queue.finish(claim,error=type(exc).__name__)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    def _interrupt(self, process, claim, error):
        if process.poll() is None:
            process.kill()
        process.communicate()
        return self.queue.finish(claim,error=error,interrupted=True)


if __name__ == "__main__":
    raise SystemExit(_child(int(sys.argv[1])))
