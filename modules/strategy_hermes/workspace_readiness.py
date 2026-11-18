"""Read-only secondary workspace views backed by persisted evidence."""
from datetime import date, datetime
import json
from pathlib import Path
import os
import re
import shutil

from redis.exceptions import RedisError

from .simulator import CHINA, moment
from .stage2.inputs import cache_client, read_file
from .stage2.worker import SLOTS, planned_at

WORKER_STATUS_TTL = 180
# Includes bounded collector and temporary-storage details written by runtime.
WORKER_STATUS_MAX_BYTES = 16 * 1024
WORKER_SWITCHES = ("hermes_schedule", "official_collection", "qq_push")
EXPERT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
MODEL_REF = re.compile(r"[\w .()@-]{1,80}/[\w.()@:/-]{1,120}\Z")
ERROR_CATEGORY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}\Z")
QQ_TEMPLATE = re.compile(r"(?:octop-markdown-v1:(?:workspace|deployment)|qq-template-v2:(?:research|maintenance|status|validation|daily):(?:managed|bundled)):[0-9a-f]{64}\Z")


def worker_status_key(namespace):
    return namespace + ":ops:worker-status"


def _read_worker_status(ledger, now):
    try:
        with cache_client(ledger.settings) as cache:
            raw = cache.get(worker_status_key(ledger.ns))
        if not raw or len(raw) > WORKER_STATUS_MAX_BYTES:
            return None
        body = json.loads(raw)
        at = moment(body["observed_at"])
        age = (now - at).total_seconds()
        if not 0 <= age <= WORKER_STATUS_TTL or any(type(body[key]) is not bool for key in WORKER_SWITCHES):
            return None
        stock_research = body.get("stock_research")
        fields = {"observed_at": at.isoformat(), **{key:body[key] for key in WORKER_SWITCHES},
                  "stock_research": stock_research if type(stock_research) is bool else None}
        findesk_iopv = body.get("findesk_iopv")
        fields["findesk_iopv"] = findesk_iopv if type(findesk_iopv) is bool else None
        if isinstance(body.get("temporary"), dict):
            fields["temporary"] = {key: body["temporary"].get(key) for key in
                                   ("managed_bytes","managed_entries","usage_error","last_cleanup")}
        return fields
    except (OSError, RedisError, ValueError, TypeError, KeyError, RecursionError):
        return None


def _upcoming(calendar, now):
    upcoming = []
    for day in calendar:
        if date.fromisoformat(day) < now.date():
            continue
        for slot in SLOTS:
            at = planned_at(day, slot)
            if at > now:
                upcoming.append(dict(trade_date=day, slot=slot, planned_at=at.isoformat()))
        if len(upcoming) >= len(SLOTS):
            break
    return upcoming[:len(SLOTS)]


def _delivery(target, report_id, row, last_sent):
    if row:
        state, attempts, delivered, error, raw = row
        body = json.loads(raw) if raw else {}
        status = body.get("http_status")
        posts = body.get("message_posts")
        receipt = {}
        if body.get("status") == "ACCEPTED":
            receipt["status"] = "ACCEPTED"
        if type(status) is int and 100 <= status <= 599:
            receipt["http_status"] = status
        if type(posts) is int and posts in (0, 1):
            receipt["message_posts"] = posts
    else:
        state, attempts, delivered, error, receipt = "NOT_QUEUED", 0, None, None, {}
    return dict(target=target, report_id=report_id, state=state, attempts=attempts, error=error,
                delivered_at=delivered.astimezone(CHINA).isoformat() if delivered else None,
                last_sent_at=last_sent.astimezone(CHINA).isoformat() if last_sent else None,
                receipt=receipt)


def qq_brief(row):
    """Expose the actual QQ body and distinguish formatting from transport."""
    delivery = _delivery("octop", None, row[:5] if row else None, None)
    body = json.loads(row[5]) if row else {}
    text = body.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 1500:
        text = None
    template = body.get("template")
    if not isinstance(template, str) or not QQ_TEMPLATE.fullmatch(template):
        template = None
    version = body.get("format")
    markdown = version in ("octop-markdown-v1", "qq-template-v2")
    if template and not template.startswith(str(version) + ":"):
        template = None
    if text:
        format_state = "READY" if markdown else "HISTORICAL"
    elif markdown:
        format_state = {"PENDING": "PENDING", "SENDING": "FORMATTING", "FAILED": "FAILED"}.get(delivery["state"], "UNAVAILABLE")
        if format_state == "PENDING" and delivery["error"] == "QQFormatUnavailable":
            format_state = "RETRYING"
    else:
        format_state = "UNAVAILABLE"
    return {key: delivery[key] for key in ("state", "attempts", "error", "delivered_at", "receipt")} | {
        "format": version if markdown else "historical-text" if text else None,
        "template": template if markdown else None,
        "template_fallback": markdown and body.get("workspace_template_invalid") is True,
        "format_state": format_state, "text": text,
    }


def _octop_status(report, failure):
    report_id = published_at = model_expert = configured_model = None
    if report:
        report_id, published_at, expert, model = report
        if isinstance(expert, str) and EXPERT_ID.fullmatch(expert):
            model_expert = expert
        if isinstance(model, str) and MODEL_REF.fullmatch(model) and "://" not in model:
            configured_model = model
    last_failure = None
    if failure:
        day, slot, error = failure
        category = error if isinstance(error, str) and ERROR_CATEGORY.fullmatch(error) else None
        last_failure = dict(trade_date=day.isoformat(), slot=slot, error=category)
    return dict(report_id=report_id, published_at=published_at, model_expert=model_expert,
                configured_model=configured_model, last_failure=last_failure)


def recent_evaluation(ledger, view, calendar):
    if not calendar or not view.get("watermark"):
        return None
    as_of = moment(view["watermark"]).date().isoformat()
    days = [day for day in calendar if day <= as_of][-20:]
    if not days:
        return None
    first, last = days[0], days[-1]
    cohort = []
    for cycle in view["cycles"]:
        fills = cycle["book"]["fills"]
        entry = next((fill for fill in fills if fill["action"] == "BUY"), None)
        if entry and first <= moment(entry["filled_at"]).date().isoformat() <= last:
            cohort.append(cycle)
    return dict(ledger._totals(cohort), start=first, end=last, entered=len(cohort),
                closed=sum(cycle["status"] == "CLOSED" for cycle in cohort), as_of=view["watermark"])


def eod_reference(path, calendar):
    if not path or not Path(path).is_file():
        return None
    return eod_body(read_file(path), calendar)


def eod_body(body, calendar):
    if body.get("schema_version") != "stock-v4-left-right-eod-v1":
        raise ValueError("invalid LEFT/RIGHT archive")
    observed = moment(body["observed_at"])
    day = observed.date().isoformat()
    if body["trade_date"] != day or observed.hour < 15 or calendar and day not in calendar:
        raise ValueError("LEFT/RIGHT archive must be dated after the close")
    items = body["items"]
    if not isinstance(items, list) or len(items) > 100 or not body.get("source"):
        raise ValueError("invalid LEFT/RIGHT archive entries")
    rows = []
    for item in items:
        if (item.get("side") not in ("LEFT", "RIGHT")
                or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", item["code"])
                or not isinstance(item["note"], str) or len(item["note"]) > 500):
            raise ValueError("invalid LEFT/RIGHT archive row")
        rows.append(dict(code=item["code"], side=item["side"], note=item["note"]))
    return dict(trade_date=day, observed_at=observed.isoformat(), source=body["source"], items=rows)


def runtime_status(ledger, paused, calendar=(), *, now=None, maintenance=None):
    now = now or datetime.now(CHINA)
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        rows = conn.execute(
            """SELECT trade_date,slot,state,error_code FROM round_tasks WHERE ns=%s
               ORDER BY trade_date DESC,slot DESC LIMIT 5""", (ledger.ns,),
        ).fetchall()
        notifications = conn.execute(
            """SELECT target,state,count(*) FROM outbox WHERE ns=%s AND state IN ('PENDING','FAILED')
               GROUP BY target,state ORDER BY target,state""", (ledger.ns,),
        ).fetchall()
        report = conn.execute(
            """SELECT t.report_id,e.at,
                      e.payload#>>'{input_meta,octop,receipt,model_expert}',
                      e.payload#>>'{input_meta,octop,receipt,configured_model}'
               FROM round_tasks t
               LEFT JOIN events e ON (e.ns,e.id)=(t.ns,t.report_id) AND e.kind='report'
               WHERE t.ns=%s AND t.state='PUBLISHED'
               ORDER BY t.trade_date DESC,t.slot DESC LIMIT 1""", (ledger.ns,),
        ).fetchone()
        failure = conn.execute(
            """SELECT trade_date,slot,error_code FROM round_tasks WHERE ns=%s AND state='FAILED'
               ORDER BY trade_date DESC,slot DESC LIMIT 1""", (ledger.ns,),
        ).fetchone()
        report_id = report[0] if report else None
        latest = {}
        if report_id:
            latest = {row[0]:row[1:] for row in conn.execute(
                """SELECT target,state,attempts,delivered_at,error_code,delivery_receipt,payload
                   FROM outbox WHERE ns=%s AND event_key=%s AND target IN ('redis','octop')""",
                (ledger.ns, "report:" + report_id),
            )}
        last_sent = dict(conn.execute(
            """SELECT target,max(delivered_at) FROM outbox
               WHERE ns=%s AND state='SENT' AND target IN ('redis','octop')
                 AND event_key LIKE 'report:%%' GROUP BY target""", (ledger.ns,),
        ))
    path = os.environ.get("APP_TMP_ROOT")
    temporary = None
    if path and Path(path).is_dir():
        usage = shutil.disk_usage(path)
        temporary = dict(filesystem_free_bytes=usage.free, filesystem_total_bytes=usage.total,
                         budget_bytes=int(os.environ.get("APP_TMP_BUDGET_BYTES", 2**31)),
                         minimum_free_bytes=int(os.environ.get("STORAGE_MIN_FREE_BYTES", 5*2**30)),
                         hard_limit=os.environ.get("TMP_FS_HARD_LIMIT_VERIFIED", "").lower() == "true")
    if temporary is not None and maintenance:
        temporary.update(maintenance.status())
    worker = _read_worker_status(ledger, now)
    schedule = dict(timezone="Asia/Shanghai", slots=SLOTS, upcoming=_upcoming(calendar, now))
    deliveries = []
    for target in ("redis", "octop"):
        row = latest.get(target)
        summary = _delivery(target, report_id, row[:5] if row else None, last_sent.get(target))
        if target == "octop":
            summary["format_state"] = qq_brief(row)["format_state"]
        deliveries.append(summary)
    from shared_kernel.diagnostics import snapshot
    return dict(paused=paused, worker=worker, schedule=schedule, octop=_octop_status(report, failure),diagnostics=snapshot(),
                deliveries=deliveries,
                rounds=[dict(trade_date=day.isoformat(), slot=slot, state=state, error=error)
                                      for day, slot, state, error in rows],
                notifications=[dict(target=target, state=state, count=count)
                               for target, state, count in notifications], temporary=temporary)
