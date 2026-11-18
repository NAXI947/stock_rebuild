"""Durable, bounded handoff between native OCTOP cron and the single stock worker.

The API methods only enqueue validated data or read stored results. Collection,
MX and publication run in ``native_tick``; no model/HTTP call runs in submit.
"""
from datetime import datetime
import json
import os
from uuid import UUID, uuid4

from ..ledger import encode, fingerprint
from ..simulator import CHINA, moment
from .inputs import SharedMarket, mapping_file, shared_market, trading_days
from .reference_runtime import runtime_calendar
from .maintenance_store import live_mapping
from .octop_analyst import OctopAnalyst, MAX_RESEARCH_SECTORS
from .octop_decision import PHASES
from .penetration import number
from .postgres import RoundClaim
from .worker import RequestBudget, SLOTS, due_round, planned_at, publish_round

ACTIVE = ('QUEUED', 'PREPARING', 'READY', 'SUBMITTED', 'PUBLISHING')
MAX_DECISIONS_BYTES = 256 * 1024
INSTRUCTIONS = """这是当轮已冻结的后台共享数据。逐一判断 analysis.sectors，向 submit 工具提交 decisions 数组，每个 sector_id 恰好一项。
每项必填 sector_id、phase、evidence_refs。phase 为 NEW、MAINTAIN、STRENGTHEN、WEAKEN、RETREAT、UNKNOWN、NOT_COVERED、EVENT_ONLY。
新 numeric_contract=scoped-facts-v1 时，每项另填 fact_refs：从该板块 fact_catalog 的键选择至多4项，有板块数字事实时至少引用一项本板块事实。catalog明确区分单板块、成分股、全部已采集板块和已采集A股；数字及口径由程序按引用渲染。
evidence_refs 只能引用该板块 current.ref。没有 current 时用 NOT_COVERED 和空引用；有 current 时至少引用一个 current.ref。
NEW 需要 prior_active=false 且当轮证据支持新方向，无需 previous；MAINTAIN/STRENGTHEN/WEAKEN/RETREAT 需要 prior_active=true 和可比 previous。仅依赖前序比较的判断缺 previous，或支撑方向的证据不足/冲突时用 UNKNOWN。
previous 为已发布报告中的板块观测，独立于 prior_active；同日价格变化见 facts.board_return_change_ratio，跨日 previous 仅作历史背景。缺板块成分、个股报价或 IOPV 只影响对应指标，不单独要求整板块 UNKNOWN。先按已有板块证据判断；尚未进入活动方向可在 research.rationale 描述价格走势，phase 仍遵守上述交易方向语义，程序另行展示价格变化。
可选 core_stock_codes（至多4只）和 core_leader_codes（至多8只），只从本板块 stock_evidence.code 选择，保留核心领涨/中军与个股观察名单的区别。
仅 research_sector_ids 中的板块可加 research：role=MAINLINE/ROTATION/WATCH/EVENT_FIRST、rationale（90字内）、counter_evidence_status=FOUND/NOT_FOUND/NOT_CHECKED、counter_evidence_refs、invalidation_condition（60字内）、key_unknown（60字内）。反证引用可取本板块 current/previous/stock_evidence.ref。
新数字协议的 research.rationale 只写定性判断，当前涨跌、家数、比例等数值只通过 fact_refs 引用，不手抄数值；有来源的代码、日期和时间可原样引用。invalidation_condition 可以提出明确的未来假设阈值，不能写成已经发生的事实。即使没有深度research，也必须选择fact_refs，程序生成基础事实解释。数字事实不合约时仅该板块转待复核，原始判断保留。
新 etf_selection_contract=explicit-etfs-v1 时，每项必填 etf_choices（至多3个ETF代码，可空），只从本板块 etf_options.products[].etf 选择。独立判断已有真实关联、暴露、行情和资料缺口是否支持选择，不按程序旧阈值机械选代表；缺必要资料可观察并留空。NEW是板块方向状态，ETF选择是独立结果。身份、交易规则、可卖份额和成交时点由程序逐项核验。
不补写价格、权重或缺失事实，不调用其它行情来源或交易工具。完成提交后检查状态，只有 PUBLISHED 才反馈已保存，附报告编号和 QQ 状态；FAILED 原样反馈失败阶段与错误。"""


def _decode(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


def _run_id(value):
    if not isinstance(value, str):
        raise ValueError('invalid native run id')
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError('invalid native run id') from exc


def _job_table(conn, ns, run_id):
    row = conn.execute('SELECT retry_of FROM native_round_attempts WHERE ns=%s AND run_id=%s',
                       (ns,run_id)).fetchone()
    return 'native_round_recovery_jobs' if row and row[0] else 'native_round_jobs'


def _validate_decisions(decisions, request):
    if not isinstance(decisions, list) or len(decisions) > 300:
        raise ValueError('decisions must be a bounded array')
    if len(encode(decisions).encode('utf-8')) > MAX_DECISIONS_BYTES:
        raise ValueError('decisions exceed size limit')
    ids = []
    for item in decisions:
        if not isinstance(item, dict) or not {'sector_id','phase','evidence_refs'} <= item.keys():
            raise ValueError('decision is missing required fields')
        if set(item) - {'sector_id','phase','evidence_refs','core_stock_codes','core_leader_codes','research','fact_refs','etf_choices'}:
            raise ValueError('unsupported decision field')
        if not isinstance(item['sector_id'], str) or not isinstance(item['phase'], str) or item['phase'] not in PHASES:
            raise ValueError('invalid sector or phase')
        refs = item['evidence_refs']
        if not isinstance(refs, list) or len(refs) > 64 or any(not isinstance(ref,str) for ref in refs):
            raise ValueError('invalid evidence references')
        ids.append(item['sector_id'])
    if len(ids) != len(set(ids)) or set(ids) != set(request['evidence']):
        raise ValueError('decisions must cover every input sector exactly once')


class NativeRounds:
    def __init__(self, ledger, calendar, paused=lambda: False):
        self.ledger, self.calendar, self.paused = ledger, calendar, paused

    def clock(self, now=None):
        if now is not None and self.ledger.origin != 'FROZEN':
            raise ValueError('explicit clock requires frozen inputs')
        return moment(now) if isinstance(now, str) else (now or datetime.now(CHINA)).astimezone(CHINA)

    def prepare(self, slot, now=None):
        if slot not in SLOTS:
            raise ValueError('unknown Hermes round slot')
        if self.paused():
            return dict(state='PAUSED', error='RELEASE_PAUSED')
        clock = self.clock(now)
        day = clock.date().isoformat()
        days = runtime_calendar(self.ledger,self.calendar,clock)
        if day not in days:
            return dict(state='SKIPPED', trade_date=day, slot=slot, reason='NON_TRADING_DAY')
        from .native_recovery import consume
        recovery = consume(self,day,slot,clock)
        if recovery:
            return self.status(recovery)
        with self.ledger._connect() as conn:
            found = conn.execute('SELECT run_id FROM native_round_jobs WHERE ns=%s AND trade_date=%s AND slot=%s',
                                 (self.ledger.ns,day,slot)).fetchone()
            previous = conn.execute('SELECT state,report_id,error_code FROM round_tasks WHERE ns=%s AND trade_date=%s AND slot=%s',
                                    (self.ledger.ns,day,slot)).fetchone()
        if found:
            return self.status(str(found[0]))
        if previous:
            return dict(state=previous[0], trade_date=day, slot=slot, report_id=previous[1], error=previous[2],
                        reason='EXISTING_ROUND', source='existing-worker')
        due = due_round(clock,days)
        if due is None or due[1] != slot:
            return dict(state='SKIPPED', trade_date=day, slot=slot, reason='OUTSIDE_ROUND_WINDOW')
        run_id = str(uuid4())
        with self.ledger._connect() as conn:
            row = conn.execute('''INSERT INTO native_round_jobs(ns,run_id,trade_date,slot,state)
                VALUES(%s,%s,%s,%s,'QUEUED') ON CONFLICT(ns,trade_date,slot) DO NOTHING RETURNING run_id''',
                (self.ledger.ns,run_id,day,slot)).fetchone()
            if row is None:
                run_id = str(conn.execute('SELECT run_id FROM native_round_jobs WHERE ns=%s AND trade_date=%s AND slot=%s',
                                         (self.ledger.ns,day,slot)).fetchone()[0])
        return self.status(run_id)

    def status(self, run_id):
        run_id = _run_id(run_id)
        with self.ledger._connect() as conn:
            row = conn.execute('''SELECT j.trade_date,j.slot,j.state,j.expires_at,j.error_code,
                CASE WHEN j.state='READY' THEN j.context->'analysis' ELSE NULL END,
                t.state,t.report_id,t.error_code,j.expires_at<=clock_timestamp(),j.failure_stage
                FROM native_round_attempts j LEFT JOIN round_tasks t
                ON (t.ns,t.trade_date,t.slot)=(j.ns,j.trade_date,j.slot)
                WHERE j.ns=%s AND j.run_id=%s''', (self.ledger.ns,run_id)).fetchone()
            if row is None:
                raise ValueError('native run not found')
            day,slot,state,expires,error,context,round_state,report_id,round_error,expired,failure_stage = row
            if round_state == 'PUBLISHED' and state != 'FAILED':
                state,error = 'PUBLISHED',None
            elif state in ACTIVE and (expired or round_state == 'FAILED'):
                failure_stage = 'PREPARE' if state in ('QUEUED','PREPARING') else 'ANALYZE' if state == 'READY' else 'PUBLISH'
                state,error = 'FAILED',round_error or 'NATIVE_ROUND_DEADLINE'
            result = dict(run_id=run_id,state=state,trade_date=day.isoformat(),slot=slot,
                          expires_at=expires.astimezone(CHINA).isoformat(),error=error)
            if state == 'FAILED':
                result['stage'] = failure_stage
            if state == 'READY':
                result.update(analysis=_decode(context),instructions=INSTRUCTIONS)
            if state == 'PUBLISHED':
                raw = conn.execute("SELECT payload FROM events WHERE ns=%s AND kind='report' AND id=%s",
                                   (self.ledger.ns,report_id)).fetchone()
                report = _decode(raw[0]) if raw else {}
                body = _decode(report.get('raw','{}'))
                delivery = conn.execute("SELECT state,attempts,error_code,delivery_receipt,payload FROM outbox WHERE ns=%s AND report_id=%s AND target='octop'",
                                        (self.ledger.ns,report_id)).fetchone()
                result.update(report_id=report_id,published_at=report.get('at'),summary=body.get('summary'),
                              report_status=report.get('status'),
                              qq=dict(state=delivery[0],attempts=delivery[1],error=delivery[2],
                                      receipt=_decode(delivery[3]) if delivery[3] else None) if delivery else dict(state='NOT_QUEUED'))
                recovery = report.get('input_meta',{}).get('manual_recovery')
                if recovery:
                    result['manual_recovery'] = recovery
                payload = _decode(delivery[4]) if delivery else {}
                text = payload.get('text')
                if isinstance(text,str) and text:
                    result['feedback'] = text[:1500]
                elif payload.get('format') == 'qq-template-v2':
                    from ..qq_templates import render_message
                    try:
                        result['feedback']=render_message('research',payload.get('sections',{}))['text']
                    except (OSError,ValueError,TypeError):
                        result['feedback']='报告已保存；QQ正文排版待处理。报告 '+str(report_id)
                else:
                    sections = payload.get('sections')
                    if not isinstance(sections,dict) and report:
                        from .notifications import brief_sections
                        orders = conn.execute('''SELECT c.etf,o.action,o.remaining_quantity,o.status,
                            o.evidence->>'eligible_at',(o.evidence->>'report_id'=%s)
                            FROM simulation_orders o JOIN cycles c ON (c.ns,c.id)=(o.ns,o.cycle_id)
                            WHERE o.ns=%s AND (o.status='PENDING' OR o.evidence->>'report_id'=%s)
                            ORDER BY o.requested_at DESC,c.etf,o.action''',
                            (report_id,self.ledger.ns,report_id)).fetchall()
                        sections = brief_sections(slot,report,orders)
                    sections = sections or {}
                    lines = []
                    for key,label in (('title',''),('clock',''),('market','市场 '),('leaders',''),
                                      ('boards','板块 '),('etf_candidates','ETF候选 '),('etf_watch','ETF穿透观察 '),
                                      ('etf_blocked','ETF穿透待核验 '),('stocks','个股观察 '),('simulation','模拟节点 ')):
                        value = sections.get(key)
                        if isinstance(value,str) and value.strip():
                            lines.append(label+value)
                    lines.extend(line for line in sections.get('details',[]) if isinstance(line,str))
                    result['feedback'] = '\n'.join(lines)[:1500]
            return result

    def submit(self, run_id, decisions):
        run_id = _run_id(run_id)
        if self.paused():
            return dict(run_id=run_id,state='PAUSED',error='RELEASE_PAUSED')
        with self.ledger._connect() as conn, conn.transaction():
            table = _job_table(conn,self.ledger.ns,run_id)
            row = conn.execute(f'''SELECT state,context,decision_hash,expires_at>clock_timestamp()
                FROM {table} WHERE ns=%s AND run_id=%s FOR UPDATE''', (self.ledger.ns,run_id)).fetchone()
            if row is None:
                raise ValueError('native run not found')
            state,context,previous,valid = row
            if context is None or state in ('QUEUED','PREPARING'):
                raise ValueError('native inputs are not ready')
            _validate_decisions(decisions,_decode(context)['request'])
            digest = fingerprint(decisions)
            if previous and previous != digest:
                raise ValueError('native decisions already submitted with different content')
            if state == 'READY' and valid:
                conn.execute(f'''UPDATE {table} SET decisions=%s,decision_hash=%s,
                    state='SUBMITTED',updated_at=clock_timestamp() WHERE ns=%s AND run_id=%s''',
                    (encode(decisions),digest,self.ledger.ns,run_id))
        return self.status(run_id)

    def expire(self):
        with self.ledger._connect() as conn, conn.transaction():
            for table in ('native_round_jobs','native_round_recovery_jobs'):
                # Reconcile after-commit crashes; failed parent remains historical.
                conn.execute(f'''UPDATE {table} j SET state='PUBLISHED',report_id=t.report_id,error_code=NULL,
                    updated_at=clock_timestamp() FROM round_tasks t
                    WHERE (t.ns,t.trade_date,t.slot)=(j.ns,j.trade_date,j.slot) AND j.ns=%s
                    AND j.state NOT IN ('PUBLISHED','FAILED') AND t.state='PUBLISHED' ''',(self.ledger.ns,))
                rows = conn.execute(f'''UPDATE {table} SET failure_stage=CASE
                    WHEN state IN ('QUEUED','PREPARING') THEN 'PREPARE' WHEN state='READY' THEN 'ANALYZE' ELSE 'PUBLISH' END,
                    state='FAILED',error_code='NATIVE_ROUND_DEADLINE',
                    updated_at=clock_timestamp() WHERE ns=%s AND state NOT IN ('PUBLISHED','FAILED')
                    AND expires_at<=clock_timestamp() RETURNING trade_date,slot,lease_token''',(self.ledger.ns,)).fetchall()
                for day,slot,token in rows:
                    conn.execute('''UPDATE round_tasks SET state='FAILED',error_code='NATIVE_ROUND_DEADLINE',
                        lease_token=NULL,lease_until=NULL WHERE ns=%s AND trade_date=%s AND slot=%s
                        AND state='RUNNING' AND lease_token=%s''',(self.ledger.ns,day,slot,token))

    def _start(self, run_id):
        with self.ledger._connect() as conn, conn.transaction():
            table = _job_table(conn,self.ledger.ns,run_id)
            parent = 'retry_of' if table == 'native_round_recovery_jobs' else 'NULL::uuid'
            row = conn.execute(f'''SELECT trade_date,slot,state,lease_token,expires_at,context,decisions,{parent}
                FROM {table} WHERE ns=%s AND run_id=%s AND expires_at>clock_timestamp() FOR UPDATE''',
                (self.ledger.ns,run_id)).fetchone()
            if row is None:
                return None
            day,slot,state,token,expires,context,decisions,retry_of = row
            if state not in ('QUEUED','PREPARING','SUBMITTED','PUBLISHING'):
                return None
            task = conn.execute('SELECT state,lease_token FROM round_tasks WHERE ns=%s AND trade_date=%s AND slot=%s',
                                (self.ledger.ns,day,slot)).fetchone()
            if state == 'QUEUED' and task is None:
                token = uuid4()
                conn.execute('''INSERT INTO round_tasks(ns,trade_date,slot,planned_at,state,lease_token,lease_until,attempts)
                    VALUES(%s,%s,%s,%s,'RUNNING',%s,%s,1)''',
                    (self.ledger.ns,day,slot,planned_at(day.isoformat(),slot),token,expires))
                task = ('RUNNING',token)
            if task is None or task[0] != 'RUNNING' or task[1] != token:
                stage = 'PREPARE' if state in ('QUEUED','PREPARING') else 'PUBLISH'
                conn.execute(f"UPDATE {table} SET state='FAILED',error_code='ROUND_OWNERSHIP_LOST',failure_stage=%s WHERE ns=%s AND run_id=%s",
                             (stage,self.ledger.ns,run_id))
                return None
            next_state = 'PREPARING' if state in ('QUEUED','PREPARING') else 'PUBLISHING'
            conn.execute(f'''UPDATE {table} SET state=%s,lease_token=%s,updated_at=clock_timestamp()
                WHERE ns=%s AND run_id=%s''',(next_state,token,self.ledger.ns,run_id))
            return dict(run_id=run_id,state=next_state,claim=RoundClaim(day.isoformat(),slot,str(token)),
                        context=_decode(context),decisions=_decode(decisions),table=table,
                        retry_of=str(retry_of) if retry_of else None)


class NativeAnalyst(OctopAnalyst):
    """Use one validated native tool submission, without inventing provider receipts."""
    def __init__(self, ledger, mapping, market, cutoff, decisions, generated_at, run_id, recovery=None):
        super().__init__(ledger,mapping,market,None,cutoff)
        self.decisions,self.generated_at,self.run_id = decisions,generated_at,run_id
        self.recovery = recovery

    def _analyze(self, request, budget):
        _validate_decisions(self.decisions,request)
        receipt = dict(status='READY',generated_at=self.generated_at.isoformat(),
                       source='octop-native-cron',native_run_id=self.run_id,
                       output=encode(dict(decisions=self.decisions)),
                       completion_evidence='VALIDATED_TOOL_SUBMISSION',usage_source='octop:usage_log')
        expert = os.environ.get('OCTOP_NATIVE_EXPERT_ID')
        if expert:
            receipt['model_expert'] = expert
        if self.recovery:
            receipt.update(source='octop-native-recovery',native_run_id=self.recovery['retry_of'],
                           recovery_run_id=self.run_id,completion_evidence='REUSED_VALIDATED_TOOL_SUBMISSION',
                           source_cutoff=self.recovery['source_cutoff'],decision_hash=self.recovery['decision_hash'])
        return receipt,{item['sector_id']:item for item in self.decisions},set()


def _request(ledger, claim, market):
    try:
        view = ledger.inspect()
    except ValueError as exc:
        if str(exc) != 'unknown replay namespace':
            raise
        view = {}
    tracked, held_etfs = set(), set()
    for row in view.get('cycles',[]):
        if row['status'] in ('WAIT_BUY','OPEN','WAIT_EXIT'):
            held_etfs.add(row['etf'])
            tracked.add(row['primary_sector'])
            tracked.update(item['sector_id'] for item in row.get('entry_sources',()))
    return dict(trade_date=claim.day,round_slot=claim.slot,planned_at=planned_at(claim.day,claim.slot).isoformat(),
                tracked_sectors=sorted(tracked),active_directions=view.get('rotation',{}),
                held_etfs=sorted(held_etfs),
                market_snapshot_id=market.snapshot_id if market else None)


def _prepare_context(queue, cache, args, client, job, now):
    ledger,claim = queue.ledger,job['claim']
    days = runtime_calendar(ledger,queue.calendar,queue.clock(now))
    mapping = live_mapping(ledger,args.mapping,queue.clock(now))
    market = shared_market(ledger,cache,key=args.snapshot_key,path=args.snapshot_file)
    if args.tickflow_config and args.kaipanla_captures:
        from .intraday import refresh
        options = {}
        if getattr(args,'stock_research',False):
            options['stock_mapping'] = mapping
        if getattr(args,'findesk_config',None):
            options['findesk'] = (args.findesk_config,mapping)
        result = refresh(ledger,cache,args.snapshot_key,(claim.day,claim.slot,planned_at(claim.day,claim.slot)),
                         market,args.tickflow_config,args.kaipanla_captures,**options)
        if result is None:
            return None
        market = shared_market(ledger,cache,key=args.snapshot_key)
    from .etf_statistics import overlay_mapping
    cutoff = queue.clock(now)
    mapping = overlay_mapping(ledger,mapping,days,cutoff)
    analyst = OctopAnalyst(ledger,mapping,market,client,cutoff)
    request = analyst.prepare(_request(ledger,claim,market),RequestBudget(ledger,claim,seconds=25))
    sectors = list(request['evidence'].values())
    ranked = sorted((row for row in sectors if row['current']),
                    key=lambda row:(not row['held'],not row['prior_active'],
                                    -abs(number(row['facts'].get('board_return_ratio')) or 0),row['sector_id']))
    analysis = {key:request[key] for key in ('trade_date','round_slot','planned_at')}
    analysis.update(input_kind=ledger.origin,sectors=sectors,research_explanations=True,
                    numeric_contract=request.get('numeric_contract'),
                    etf_selection_contract=request.get('etf_selection_contract'),
                    research_sector_ids=[row['sector_id'] for row in ranked[:MAX_RESEARCH_SECTORS]])
    return dict(calendar=days,mapping=mapping,market=market.body if market else None,
                cutoff=cutoff.isoformat(),request=request,analysis=analysis)


def native_tick(queue, cache, args, client, now=None):
    """Run at most one durable preparation/publication under a worker-only lock."""
    ledger = queue.ledger
    if queue.paused():
        return dict(state='PAUSED')
    with ledger._connect() as lock:
        acquired, = lock.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',
                                 (ledger.ns+':native-round-worker',)).fetchone()
        if not acquired:
            return dict(state='BUSY')
        queue.expire()
        row = lock.execute('''SELECT run_id FROM native_round_attempts WHERE ns=%s
            AND state IN ('QUEUED','PREPARING','SUBMITTED','PUBLISHING')
            ORDER BY created_at LIMIT 1''',(ledger.ns,)).fetchone()
        if row is None:
            return dict(state='IDLE')
        run_id = str(row[0])
        job = queue._start(run_id)
        if job is None:
            return queue.status(run_id)
        try:
            if job['state'] == 'PREPARING':
                context = _prepare_context(queue,cache,args,client,job,now)
                if context is None:
                    return dict(run_id=run_id,state='PREPARING')
                with ledger._connect() as conn:
                    conn.execute('''UPDATE native_round_jobs SET state='READY',context=%s,updated_at=clock_timestamp()
                        WHERE ns=%s AND run_id=%s AND state='PREPARING' AND expires_at>clock_timestamp()''',
                        (encode(context),ledger.ns,run_id))
            else:
                context = job['context']
                market = SharedMarket.read(context['market']) if context['market'] else None
                generated = queue.clock(now)
                from .native_recovery import metadata
                recovery = metadata(queue,job,generated)
                analyst = NativeAnalyst(ledger,context['mapping'],market,moment(context['cutoff']),
                                        job['decisions'],generated,run_id,recovery)
                report = analyst(context['request'],None)
                report['analyst_version'] = 'octop-native-cron-v1'
                if recovery:
                    report['manual_recovery'] = recovery
                saved = publish_round(ledger,job['claim'],report,context['calendar'],context['mapping'],market,
                                      targets=('redis','octop') if args.qq else ('redis',))
                with ledger._connect() as conn:
                    conn.execute(f'''UPDATE {job['table']} SET state='PUBLISHED',report_id=%s,
                        updated_at=clock_timestamp() WHERE ns=%s AND run_id=%s''',(saved['id'],ledger.ns,run_id))
        except Exception as exc:
            # Preserve the report if publishing committed before an after-commit failure.
            current = queue.status(run_id)
            if current['state'] != 'PUBLISHED':
                from .native_errors import error_code
                error = current.get('error') or error_code(exc)
                ledger.fail_round(job['claim'],error)
                with ledger._connect() as conn:
                    conn.execute(f'''UPDATE {job['table']} SET state='FAILED',error_code=%s,failure_stage=%s,
                        updated_at=clock_timestamp() WHERE ns=%s AND run_id=%s''',
                        (error,'PREPARE' if job['state']=='PREPARING' else 'PUBLISH',ledger.ns,run_id))
        return queue.status(run_id)
