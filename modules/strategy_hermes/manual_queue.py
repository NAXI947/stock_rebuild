"""Bounded durable manual backtests using the existing Stage 2 database."""
from dataclasses import dataclass
import json
import os
import re
from uuid import uuid4

from .ledger import encode, fingerprint
from .manual_portfolio import SCHEMA, execution_version

DRAFT_ID = re.compile(r"manual-draft-[a-f0-9]{32}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
MAX_INPUT_BYTES = 8 * 1024 * 1024
QUEUE_CAPACITY = 10
LEASE_SECONDS = 20
LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('stock-v4-manual-queue-v1',0))"
JOB_COLUMNS = "status,attempts,cancel_requested,result,error_code,submitted_at,finished_at"


class DraftNotFound(LookupError):
    pass


class QueueFull(RuntimeError):
    pass


@dataclass(frozen=True)
class Claim:
    draft_id: str
    token: str
    bundle: str


def _job(row, *, result=False):
    if not row:
        return None
    status, attempts, requested, output, error, created, finished = row
    view = dict(status=status, attempts=attempts, cancel_requested=requested, error_code=error,
                submitted_at=created.isoformat(), finished_at=finished.isoformat() if finished else None)
    if result and output is not None:
        view["result"] = json.loads(output)
    return view


class ManualQueue:
    def __init__(self, ledger):
        self.ledger, self.ns = ledger, ledger.ns
        self.capacity = int(os.environ.get("HEAVY_BACKTEST_QUEUE_LIMIT", QUEUE_CAPACITY))
        self.concurrency = int(os.environ.get("HEAVY_BACKTEST_MAX_CONCURRENCY", 1))
        if not 1 <= self.capacity <= 100 or not 1 <= self.concurrency <= 4:
            raise ValueError("manual queue limits outside supported range")

    def _draft(self, conn, identifier):
        if not isinstance(identifier, str) or not DRAFT_ID.fullmatch(identifier):
            raise DraftNotFound
        row = conn.execute(
            "SELECT parent_id,cloned_from,body FROM manual_drafts WHERE ns=%s AND id=%s",
            (self.ns, identifier),
        ).fetchone()
        if row is None:
            raise DraftNotFound
        return row

    def _save(self, conn, bundle, *, parent=None, clone=None, request_id=None):
        if not isinstance(bundle, dict) or bundle.get("schema_version") != SCHEMA:
            raise ValueError("unsupported manual scenario schema")
        raw = encode(bundle)
        if len(raw.encode()) > MAX_INPUT_BYTES:
            raise ValueError("manual scenario exceeds the size limit")
        identifier = "manual-draft-" + fingerprint([execution_version(bundle),parent,clone,request_id,bundle])[:32]
        conn.execute(
            """INSERT INTO manual_drafts(ns,id,parent_id,cloned_from,body) VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""", (self.ns,identifier,parent,clone,raw),
        )
        return identifier

    def create(self, bundle, parent=None):
        with self.ledger._connect() as conn, conn.transaction():
            if parent is not None:
                self._draft(conn,parent)
            return self._save(conn,bundle,parent=parent)

    def clone(self, identifier, request_id):
        if not isinstance(request_id,str) or not REQUEST_ID.fullmatch(request_id):
            raise ValueError("clone requires a valid request ID")
        with self.ledger._connect() as conn, conn.transaction():
            _, _, raw = self._draft(conn,identifier)
            return self._save(conn,json.loads(raw),clone=identifier,request_id=request_id)

    def get(self, identifier):
        with self.ledger._connect() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            parent, clone, raw = self._draft(conn,identifier)
            row = conn.execute(f"SELECT {JOB_COLUMNS} FROM manual_jobs WHERE ns=%s AND draft_id=%s",
                               (self.ns,identifier)).fetchone()
        return dict(id=identifier,parent_id=parent,cloned_from=clone,bundle=json.loads(raw),job=_job(row,result=True))

    def submit(self, identifier, *, retry=False):
        if type(retry) is not bool:
            raise ValueError("retry must be a boolean")
        with self.ledger._connect() as conn, conn.transaction():
            conn.execute(LOCK)
            parent, _, _ = self._draft(conn,identifier)
            row = conn.execute(f"SELECT {JOB_COLUMNS} FROM manual_jobs WHERE ns=%s AND draft_id=%s",
                               (self.ns,identifier)).fetchone()
            if row and not (retry and row[0] in ("CANCELLED","FAILED")):
                return _job(row)
            if parent:
                conn.execute(
                    """WITH RECURSIVE ancestors(id,parent_id) AS (
                           SELECT id,parent_id FROM manual_drafts WHERE ns=%s AND id=%s
                           UNION ALL
                           SELECT d.id,d.parent_id FROM manual_drafts d JOIN ancestors a ON d.id=a.parent_id WHERE d.ns=%s
                       ) UPDATE manual_jobs SET status='CANCELLED',cancel_requested=true,
                           error_code='SUPERSEDED',finished_at=clock_timestamp()
                       WHERE ns=%s AND draft_id IN (SELECT id FROM ancestors) AND status='QUEUED'""",
                    (self.ns,parent,self.ns,self.ns),
                )
            queued, = conn.execute("SELECT count(*) FROM manual_jobs WHERE status='QUEUED'").fetchone()
            if queued >= self.capacity:
                raise QueueFull
            row = conn.execute(
                f"""INSERT INTO manual_jobs(ns,draft_id,status) VALUES (%s,%s,'QUEUED')
                    ON CONFLICT(ns,draft_id) DO UPDATE SET status='QUEUED',cancel_requested=false,
                        error_code=NULL, result=NULL, submitted_at=clock_timestamp(),finished_at=NULL
                    RETURNING {JOB_COLUMNS}""", (self.ns,identifier),
            ).fetchone()
            return _job(row)

    def claim(self):
        with self.ledger._connect() as conn, conn.transaction():
            conn.execute(LOCK)
            conn.execute(
                """UPDATE manual_jobs SET status=CASE WHEN cancel_requested THEN 'CANCELLED' ELSE 'FAILED' END,
                     error_code=CASE WHEN cancel_requested THEN NULL ELSE 'WORKER_LEASE_EXPIRED' END,
                     claim_token=NULL,lease_until=NULL,finished_at=clock_timestamp()
                   WHERE status='RUNNING' AND lease_until<=clock_timestamp()"""
            )
            if conn.execute("SELECT count(*) FROM manual_jobs WHERE status='RUNNING'").fetchone()[0] >= self.concurrency:
                return None
            token = str(uuid4())
            row = conn.execute(
                """WITH next AS (
                       SELECT ns,draft_id FROM manual_jobs WHERE ns=%s AND status='QUEUED'
                       ORDER BY submitted_at,draft_id LIMIT 1 FOR UPDATE
                   ) UPDATE manual_jobs j SET status='RUNNING',attempts=attempts+1,
                       claim_token=%s,lease_until=clock_timestamp()+%s*interval '1 second'
                   FROM next n JOIN manual_drafts d ON (d.ns,d.id)=(n.ns,n.draft_id)
                   WHERE (j.ns,j.draft_id)=(n.ns,n.draft_id) RETURNING j.draft_id,d.body""",
                (self.ns,token,LEASE_SECONDS),
            ).fetchone()
        return Claim(row[0],token,encode(json.loads(row[1]))) if row else None

    def pulse(self, claim):
        with self.ledger._connect() as conn:
            row = conn.execute(
                """UPDATE manual_jobs SET lease_until=clock_timestamp()+%s*interval '1 second'
                   WHERE ns=%s AND draft_id=%s AND claim_token=%s AND status='RUNNING'
                     AND lease_until>clock_timestamp()
                   RETURNING cancel_requested""", (LEASE_SECONDS,self.ns,claim.draft_id,claim.token),
            ).fetchone()
            return row is not None and not row[0]

    def cancel(self, identifier):
        with self.ledger._connect() as conn, conn.transaction():
            self._draft(conn,identifier)
            row = conn.execute(
                f"""UPDATE manual_jobs SET cancel_requested=true,
                       status=CASE WHEN status='QUEUED' THEN 'CANCELLED' ELSE status END,
                       finished_at=CASE WHEN status='QUEUED' THEN clock_timestamp() ELSE finished_at END
                   WHERE ns=%s AND draft_id=%s AND status IN ('QUEUED','RUNNING')
                   RETURNING {JOB_COLUMNS}""", (self.ns,identifier),
            ).fetchone()
            if not row:
                row = conn.execute(f"SELECT {JOB_COLUMNS} FROM manual_jobs WHERE ns=%s AND draft_id=%s",
                                   (self.ns,identifier)).fetchone()
            return _job(row)

    def finish(self, claim, *, result=None, error=None, interrupted=False):
        status = "FAILED"
        if result is not None:
            waiting = any(row.get("reason") in ("WAIT_PRICE","WAIT_CALENDAR")
                          and row["status"] in ("PENDING","PARTIAL") for row in result["nodes"])
            status = "WAIT_DATA" if waiting and result["status"] != "CONFLICT" else "SUCCEEDED"
        if interrupted:
            error = error or "WORKER_STOPPED"
        with self.ledger._connect() as conn:
            row = conn.execute(
                """UPDATE manual_jobs SET
                       status=CASE WHEN cancel_requested THEN 'CANCELLED' ELSE %s END,
                       result=CASE WHEN cancel_requested THEN NULL ELSE %s::jsonb END,
                       error_code=CASE WHEN cancel_requested THEN NULL ELSE %s END,
                       claim_token=NULL,lease_until=NULL,finished_at=clock_timestamp()
                   WHERE ns=%s AND draft_id=%s AND claim_token=%s AND status='RUNNING'
                     AND lease_until>clock_timestamp() RETURNING status""",
                (status,encode(result) if result is not None else None,error,self.ns,claim.draft_id,claim.token),
            ).fetchone()
        return row[0] if row else "LEASE_LOST"
