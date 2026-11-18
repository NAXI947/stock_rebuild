"""Read dated reference observations; maintenance failures never trigger I/O."""
from datetime import date, datetime
import json
import logging

import psycopg

from ..simulator import CHINA, moment
from .inputs import read_file

LOG = logging.getLogger(__name__)
MAX_CALENDAR_BATCHES = 64


def calendar_days(records):
    """Accept both legacy provider rows and the reference-maintenance envelope.

    Only explicitly observed SSE dates are returned. Invalid individual rows
    are skipped; gaps do not become invented closed/open days.
    """
    records = json.loads(records) if isinstance(records, (str, bytes)) else records
    if not isinstance(records, list):
        raise ValueError("calendar rows must be a list")
    days = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        row = item.get("body", item)
        if not isinstance(row, dict) or row.get("exchange") != "SSE":
            continue
        try:
            day = date.fromisoformat(row["cal_date"]).isoformat()
        except (TypeError, ValueError, KeyError):
            continue
        if type(row.get("is_open")) is bool:
            days[day] = row["is_open"]
    return days


def _calendar_state(ledger, path, at=None):
    """Merge deployed fallback and stored calendar batches available by ``at``.

    A newer observation can explicitly close a formerly open date. The query
    uses immutable batches, so a subsequent calendar correction cannot leak
    into a historical replay. No provider is called and no file is rewritten.
    """
    at = at or datetime.now(CHINA)
    at = moment(at) if isinstance(at, str) else at
    if at.utcoffset() is None:
        raise ValueError("calendar cutoff requires a timezone")
    days, observed, sources, issues = {}, {}, [], []
    fallback_available = None
    try:
        fallback = read_file(path)
        if (fallback.get("schema_version") != "hotspot_trade_calendar_v1"
                or fallback.get("source") != "quant.trade_calendar"
                or fallback.get("exchange") != "SSE"):
            raise ValueError("unsupported shared trade calendar")
        available = moment(fallback["generated_at"]) if fallback.get("generated_at") else None
        fallback_available = available
        first, last = date.fromisoformat(fallback["range_start"]), date.fromisoformat(fallback["range_end"])
        if available is None or available <= at:
            for day, flag in fallback["days"].items():
                try:
                    value = date.fromisoformat(day)
                except (TypeError, ValueError):
                    continue
                if first <= value <= last and type(flag) is bool:
                    days[day], observed[day] = flag, available
    except (OSError, ValueError, TypeError, KeyError) as exc:
        LOG.warning("Calendar fallback unavailable: %s", type(exc).__name__)
        issues.append("DEPLOYED_CALENDAR_UNAVAILABLE")

    try:
        with ledger._connect() as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            rows = conn.execute(
                """SELECT normalized,collected_at,batch_id FROM supplier_batches
                   WHERE api='trade_cal' AND collected_at<=%s
                   ORDER BY collected_at DESC,batch_id DESC LIMIT %s""",
                (at, MAX_CALENDAR_BATCHES),
            ).fetchall()
        for records, available, batch in reversed(rows):
            available = moment(available) if isinstance(available, str) else available
            if available.utcoffset() is None or available > at:
                continue
            try:
                additions = calendar_days(records)
            except (TypeError, ValueError) as exc:
                LOG.warning("Stored calendar batch unavailable: %s", type(exc).__name__)
                issues.append("INVALID_STORED_CALENDAR_BATCH")
                continue
            applied = False
            for day, flag in additions.items():
                if observed.get(day) is None or observed[day] <= available:
                    days[day], observed[day] = flag, available
                    applied = True
            if applied:
                sources.append(batch)
    except (OSError, ValueError, TypeError, psycopg.Error) as exc:
        LOG.warning("Stored calendar unavailable; retaining deployed dates: %s", type(exc).__name__)
        issues.append("STORED_CALENDAR_UNAVAILABLE")
    dates = sorted(days)
    available = [value for value in observed.values() if value is not None]
    return dict(open_days=sorted(day for day, flag in days.items() if flag),
                range_start=dates[0] if dates else None, range_end=dates[-1] if dates else None,
                observed_day_count=len(dates), source_batches=sources,
                available_at=max(available).astimezone(CHINA).isoformat() if available else None,
                deployed_available_at=fallback_available.astimezone(CHINA).isoformat() if fallback_available else None,
                issues=sorted(set(issues)))


def runtime_calendar(ledger, path, at=None):
    """Return explicitly observed open dates available by the cutoff."""
    return _calendar_state(ledger, path, at)["open_days"]


def calendar_status(ledger, path, at=None):
    """Report actual reference coverage without turning it into a run gate."""
    state = _calendar_state(ledger, path, at)
    state["open_day_count"] = len(state.pop("open_days"))
    return state
