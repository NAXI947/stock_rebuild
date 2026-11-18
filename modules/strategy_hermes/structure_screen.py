"""Frozen, deterministic research filters over existing scores and daily bars.

The caller supplies complete SSE sessions, retaining explicit missing slots.
This module does not recalculate LEFT/RIGHT or infer an exchange calendar.
"""
from datetime import date, datetime
from math import isfinite, isclose
from statistics import mean, median


VERSION = 'structure_watch_v2'
RULES = {
    'STARTUP': {
        'label': '整理后刚转强',
        'description': 'RIGHT近期净改善且最新日上升，价格接近整理平台高点时纳入观察；LEFT参考、历史活跃、缩量与振幅收敛作为进一步确认。',
        'defaults': {'min_left': 70, 'min_right_gain': 3, 'max_ma20_distance': .10},
        'price_rule': '前5日最高价/最低价−1不超过12%；收盘在前5日最高价的98%–105%内且较前日上升，不要求LEFT当日上涨。',
    },
    'PULLBACK': {
        'label': '回踩后再转强',
        'description': '价格回撤后守住均线附近并当日回升，RIGHT近期净改善时纳入观察；此前强势、LEFT恢复、历史活跃及量价收敛作为进一步确认。',
        'defaults': {'min_left': 70, 'min_right_gain': 3, 'max_ma20_distance': .10},
        'price_rule': '入池需前段6日高点至随后4日低点回撤2%–12%，最近5日低点不低于当前辅助MA20的95%，且当日收盘上涨。软确认：评分前3日RIGHT峰值≥70，至T-2回落≥3分；T-2/T-1的LEFT峰值较前3日最低恢复≥2分且达到LEFT参考值。',
    },
}
for _rule in RULES.values():
    _rule['version'] = VERSION
    _rule['validation'] = '研究默认规则，未经未来收益验证，不代表上涨概率；观察中表示核心条件满足但部分软确认未满足，条件齐备也不表示走势得到保证。'
    _rule['common_rule'] = ('入池硬条件：6日双分均READY、quality≥0.60，每族版本及权重不变；'
        '最新RIGHT趋势分≥80、双模型安全分≥60；RIGHT两日净增达到门槛且最新日上涨，中间日允许回落，趋势与斜率的加权贡献两日净改善>0。'
        '需完整20日日线，20日成交额中位≥2000万元、当日≥3000万元，窗口无零成交量/额；最近10日复权因子值一致。'
        '价格复权至当日基准，收盘较前日上涨且≥20日收盘均值，偏离不超过设置上限（默认10%）。'
        '软确认：LEFT前5日峰值达到参考值、至少一次left_wash>0；T-3至T-1对T-8至T-4的均量比≤0.85、平均日振幅比≤0.90。'
        '修改LEFT参考值只影响优先标记及确认状态，不改变入池数量；所有软项满足才为条件齐备。')


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _day(value):
    try:
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _dates(rows, count):
    if not isinstance(rows, list) or len(rows) != count or any(not isinstance(row, dict) for row in rows):
        return None
    days = [_day(row.get('trade_date')) for row in rows]
    if any(day is None for day in days) or any(a >= b for a, b in zip(days, days[1:])):
        return None
    return days


def _parameters(min_left, min_right_gain, max_ma20_distance=None):
    for name, value, lower, upper in (
        ('min_left', min_left, 50, 90), ('min_right_gain', min_right_gain, 1, 15),
    ):
        number = _number(value)
        if number is None or not lower <= number <= upper:
            raise ValueError(f'{name} must be between {lower} and {upper}')
    if max_ma20_distance is not None:
        distance = _number(max_ma20_distance)
        if distance is None or not .03 <= distance <= .20:
            raise ValueError('max_ma20_distance must be between 0.03 and 0.20')


def _result(status, *reasons, **extra):
    return {'status': status, 'reasons': list(reasons), **extra}


def _confirmation(key, label, met, detail):
    return {'key': key, 'label': label, 'met': bool(met), 'detail': detail}


def _scores(scores):
    if _dates(scores, 6) is None:
        return None, '评分需为完整6个交易日，日期须递增且不能有缺失位置'
    values = {'left': [], 'right': []}
    required = {'left': ('left_wash', 'safety'), 'right': ('trend', 'slope', 'safety')}
    for family in ('left', 'right'):
        version, frozen_weights = None, None
        for row in scores:
            source = row.get(family)
            if not isinstance(source, dict) or source.get('status') != 'READY':
                return None, f'{family.upper()}存在未就绪或缺失评分'
            score, quality = _number(source.get('score')), _number(source.get('quality'))
            if score is None or not 0 <= score <= 100 or quality is None or not .6 <= quality <= 1:
                return None, f'{family.upper()}评分或质量不足（需有效分数且quality≥0.60）'
            current_version = source.get('version_id')
            if not isinstance(current_version, str) or not current_version:
                return None, f'{family.upper()}评分版本缺失'
            if version is not None and version != current_version:
                return None, f'{family.upper()}评分跨版本，不能连续比较'
            version = current_version
            subscores = source.get('subscores')
            if not isinstance(subscores, dict):
                return None, f'{family.upper()}子因子缺失'
            raw_parts, raw_weights = subscores.get('parts'), subscores.get('weights')
            if not isinstance(raw_parts, dict) or not isinstance(raw_weights, dict) or not raw_weights:
                return None, f'{family.upper()}子因子或权重缺失'
            parts = {key: _number(raw_parts.get(key)) for key in required[family]}
            weights = {key: _number(value) for key, value in raw_weights.items()}
            if (any(value is None or not 0 <= value <= 100 for value in parts.values())
                    or any(value is None or not 0 <= value <= 1 for value in weights.values())
                    or any(key not in weights for key in required[family])):
                return None, f'{family.upper()}所需因子或权重无效，缺失不能填零'
            if frozen_weights is not None and weights != frozen_weights:
                return None, f'{family.upper()}窗口内权重变化，不能连续比较'
            frozen_weights = weights
            values[family].append({'score': score, 'parts': parts, 'weights': weights})
    return values, None


def score_gate(scores, min_left=70, min_right_gain=3):
    """Cheap shared score gate for market screening before loading daily bars."""
    _parameters(min_left, min_right_gain)
    values, error = _scores(scores)
    if error:
        return _result('INSUFFICIENT_DATA', error)
    left, right = values['left'], values['right']
    left_peak = max(row['score'] for row in left[:-1])
    gain = right[-1]['score'] - right[-3]['score']
    latest_gain = right[-1]['score'] - right[-2]['score']
    contribution = lambda row: sum(row['parts'][key] * row['weights'][key] for key in ('trend', 'slope'))
    contribution_gain = contribution(right[-1]) - contribution(right[-3])
    metrics = {'left_score': left[-1]['score'], 'right_score': right[-1]['score'],
               'left_peak': left_peak, 'right_gain_2d': gain, 'right_gain_1d': latest_gain,
               'trend_slope_gain_2d': contribution_gain,
               'latest_trend_score': right[-1]['parts']['trend'], 'latest_slope_score': right[-1]['parts']['slope']}
    reasons = []
    if right[-1]['parts']['trend'] < 80:
        reasons.append(f"最新RIGHT趋势分{right[-1]['parts']['trend']:.1f}，未达到80")
    if min(left[-1]['parts']['safety'], right[-1]['parts']['safety']) < 60:
        reasons.append('最新LEFT/RIGHT安全分低于60')
    if latest_gain <= 0 or gain < float(min_right_gain):
        reasons.append(f'RIGHT需两日净增≥{float(min_right_gain):g}分且最新日上涨（两日{gain:.1f}分，最新日{latest_gain:.1f}分）')
    if contribution_gain <= 0:
        reasons.append('RIGHT趋势与斜率加权贡献未净改善，可能仅由共享因子推高')
    active_days = sum(row['parts']['left_wash'] > 0 for row in left[:-1])
    confirmations = [
        _confirmation('left_reference', 'LEFT优先参考', left_peak >= float(min_left),
                      f'LEFT前5日峰值{left_peak:.1f}，优先参考值≥{float(min_left):g}'),
        _confirmation('left_activity', '历史活跃依据', active_days > 0,
                      f'LEFT前5日left_wash>0共{active_days}日，确认需至少1日'),
    ]
    return _result('NO_MATCH' if reasons else 'MATCH', *reasons, metrics=metrics,
                   left_priority=confirmations[0]['met'], confirmations=confirmations)


def _adjusted_bars(bars):
    fields = ('open', 'high', 'low', 'close', 'volume_shares', 'amount_cny', 'adj_factor')
    result = []
    for source in bars:
        row = {key: _number(source.get(key)) for key in fields}
        if any(value is None for value in row.values()):
            return None, '20日行情存在缺失或非有限数值'
        if (min(row[key] for key in ('open', 'high', 'low', 'close', 'adj_factor')) <= 0
                or min(row['volume_shares'], row['amount_cny']) < 0
                or row['low'] > min(row['open'], row['close'])
                or row['high'] < max(row['open'], row['close'])):
            return None, '20日行情价格、成交量或复权因子无效'
        result.append(row)
    latest_factor = result[-1]['adj_factor']
    for row in result:
        for key in ('open', 'high', 'low', 'close'):
            row[key] *= row['adj_factor'] / latest_factor
            if not isfinite(row[key]):
                return None, '复权价格计算不可用'
    return result, None


def evaluate_structure(scores, bars, *, pattern, min_left=70, min_right_gain=3, max_ma20_distance=.10):
    """Evaluate a frozen research pattern, without changing any model output."""
    if pattern not in RULES:
        raise ValueError('pattern must be STARTUP or PULLBACK')
    if max_ma20_distance is None:
        raise ValueError('max_ma20_distance must be between 0.03 and 0.20')
    _parameters(min_left, min_right_gain, max_ma20_distance)
    gate = score_gate(scores, min_left, min_right_gain)
    if gate['status'] != 'MATCH':
        return gate
    bar_days, score_days = _dates(bars, 20), _dates(scores, 6)
    if bar_days is None or bar_days[-6:] != score_days:
        return _result('INSUFFICIENT_DATA', '需完整20个交易日日线，且尾部6日与评分日期逐日一致')
    adjusted, error = _adjusted_bars(bars)
    if error:
        return _result('INSUFFICIENT_DATA', error)
    if any(row['volume_shares'] == 0 or row['amount_cny'] == 0 for row in adjusted):
        return _result('NO_MATCH', '20日窗口存在零成交量或零成交额，排除停牌和无量样本')
    if any(not isclose(row['adj_factor'], adjusted[-1]['adj_factor'], rel_tol=1e-10, abs_tol=0)
           for row in adjusted[-10:]):
        return _result('NO_MATCH', '近10日复权因子改变，暂排除公司行为可能造成的伪缩量')
    current = adjusted[-1]
    ma20 = mean(row['close'] for row in adjusted)
    distance = current['close'] / ma20 - 1
    liquidity = median(row['amount_cny'] for row in adjusted)
    volume_ratio = mean(row['volume_shares'] for row in adjusted[-4:-1]) / mean(row['volume_shares'] for row in adjusted[-9:-4])
    # T-3..T-1 and T-8..T-4; each amplitude uses its own previous close.
    amplitudes = [(adjusted[index]['high'] - adjusted[index]['low']) / adjusted[index-1]['close']
                  for index in range(1, 20)]
    prior_amplitude = mean(amplitudes[-9:-4])
    amplitude_ratio = mean(amplitudes[-4:-1]) / prior_amplitude if prior_amplitude > 0 else None
    platform_high = max(row['high'] for row in adjusted[-6:-1])
    platform_low = min(row['low'] for row in adjusted[-6:-1])
    range_ratio = platform_high / platform_low - 1
    if (not all(isfinite(value) for value in (ma20, distance, liquidity, volume_ratio, range_ratio, prior_amplitude))
            or (amplitude_ratio is not None and not isfinite(amplitude_ratio))):
        return _result('INSUFFICIENT_DATA', '行情衍生计算出现非有限值，无法判断结构')
    reasons = []
    if liquidity < 20_000_000 or current['amount_cny'] < 30_000_000:
        reasons.append(f'流动性不足：20日成交额中位{liquidity/1e4:.0f}万，最新{current["amount_cny"]/1e4:.0f}万')
    if distance < 0 or distance > float(max_ma20_distance):
        reasons.append(f'收盘距辅助MA20为{distance:.2%}，须在0%至{float(max_ma20_distance):.0%}之间')
    if current['close'] <= adjusted[-2]['close']:
        reasons.append('最新收盘未较前日上涨，尚不符合价格转强的核心条件')
    confirmations = [*gate['confirmations'],
        _confirmation('volume_contraction', '成交量收敛', volume_ratio <= .85,
                      f'近期3日/前5日均量比{volume_ratio:.3f}，确认标准≤0.85'),
        _confirmation('amplitude_contraction', '振幅收敛', amplitude_ratio is not None and amplitude_ratio <= .9,
                      f'近期3日/前5日平均振幅比{amplitude_ratio:.3f}，确认标准≤0.90'
                      if amplitude_ratio is not None else '前5日振幅基准为零，振幅收敛尚无法确认'),
    ]
    metrics = {**gate['metrics'], 'volume_ratio': volume_ratio, 'amplitude_ratio': amplitude_ratio,
               'ma20_distance': distance, 'range_ratio': range_ratio, 'ma20': ma20,
               'platform_high': platform_high, 'median_amount_cny': liquidity,
               'current_amount_cny': current['amount_cny']}
    if pattern == 'STARTUP':
        support = max(ma20, platform_low)
        if range_ratio > .12:
            reasons.append(f'前5日平台区间{range_ratio:.2%}，超过12%')
        position = current['close'] / platform_high
        if not .98 <= position <= 1.05:
            reasons.append(f'收盘/前5日平台高点为{position:.3f}，不在0.98–1.05范围')
    else:
        support = ma20 * .95
        old_high = max(row['high'] for row in adjusted[-11:-5])
        pullback_low = min(row['low'] for row in adjusted[-5:-1])
        drawdown = 1 - pullback_low / old_high
        recent_low = min(row['low'] for row in adjusted[-5:])
        right = [float(row['right']['score']) for row in scores]
        left = [float(row['left']['score']) for row in scores]
        prior_right_peak = max(right[:3])
        left_recovery_peak = max(left[-3:-1])
        left_recovery = left_recovery_peak - min(left[:3])
        metrics.update(drawdown_ratio=drawdown, left_recovery=left_recovery,
                       prior_right_peak=prior_right_peak, prior_right_drawdown=prior_right_peak-right[-3])
        if not .02 <= drawdown <= .12:
            reasons.append(f'前段高点至整理低点回撤{drawdown:.2%}，不在2%–12%范围')
        if recent_low < support:
            reasons.append(f'最近5日低点{recent_low:.3f}低于辅助MA20的95%支撑{support:.3f}')
        confirmations.extend([
            _confirmation('prior_right_strength', '此前强势后回落', prior_right_peak >= 70 and prior_right_peak - right[-3] >= 3,
                          f'前3日RIGHT峰值{prior_right_peak:.1f}、至T-2回落{prior_right_peak-right[-3]:.1f}分，确认需峰值≥70且回落≥3分'),
            _confirmation('left_recovery', 'LEFT回调期间恢复', left_recovery >= 2 and left_recovery_peak >= float(min_left),
                          f'T-2/T-1的LEFT峰值{left_recovery_peak:.1f}，较前3日最低恢复{left_recovery:.1f}分，确认需恢复≥2分且峰值≥{float(min_left):g}'),
        ])
    metrics['support_price'] = support
    if reasons:
        return _result('NO_MATCH', *reasons)
    positive = [f'最新RIGHT趋势分{gate["metrics"]["latest_trend_score"]:.1f}，两日净增{gate["metrics"]["right_gain_2d"]:.1f}分、最新日增{gate["metrics"]["right_gain_1d"]:.1f}分',
                f'RIGHT趋势与斜率加权贡献净增{gate["metrics"]["trend_slope_gain_2d"]:.2f}分',
                f'收盘较前日上涨、距辅助MA20为{distance:.2%}；20日成交额中位{liquidity/1e4:.0f}万']
    if pattern == 'STARTUP':
        positive.append(f'前5日平台区间{range_ratio:.2%}，收盘/平台高点{current["close"]/platform_high:.3f}且收盘较前日上涨')
    else:
        positive.append(f'前段高点至整理低点回撤{metrics["drawdown_ratio"]:.2%}，最近5日低点不低于观察支撑{support:.3f}')
    positive.extend(item['detail'] for item in confirmations if item['met'])
    pending = [f'{item["label"]}：{item["detail"]}' for item in confirmations if not item['met']]
    stage = 'WATCH' if pending else 'CONFIRMED'
    risks = ['这是研究默认筛选，未经未来收益验证，不代表上涨概率。',
             f'跌破当前观察支撑{support:.3f}将削弱或使本次结构判断失效；该价位为复权至当日基准的辅助观察值。',
             '尚缺资金流、公告与新闻确认，需结合后续资料判断。',
             '辅助MA20由本次20日复权收盘均值计算，仅用于筛选，不重算原模型评分。']
    if float(scores[-1]['left']['score']) < float(scores[-2]['left']['score']):
        risks.append('LEFT当日下降：需核对子因子下降来源，不能自动解释为洗盘或启动。')
    if gate['metrics']['latest_slope_score'] <= 100 / 3:
        risks.append('均线斜率因子仍未高于中性参考，转强不等于均线全面向上。')
    structure = {'pattern': pattern, 'label': RULES[pattern]['label'],
                 **{key: gate['metrics'][key] for key in ('left_score', 'right_score', 'left_peak', 'right_gain_2d')},
                 'stage': stage, 'stage_label': '观察中' if stage == 'WATCH' else '条件齐备',
                 'left_priority': gate['left_priority'], 'confirmations': confirmations,
                 'pending_confirmations': pending, 'confirmed_count': sum(item['met'] for item in confirmations),
                 'confirmation_total': len(confirmations), 'reasons': positive, 'risks': risks,
                 'metrics': {key: round(value, 8) if value is not None else None for key, value in metrics.items()}}
    return _result('MATCH', *positive, structure=structure)
