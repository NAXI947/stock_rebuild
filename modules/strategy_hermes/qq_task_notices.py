"""Durable QQ task outcomes from trusted runtime facts, independent of research."""
from datetime import datetime
import os
import re

from .ledger import encode,fingerprint
from .simulator import CHINA,moment
from .qq_templates import MESSAGE_FORMAT
from .qq_message_fields import status_fields

BINDINGS={
 'cFSMGMB':('6T6VMW','research','09:35'), 'cKAZXDS':('6T6VMW','research','10:35'),
 'c3FDPYP':('6T6VMW','research','13:05'), 'cG7V9GS':('6T6VMW','research','14:05'),
 'c2XGZ0F':('6T6VMW','research','14:30'), 'cMWFP36':('ATAPBR','maintenance','market'),
 'c5PT18W':('ATAPBR','maintenance','retry'), 'cY8HM7S':('ATAPBR','maintenance','preopen'),
}
from .maintenance_policy import MODES as SLOTS
STATES={'FAILED','SKIPPED','PAUSED','CANCELLED'}


def enabled(ledger):
    return ledger.origin=='LIVE' and os.environ.get('ENABLE_QQ_TASK_NOTICES','false').lower()=='true'


def enqueue_status(ledger,result,kind,slot,*,at=None,identity=None):
    if not enabled(ledger) or result.get('state') not in STATES:
        return dict(state='NOT_QUEUED')
    clock=at or datetime.now(CHINA)
    run_id=result.get('run_id')
    if run_id:
        # Maintenance already writes its result notification atomically, including failures.
        with ledger._connect() as conn:
            existing=conn.execute("SELECT state FROM outbox WHERE ns=%s AND report_id=%s AND target='octop'",(ledger.ns,str(run_id))).fetchone()
        if existing:return dict(state=existing[0],reason='EXISTING_RESULT_NOTIFICATION')
    reason=result.get('error') or result.get('reason') or 'UNKNOWN'
    identity=run_id or identity or ':'.join((clock.date().isoformat(),str(slot),result['state'],str(reason)))
    key='task-status:'+kind+':'+fingerprint(str(identity))[:32]
    name=('板块轮动·ETF穿透 ' if kind=='research' else 'ETF资料维护 ')+SLOTS.get(slot,slot or '')
    fields=status_fields(name,result['state'],at=clock,task_id=str(run_id or identity),reason=reason,
                         result=result.get('notice_result'),qq=result.get('qq') or {'state':'NOT_QUEUED'},
                         next_action='查看任务记录；后续按既定计划执行')
    if result.get('notice_status'):fields['status']=result['notice_status']
    payload=dict(format=MESSAGE_FORMAT,template_kind='status',sections=fields,kind='task_status')
    with ledger._connect() as conn:
        conn.execute("INSERT INTO outbox(ns,report_id,event_key,target,payload) VALUES(%s,%s,%s,'octop',%s) ON CONFLICT DO NOTHING",
                     (ledger.ns,str(run_id or identity),key,encode(payload)))
        row=conn.execute("SELECT state FROM outbox WHERE ns=%s AND event_key=%s AND target='octop'",(ledger.ns,key)).fetchone()
    return dict(state=row[0],event_key=key)


def receive_native_failure(queue,maintenance,payload,*,now=None):
    if not isinstance(payload,dict) or set(payload)!={'agent_id','cron_id','thread_id','observed_at','error_code'}:
        raise ValueError('INVALID_NOTICE_FIELDS')
    if not all(isinstance(v,str) and len(v)<=128 for v in payload.values()):
        raise ValueError('INVALID_NOTICE_VALUE')
    binding=BINDINGS.get(payload['cron_id'])
    if not binding or payload['agent_id']!=binding[0]:raise ValueError('INVALID_NOTICE_BINDING')
    if not re.fullmatch(r'thr_[A-Za-z0-9_-]{8,80}',payload['thread_id']):raise ValueError('INVALID_NOTICE_THREAD')
    if not re.fullmatch(r'(?:STOCK_NATIVE_[A-Z_]+|[A-Za-z][A-Za-z0-9]*(?:Error|Exception))',payload['error_code']):
        raise ValueError('INVALID_NOTICE_ERROR')
    clock=now or datetime.now(CHINA);seen=moment(payload['observed_at'])
    if abs((clock-seen).total_seconds())>180:raise ValueError('EXPIRED_NOTICE')
    _,kind,slot=binding
    if kind=='maintenance' and maintenance is None:raise ValueError('MAINTENANCE_DISABLED')
    ledger=queue.ledger
    if not enabled(ledger):return dict(state='DISABLED')
    result=dict(state='FAILED',error=payload['error_code'])
    with ledger._connect() as conn:
        if kind=='research':
            row=conn.execute('''SELECT run_id FROM native_round_attempts WHERE ns=%s AND trade_date=%s AND slot=%s
                ORDER BY (retry_of IS NOT NULL) DESC,created_at DESC LIMIT 1''',
                             (ledger.ns,clock.date(),slot)).fetchone()
            if row:
                actual=queue.status(str(row[0]))
                if actual['state']=='PUBLISHED':return dict(state='SKIPPED',reason='REPORT_ALREADY_PUBLISHED')
                result['run_id']=str(row[0])
                if actual['state'] not in STATES:
                    result.update(notice_status='🟠 原生会话异常',notice_result='会话已中断；后台任务状态：'+actual['state'],qq=actual.get('qq'))
        else:
            row=conn.execute("SELECT job_id FROM left_right_backfill_jobs WHERE ns=%s AND body->>'request_id'=%s",
                             (ledger.ns,'etf-maint-'+clock.date().isoformat()+'-'+slot)).fetchone()
            if row:
                actual=maintenance.status(str(row[0]))
                if actual['state'] in ('SUCCEEDED','PARTIAL') or actual.get('qq',{}).get('state')!='NOT_QUEUED':
                    return dict(state='SKIPPED',reason='MAINTENANCE_RESULT_EXISTS')
                result['run_id']=str(row[0])
                if actual['state'] not in STATES:
                    result.update(notice_status='🟠 原生会话异常',notice_result='会话已中断；后台维护仍在处理，状态：'+actual['state'],qq=actual.get('qq'))
    return enqueue_status(ledger,result,kind,slot,at=clock,identity=payload['thread_id'])
