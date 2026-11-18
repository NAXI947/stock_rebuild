"""OCTOP-triggered reference upkeep in the existing bounded worker queue.

Maintenance results never enter the Hermes direction/transaction decision path.
Only successfully published reference versions are consumed by later rounds.
"""
from datetime import date, datetime
import json
import os
from pathlib import Path
from time import monotonic

import psycopg

from .ledger import encode, fingerprint
from .simulator import CHINA
from .left_right_backfill import BackfillQueue, Interrupted, QueueFull, LOCK, CAPACITY
from .stage2.maintenance_store import live_mapping, publish_mapping
from .stage2.reference_runtime import runtime_calendar, calendar_status
from .qq_message_fields import maintenance_fields, verification_lines
from .qq_templates import MESSAGE_FORMAT
from .maintenance_policy import ACTIVE_MODES, LEGACY_MODES, MANUAL_MODES, MODES, allowed_window, completed_session

TERMINAL = {'SUCCEEDED','PARTIAL','FAILED','CANCELLED','SKIPPED','PAUSED'}
MAX_REQUESTS = 120
MAX_SECONDS = 12 * 60
GOOD_STATES = {'READY','OK','EMPTY','CURRENT','UNCHANGED','UPDATED','STORED','ALREADY_STORED','NOT_DUE'}
VERIFY_RETRY_NOTE='本次官方请求已消耗预算；重复verify只返回原回执，待下一原维护窗口（10:00/14:15）回核。'
SOURCE_UPGRADE_KEYS=('material_source_upgrade','material_source_upgrade_records')


def _notification_key(job_id,result):
    upgrade=result.get('material_source_upgrade') or {}
    return 'maintenance:'+job_id+(':source:'+upgrade['fingerprint'] if upgrade else '')


class MaintenanceQueue:
    def __init__(self,ledger,calendar,*,paused=lambda:False):
        self.ledger,self.calendar,self.paused=ledger,calendar,paused
        self.queue=BackfillQueue(ledger,calendar)

    def prepare(self,mode,*,now=None,request_ids=None,target_codes=None):
        now=(now or datetime.now(CHINA)).astimezone(CHINA)
        if mode in LEGACY_MODES:
            return dict(state='SKIPPED',mode=mode,reason='SCHEDULE_REPLACED')
        if mode not in ACTIVE_MODES and mode not in MANUAL_MODES:
            raise ValueError('unknown maintenance mode')
        if self.paused():
            return dict(state='PAUSED',mode=mode)
        if target_codes is not None and mode!='materials':
            raise ValueError('target scope requires materials mode')
        if mode == 'verify':
            return self._prepare_verify(now,request_ids)
        if request_ids is not None:
            raise ValueError('verification scope requires verify mode')
        if mode == 'materials':
            return self._prepare_materials(now,target_codes)
        if not allowed_window(mode,now):
            return dict(state='SKIPPED',mode=mode,reason='OUTSIDE_MAINTENANCE_WINDOW',next_slot=MODES[mode])
        days=runtime_calendar(self.ledger,self.calendar,now)
        if now.date().isoformat() not in days:
            return dict(state='SKIPPED',mode=mode,reason='NOT_TRADING_DAY')
        completed=completed_session(days,now)
        if completed is None:
            return dict(state='SKIPPED',mode=mode,reason='NO_COMPLETED_SESSION')
        body=dict(request_id='etf-maint-'+now.date().isoformat()+'-'+mode,task='etf_maintenance',mode=mode,
                  business_date=completed,scheduled_date=now.date().isoformat(),version='etf-maintenance-v2')
        try:
            job=self.queue.enqueue(body,MAX_REQUESTS)
        except QueueFull:
            return dict(state='SKIPPED',mode=mode,reason='MAINTENANCE_QUEUE_FULL',hermes='CONTINUES')
        return self.status(job['job_id'])

    def _prepare_materials(self,now,target_codes):
        """Persist one scope per genuine association/version/review invalidation.

        Target state changes made by this job never create a fresh budget.
        A later observed document mismatch permits a distinct, auditable scope.
        """
        from .product_materials import associated_targets
        from .stage2.mapping_maintenance import _priorities
        with self.ledger._connect() as lock:
            lock.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))',(self.ledger.ns+':materials-trigger',))
            mapping=live_mapping(self.ledger,Path(os.environ.get('APP_MAPPING_FILE','/app/data/etf-mapping.json')),now)
            targets=associated_targets(self.ledger,mapping,now,target_codes=target_codes)
            codes=[row[0] for row in targets]
            # Automatic selection rotates only in a new logical maintenance
            # scope, never merely because this batch reached AWAITING_REVIEW.
            held,sectors=_priorities(self.ledger)
            scope=(dict(target_codes=sorted(target_codes)) if target_codes is not None
                   else dict(held=sorted(held),sectors=sorted(sectors)))
            invalidation_codes=sorted(target_codes) if target_codes is not None else list(mapping['products'])
            invalidations=lock.execute('''SELECT DISTINCT ON (etf_code) request_id,result->>'reason' FROM product_verification_requests
                WHERE ns=%s AND etf_code=ANY(%s::text[]) AND state='REVIEW_REQUIRED'
                AND result->>'reason' LIKE 'OFFICIAL_DOCUMENT_CHANGED:%%'
                ORDER BY etf_code,created_at DESC LIMIT 8''',(self.ledger.ns,invalidation_codes)).fetchall()
            # An unrelated mapping publication must not turn the same missing
            # official rule into a new logical task or a fresh request budget.
            scoped=lock.execute("""SELECT job_id FROM left_right_backfill_jobs WHERE ns=%s
                AND body->>'task'='etf_maintenance' AND body->>'mode'='materials'
                AND body->'material_scope'=%s::jsonb ORDER BY created_at DESC,job_id DESC LIMIT 1""",
                (self.ledger.ns,encode(scope))).fetchone()
            if scoped:
                applies,reason=self._resume_material_sources(scoped[0],mapping,now,[row[0] for row in invalidations])
                if applies:
                    receipt=self.status(scoped[0])
                    if reason:receipt['next_action']=reason
                    return receipt
            identity=fingerprint([mapping['version'],scope,invalidations])
            prior=lock.execute('SELECT job_id FROM left_right_backfill_jobs WHERE ns=%s AND request_id=%s',
                               (self.ledger.ns,'etf-materials-'+identity[:40])).fetchone()
            if prior:return self.status(prior[0])
            body=dict(request_id='etf-materials-'+identity[:40],task='etf_maintenance',mode='materials',
                expected_mapping_version=mapping['version'],material_targets=codes,material_scope=scope,
                material_invalidations=[row[0] for row in invalidations],
                business_date=now.date().isoformat(),scheduled_date=now.date().isoformat(),version='etf-static-materials-v1')
            try:job=self.queue.enqueue(body,MAX_REQUESTS)
            except QueueFull:
                return dict(state='SKIPPED',mode='materials',reason='MAINTENANCE_QUEUE_FULL',hermes='CONTINUES')
            return self.status(job['job_id'])

    def _resume_material_sources(self,job_id,mapping,now,invalidations):
        """One controlled static-source revision, using the original task ledger."""
        from .product_materials import source_upgrades, product_basis
        with self.ledger._connect() as conn, conn.transaction():
            conn.execute(LOCK)
            row=conn.execute('''SELECT body,status,cancel_requested,requests_used,request_budget,
                elapsed_seconds,result FROM left_right_backfill_jobs WHERE ns=%s AND job_id=%s FOR UPDATE''',
                (self.ledger.ns,job_id)).fetchone()
            if not row:return False,None
            body,status,cancelled,used,budget,elapsed,details=row
            body=json.loads(body) if isinstance(body,str) else body
            details=json.loads(details) if isinstance(details,str) else details or {}
            targets=body.get('material_targets') or []
            if not targets:return False,None
            if set(body.get('material_invalidations') or [])!=set(invalidations):return False,None
            records=details.get('material_source_upgrade_records') or []
            changes=source_upgrades(self.ledger,mapping,targets)
            if not changes:
                # A completed replacement no longer appears as a URL delta.
                # Its durable target/basis still owns this logical task, even
                # after another product has published a newer mapping parent.
                recorded={change['target_id']:change['etf_code'] for record in records for change in record.get('changes',[])}
                for target_id,code in recorded.items():
                    product=mapping['products'].get(code)
                    if code not in targets or not product:return False,None
                    basis=product_basis(product)
                    if target_id!='pt-'+fingerprint([code,basis])[:40]:return False,None
                    target=conn.execute('SELECT etf_code,product_basis FROM product_verification_targets WHERE ns=%s AND target_id=%s',
                                        (self.ledger.ns,target_id)).fetchone()
                    if not target or target[0]!=code or (json.loads(target[1]) if isinstance(target[1],str) else target[1])!=basis:
                        return False,None
                return bool(recorded),None
            if any(change.get('etf_code') not in targets for change in changes):
                raise ValueError('SOURCE_UPGRADE_OUTSIDE_FROZEN_SCOPE')
            def identity(change):
                return [change[key] for key in ('etf_code','target_id','label','url')]
            attempted={fingerprint(identity(change)) for record in records for change in record.get('changes',[])}
            changes=[change for change in changes if fingerprint(identity(change)) not in attempted]
            if not changes:return True,None
            if cancelled or status not in ('PARTIAL','SUCCEEDED'):
                return True,None
            if used>=budget or elapsed>=MAX_SECONDS:
                return True,'原材料任务预算已用尽；保留现有回执，待下一原维护窗口处理。'
            if conn.execute("SELECT count(*) FROM left_right_backfill_jobs WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]>=CAPACITY:
                return True,'维护队列已满；本次未重排，稍后查询并沿原材料任务续补。'
            changes=sorted(changes,key=lambda change:tuple(identity(change)))
            upgrade=dict(fingerprint=fingerprint([identity(change) for change in changes]),changes=changes,
                         requested_at=now.isoformat(),previous_status=status)
            details.update(material_source_upgrade=upgrade,material_source_upgrade_records=[*records,upgrade])
            conn.execute('''UPDATE left_right_backfill_jobs SET status='QUEUED',result=%s,error_code=NULL,
                finished_at=NULL WHERE ns=%s AND job_id=%s''',(encode(details),self.ledger.ns,job_id))
        return True,None

    def _prepare_verify(self,now,request_ids):
        """Freeze imported application IDs; repeats reuse the original job budget."""
        with self.ledger._connect() as lock:
            lock.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))',(self.ledger.ns+':verification-trigger',))
            return self._enqueue_verify(now,request_ids)

    def _enqueue_verify(self,now,request_ids):
        from .product_verification import ACTIVE, MAX_REQUESTS as MAX_PRODUCTS, queue_snapshot
        if request_ids is not None and (not isinstance(request_ids,list) or not 1<=len(request_ids)<=MAX_PRODUCTS
                or any(not isinstance(key,str) or not 1<=len(key)<=80 for key in request_ids)
                or len(set(request_ids))!=len(request_ids)):
            raise ValueError('invalid verification request IDs')
        with self.ledger._connect() as conn:
            if request_ids is None:
                request_ids=[row[0] for row in conn.execute('''SELECT request_id FROM product_verification_requests
                    WHERE ns=%s AND state=ANY(%s::text[]) ORDER BY created_at,request_id LIMIT %s''',
                    (self.ledger.ns,list(ACTIVE),MAX_PRODUCTS))]
            elif conn.execute('SELECT count(*) FROM product_verification_requests WHERE ns=%s AND request_id=ANY(%s::text[])',
                              (self.ledger.ns,request_ids)).fetchone()[0]!=len(request_ids):
                raise ValueError('verification request not found')
            if request_ids:
                prior=conn.execute('''SELECT job_id,body->'verification_request_ids' FROM left_right_backfill_jobs WHERE ns=%s
                    AND body->>'task'='etf_maintenance' AND body->>'mode'='verify'
                    AND body->'verification_request_ids' ?| %s::text[] ORDER BY created_at LIMIT 8''',
                    (self.ledger.ns,request_ids)).fetchall()
            else:prior=[]
        if prior:
            assigned=set()
            for job_id,raw in prior:
                ids=json.loads(raw) if isinstance(raw,str) else raw
                if set(request_ids)<=set(ids):return self.status(job_id)
                assigned.update(ids)
            return dict(state='SKIPPED',mode='verify',reason='VERIFICATION_SCOPE_ALREADY_ASSIGNED',
                        existing_run_id=prior[0][0],existing_run_ids=[row[0] for row in prior],
                        unbound_request_ids=sorted(set(request_ids)-assigned),
                        verification=queue_snapshot(self.ledger,request_ids=request_ids))
        if not request_ids:
            return dict(state='SKIPPED',mode='verify',reason='NO_IMPORTED_VERIFICATION_REQUESTS',
                        verification=queue_snapshot(self.ledger))
        mapping=live_mapping(self.ledger,Path(os.environ.get('APP_MAPPING_FILE','/app/data/etf-mapping.json')),now)
        ids=sorted(request_ids)
        identity=fingerprint([ids,mapping['version']])
        body=dict(request_id='etf-verify-'+identity[:40],task='etf_maintenance',mode='verify',
            verification_request_ids=ids,expected_mapping_version=mapping['version'],
            business_date=now.date().isoformat(),scheduled_date=now.date().isoformat(),version='etf-static-verification-v1')
        try:job=self.queue.enqueue(body,MAX_REQUESTS)
        except QueueFull:
            return dict(state='SKIPPED',mode='verify',reason='MAINTENANCE_QUEUE_FULL',hermes='CONTINUES')
        return self.status(job['job_id'])

    def context(self,target_id):
        from .product_verification import review_context
        return review_context(self.ledger,target_id)

    def submit_review(self,target_id,review):
        from .product_verification import submit_review
        if self.paused():
            return dict(state='PAUSED',target_id=target_id,reason='RELEASE_PAUSED')
        return submit_review(self.ledger,target_id,review)

    def status(self,run_id):
        job=self.queue.get(run_id)
        if job.get('task')!='etf_maintenance':
            raise ValueError('not an ETF maintenance job')
        details=job.get('result') or {}
        result=dict(run_id=run_id,state=job['status'],mode=job['mode'],business_date=job['business_date'],
                    requests_used=job['requests_used'],request_budget=job['request_budget'],
                    result={k:details[k] for k in ('mapping','progress','hermes','items','verification',*SOURCE_UPGRADE_KEYS) if k in details},error=job.get('error_code'))
        from .product_verification import queue_snapshot
        result['verification']=queue_snapshot(self.ledger,request_ids=job.get('verification_request_ids'))
        if job.get('verification_request_ids'):
            result['verification_request_ids']=job['verification_request_ids']
            result['expected_mapping_version']=job['expected_mapping_version']
        result['result']['verification']=result['verification']
        retry_note=(VERIFY_RETRY_NOTE if job['mode']=='verify' and job['status'] in TERMINAL
                    and result['verification'].get('counts',{}).get('WAITING_DOCUMENTS') else None)
        if retry_note:result['next_action']=retry_note
        if isinstance(job.get('result'),dict):
            result['feedback']=feedback(job,details|{'verification':result['verification'],
                'next_action':retry_note or details.get('next_action')},state=job['status'])
        with self.ledger._connect() as conn:
            event_key=_notification_key(run_id,details)
            row=conn.execute("SELECT state,attempts,error_code,delivery_receipt FROM outbox WHERE ns=%s AND event_key=%s AND target='octop'",
                             (self.ledger.ns,event_key)).fetchone()
            previous=(conn.execute("SELECT state,attempts,error_code,delivery_receipt FROM outbox WHERE ns=%s AND event_key=%s AND target='octop'",
                (self.ledger.ns,'maintenance:'+run_id)).fetchone() if details.get('material_source_upgrade') else None)
        result['qq']=dict(state=row[0],attempts=row[1],error=row[2],receipt=json.loads(row[3]) if isinstance(row[3],str) else row[3]) if row else dict(state='NOT_QUEUED')
        result['qq']['event_key']=event_key
        if previous:
            result['qq']['previous_notification']=dict(state=previous[0],is_current_revision=False,
                event_key='maintenance:'+run_id,receipt=json.loads(previous[3]) if isinstance(previous[3],str) else previous[3])
        return result


def feedback(body,result,*,state=None):
    mapping=result.get('mapping') or {}
    entries=result.get('items') or []
    failed=[item for item in entries if item.get('status') not in GOOD_STATES]
    outcome={'FAILED':'维护失败','PARTIAL':'部分完成','SUCCEEDED':'维护完成',
             'CANCELLED':'已取消','PAUSED':'已暂停','SKIPPED':'已跳过','QUEUED':'待执行','RUNNING':'执行中'}.get(state)
    lines=['【ETF资料维护｜'+MODES.get(body['mode'],body['mode'])+'】',
           ('执行日期 '+body['scheduled_date']+' · ' if body.get('scheduled_date') else '')+'资料日期 '+body['business_date']+' · '+(outcome or ('部分完成' if failed else '维护完成')),
           ('映射 '+str(mapping['sectors'])+'板块／'+str(mapping['products'])+'只ETF／'+str(mapping['weights'])+'个指数'
            if mapping else '映射待核（本轮未完成读取）'),
           '版本 '+str(mapping.get('version','本轮未读取'))]
    lines.extend(verification_lines(result.get('verification')))
    if result.get('material_source_upgrade'):
        lines.append('官方静态资料源续补：沿用原任务、原请求预算及累计耗时；本次通知按源版本单独记录。')
    if result.get('next_action'):lines.append(result['next_action'])
    for item in entries[:12]:
        lines.append(str(item.get('label',item.get('dataset','资料')))+'：'+str(item.get('message') or item.get('status','UNKNOWN'))[:100])
    calendar=result.get('calendar') or {}
    if calendar.get('range_end'):
        lines.append('交易日历覆盖至 '+calendar['range_end'])
    detail=result.get('mapping_details') or {}
    if (detail.get('cache_members') or {}).get('error'):
        lines.append('本地成员缓存：缓存校验失败，保留当前映射')
    weights=detail.get('weights') or {}
    dated=[key+' '+str(value.get('as_of'))+('（新期资料待核验）' if value.get('pending_as_of') else '')
           for key,value in weights.items() if value.get('as_of')]
    if dated:lines.append('权重来源 '+ '；'.join(dated[:4]))
    issues=[]
    for group in ('members','weights','aum','rules','weight_evidence'):
        for key,value in (detail.get(group) or {}).items():
            if value.get('status') in GOOD_STATES:
                continue
            if group=='members':
                reason=('历史名单'+str(value.get('as_of_date',''))+'可用；当期资料待补'
                        if value.get('status')=='RETAINED' and value.get('usable') is True else '名单待补')
            else:
                reason=str(value.get('error_kind') or value.get('error') or value.get('error_code') or value.get('reason') or value.get('status'))
            issues.append(key+' '+reason)
    if issues:lines.append('待补项 '+ '；'.join(issues[:4]))
    deferred=(detail.get('members_scope') or {}).get('deferred_count',0)
    if deferred:lines.append(f'本轮名额外{deferred}个板块待后续维护')
    overflow=(detail.get('members_scope') or {}).get('overflow_count',0)
    if overflow:lines.append(f'待补队列已满，另{overflow}个板块未纳入队列')
    lines.extend(['使用已核验资料，缺名单明确标待补；Hermes照常研判。','维护 '+body['request_id']])
    return '\n'.join(lines)[:1400]


def _latest_market_result(ledger, *, day=None):
    with ledger._connect() as conn:
        row=conn.execute("SELECT result FROM left_right_backfill_jobs WHERE ns=%s "
                         "AND body->>'task'='etf_maintenance' AND body->>'mode'='market' "
                         "AND status IN ('SUCCEEDED','PARTIAL','FAILED') "
                         "AND (%s::text IS NULL OR body->>'scheduled_date'=%s) "
                         "ORDER BY finished_at DESC LIMIT 1",(ledger.ns,day,day)).fetchone()
    return (json.loads(row[0]) if isinstance(row[0],str) else row[0]) if row and row[0] else {}


def _reference(executor,claim,request,checkpoint,now,result):
    from .data_updates_collect import UpdateTushare,collect_reference
    previous=_latest_market_result(executor.queue.ledger,
        day=now.date().isoformat() if claim.body['mode']=='retry' else None)
    failed={item.get('dataset') for item in previous.get('items',[])
            if item.get('status') not in GOOD_STATES}
    detail=previous.get('mapping_details') or {}
    identity_missing=(detail.get('identity_status')=='UNAVAILABLE'
        or any(row.get('error')=='IDENTITIES_UNAVAILABLE' for row in (detail.get('members') or {}).values()))
    datasets=[]
    with executor.queue.ledger._connect() as conn:
        for dataset in ('stock_basic','trade_cal'):
            last=conn.execute("SELECT max(collected_at) FROM supplier_batches WHERE api=%s",(dataset,)).fetchone()[0]
            due=last is None or (now-last.astimezone(CHINA)).total_seconds()>=7*86400
            if claim.body['mode']=='retry':
                due=last is None or dataset in failed or ('reference' in failed and due)
            elif dataset=='stock_basic' and identity_missing:
                due=True
            if due:datasets.append(dataset)
    if not datasets:
        result['items'].append(dict(dataset='reference',status='CURRENT',message='基础资料未到期，复用已存资料'))
        return
    try:
        client=UpdateTushare(executor.config)
    except (ValueError,TypeError,KeyError,OSError):
        result['items'].append(dict(dataset='reference',status='CONFIG_UNAVAILABLE',
                                    message='基础资料配置不可用，更新待补'))
        return
    body=dict(submitted_at=now.isoformat())
    day=date.fromisoformat(claim.body['business_date'])
    for dataset in datasets:
        checkpoint()
        try:
            receipt=collect_reference(executor.queue.ledger,client,dataset,day,body,request,checkpoint)
            result['items'].append(dict(dataset=dataset,status=receipt['status'],message=str(receipt.get('row_count',0))+'条已保存'))
        except (ValueError,TypeError,KeyError,OSError,psycopg.Error) as exc:
            result['items'].append(dict(dataset=dataset,status='FAILED',message=type(exc).__name__+'，更新待补'))


def _history_targets(ledger,mapping,calendar,now,mode):
    from .stage2.etf_statistics import select_targets
    if mode=='market':
        return select_targets(ledger,mapping,calendar,now,limit=12)
    from .stage2.penetration import research_products
    previous=_latest_market_result(ledger,day=now.date().isoformat())
    eligible=research_products(mapping,now)
    failed=[item.get('etf_code') or str(item.get('label',''))[:9]
            for item in previous.get('items',[])
            if item.get('dataset')=='etf_history' and item.get('status') not in GOOD_STATES]
    return [code for code in dict.fromkeys(failed) if code in eligible][:12]


def execute_job(executor,claim):
    from .stage2.mapping_maintenance import maintain
    from .stage2.etf_statistics import refresh
    queue=executor.queue
    mode=claim.body['mode']
    static=mode in MANUAL_MODES
    started=monotonic()
    result=dict(items=[],progress={},hermes='CONTINUES')
    prior=queue.get(claim.job_id).get('result') or {}
    result.update({key:prior[key] for key in SOURCE_UPGRADE_KEYS if key in prior})
    def checkpoint():
        if not queue.pulse(claim):
            raise Interrupted('LEASE_LOST_OR_CANCELLED')
        if executor.stop.is_set() or executor.paused():
            raise Interrupted('MAINTENANCE_PAUSED')
        if not executor.external_enabled():
            raise Interrupted('EXTERNAL_DISABLED')
        with queue.ledger._connect() as conn:
            elapsed=conn.execute('SELECT elapsed_seconds FROM left_right_backfill_jobs WHERE ns=%s AND job_id=%s',
                                (queue.ns,claim.job_id)).fetchone()[0]
        if monotonic()-started>=MAX_SECONDS or elapsed>=MAX_SECONDS:
            raise Interrupted('MAINTENANCE_TIME_BUDGET')
        if not static and not allowed_window(claim.body['mode'],datetime.now(CHINA)):
            raise Interrupted('MAINTENANCE_WINDOW_ENDED')
    def request(source,scope,operation):
        permitted={'verify':{'maintenance:rule_verification'},
                   'materials':{'maintenance:official_discovery','maintenance:official_material'}}
        if static and source not in permitted[mode]:
            raise ValueError('STATIC_VERIFICATION_SOURCE_FORBIDDEN')
        checkpoint()
        receipt=queue.request(claim,source,scope,operation)
        checkpoint()
        return receipt
    def progress(phase):
        result['progress']['phase']=phase
        queue.progress(claim,result)
    try:
        checkpoint()
        now=datetime.now(CHINA)
        if not static:
            progress('REFERENCES')
            _reference(executor,claim,request,checkpoint,now,result)
        now=datetime.now(CHINA)
        calendar=[]
        if not static:
            calendar=runtime_calendar(queue.ledger,queue.calendar_source,now)
            result['calendar']=calendar_status(queue.ledger,queue.calendar_source,now)
        path=Path(os.environ.get('APP_MAPPING_FILE','/app/data/etf-mapping.json'))
        mapping=live_mapping(queue.ledger,path,now)
        market={}
        if mode!='verify':
            with queue.ledger._connect() as conn:
                row=conn.execute("SELECT body FROM snapshot_state WHERE ns=%s AND dataset='market'",(queue.ns,)).fetchone()
            market=json.loads(row[0]) if row and isinstance(row[0],str) else row[0] if row else {}
        # Explicit reviewed evidence has priority inside this same task budget.
        # A large catalog/member backlog must not starve first verification.
        from copy import deepcopy
        from .product_verification import apply_queued, prepare_materials
        if mode=='materials':
            progress('PRODUCT_MATERIALS')
            if claim.body['material_targets']:
                options={}
                if result.get('material_source_upgrade'):
                    options['source_upgrades']=result['material_source_upgrade']['changes']
                result['verification']=prepare_materials(queue.ledger,mapping,market,datetime.now(CHINA),request,checkpoint,
                                                        target_codes=claim.body['material_targets'],**options)
            checkpoint()
            updated,summary=mapping,dict(status='CURRENT',message='官方材料已检查；产品资格由审阅及回核决定')
            verification_summary={}
        else:
            progress('PRODUCT_VERIFICATION')
            options={'request_ids':claim.body['verification_request_ids']} if mode=='verify' else {}
            verified,verification_summary=apply_queued(queue.ledger,mapping,deepcopy(mapping),
                {'mode':mode},datetime.now(CHINA),request,checkpoint,**options)
        if mode=='verify':
            updated,summary=verified,verification_summary
        elif not static:
            progress('PRODUCT_MATERIALS')
            result['verification']=prepare_materials(queue.ledger,verified,market,datetime.now(CHINA),request,checkpoint)
            progress('MAPPING')
            updated,summary=maintain(queue.ledger,verified,market,calendar,executor.config,executor.captures,
                                    now,request,checkpoint,mode=claim.body['mode'])
        if not static and verification_summary.get('product_verification'):
            summary['product_verification']=verification_summary['product_verification']
            if verification_summary.get('interrupted'):
                summary.setdefault('interrupted',verification_summary['interrupted'])
            from .stage2.mapping_maintenance import _finish
            # One final version and the original published parent, even when
            # ordinary maintenance created an intermediate candidate version.
            updated,summary=_finish(mapping,updated,summary)
        result['mapping_details']=summary
        if updated['version']!=mapping['version']:
            # Budget exhaustion may publish completed items; cancellation or a
            # lost lease must not activate a new version. Lock makes this atomic.
            with queue.ledger._connect() as conn, conn.transaction():
                active=conn.execute("SELECT 1 FROM left_right_backfill_jobs WHERE ns=%s AND job_id=%s "
                    "AND claim_token=%s AND status='RUNNING' AND NOT cancel_requested "
                    "AND lease_until>clock_timestamp() FOR UPDATE",(queue.ns,claim.job_id,claim.token)).fetchone()
                if not active:
                    raise Interrupted('LEASE_LOST_OR_CANCELLED')
                if executor.stop.is_set() or executor.paused():
                    raise Interrupted('MAINTENANCE_PAUSED')
                publish_mapping(queue.ledger,updated,summary,connection=conn,expected_parent=mapping['version'])
        mapping=updated
        result['mapping']=dict(version=mapping['version'],sectors=len(mapping['sectors']),
                               products=len(mapping['products']),weights=len(mapping['weights']))
        result['items'].append(dict(dataset='mapping',label='板块与ETF资料',status=summary.get('status','PARTIAL'),
                                    message=summary.get('message') or summary.get('status','已检查')))
        if not static:
            progress('ETF_HISTORY')
            history_config=Path('/app/automation/etf-statistics.json')
            targets=_history_targets(queue.ledger,mapping,calendar,now,claim.body['mode'])
            if not targets:
                result['items'].append(dict(dataset='etf_history',status='CURRENT',message='本轮无历史统计补采目标'))
            for code in targets:
                checkpoint()
                update=refresh(queue.ledger,history_config,mapping,calendar,datetime.now(CHINA),
                               symbols=[code],attempt=claim.body['mode'],request=request)
                result['items'].append(dict(dataset='etf_history',etf_code=code,label=code+'历史统计',status=update['status'],
                                           message=update['status'],details=update))
        result['progress']['phase']='COMPLETE'
        state='PARTIAL' if any(x.get('status') not in GOOD_STATES for x in result['items']) else 'SUCCEEDED'
    except Interrupted as exc:
        result['items'].append(dict(dataset='maintenance',status='PARTIAL',message=str(exc)))
        state='QUEUED' if str(exc)=='MAINTENANCE_PAUSED' else 'PARTIAL'
    except (ValueError,TypeError,KeyError,OSError,RuntimeError,psycopg.Error) as exc:
        result['items'].append(dict(dataset='maintenance',status='FAILED',message=type(exc).__name__))
        state='PARTIAL' if result.get('mapping') else 'FAILED'
    from .product_verification import queue_snapshot
    result['verification']=queue_snapshot(queue.ledger,request_ids=claim.body.get('verification_request_ids'))
    if static and state in TERMINAL and result['verification'].get('counts',{}).get('WAITING_DOCUMENTS'):
        result['next_action']=VERIFY_RETRY_NOTE
    if state=='SUCCEEDED' and any(count for name,count in result['verification'].get('counts',{}).items()
                                  if name not in ('PUBLISHED','SUPERSEDED')):
        state='PARTIAL'
    result['feedback']=feedback(claim.body,result,state=state)
    # Save feedback and terminal job result together. QQ retries do not run maintenance again.
    with queue.ledger._connect() as conn, conn.transaction():
        saved=conn.execute("""UPDATE left_right_backfill_jobs SET
            status=CASE WHEN cancel_requested THEN 'CANCELLED' ELSE %s END,
            result=%s,error_code=NULL,claim_token=NULL,lease_until=NULL,
            elapsed_seconds=elapsed_seconds+greatest(0,extract(epoch FROM clock_timestamp()-last_active_at)),
            last_active_at=NULL,finished_at=CASE WHEN %s='QUEUED' AND NOT cancel_requested THEN NULL ELSE clock_timestamp() END
            WHERE ns=%s AND job_id=%s AND claim_token=%s AND status='RUNNING'
                AND lease_until>clock_timestamp() RETURNING status""",
            (state,encode(result),state,queue.ns,claim.job_id,claim.token)).fetchone()
        if saved and saved[0] not in ('CANCELLED','QUEUED') and os.environ.get('ENABLE_QQ_PUSH','false').lower()=='true':
            conn.execute("INSERT INTO outbox(ns,report_id,event_key,target,payload) VALUES(%s,%s,%s,'octop',%s) ON CONFLICT DO NOTHING",
                         (queue.ns,claim.job_id,_notification_key(claim.job_id,result),
                          encode(dict(format=MESSAGE_FORMAT,template_kind='maintenance',
                                      sections=maintenance_fields(claim.body,result,saved[0]),kind='etf_maintenance'))))
    return saved[0] if saved else 'LEASE_LOST'
