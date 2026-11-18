"""Explicit, durable and bounded historical LEFT/RIGHT jobs; HTTP only queues."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
import logging
from pathlib import Path
import re
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

import psycopg

from .ledger import encode, fingerprint
from .simulator import CHINA
from .stage2.inputs import trading_days
from .stage2.provider_errors import error_diagnostic

LOG = logging.getLogger(__name__)
STOCK = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
JOB_ID = re.compile(r"lr-backfill-[a-f0-9]{32}\Z")
MAX_CODES, MAX_DAYS, CAPACITY = 10, 31, 3
MAX_REQUESTS, MAX_SECONDS, LEASE_SECONDS = 12000, 5400, 120
DETAIL_LIMIT = 100
LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('stock-v4-lr-backfill-v1',0))"
COLUMNS = ("job_id,request_id,body,status,request_budget,requests_used,attempts,cancel_requested,"
           "result,error_code,created_at,started_at,finished_at")


class QueueFull(RuntimeError):
    pass


class RequestConflict(RuntimeError):
    pass


class JobNotFound(LookupError):
    pass


class Interrupted(RuntimeError):
    pass


@dataclass(frozen=True)
class Claim:
    job_id: str
    token: str
    body: dict


def _json(raw):
    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw


def _view(row):
    names = COLUMNS.split(",")
    result = dict(zip(names, row, strict=True))
    result.update(_json(result.pop("body")))
    result["result"] = _json(result["result"])
    for name in ("created_at", "started_at", "finished_at"):
        result[name] = result[name].isoformat() if result[name] else None
    return result


def normalize_request(body, calendar, now):
    common = {"request_id", "mode", "date_from", "date_to"}
    if not isinstance(body, dict) or set(body) not in (common | {"scope"}, common | {"codes"}):
        raise ValueError("expected request_id, mode, date_from, date_to and scope ALL")
    if not isinstance(body["request_id"], str) or not REQUEST_ID.fullmatch(body["request_id"]):
        raise ValueError("invalid request_id")
    if body["mode"] not in ("collect", "recompute"):
        raise ValueError("mode must be collect or recompute")
    bulk = body.get("scope") == "ALL"
    if "scope" in body and not bulk:
        raise ValueError("scope must be ALL")
    codes = body.get("codes")
    if not bulk and (not isinstance(codes, list) or not 1 <= len(codes) <= MAX_CODES
            or any(not isinstance(code, str) or not STOCK.fullmatch(code) for code in codes)
            or len(set(codes)) != len(codes)):
        raise ValueError("provide 1-10 distinct stock codes such as 600000.SH")
    try:
        first, last = date.fromisoformat(body["date_from"]), date.fromisoformat(body["date_to"])
    except (TypeError, ValueError):
        raise ValueError("invalid date range") from None
    if (body["date_from"] != first.isoformat() or body["date_to"] != last.isoformat()
            or not 0 <= (last - first).days < MAX_DAYS):
        raise ValueError("output range must contain 1-31 calendar days")
    closed = closed_days(calendar, now)
    historical_collect = bulk and body["mode"] == "collect"
    if (not closed or last.isoformat() > closed[-1]
            or (not historical_collect and first.isoformat() < calendar[0])):
        raise ValueError("range must be within the calendar and end by the latest closed trading day")
    if (not any(first.isoformat() <= day <= last.isoformat() for day in closed)
            and not (historical_collect and first.isoformat() < calendar[0])):
        raise ValueError("range contains no closed trading day")
    return dict(body) if bulk else {**body, "codes": sorted(codes)}


def closed_days(calendar, now):
    now = now.astimezone(CHINA)
    return [day for day in calendar
            if datetime.combine(date.fromisoformat(day), time(15, 35), CHINA) <= now]


class BackfillQueue:
    def __init__(self, ledger, calendar):
        self.ledger, self.ns, self.calendar_source = ledger, ledger.ns, calendar

    def calendar(self):
        source = self.calendar_source
        values = trading_days(source) if isinstance(source, (str, Path)) else list(source or ())
        with self.ledger._connect() as conn:
            values.extend(str(row[0]) for row in conn.execute(
                "SELECT DISTINCT cal_date FROM supplier_trade_calendar WHERE is_open=true").fetchall())
        if any(not isinstance(day, str) or date.fromisoformat(day).isoformat() != day for day in values):
            raise ValueError("invalid trading calendar")
        return sorted(set(values))

    def listing(self, *, now=None):
        now = now or datetime.now(CHINA)
        closed = closed_days(self.calendar(), now)
        with self.ledger._connect() as conn:
            rows = conn.execute(f"SELECT {COLUMNS} FROM left_right_backfill_jobs WHERE ns=%s "
                                "ORDER BY created_at DESC,job_id DESC LIMIT 20", (self.ns,)).fetchall()
        return dict(jobs=[_view(row) for row in rows], limits=dict(
            scope="ALL", page_size=3000, max_pages=10,
            max_calendar_days=MAX_DAYS, queue_capacity=CAPACITY, concurrency=1,
            max_requests=MAX_REQUESTS, max_seconds=MAX_SECONDS,
            latest_closed_trade_date=closed[-1] if closed else None))

    def submit(self, body, *, now=None):
        body = normalize_request(body, self.calendar(), now or datetime.now(CHINA))
        budget = (MAX_REQUESTS if body.get("scope") == "ALL" else 800) if body["mode"] == "collect" else 0
        return self.enqueue(body, budget)

    def enqueue(self, body, budget, *, digest=None, initialize=None):
        """Persist a validated group and its logical units in one transaction."""
        digest = digest or fingerprint(body)
        identifier = "lr-backfill-" + uuid4().hex
        with self.ledger._connect() as conn, conn.transaction():
            conn.execute(LOCK)
            row = conn.execute(f"SELECT payload_hash,{COLUMNS} FROM left_right_backfill_jobs "
                               "WHERE ns=%s AND request_id=%s", (self.ns, body["request_id"])).fetchone()
            if row:
                if row[0] != digest:
                    raise RequestConflict("request_id already has a different payload")
                return _view(row[1:])
            if conn.execute("SELECT count(*) FROM left_right_backfill_jobs WHERE status IN ('QUEUED','RUNNING')").fetchone()[0] >= CAPACITY:
                raise QueueFull("at most three unfinished backfill jobs are allowed")
            row = conn.execute(f"""INSERT INTO left_right_backfill_jobs
                (ns,job_id,request_id,payload_hash,body,status,request_budget)
                VALUES (%s,%s,%s,%s,%s,'QUEUED',%s) RETURNING {COLUMNS}""",
                (self.ns, identifier, body["request_id"], digest, encode(body), budget)).fetchone()
            if initialize:
                initialize(conn, identifier, body)
        return _view(row)

    def get(self, identifier):
        if not isinstance(identifier, str) or not JOB_ID.fullmatch(identifier):
            raise JobNotFound(identifier)
        with self.ledger._connect() as conn:
            row = conn.execute(f"SELECT {COLUMNS} FROM left_right_backfill_jobs WHERE ns=%s AND job_id=%s",
                               (self.ns, identifier)).fetchone()
        if not row:
            raise JobNotFound(identifier)
        return _view(row)

    def cancel(self, identifier):
        self.get(identifier)
        with self.ledger._connect() as conn:
            conn.execute("""UPDATE left_right_backfill_jobs SET cancel_requested=true,
                status=CASE WHEN status='QUEUED' THEN 'CANCELLED' ELSE status END,
                finished_at=CASE WHEN status='QUEUED' THEN clock_timestamp() ELSE finished_at END
                WHERE ns=%s AND job_id=%s AND status IN ('QUEUED','RUNNING')""", (self.ns, identifier))
        return self.get(identifier)

    def claim(self):
        with self.ledger._connect() as conn, conn.transaction():
            conn.execute(LOCK)
            conn.execute("""UPDATE left_right_backfill_jobs SET
                status=CASE WHEN cancel_requested THEN 'CANCELLED' ELSE 'FAILED' END,
                error_code='WORKER_LEASE_EXPIRED',claim_token=NULL,lease_until=NULL,finished_at=clock_timestamp()
                WHERE status='RUNNING' AND lease_until<=clock_timestamp()""")
            if conn.execute("SELECT 1 FROM left_right_backfill_jobs WHERE status='RUNNING' LIMIT 1").fetchone():
                return None
            token = uuid4().hex
            row = conn.execute("""WITH next AS (
                SELECT ns,job_id FROM left_right_backfill_jobs WHERE ns=%s AND status='QUEUED'
                ORDER BY created_at,job_id LIMIT 1 FOR UPDATE)
                UPDATE left_right_backfill_jobs j SET status='RUNNING',attempts=attempts+1,
                claim_token=%s,lease_until=clock_timestamp()+%s*interval '1 second',last_active_at=clock_timestamp(),
                started_at=coalesce(started_at,clock_timestamp()) FROM next n
                WHERE (j.ns,j.job_id)=(n.ns,n.job_id) RETURNING j.job_id,j.body""",
                (self.ns, token, LEASE_SECONDS)).fetchone()
        return Claim(row[0], token, _json(row[1])) if row else None

    def pulse(self, claim):
        with self.ledger._connect() as conn:
            row = conn.execute("""UPDATE left_right_backfill_jobs
                SET lease_until=clock_timestamp()+%s*interval '1 second',
                elapsed_seconds=elapsed_seconds+greatest(0,extract(epoch FROM clock_timestamp()-last_active_at)),
                last_active_at=clock_timestamp()
                WHERE ns=%s AND job_id=%s AND claim_token=%s AND status='RUNNING'
                AND lease_until>clock_timestamp() RETURNING cancel_requested,elapsed_seconds""",
                (LEASE_SECONDS, self.ns, claim.job_id, claim.token)).fetchone()
        if row is not None and not row[0] and row[1] >= MAX_SECONDS:
            raise Interrupted("TIME_BUDGET_EXHAUSTED")
        return row is not None and not row[0]

    def request(self, claim, source, scope, operation):
        """Count before I/O. Failed/uncertain calls are durable and never retried."""
        key = fingerprint([source, scope])
        with self.ledger._connect() as conn, conn.transaction():
            active = conn.execute("""SELECT requests_used,request_budget FROM left_right_backfill_jobs
                WHERE ns=%s AND job_id=%s AND claim_token=%s AND status='RUNNING'
                AND NOT cancel_requested AND lease_until>clock_timestamp() FOR UPDATE""",
                (self.ns, claim.job_id, claim.token)).fetchone()
            if not active:
                raise Interrupted("LEASE_LOST_OR_CANCELLED")
            previous = conn.execute("""SELECT status,result FROM left_right_backfill_requests
                WHERE ns=%s AND job_id=%s AND request_key=%s""", (self.ns, claim.job_id, key)).fetchone()
            if previous:
                return _json(previous[1]) or dict(source=source, status="FAILED", error_code="PREVIOUS_REQUEST_UNCERTAIN")
            if active[0] >= active[1]:
                raise Interrupted("REQUEST_BUDGET_EXHAUSTED")
            conn.execute("UPDATE left_right_backfill_jobs SET requests_used=requests_used+1 WHERE ns=%s AND job_id=%s",
                         (self.ns, claim.job_id))
            conn.execute("""INSERT INTO left_right_backfill_requests(ns,job_id,request_key,source,status)
                VALUES (%s,%s,%s,%s,'STARTED')""", (self.ns, claim.job_id, key, source))
        try:
            result = dict(source=source, status="SAVED", receipt=operation())
        except (ValueError, TypeError, KeyError, RuntimeError, OSError, psycopg.Error) as exc:
            # Preserve compatible exception types plus whitelisted facts only.
            # Raw exception/source text may contain private configured URLs.
            result = dict(source=source, status="FAILED", **error_diagnostic(exc))
        with self.ledger._connect() as conn:
            conn.execute("""UPDATE left_right_backfill_requests SET status=%s,result=%s,finished_at=clock_timestamp()
                WHERE ns=%s AND job_id=%s AND request_key=%s AND status='STARTED'""",
                (result["status"], encode(result), self.ns, claim.job_id, key))
        return result

    def progress(self, claim, result):
        with self.ledger._connect() as conn:
            row = conn.execute("""UPDATE left_right_backfill_jobs SET result=%s
                WHERE ns=%s AND job_id=%s AND claim_token=%s AND status='RUNNING'
                AND NOT cancel_requested AND lease_until>clock_timestamp() RETURNING job_id""",
                (encode(result), self.ns, claim.job_id, claim.token)).fetchone()
        if row is None:
            raise Interrupted("LEASE_LOST_OR_CANCELLED")

    def finish(self, claim, *, status="FAILED", result=None, error=None):
        if status not in ("QUEUED", "SUCCEEDED", "PARTIAL", "FAILED"):
            raise ValueError("invalid final status")
        with self.ledger._connect() as conn:
            row = conn.execute("""UPDATE left_right_backfill_jobs SET
                status=CASE WHEN cancel_requested THEN 'CANCELLED' ELSE %s END,
                result=%s,error_code=%s,claim_token=NULL,lease_until=NULL,
                elapsed_seconds=elapsed_seconds+greatest(0,extract(epoch FROM clock_timestamp()-last_active_at)),
                last_active_at=NULL,
                finished_at=CASE WHEN %s='QUEUED' AND NOT cancel_requested THEN NULL ELSE clock_timestamp() END
                WHERE ns=%s AND job_id=%s AND claim_token=%s AND status='RUNNING'
                AND lease_until>clock_timestamp() RETURNING status""",
                (status, encode(result) if result is not None else None, error, status,
                 self.ns, claim.job_id, claim.token)).fetchone()
        return row[0] if row else "LEASE_LOST"


class BackfillExecutor:
    """One worker-owned I/O thread, empty queues perform no provider requests."""
    def __init__(self, queue, *, config=None, captures=None, paused=lambda: False,
                 external_enabled=lambda: False, collector=None, builder=None, persist=None, scheduler=None):
        self.queue, self.config, self.captures = queue, config, captures
        self.paused, self.external_enabled = paused, external_enabled
        self.collector, self.builder, self.persist = collector, builder, persist
        self.scheduler, self.schedule_due = scheduler, 0
        self.stop = Event()
        self.thread = None

    def __enter__(self):
        if self.thread is not None:
            raise RuntimeError("backfill executor already started")
        self.thread = Thread(target=self._run, name="left-right-backfill", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        if self.thread:
            self.thread.join(65)
            if self.thread.is_alive():
                raise RuntimeError("LEFT/RIGHT backfill did not stop within its drain bound")

    def _run(self):
        while not self.stop.is_set():
            try:
                if not self.paused():
                    if self.scheduler and monotonic() >= self.schedule_due:
                        self.schedule_due = monotonic() + 30
                        self.scheduler()
                    if self.stop.is_set() or self.paused():
                        continue
                    claim = self.queue.claim()
                    if claim:
                        self.execute(claim)
            except (ValueError, OSError, RuntimeError, psycopg.Error) as exc:
                LOG.warning("LEFT/RIGHT backfill poll failed: %s", type(exc).__name__)
            self.stop.wait(.5)

    def execute(self, claim):
        if claim.body.get("task") == "etf_maintenance":
            from .etf_maintenance import execute_job
            return execute_job(self, claim)
        if claim.body.get("task") == "data_update":
            from .data_updates import execute_job
            return execute_job(self, claim)
        from .stage2.left_right_refresh import build_manual_archives, persist_archive
        started = monotonic()
        body, requests = claim.body, []
        bulk = body.get("scope") == "ALL"
        result = dict(archives_saved=0, ready_items=0, expected_items=0, missing_count=0,
                      missing=[], requests=requests, request_failures=0, progress={})

        def checkpoint():
            if not self.queue.pulse(claim):
                raise Interrupted("LEASE_LOST_OR_CANCELLED")
            # A release pause is resumable even when SIGTERM arrives while the
            # current bounded request finishes. Cancellation/lease fencing wins.
            if self.paused():
                raise Interrupted("PAUSED")
            if self.stop.is_set():
                raise Interrupted("WORKER_STOPPED")
            if monotonic() - started >= MAX_SECONDS:
                raise Interrupted("TIME_BUDGET_EXHAUSTED")
            if body["mode"] == "collect" and not self.external_enabled():
                raise Interrupted("EXTERNAL_DISABLED")

        def request(source, scope, operation):
            checkpoint()
            result = self.queue.request(claim, source, scope, operation)
            if len(requests) < DETAIL_LIMIT:
                requests.append(result)
            if result["status"] != "SAVED":
                summary["request_failures"] += 1
            checkpoint()
            return result

        summary = result

        def progress(**fields):
            checkpoint()
            result["progress"].update(fields)
            self.queue.progress(claim, result)

        try:
            checkpoint()
            if body["mode"] == "collect":
                if bulk:
                    from .bulk_eod_collect import collect_all
                    issues = (self.collector or collect_all)(self.queue.ledger, body, self.config,
                                                           self.captures, request, checkpoint, progress=progress)
                else:
                    from .left_right_backfill_collectors import collect
                    issues = (self.collector or collect)(self.queue.ledger, body, self.config,
                                                        self.captures, request, checkpoint)
                issues = issues or []
                result.update(collect_issues=issues[:DETAIL_LIMIT], collect_issue_count=len(issues))
            dates = [day for day in self.queue.calendar() if body["date_from"] <= day <= body["date_to"]]
            checkpoint()
            if not dates:
                result["missing_count"] += 1
                result["missing"].append(dict(reason="NO_COLLECTED_TRADING_DAYS"))
            as_of = datetime.now(CHINA)
            for index, day in enumerate(dates):
                checkpoint()
                progress(phase="recompute", dataset=None, trade_date=day, completed_days=index, total_days=len(dates),
                         processed_stocks=0, ready_items=result["ready_items"])
                kwargs = dict(checkpoint=checkpoint)
                if bulk:
                    def day_progress(**fields):
                        fields.update(completed_days=index, total_days=len(dates), trade_date=day)
                        if "ready_items" in fields:
                            fields["ready_items"] += result["ready_items"]
                        progress(**fields)
                    kwargs.update(persist=True, progress=day_progress)
                built = (self.builder or build_manual_archives)(self.queue.ledger, [day], body.get("codes"), as_of,
                                                               **kwargs)
                result["missing_count"] += built.get("missing_count", len(built["missing"]))
                result["missing"].extend(built["missing"][:max(0, DETAIL_LIMIT-len(result["missing"]))])
                result["expected_items"] += built.get("expected_items", 0 if bulk else len(body["codes"])*2)
                if bulk:
                    result["archives_saved"] += built.get("archives_saved", 0)
                    result["ready_items"] += built.get("ready_items", 0)
                for archive in built["archives"]:
                    checkpoint()
                    (self.persist or persist_archive)(self.queue.ledger, archive)
                    result["archives_saved"] += 1
                    result["ready_items"] += sum(item.get("status") == "READY" for item in archive["items"])
                progress(completed_days=index+1, ready_items=result["ready_items"])
            checkpoint()
            complete = (result["expected_items"] > 0 and result["ready_items"] == result["expected_items"]
                        and not result["missing_count"] and not result.get("collect_issue_count")
                        and not result["request_failures"])
            progress(phase="complete")
            return self.queue.finish(claim, status="SUCCEEDED" if complete else "PARTIAL", result=result)
        except Interrupted as exc:
            code = str(exc)
            return self.queue.finish(claim, status="QUEUED" if code == "PAUSED" else "FAILED", result=result, error=code)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, psycopg.Error) as exc:
            return self.queue.finish(claim, result=result, error=type(exc).__name__)
