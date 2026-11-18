"""Original five-period data updates, backed by the existing bounded worker."""
from calendar import monthrange
from datetime import date, datetime, timedelta
import json
import re
from urllib.parse import parse_qs
from uuid import uuid4

import psycopg

from .ledger import encode, fingerprint
from .left_right_backfill import BackfillQueue, Interrupted, MAX_REQUESTS, QueueFull, REQUEST_ID, closed_days
from .simulator import CHINA

DATASETS = (
    ("daily", "日线行情", "日更"), ("adj_factor", "复权因子", "日更"),
    ("daily_basic", "每日指标", "日更"), ("cyq_perf", "筹码分布", "日更"),
    ("weekly", "周线行情", "周更"), ("stock_basic", "股票资料", "基础资料"),
    ("trade_cal", "交易日历", "基础资料"), ("index_member_all", "申万行业", "基础资料"),
    ("dc_member", "概念成分", "月更"), ("share_float", "限售股解禁", "月更"),
    ("fina_indicator", "年度财务指标", "年更"),
)
MODES = {"daily", "weekly", "basic", "monthly", "yearly"}


class UpdateError(ValueError):
    pass


def _object(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


def plan(payload, calendar, now):
    """Freeze the requested original scope; no implicit history expansion."""
    if not isinstance(payload, dict) or payload.get("mode") not in MODES:
        raise UpdateError("INVALID_UPDATE_MODE")
    mode = payload["mode"]
    fields = {"daily": {"date_from", "date_to"}, "weekly": {"date_from", "date_to"},
              "basic": set(), "monthly": {"month"}, "yearly": {"year"}}[mode]
    if set(payload) != fields | {"mode", "request_id"} or not isinstance(payload["request_id"], str) or not REQUEST_ID.fullmatch(payload["request_id"]):
        raise UpdateError("INVALID_UPDATE_REQUEST")
    now = now.astimezone(CHINA)
    calendar = sorted(set(calendar))
    closed = closed_days(calendar, now)
    latest = closed[-1] if closed else None
    if not latest and mode != "basic":
        raise UpdateError("TRADE_CALENDAR_RANGE_MISSING")
    body = dict(payload, task="data_update", submitted_at=now.isoformat(),
                latest_business_date=latest or now.date().isoformat(), trade_dates=[], weekly_dates=[])
    if mode in ("daily", "weekly"):
        try:
            first, last = (date.fromisoformat(payload[key]) for key in ("date_from", "date_to"))
        except (TypeError, ValueError):
            raise UpdateError("USE_COMPLETED_DATES_MAX_31_DAYS") from None
        if (str(first) != payload["date_from"] or str(last) != payload["date_to"]
                or not 0 <= (last-first).days < 31 or str(last) > latest):
            raise UpdateError("USE_COMPLETED_DATES_MAX_31_DAYS")
        if str(first) < calendar[0]:
            raise UpdateError("TRADE_CALENDAR_RANGE_MISSING")
        body["trade_dates"] = [day for day in closed if str(first) <= day <= str(last)]
        if mode == "weekly":
            body["calendar"] = {}
            for value in body["trade_dates"]:
                day = date.fromisoformat(value)
                start = day - timedelta(days=day.weekday())
                end = day + timedelta(days=6-day.weekday())
                if str(start) < calendar[0] or str(end) > calendar[-1]:
                    raise UpdateError("TRADE_CALENDAR_RANGE_MISSING")
                if not any(value < d <= str(end) for d in calendar):
                    body["weekly_dates"].append(value)
                    for offset in range(7):
                        stamp = str(start+timedelta(days=offset))
                        body["calendar"][stamp] = stamp in calendar
        if not body["trade_dates"] or mode == "weekly" and not body["weekly_dates"]:
            raise UpdateError("NO_COMPLETED_TRADING_DAY_IN_RANGE")
    elif mode == "monthly":
        month = payload["month"]
        if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", month):
            raise UpdateError("INVALID_MONTH")
        try:
            first = date.fromisoformat(month+"-01")
        except ValueError:
            raise UpdateError("INVALID_MONTH") from None
        last = first.replace(day=monthrange(first.year, first.month)[1])
        if first > now.date():
            raise UpdateError("INVALID_MONTH")
        if str(first) < calendar[0]:
            raise UpdateError("TRADE_CALENDAR_RANGE_MISSING")
        body["trade_dates"] = [d for d in closed if str(first) <= d <= str(last)]
        if not body["trade_dates"]:
            raise UpdateError("NO_COMPLETED_TRADING_DAY_IN_RANGE")
        body["snapshot_date"] = body["trade_dates"][-1]
    elif mode == "yearly":
        if type(payload["year"]) is not int or not 2000 <= payload["year"] < now.year:
            raise UpdateError("USE_COMPLETED_REPORT_YEAR")
    from .data_updates_collect import planned_units
    units = planned_units(body)
    if mode == "daily":
        units.extend(("daily_assessment", day) for day in body["trade_dates"])
    if mode == "yearly":
        units.append(("daily_assessment", body["latest_business_date"]))
    body["units"] = [dict(dataset=dataset, business_date=day, unit_key=fingerprint([dataset, day])) for dataset, day in units]
    return body


def submit(ledger, calendar, payload, *, now=None):
    payload = dict(payload)
    payload.setdefault("request_id", uuid4().hex)
    queue = BackfillQueue(ledger, calendar)
    body = plan(payload, queue.calendar(), now or datetime.now(CHINA))
    def initialize(conn, identifier, frozen):
        with conn.cursor() as cursor:
            cursor.executemany("""INSERT INTO data_update_units(ns,job_id,unit_key,dataset,business_date,status)
                VALUES(%s,%s,%s,%s,%s,'PENDING')""",
                [(ledger.ns, identifier, unit["unit_key"], unit["dataset"], unit["business_date"]) for unit in frozen["units"]])
    job = queue.enqueue(body, MAX_REQUESTS, digest=fingerprint(payload), initialize=initialize)
    return dict(job_count=len(job["units"]), job_ids=[job["job_id"]+":"+unit["unit_key"] for unit in job["units"]])


def _effective_status(unit, job, cancelled):
    if unit not in ("PENDING", "RUNNING"):
        return unit
    if job == "CANCELLED" or cancelled:
        return "CANCELLED"
    if job in ("FAILED", "PARTIAL", "SUCCEEDED"):
        return "FAILED"
    return "PENDING" if job == "QUEUED" else unit


def jobs(ledger, query=""):
    params = parse_qs(query, keep_blank_values=True)
    if set(params) - {"page", "page_size"} or any(len(values) != 1 for values in params.values()):
        raise UpdateError("INVALID_JOB_PAGE")
    try:
        page, size = (int(params.get(key, [default])[0]) for key, default in (("page", "1"), ("page_size", "50")))
    except ValueError:
        raise UpdateError("INVALID_JOB_PAGE") from None
    if not 1 <= page <= 10000 or not 1 <= size <= 100:
        raise UpdateError("INVALID_JOB_PAGE")
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        total = conn.execute("SELECT count(*) FROM data_update_units WHERE ns=%s", (ledger.ns,)).fetchone()[0]
        rows = conn.execute("""SELECT u.job_id,u.unit_key,u.dataset,u.business_date,u.status,
            u.created_at,coalesce(u.finished_at,j.finished_at),u.error_code,j.body->>'mode',j.status,j.error_code,j.cancel_requested
            FROM data_update_units u JOIN left_right_backfill_jobs j USING(ns,job_id)
            WHERE u.ns=%s ORDER BY u.created_at DESC,u.job_id,u.unit_key LIMIT %s OFFSET %s""",
            (ledger.ns, size, (page-1)*size)).fetchall()
    items = []
    labels = {key: label for key, label, _ in DATASETS}
    for jid, key, dataset, day, status, created, finished, error, mode, parent, parent_error, cancelled in rows:
        status = _effective_status(status, parent, cancelled)
        items.append(dict(job_id=jid+":"+key, dataset=labels.get(dataset), task=dataset, mode=mode,
            status="SUCCEEDED" if status in ("OK", "EMPTY") else status, business_date=str(day),
            created_at=created.isoformat(), finished_at=finished.isoformat() if finished else None,
            error_code=error or (parent_error if status == "FAILED" else None)))
    return dict(items=items, total=total, page=page, page_size=size, pages=(total+size-1)//size)


def snapshot(ledger, calendar, *, now=None):
    now = now or datetime.now(CHINA)
    closed = closed_days(BackfillQueue(ledger, calendar).calendar(), now)
    latest = closed[-1] if closed else None
    datasets = []
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for dataset, label, cadence in DATASETS:
            attempt = conn.execute("""SELECT u.status,u.error_code,j.status,j.error_code,j.cancel_requested
                FROM data_update_units u JOIN left_right_backfill_jobs j USING(ns,job_id)
                WHERE u.ns=%s AND u.dataset=%s ORDER BY u.created_at DESC,u.job_id DESC LIMIT 1""",
                (ledger.ns, dataset)).fetchone()
            saved = conn.execute("""SELECT business_date,source_at,row_count,details FROM data_update_units
                WHERE ns=%s AND dataset=%s AND status IN ('OK','EMPTY')
                ORDER BY business_date DESC,finished_at DESC LIMIT 1""", (ledger.ns, dataset)).fetchone()
            if not saved:
                if dataset in ("daily", "adj_factor", "daily_basic", "cyq_perf", "weekly"):
                    batch_table = "supplier_batches" if dataset == "daily" else "supplier_market_batches"
                    saved = conn.execute("""SELECT r.trade_date,b.collected_at,r.row_count,'{}' FROM supplier_tushare_eod_runs r
                        JOIN """+batch_table+""" b ON b.batch_id=r.batch_id WHERE r.api=%s
                        ORDER BY r.trade_date DESC,r.completed_at DESC LIMIT 1""", (dataset,)).fetchone()
                else:
                    saved = conn.execute("""SELECT (scope->>'trade_date')::date,collected_at,row_count,body->'receipt'->'details'
                        FROM supplier_market_batches WHERE provider='tushare' AND endpoint=%s
                        AND scope->>'schema_version'='tushare-reference-restored-v1'
                        ORDER BY scope->>'trade_date' DESC,collected_at DESC LIMIT 1""", (dataset,)).fetchone()
            status = _effective_status(attempt[0], attempt[2], attempt[4]) if attempt else ("OK" if saved else "NOT_SYNCED")
            error = (attempt[1] or attempt[3]) if attempt and status == "FAILED" else None
            if saved and status in ("OK", "EMPTY"):
                if saved[2] == 0:
                    status = "EMPTY"
                elif latest and cadence == "日更" and str(saved[0]) < latest:
                    status = "STALE"
                elif cadence == "月更" and str(saved[0])[:7] < now.astimezone(CHINA).strftime("%Y-%m"):
                    status = "STALE"
                elif cadence == "年更" and saved[0].year < now.astimezone(CHINA).year-1:
                    status = "STALE"
            datasets.append(dict(dataset=dataset,label=label,cadence=cadence,status=status,
                business_date=str(saved[0]) if saved else None, last_success_at=saved[1].isoformat() if saved and saved[1] else None,
                row_count=saved[2] if saved else None,details=_object(saved[3]) or {} if saved else {},error_code=error))
    return dict(latest_business_date=latest,datasets=datasets,recent_jobs=jobs(ledger,"page_size=20")["items"])


def execute_job(executor, claim, *, collector=None, builder=None, board_collector=None):
    from .data_updates_collect import execute_update
    from .left_right_backfill_collectors import WARMUP_DAYS, collect_boards
    from .stage2.left_right_refresh import build_manual_archives
    queue, body = executor.queue, claim.body
    result = dict(completed=0, failed=0, progress={})
    receipts = {}
    def checkpoint():
        if not queue.pulse(claim):
            raise Interrupted("LEASE_LOST_OR_CANCELLED")
        if executor.paused():
            raise Interrupted("PAUSED")
        if executor.stop.is_set():
            raise Interrupted("WORKER_STOPPED")
        if not executor.external_enabled():
            raise Interrupted("EXTERNAL_DISABLED")
    def request(source, scope, operation):
        checkpoint()
        receipt = queue.request(claim, source, scope, operation)
        checkpoint()
        return receipt
    def progress(**fields):
        checkpoint()
        result["progress"].update(fields)
        queue.progress(claim, result)
    def record(dataset, day, receipt):
        checkpoint()
        candidates = [u for u in body["units"] if u["dataset"] == dataset and
                      (u["business_date"] == day or dataset == "daily_assessment" and body["mode"] == "yearly")]
        if len(candidates) != 1 or receipt["status"] not in ("RUNNING", "OK", "EMPTY", "FAILED"):
            raise ValueError("unexpected update unit receipt")
        terminal = receipt["status"] != "RUNNING"
        with queue.ledger._connect() as conn, conn.transaction():
            active = conn.execute("""SELECT 1 FROM left_right_backfill_jobs
                WHERE ns=%s AND job_id=%s AND claim_token=%s AND status='RUNNING'
                AND NOT cancel_requested AND lease_until>clock_timestamp() FOR UPDATE""",
                (queue.ns,claim.job_id,claim.token)).fetchone()
            if not active:
                raise Interrupted("LEASE_LOST_OR_CANCELLED")
            conn.execute("""UPDATE data_update_units SET business_date=%s,status=%s,
                row_count=CASE WHEN %s THEN %s ELSE row_count END,
                source_at=CASE WHEN %s THEN %s ELSE source_at END,
                details=CASE WHEN %s THEN %s::jsonb ELSE details END,error_code=%s,
                started_at=coalesce(started_at,clock_timestamp()),
                finished_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END
                WHERE ns=%s AND job_id=%s AND unit_key=%s""",
                (day,receipt["status"],terminal,receipt.get("row_count"),terminal,receipt.get("observed_at"),
                 terminal,encode(receipt.get("details") or {}),receipt.get("error_code"),terminal,
                 queue.ns,claim.job_id,candidates[0]["unit_key"]))
        if terminal:
            receipts[(dataset,day)] = receipt["status"]
            result["failed" if receipt["status"] == "FAILED" else "completed"] += 1
    def assessment(day):
        record("daily_assessment",day,dict(status="RUNNING"))
        as_of = datetime.now(CHINA)
        try:
            built = (builder or build_manual_archives)(queue.ledger,[day],None,as_of,
                persist=True,checkpoint=checkpoint,progress=progress)
        except (ValueError,TypeError,KeyError,OSError,psycopg.Error) as exc:
            record("daily_assessment",day,dict(status="FAILED",error_code=type(exc).__name__))
            return
        usable = built["archives_saved"] > 0 and built["ready_items"] > 0
        record("daily_assessment",day,dict(status="OK" if usable else "FAILED",
            row_count=built["expected_items"] if built["archives_saved"] else None,
            observed_at=as_of.isoformat(),details={key:built[key] for key in ("ready_items","missing_count")},
            error_code=None if usable else "ASSESSMENT_INPUT_MISSING"))
    try:
        checkpoint()
        collected = (collector or execute_update)(queue.ledger, body, executor.config, executor.captures,
                                                  request, checkpoint, record, progress)
        if body["mode"] == "daily":
            # The retained LR assessment depends on dated Kaipanla sectors and
            # memberships. This helper only fills the requested sector window;
            # it does not expand the user's requested daily-bar date range.
            with queue.ledger._connect() as conn:
                has_daily = conn.execute("SELECT 1 FROM supplier_daily_bars WHERE trade_date=ANY(%s::date[]) LIMIT 1",
                    (body["trade_dates"],)).fetchone() is not None
            if has_daily or any(receipts.get(("daily",day)) == "OK" for day in body["trade_dates"]):
                first, last = date.fromisoformat(body["date_from"]),date.fromisoformat(body["date_to"])
                issues = []
                progress(phase="COLLECTING_SECTORS")
                (board_collector or collect_boards)(queue.ledger,body,first-timedelta(days=WARMUP_DAYS),
                    last+timedelta(days=6-last.weekday()),executor.captures,request,checkpoint,issues)
                result.update(sector_issues=issues[:100],sector_issue_count=len(issues))
            for day in body["trade_dates"]:
                assessment(day)
        if body["mode"] == "yearly":
            day = collected.get("recompute_date")
            if not day:
                record("daily_assessment",body["latest_business_date"],dict(status="FAILED",error_code="NO_SAVED_MARKET_CLOSES"))
            else:
                assessment(day)
        checkpoint()
        complete = result["completed"] == len(body["units"]) and result["failed"] == 0
        return queue.finish(claim,status="SUCCEEDED" if complete else "PARTIAL",result=result)
    except Interrupted as exc:
        code = str(exc)
        return queue.finish(claim,status="QUEUED" if code == "PAUSED" else "FAILED",result=result,error=code)
    except (ValueError,TypeError,KeyError,OSError,psycopg.Error) as exc:
        return queue.finish(claim,result=result,error=type(exc).__name__)


class UpdateSchedule:
    """The retained next-morning 06:00 daily sync, in the single worker."""
    def __init__(self, queue, enabled):
        self.queue, self.enabled = queue, enabled

    def __call__(self, now=None):
        now = (now or datetime.now(CHINA)).astimezone(CHINA)
        if not self.enabled() or not 6*60 <= now.hour*60+now.minute < 8*60+30:
            return None
        calendar = self.queue.calendar()
        prior = [day for day in calendar if day < now.date().isoformat()]
        if not prior:
            return None
        day = prior[-1]
        payload = dict(request_id="scheduled-eod-"+day,mode="daily",date_from=day,date_to=day)
        try:
            return submit(self.queue.ledger,self.queue.calendar_source,payload,now=now)
        except QueueFull:
            return None
