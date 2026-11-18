"""Bounded recovery and usage snapshots for the process-owned temporary root."""

from datetime import datetime
import logging
import os
from time import monotonic

from shared_kernel.temp_manager import BudgetError, TempManager

from .simulator import CHINA

LOG = logging.getLogger(__name__)
INTERVAL = 15 * 60
BATCH = 128


class TempMaintenance:
    def __init__(self, manager):
        self.manager = manager
        self.due = None
        self.last_cleanup = None

    @classmethod
    def from_environment(cls):
        root = os.environ.get("APP_TMP_ROOT")
        if not root:
            return None
        manager = TempManager(
            root, persistent=os.environ.get("STORAGE_GUARD_PERSISTENT_PATH"),
            total=int(os.environ.get("APP_TMP_BUDGET_BYTES", 2**31)),
            per_task=int(os.environ.get("APP_TMP_TASK_BUDGET_BYTES", 2**28)),
            entries=int(os.environ.get("APP_TMP_ENTRY_LIMIT", 20000)),
            min_free=int(os.environ.get("STORAGE_MIN_FREE_BYTES", 5*2**30)),
        )
        return cls(manager)

    def tick(self, *, now=None):
        tick = monotonic() if now is None else now
        if self.due is not None and tick < self.due:
            return
        self.due = tick + INTERVAL
        result = dict(at=datetime.now(CHINA).isoformat(), removed=0, failed=0, error=None)
        try:
            report = self.manager.collect(limit=BATCH)
            result.update(removed=len(report["removed"]), failed=len(report["failed"]))
        except (OSError, ValueError, BudgetError) as exc:
            result["error"] = type(exc).__name__
            LOG.warning("Managed temporary recovery failed: %s", type(exc).__name__)
        self.last_cleanup = result

    def status(self):
        try:
            used, entries = self.manager.usage()
            error = None
        except (OSError, ValueError, BudgetError) as exc:
            used = entries = None
            error = type(exc).__name__
        return dict(managed_bytes=used, managed_entries=entries, usage_error=error,
                    scope="GLOBAL", services=self.manager.breakdown() if error is None else {},
                    last_cleanup=self.last_cleanup)
