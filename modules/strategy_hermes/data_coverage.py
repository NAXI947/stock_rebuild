"""Monthly coverage from durable counters; GET never scans market facts or collects."""
from calendar import monthrange
from datetime import date, datetime, time, timedelta
import json
import re
from urllib.parse import parse_qs

from .data_updates import DATASETS
from .simulator import CHINA

DAILY = ("daily", "adj_factor", "daily_basic", "cyq_perf")
LABELS = {name: label for name, label, _ in DATASETS} | {"daily_assessment": "盘后策略评估"}
VIEWS = {"daily": DAILY, "weekly": ("weekly",), "assessment": ("daily_assessment",)}
CLOSE = time(15, 35)


def parse_query(query, now):
    try:
        if not isinstance(query, str) or len(query) > 128:
            raise ValueError
        args = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
        if set(args) - {"month", "view"} or any(len(v) != 1 for v in args.values()):
            raise ValueError
        month = args.get("month", [now.strftime("%Y-%m")])[0]
        view = args.get("view", ["daily"])[0]
        if not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", month) or view not in VIEWS:
            raise ValueError
        year, number = map(int, month.split("-"))
        if not 2 <= year <= 9998:
            raise ValueError
        first = date(year, number, 1)
        return first, first.replace(day=monthrange(year, number)[1]), view
    except (ValueError, TypeError):
        raise ValueError("INVALID_COVERAGE_QUERY") from None


def _stamp(value):
    return value.astimezone(CHINA).isoformat() if value else None


def _week_end(day, calendar):
    start = day - timedelta(days=day.weekday())
    days = [start + timedelta(days=n) for n in range(7)]
    if any(value not in calendar for value in days):
        return None
    opened = [value for value in days if calendar[value]]
    return bool(opened and day == opened[-1])


def assemble(first, last, view, calendar, records, now):
    """Availability is distinct from a complete scoring window or market coverage."""
    days = []
    for offset in range((last-first).days+1):
        day = first + timedelta(days=offset)
        opened, week_end = calendar.get(day), _week_end(day, calendar)
        closed = datetime.combine(day, CLOSE, CHINA) <= now
        existing = {dataset: value for (dataset, when), value in records.items() if when == day}
        expected = set(DAILY + ("daily_assessment",)) if opened and closed else set()
        if opened and closed and week_end:
            expected.add("weekly")
        names = list(DAILY) + ["weekly", "daily_assessment"]
        names += [name for name in LABELS if name in existing and name not in names]
        items = []
        for dataset in names:
            saved = existing.get(dataset)
            count = saved["row_count"] if saved else None
            valid_empty = bool(saved and saved.get("details", {}).get("valid_empty"))
            status = ("AVAILABLE" if count and count > 0 else "EMPTY" if valid_empty
                      else "MISSING" if dataset in expected else "NOT_DUE")
            items.append(dict(dataset=dataset, label=LABELS[dataset], status=status,
                row_count=count, ready_count=saved.get("ready_count") if saved else None,
                source_at=saved.get("source_at") if saved else None,
                updated_at=saved.get("updated_at") if saved else None))
        chosen = [item for item in items if item["dataset"] in VIEWS[view]]
        have = sum(item["status"] in ("AVAILABLE", "EMPTY") for item in chosen)
        due = bool(opened and closed and (view != "weekly" or week_end))
        if have:
            status = "AVAILABLE" if have == len(chosen) else "PARTIAL"
            if view == "assessment" and existing.get("daily", {}).get("row_count", 0)*2 > chosen[0]["row_count"]:
                status = "PARTIAL"
        elif opened is None:
            status = "UNKNOWN"
        elif not opened:
            status = "CLOSED"
        elif not closed:
            status = "PENDING"
        elif view == "weekly" and week_end is None:
            status = "UNKNOWN"
        elif view == "weekly" and not week_end:
            status = "NOT_DUE"
        else:
            status = "MISSING"
        days.append(dict(date=day.isoformat(), is_open=opened, is_week_end=week_end,
            status=status, available_count=have, expected_count=len(chosen) if due else 0, datasets=items))
    return days


def read_calendar(ledger, calendar, query="", *, now=None):
    now = (now or datetime.now(CHINA)).astimezone(CHINA)
    first, last, view = parse_query(query, now)
    # The existing workspace passes a validated, sorted list of open sessions.
    fallback = sorted({date.fromisoformat(day) for day in calendar})
    start, end = first - timedelta(days=7), last + timedelta(days=7)
    opened = set(fallback)
    sessions = {start + timedelta(days=i): start + timedelta(days=i) in opened
                for i in range((end-start).days+1)
                if fallback and fallback[0] <= start + timedelta(days=i) <= fallback[-1]}
    cutoff = now.date() if now.time() >= CLOSE else now.date()-timedelta(days=1)
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        sessions.update(conn.execute("""SELECT cal_date,is_open FROM supplier_trade_calendar
            WHERE exchange='SSE' AND cal_date BETWEEN %s AND %s""", (start, end)).fetchall())
        latest = conn.execute("""WITH fallback(day) AS (SELECT unnest(%s::date[])), known AS (
            SELECT cal_date AS day FROM supplier_trade_calendar WHERE exchange='SSE' AND is_open
            UNION ALL SELECT day FROM fallback f WHERE NOT EXISTS
                (SELECT 1 FROM supplier_trade_calendar c WHERE c.exchange='SSE' AND c.cal_date=f.day))
            SELECT max(day) FROM known WHERE day<=%s""", (fallback, cutoff)).fetchone()[0]
        saved = conn.execute("""SELECT dataset,business_date,row_count,ready_count,source_at,updated_at,details
            FROM data_coverage WHERE business_date BETWEEN %s AND %s
            AND ((scope='' AND dataset<>'daily_assessment') OR (scope=%s AND dataset='daily_assessment'))""",
            (first, last, ledger.ns)).fetchall()
    records = {(dataset, day): dict(row_count=count, ready_count=ready, source_at=_stamp(source),
        updated_at=_stamp(updated), details=json.loads(details) if isinstance(details, str) else details)
        for dataset, day, count, ready, source, updated, details in saved if dataset in LABELS}
    days = assemble(first, last, view, sessions, records, now)
    return dict(month=first.isoformat()[:7], view=view,
        latest_business_date=latest.isoformat() if latest else None,
        summary={key: sum(day["status"] == status for day in days)
                 for key, status in (("available", "AVAILABLE"), ("partial", "PARTIAL"), ("missing", "MISSING"))},
        days=days)
