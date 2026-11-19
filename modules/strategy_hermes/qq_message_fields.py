"""Presentation fields for the shared QQ templates; no research or task decisions."""
from datetime import datetime

from .simulator import CHINA, moment


from .maintenance_policy import MODES as MAINTENANCE_SLOTS
GOOD_STATES = {'READY', 'OK', 'EMPTY', 'CURRENT', 'UNCHANGED', 'UPDATED', 'STORED', 'ALREADY_STORED', 'NOT_DUE'}
LABELS = {
    'stock_basic': '证券主数据', 'trade_cal': '交易日历', 'mapping': '板块与ETF资料',
    'etf_history': 'ETF历史统计', 'reference': '基础资料', 'maintenance': '维护任务',
}
ITEM_STATES = {
    'READY': '已就绪', 'OK': '已保存', 'EMPTY': '来源为空', 'CURRENT': '当前版本',
    'UNCHANGED': '内容未变', 'UPDATED': '已更新', 'STORED': '已保存',
    'NOT_DUE': '未到更新周期，复用已存资料', 'ALREADY_STORED': '已有相同资料', 'PARTIAL': '部分完成', 'FAILED': '处理失败',
    'UNAVAILABLE': '资料待补', 'CONFIG_UNAVAILABLE': '配置不可用', 'EMPTY_SCOPE': '无待处理标的',
    'OUTSIDE_WINDOW': '当前不在资料更新时间窗', 'INSUFFICIENT_CALENDAR': '交易日历待补',
    'RETAINED': '沿用旧版', 'PENDING': '资料待补', 'UNVERIFIED': '待核验',
}
ERRORS = {
    'NOT_PUBLISHED': '来源尚未发布', 'CONNECTION_FAILED': '来源连接失败',
    'HTTP_STATUS': '来源HTTP响应异常', 'PROVIDER_STATUS': '来源业务状态异常',
    'FIELD_MISMATCH': '来源字段不匹配', 'DATE_MISMATCH': '来源日期不匹配',
    'INVALID_JSON': '来源响应格式异常', 'PAGE_MISMATCH': '分页数据不一致',
    'INVALID_IDENTITY': '证券身份待核验', 'INVALID_TIMESTAMP': '来源时间待核验',
    'INCOMPLETE_RESPONSE': '来源资料不完整', 'RESPONSE_TOO_LARGE': '来源响应超过限额',
    'DATE_NOT_COMPLETED': '目标交易日尚未结束', 'SOURCE_FAILURE': '资料来源返回异常',
    'REQUEST_BUDGET_EXHAUSTED': '本次采集预算已用完',
    'SCHEDULE_REPLACED': '旧维护时段已停用，改为10:00主维护与14:15缺项复核',
    'OUTSIDE_MAINTENANCE_WINDOW': '当前不在维护时段', 'OUTSIDE_ROUND_WINDOW': '当前不在研判时段',
    'NOT_TRADING_DAY': '非交易日', 'NO_COMPLETED_SESSION': '暂无已完成交易日',
    'RELEASE_PAUSED': '系统处于发布暂停状态', 'MAINTENANCE_PAUSED': '资料维护已暂停',
    'MAINTENANCE_WINDOW_ENDED': '资料维护时段已结束', 'MAINTENANCE_TIME_BUDGET': '维护时间预算已用完',
    'MAINTENANCE_QUEUE_FULL': '维护队列已满', 'EXTERNAL_DISABLED': '外部采集已关闭',
    'NO_IMPORTED_VERIFICATION_REQUESTS': '暂无已导入的待回核申请',
    'VERIFICATION_SCOPE_ALREADY_ASSIGNED': '部分申请已属于原任务，请单独提交未绑定申请',
    'LEASE_LOST_OR_CANCELLED': '任务租约失效或已取消', 'TimeoutError': '模型调用超时',
    'APITimeoutError': '模型接口超时', 'SourceFailure': '资料来源返回异常',
    'NON_TRADING_DAY': '非交易日', 'EXISTING_ROUND': '本轮已有任务记录',
    'NATIVE_ROUND_DEADLINE': '本轮处理时限已到',
    'NATIVE_ANALYSIS_TOO_LARGE': '本轮完整研判证据超过传输上限',
    'STOCK_NATIVE_PREPARE_FAILED': '研判准备阶段异常',
    'STOCK_NATIVE_ANALYZE_FAILED': '研判会话阶段异常',
    'STOCK_NATIVE_PUBLISH_FAILED': '报告发布阶段异常',
    'STOCK_NATIVE_STREAM_FAILED': '会话响应中断',
    'STOCK_NATIVE_EMPTY_RESPONSE': '会话未返回最终答复',
    'STOCK_NATIVE_INTERACTION_REQUIRED': '会话需要人工处理',
    'STOCK_NATIVE_MODEL_CALL_BUDGET_EXHAUSTED': '本轮模型调用预算已用完',
    'STOCK_NATIVE_ROUND_NOT_COMMITTED': '尚未取得本轮报告保存回执',
    'STOCK_NATIVE_MAINTENANCE_NOT_COMMITTED': '尚未取得维护完成回执',
}
TASK_STATES = {
    'PUBLISHED': '🟢 执行成功', 'SUCCEEDED': '🟢 执行成功', 'PARTIAL': '🟡 部分完成',
    'FAILED': '🔴 执行失败', 'SKIPPED': '⏭️ 已跳过', 'PAUSED': '⏸️ 已暂停',
    'CANCELLED': '🚫 已取消', 'QUEUED': '⏳ 排队中', 'PREPARING': '⏳ 准备中',
    'RUNNING': '⏳ 执行中', 'READY': '⏳ 待研判', 'SUBMITTED': '⏳ 已提交',
}
VALIDATION_FIELDS = ('time', 'conclusion', 'data_result', 'maintenance_result', 'research_result',
                     'octop_result', 'fix_result', 'qq_result', 'pending_items', 'schedule', 'report_id')


def _line(value, limit=180):
    """Bound a display phrase without rewriting its meaning or source object."""
    if value is None:
        return ''
    text = ' '.join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _icon(state):
    return '✅' if state in GOOD_STATES else '🔴' if state in ('FAILED', 'ERROR') else '⏳'


def _item_line(item):
    state = item.get('status', 'UNKNOWN')
    label = _line(item.get('label') or LABELS.get(item.get('dataset'), item.get('dataset')) or '资料', 42)
    message = item.get('message') or state
    phrase = ITEM_STATES.get(message, ERRORS.get(message, message)) if isinstance(message, str) else message
    return _icon(state) + ' ' + label + '：' + _line(phrase, 140)


def _group_line(label, entries, *, members=False):
    values = list(entries.values())
    if values and all(item.get('status')=='NOT_DUE' for item in values):
        return '✅ '+label+'：未到期，复用已存资料'
    complete = sum(item.get('status') in GOOD_STATES for item in values)
    updated = sum(item.get('status') == 'UPDATED' for item in values)
    if members:
        retained = sum(item.get('status') == 'RETAINED' and item.get('usable') is True for item in values)
        pending = len(values) - complete - retained
        parts = [f'已更新{updated}项' if updated else f'当前可用{complete}项']
        if retained:
            parts.append(f'沿用历史{retained}项')
        if pending:
            parts.append(f'待补{pending}项')
        return ('⏳ ' if pending or retained else '✅ ') + label + '：' + '，'.join(parts)
    pending = len(values) - complete
    if pending:
        return '⏳ ' + label + f'：已处理{complete}项，待补{pending}项'
    suffix = f'（更新{updated}项）' if updated else ''
    return '✅ ' + label + f'：已处理{complete}项' + suffix


def _short_items(items, maximum):
    if len(items) <= maximum:
        return items
    return [*items[:maximum - 1], f'⋯ 其余{len(items) - maximum + 1}项见维护详情']


VERIFICATION_STATES = {
    'NEEDS_MATERIALS': '待材料', 'AWAITING_REVIEW': '待审阅', 'PENDING': '待回核',
    'WAITING_DOCUMENTS': '待文件回核', 'VERIFIED': '待发布', 'PUBLISHED': '已发布',
    'REVIEW_REQUIRED': '需复核', 'NEEDS_REBASE': '待版本协调', 'SUPERSEDED': '已替换',
}


def verification_lines(snapshot):
    """Render the durable target/application queue, including exact identifiers."""
    if not isinstance(snapshot,dict):
        return []
    counts=snapshot.get('counts') or {}
    rows=snapshot.get('items') or []
    parts=[VERIFICATION_STATES.get(state,state)+str(count) for state,count in counts.items() if count]
    lines=['🗂️ 核验队列：'+' / '.join(parts)] if parts else ['🗂️ 核验队列：暂无已登记目标或申请']
    for row in rows[:4]:
        state=row.get('state','UNKNOWN')
        code=_line(row.get('etf_code') or row.get('etf'),16)
        identifier=_line(row.get('request_id') or row.get('target_id'),80)
        text=('✅ ' if state=='PUBLISHED' else '⏳ ')+code+' '+VERIFICATION_STATES.get(state,state)
        if identifier:text+=' ['+identifier+']'
        reason=row.get('reason') or row.get('error_code')
        if reason:text+=' · '+_line(ERRORS.get(reason,reason),72)
        if state=='PUBLISHED' and row.get('published_version'):
            text+=' · '+_line(row['published_version'],48)
        lines.append(text)
    omitted=max(0,len(rows)-4)+snapshot.get('omitted',0)
    if omitted:lines.append(f'⋯ 其余{omitted}项见核验队列详情')
    return lines


def maintenance_fields(body, result, state):
    """Use the worker's terminal state, including a failure before a mapping exists."""
    mapping = result.get('mapping') or {}
    detail = result.get('mapping_details') or {}
    source_items = result.get('items') or []
    groups = (('catalog', 'ETF目录'), ('members', '板块成员'), ('weights', '指数权重'),
              ('aum', 'ETF规模（日批次）'), ('rules', '产品规则'), ('weight_evidence', '权重依据'))
    lines = verification_lines(result.get('verification'))
    if result.get('next_action'):lines.append('⏳ '+_line(result['next_action'],180))
    for item in source_items:
        if item.get('dataset') == 'mapping':
            if any(detail.get(group) for group, _ in groups):
                lines.extend(_group_line(label, detail[group], members=group == 'members')
                             for group, label in groups if detail.get(group))
            else:
                lines.append(_item_line(item))
            catalog_review=sum(row.get('count',0) for row in (detail.get('catalog_review') or {}).values()
                               if isinstance(row,dict))
            if catalog_review:
                lines.append(f'📝 目录关联：{catalog_review}条产品范围或指数关联待人工整理')
            cached=detail.get('cache_members') or {}
            if cached.get('error'):
                lines.append('⏳ 本地成员缓存：缓存校验失败，保留当前映射')
            if cached.get('checked'):
                lines.append(f'✅ 本地成员缓存：检查{cached["checked"]}项，吸收{cached.get("updated",0)}项')
            retries=detail.get('retry_queue') or {}
            if retries.get('pending_count'):
                phrase=f'⏳ 持久待补：{retries["pending_count"]}项'
                if retries.get('review_required'):
                    phrase+=f'（{retries["review_required"]}项达自动重试上限，待人工处理）'
                lines.append(phrase)
            if retries.get('overflow_count'):
                lines.append(f'⏳ 资料待补队列已满：另{retries["overflow_count"]}项待登记')
            deferred = (detail.get('members_scope') or {}).get('deferred_count', 0)
            if deferred:
                lines.append(f'⏳ 本轮名额外：{deferred}个板块待后续维护')
            overflow = (detail.get('members_scope') or {}).get('overflow_count', 0)
            if overflow:
                lines.append(f'⏳ 待补队列已满：另{overflow}个板块未纳入队列')
            unverified = detail.get('unverified_products')
            if type(unverified) is int and unverified > 0 and not result.get('verification'):
                lines.append(f'⏳ 产品交易规则：{unverified}只待核验')
        else:
            lines.append(_item_line(item))
    if not lines:
        lines = ['⚪ 暂无已保存的资料结果']

    dated = []
    for key, value in (detail.get('weights') or {}).items():
        if value.get('as_of'):
            item = _line(key, 32) + ' (' + _line(value['as_of'], 16) + ')'
            if value.get('pending_as_of'):
                item += ' [新期资料待核验]'
            dated.append(item)
    issues = []
    for group, _ in groups:
        for key, value in (detail.get(group) or {}).items():
            if value.get('status') not in GOOD_STATES:
                reason = value.get('error_kind') or value.get('error') or value.get('error_code') or value.get('reason') or value.get('status', 'UNKNOWN')
                reason = ERRORS.get(reason, ITEM_STATES.get(reason, reason))
                if group == 'members':
                    if value.get('status') == 'RETAINED' and value.get('usable') is True:
                        reason = '历史名单' + _line(value.get('as_of_date'), 16) + '可用；当期资料待补'
                    else:
                        reason = '名单待补：' + str(reason)
                issues.append(_line(key, 36) + ' (' + _line(reason, 72) + ')')
    issues.extend(_item_line(item).split(' ', 1)[-1] for item in source_items
                  if item.get('status') not in GOOD_STATES and item.get('dataset') != 'mapping')
    issues = list(dict.fromkeys(issues))
    counts = (str(mapping.get('sectors', '待核')) + '板块 / ' + str(mapping.get('products', '待核'))
              + '只ETF / ' + str(mapping.get('weights', '待核')) + '指数') if mapping else '映射待核（本轮未完成读取）'
    status = {'SUCCEEDED': '🟢 全部完成', 'PARTIAL': '🟡 部分完成', 'FAILED': '🔴 维护失败',
              'CANCELLED': '🚫 已取消', 'PAUSED': '⏸️ 已暂停', 'SKIPPED': '⏭️ 已跳过'}.get(state)
    return dict(
        slot=MAINTENANCE_SLOTS.get(body.get('mode'), _line(body.get('mode'), 24)),
        business_date=(('执行 '+_line(body['scheduled_date'],16)+' / 资料 ') if body.get('scheduled_date') else '')
                      +_line(body.get('business_date'),16),
        status=status or '⚪ ' + _line(state, 24),
        mapping_counts=counts, mapping_version=_line(mapping.get('version') or '本轮未读取', 128),
        items=_short_items(lines, 12),
        calendar_end=_line((result.get('calendar') or {}).get('range_end'), 16),
        weight_sources=' ｜ '.join(_short_items(dated, 4)),
        pending_items=' ｜ '.join(_short_items(issues, 4)),
        research_impact='维护不阻断Hermes；各标的按已核验资料研判，缺项标待补',
        task_id=_line(body.get('request_id'), 128),
    )


def _qq_state(qq):
    if not isinstance(qq, dict):
        return _line(qq, 160) or '投递状态待核实'
    state = qq.get('state')
    labels = {'SENT': '已发送', 'PENDING': '等待发送', 'SENDING': '发送中',
              'FAILED': '发送失败', 'NOT_QUEUED': '未生成本轮消息'}
    text = labels.get(state, '投递状态待核实')
    receipt = qq.get('receipt')
    code = receipt.get('http_status') if isinstance(receipt, dict) else None
    if type(code) is int and 100 <= code <= 599:
        text += f' (HTTP {code})'
    if state == 'FAILED' and qq.get('error'):
        text += ' · ' + _line(qq['error'], 60)
    return text


def status_fields(task_name, state, *, at, task_id, result=None, reason=None, next_action=None, qq=None):
    """Format an observed status; unknown values stay unknown, without promised retries."""
    clock = at if isinstance(at, datetime) else moment(at)
    if clock.tzinfo is None:
        raise ValueError('QQ task time must include its timezone')
    defaults = {'PUBLISHED': '报告已保存', 'SUCCEEDED': '任务已完成', 'PARTIAL': '可用结果已保存，部分事项待补',
                'FAILED': '任务执行失败', 'SKIPPED': '本次任务已跳过', 'PAUSED': '本次任务已暂停',
                'CANCELLED': '本次任务已取消'}
    return dict(task_name=_line(task_name, 80), status=TASK_STATES.get(state, '⚪ ' + _line(state, 32)),
                time=clock.astimezone(CHINA).strftime('%Y-%m-%d %H:%M:%S') + '（北京时间）',
                result=_line(result, 180) or defaults.get(state, '任务处理中' if state in TASK_STATES else '任务状态待核实'),
                reason=_line(ERRORS.get(reason, reason), 180) if reason else ('正常完成' if state in ('PUBLISHED', 'SUCCEEDED') else '原因待核实'),
                next_action=_line(next_action, 180) or '查看任务记录', qq_state=_qq_state(qq),
                task_id=_line(task_id, 128))


def validation_fields(values):
    """Prepare caller-supplied acceptance facts; missing checks never become passes."""
    if not isinstance(values, dict) or set(values) - set(VALIDATION_FIELDS):
        raise ValueError('unsupported QQ validation fields')
    return {name: _line(values.get(name), 220) or '待核实' for name in VALIDATION_FIELDS}
