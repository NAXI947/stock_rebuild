"""Operator authorization; OCTOP alone consumes it, worker alone republishes.

Only a same-day failed PUBLISH with saved, validated inputs can be recovered.
No collection, model call, source-clock rewrite or retry-budget reset occurs.
"""
from datetime import datetime, time
from hashlib import sha256
from uuid import uuid4

from ..ledger import encode, fingerprint
from ..simulator import CHINA, moment


class RecoveryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    import json
    return json.loads(value) if isinstance(value, str) else value


def _lock(conn, ns):
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (ns+':native-recovery',))


def _check_clock(clock, day):
    # Publication uses saved facts, so lunch is allowed; it never collects data.
    if clock.date().isoformat() != day or not time(9,30) <= clock.time().replace(tzinfo=None) < time(15):
        raise RecoveryError('RECOVERY_OUTSIDE_SAME_TRADING_DAY')


def _check_state(ledger, conn, day, slot, child=None):
    later = conn.execute('''SELECT 1 FROM round_tasks WHERE ns=%s AND trade_date=%s
        AND slot>%s AND state='PUBLISHED' LIMIT 1''', (ledger.ns,day,slot)).fetchone()
    if later:
        raise RecoveryError('RECOVERY_LATER_ROUND_PUBLISHED')
    active = conn.execute('''SELECT 1 FROM native_round_attempts WHERE ns=%s
        AND state NOT IN ('FAILED','PUBLISHED') AND (%s::uuid IS NULL OR run_id<>%s::uuid) LIMIT 1''',
        (ledger.ns,child,child)).fetchone()
    other = conn.execute('''SELECT 1 FROM round_tasks WHERE ns=%s AND state='RUNNING'
        AND (trade_date<>%s OR slot<>%s) LIMIT 1''',(ledger.ns,day,slot)).fetchone()
    if active or other:
        raise RecoveryError('RECOVERY_OTHER_ROUND_ACTIVE')


def _parent(ledger, conn, parent_id):
    from .native_rounds import _validate_decisions
    row = conn.execute('''SELECT trade_date,slot,state,failure_stage,context,decisions,decision_hash,retry_of
        FROM native_round_attempts WHERE ns=%s AND run_id=%s''', (ledger.ns,parent_id)).fetchone()
    if row is None:
        raise RecoveryError('RECOVERY_PARENT_NOT_FOUND')
    day,slot,state,stage,context,decisions,digest,retry_of = row
    if state != 'FAILED' or stage != 'PUBLISH' or retry_of is not None:
        raise RecoveryError('RECOVERY_REQUIRES_ORIGINAL_PUBLISH_FAILURE')
    context,decisions = _json(context),_json(decisions)
    if not isinstance(context,dict) or not {'request','calendar','mapping','market','cutoff','analysis'} <= context.keys() or not digest:
        raise RecoveryError('RECOVERY_FROZEN_INPUTS_INCOMPLETE')
    try:
        _validate_decisions(decisions,context['request'])
        same = (fingerprint(decisions) == digest and context['request']['trade_date'] == day.isoformat()
                and context['request']['round_slot'] == slot and day.isoformat() in context['calendar']
                and moment(context['cutoff']).date() == day)
    except (ValueError,TypeError,KeyError):
        same = False
    if not same:
        raise RecoveryError('RECOVERY_FROZEN_INPUTS_INVALID')
    report_id = 'octop-'+sha256(encode([ledger.ns,context['request']['planned_at']]).encode()).hexdigest()[:28]
    exists = conn.execute('''SELECT 1 FROM events WHERE ns=%s AND kind='report' AND id=%s
        UNION ALL SELECT 1 FROM outbox WHERE ns=%s AND report_id=%s LIMIT 1''',
        (ledger.ns,report_id,ledger.ns,report_id)).fetchone()
    if exists:
        raise RecoveryError('RECOVERY_REPORT_ALREADY_COMMITTED')
    return day.isoformat(),slot,context,decisions,digest


def authorize(queue, parent_id, *, operator, reason, now=None):
    """Administrative CLI only. Does not enqueue, collect, publish or notify."""
    from .native_rounds import _run_id
    parent_id = _run_id(parent_id)
    if queue.paused():
        raise RecoveryError('RELEASE_PAUSED')
    if not isinstance(operator,str) or not 1 <= len(operator.strip()) <= 100 or not isinstance(reason,str) or not 1 <= len(reason.strip()) <= 500:
        raise ValueError('recovery operator and reason must be bounded nonempty text')
    clock = queue.clock(now)
    ledger = queue.ledger
    with ledger._connect() as conn, conn.transaction():
        _lock(conn,ledger.ns)
        previous = conn.execute('''SELECT child_run_id,expires_at,consumed_at,expires_at>clock_timestamp() FROM native_publication_recoveries
            WHERE ns=%s AND parent_run_id=%s''',(ledger.ns,parent_id)).fetchone()
        if previous:
            return dict(state=('AUTHORIZED' if previous[3] else 'EXPIRED') if previous[2] is None else 'CONSUMED',run_id=str(previous[0]),
                        retry_of=parent_id,expires_at=previous[1].astimezone(CHINA).isoformat())
        day,slot,context,decisions,digest = _parent(ledger,conn,parent_id)
        _check_clock(clock,day)
        _check_state(ledger,conn,day,slot)
        task = conn.execute('''SELECT state,attempts,to_jsonb(t) FROM round_tasks t
            WHERE ns=%s AND trade_date=%s AND slot=%s FOR UPDATE''',(ledger.ns,day,slot)).fetchone()
        if not task or task[0] != 'FAILED' or task[1] != 1:
            raise RecoveryError('RECOVERY_ORIGINAL_ATTEMPT_UNAVAILABLE')
        child = str(uuid4())
        expires = conn.execute('''INSERT INTO native_publication_recoveries
            (ns,parent_run_id,child_run_id,authorized_by,reason,parent_task,context_hash,decision_hash)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING expires_at''',
            (ledger.ns,parent_id,child,operator.strip(),reason.strip(),task[2],fingerprint(context),digest)).fetchone()[0]
    return dict(state='AUTHORIZED',run_id=child,retry_of=parent_id,trade_date=day,slot=slot,
                expires_at=expires.astimezone(CHINA).isoformat(),next_action='OCTOP_RUN_NOW')


def consume(queue, day, slot, clock):
    """Called only by the original prepare(slot) tool, inside OCTOP run-now."""
    ledger = queue.ledger
    with ledger._connect() as conn, conn.transaction():
        _lock(conn,ledger.ns)
        row = conn.execute('''SELECT r.parent_run_id,r.child_run_id,r.expires_at,r.consumed_at,
            r.context_hash,r.decision_hash,r.expires_at>clock_timestamp()
            FROM native_publication_recoveries r JOIN native_round_jobs j
            ON (j.ns,j.run_id)=(r.ns,r.parent_run_id)
            WHERE r.ns=%s AND j.trade_date=%s AND j.slot=%s FOR UPDATE OF r''',
            (ledger.ns,day,slot)).fetchone()
        if row is None:
            return None
        parent,child,expires,consumed,context_hash,digest,valid = row
        if consumed:
            return str(child)
        if not valid:
            raise RecoveryError('RECOVERY_AUTHORIZATION_EXPIRED')
        _check_clock(clock,day)
        _check_state(ledger,conn,day,slot)
        _,_,context,decisions,parent_digest = _parent(ledger,conn,str(parent))
        if fingerprint(context) != context_hash or parent_digest != digest:
            raise RecoveryError('RECOVERY_FROZEN_INPUTS_CHANGED')
        task = conn.execute('''SELECT state,attempts FROM round_tasks WHERE ns=%s
            AND trade_date=%s AND slot=%s FOR UPDATE''',(ledger.ns,day,slot)).fetchone()
        if not task or task != ('FAILED',1):
            raise RecoveryError('RECOVERY_ORIGINAL_ATTEMPT_UNAVAILABLE')
        from .worker import planned_at
        claim = ledger._claim_round(conn,day,slot,planned_at(day,slot),lease_seconds=120,max_attempts=2)
        if claim is None:
            raise RecoveryError('RECOVERY_ATTEMPT_EXHAUSTED')
        conn.execute('''INSERT INTO native_round_recovery_jobs
            (ns,run_id,trade_date,slot,state,expires_at,lease_token,context,decisions,decision_hash,retry_of)
            VALUES(%s,%s,%s,%s,'SUBMITTED',LEAST(%s,clock_timestamp()+interval '120 seconds'),%s,%s,%s,%s,%s)''',
            (ledger.ns,child,day,slot,expires,claim.token,encode(context),encode(decisions),digest,parent))
        conn.execute('''UPDATE native_publication_recoveries SET consumed_at=clock_timestamp()
            WHERE ns=%s AND parent_run_id=%s''',(ledger.ns,parent))
    return str(child)


def metadata(queue, job, executed_at):
    if not job.get('retry_of'):
        return None
    return dict(retry_of=job['retry_of'],recovery_run_id=job['run_id'],source_cutoff=job['context']['cutoff'],
                decision_hash=fingerprint(job['decisions']),executed_at=executed_at.isoformat(),
                mode='FROZEN_PUBLICATION_RECOVERY')


def validate_publication(ledger, conn, claim, recovery):
    """Recheck in the committing transaction; no stale replay after a later round."""
    clock = moment(recovery['executed_at']) if ledger.origin == 'FROZEN' else datetime.now(CHINA)
    _check_clock(clock,claim.day)
    child = recovery['recovery_run_id']
    _check_state(ledger,conn,claim.day,claim.slot,child)
    row = conn.execute('''SELECT r.expires_at>clock_timestamp(),r.consumed_at,j.state,j.lease_token,
        r.context_hash,r.decision_hash,j.context,j.decisions,j.retry_of
        FROM native_publication_recoveries r JOIN native_round_recovery_jobs j
        ON (j.ns,j.run_id)=(r.ns,r.child_run_id)
        WHERE r.ns=%s AND r.parent_run_id=%s AND r.child_run_id=%s''',
        (ledger.ns,recovery['retry_of'],child)).fetchone()
    if not row or not row[0] or row[1] is None or row[2] != 'PUBLISHING' or str(row[3]) != claim.token:
        raise RecoveryError('RECOVERY_PUBLICATION_NOT_AUTHORIZED')
    if row[4] != fingerprint(_json(row[6])) or row[5] != fingerprint(_json(row[7])) or row[5] != recovery['decision_hash']:
        raise RecoveryError('RECOVERY_FROZEN_INPUTS_CHANGED')
    _parent(ledger,conn,recovery['retry_of'])
