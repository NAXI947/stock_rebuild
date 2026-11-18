"""v3.1.1 模型输入特征（依据 docs/03_指标与模型公式.md）。

原则：
- 只实现文档给出显式公式的部分；输入不足或尚未接入的来源一律 NULL + 原因，绝不用 0 或默认分替代。
- 文档中存在单位歧义的算式按字面实现，并把 `ASSUMPTION_*` 写入 reasons，便于审计与后续升版本。
- 新股或数据不足得 NULL，不重标权重（docs/03 §5.1）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import exp, isfinite
from statistics import mean

from .daily import FeatureValue
from .scores import R, indicator
from .chaos import compute_chaos_score

LEFT_WASH_DAY_SCORE = {0: 0, 1: 3, 2: 6, 3: 10, 4: 13, 5: 15}
WEEKLY_MIN_WEEKS = 26
# 文档的区间端点包含在内；浮点噪声不应把刚好落在端点的样本判到下一档
BAND_TOLERANCE = 1e-9


@dataclass(frozen=True)
class Point:
    """一行已完成的日线观测（turnover 为 canonical 小数比例）。"""

    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    turnover_ratio: float | None
    volume_ratio: float | None
    adj_factor: float | None
    circ_mv_cny: float | None


def adjusted_closes(points: list[Point]) -> tuple[list[float] | None, list[str]]:
    """docs/03 §2.1：adjusted_close_i=close_i×adj_factor_i/当前 adj_factor。

    新版本取消"单条因子缺失用当前因子回落"的旧行为，因此任一因子缺失即 NULL。
    """
    if not points:
        return None, ['NO_BARS']
    anchor = points[-1].adj_factor
    if anchor is None or anchor <= 0:
        return None, ['ADJ_FACTOR_MISSING']
    closes = []
    for point in points:
        if point.close is None or point.adj_factor is None or point.adj_factor <= 0:
            return None, ['ADJ_FACTOR_MISSING']
        closes.append(point.close * point.adj_factor / anchor)
    return closes, []


def mean_or_none(values: list[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None and isfinite(float(value))]
    if len(clean) != len(values) or not clean:
        return None
    return mean(clean)


def amplitude(point: Point, previous_close: float | None) -> float | None:
    """旧实现口径：(H-L)/pre_close，canonical 小数（docs/03 §2.3 与 sniper 冻结实现一致）。"""
    if point.high is None or point.low is None or previous_close in (None, 0):
        return None
    return (point.high - point.low) / previous_close


def moving_average(closes: list[float], window: int) -> float | None:
    if window <= 0 or len(closes) < window:
        return None
    return mean(closes[-window:])


def safety_score(close: float | None, ma20: float | None) -> FeatureValue:
    """docs/03 §3.2 安全分 S：按顺序匹配含端点。"""
    if close is None or ma20 in (None, 0):
        return FeatureValue(None, 'MISSING', ('MA20_UNAVAILABLE',))
    relative = close / ma20
    if 0.95 - BAND_TOLERANCE <= relative <= 1.10 + BAND_TOLERANCE:
        return FeatureValue(100.0, 'READY')
    if 0.90 - BAND_TOLERANCE <= relative <= 1.20 + BAND_TOLERANCE:
        return FeatureValue(60.0, 'READY')
    if 0.85 - BAND_TOLERANCE <= relative <= 1.30 + BAND_TOLERANCE:
        return FeatureValue(30.0, 'READY')
    return FeatureValue(0.0, 'READY')


def resistance_score(cost85: float | None, close: float | None) -> FeatureValue:
    """docs/03 §3.2：resistance=max(cost85/C-1,0)，K=100-R(resistance,0,0.20)。"""
    if cost85 is None or close in (None, 0):
        return FeatureValue(None, 'MISSING', ('COST85_UNAVAILABLE',))
    resistance = max(cost85 / close - 1.0, 0.0)
    value = R(resistance, 0.0, 0.20)
    if value.value is None:
        return value
    return FeatureValue(100.0 - value.value, 'READY')


def ma_state_score(close: float | None, ma5, ma10, ma20, ma60) -> FeatureValue:
    """docs/03 §5.1 均线基底 M。"""
    if close is None or any(value is None for value in (ma5, ma10, ma20, ma60)):
        return FeatureValue(None, 'MISSING', ('MA_UNAVAILABLE',))
    values = [ma5, ma10, ma20, ma60]
    lowest = min(values)
    if lowest <= 0:
        return FeatureValue(None, 'MISSING', ('MA_NON_POSITIVE',))
    if (max(values) - lowest) / lowest <= 0.03 + BAND_TOLERANCE:
        return FeatureValue(100.0, 'READY')
    if ma5 > ma10 > ma20 > ma60:
        return FeatureValue(70.0, 'READY')
    if close > ma20 > ma60:
        return FeatureValue(40.0, 'READY')
    return FeatureValue(0.0, 'READY')


def trend_score(close: float | None, ma20, ma60) -> FeatureValue:
    """docs/03 §6：Tn=0.5I(C>MA60)+0.3I(MA20>MA60)+0.2I(C>MA20)。"""
    if close is None or ma20 is None or ma60 is None:
        return FeatureValue(None, 'MISSING', ('MA_UNAVAILABLE',))
    value = (50.0 * (close > ma60) + 30.0 * (ma20 > ma60) + 20.0 * (close > ma20))
    return FeatureValue(value, 'READY')


def slope_score(ma20_slope5: float | None, ma60_slope10: float | None) -> FeatureValue:
    """docs/03 §6：Sl=0.6R(ma20_slope5,-0.02,0.04)+0.4R(ma60_slope10,-0.02,0.04)。"""
    first = R(ma20_slope5, -0.02, 0.04)
    second = R(ma60_slope10, -0.02, 0.04)
    if first.value is None or second.value is None:
        return FeatureValue(None, 'MISSING', ('MA_SLOPE_UNAVAILABLE',))
    return FeatureValue(0.6 * first.value + 0.4 * second.value, 'READY')


def memory_score(closes: list[float]) -> FeatureValue:
    """docs/03 §6 记忆 Mem：过去 20 个已完成交易日，不含当前盘中。

    `I` 的统一量纲是 100/0，因此公式中的 `I/100` 先归一化，最外层再乘 100；
    最近一次满足条件时分数应为 100，而不是 1。
    """
    if len(closes) < 21:
        return FeatureValue(None, 'MISSING', ('INSUFFICIENT_HISTORY_FOR_MEMORY',))
    window = closes[-21:]
    best = 0.0
    for offset, index in enumerate(range(1, len(window))):
        previous = window[index - 1]
        if previous <= 0:
            continue
        ret = window[index] / previous - 1.0
        age = len(window) - 1 - index
        if ret >= 0.05:
            best = max(best, exp(-age / 10.0))
    return FeatureValue(100.0 * best, 'READY')


def efficiency_score(points: list[Point]) -> FeatureValue:
    """docs/03 §3.3：Eff=100-R(inefficiency,1,8)，inefficiency 沿用旧 chaos 原始量纲。"""
    if len(points) < 21:
        return FeatureValue(None, 'MISSING', ('INSUFFICIENT_HISTORY_FOR_EFFICIENCY',))
    series = []
    for point in points:
        if point.turnover_ratio is None:
            return FeatureValue(None, 'MISSING', ('TURNOVER_UNAVAILABLE',))
        series.append({'close': point.close, 'high': point.high, 'low': point.low,
                       'turnover_rate': point.turnover_ratio * 100.0})
    chaos = compute_chaos_score(series, len(series) - 1)
    if chaos.value is None:
        return FeatureValue(None, 'MISSING', ('CHAOS_UNAVAILABLE',))
    value = R(chaos.value, 1.0, 8.0)
    if value.value is None:
        return value
    return FeatureValue(100.0 - value.value, 'READY')


def _low_volume_metric(points: list[Point], absolute: int) -> float | None:
    """量比缺失时的显式回退：V_i / 该日前 5 个有效正成交量均值（docs/03 §5.1）。"""
    point = points[absolute]
    prior = [item.volume for item in points[max(0, absolute - 5):absolute]
             if item.volume is not None and item.volume > 0]
    if point.volume is None or len(prior) < 5:
        return None
    baseline = mean(prior)
    if baseline <= 0:
        return None
    return point.volume / baseline


def left_wash_score(points: list[Point]) -> FeatureValue:
    """docs/03 §5.1 活跃收敛 L：A=T-5..T-1，B=T-60..T-6。"""
    # T-60 的涨幅需要 T-61 的真实前收；B(55)+A(5)+T(1)之外还需该前收日。
    if len(points) < 62:
        return FeatureValue(None, 'MISSING', ('INSUFFICIENT_HISTORY_FOR_A_B',))
    start = len(points) - 6
    window = points[start:start + 5]          # A
    base = points[start - 55:start]           # B: T-60..T-6
    reasons: list[str] = []
    amplitudes = [amplitude(window[index], points[start + index - 1].close)
                  for index in range(5)]
    if any(value is None for value in amplitudes):
        return FeatureValue(None, 'MISSING', ('AMPLITUDE_UNKNOWN',))
    if any(point.volume_ratio is not None for point in window):
        if any(point.volume_ratio is None for point in window):
            reasons.append('ASSUMPTION_LEFT_WASH_MIXED_VOLUME_RATIO_REJECTED')
            return FeatureValue(None, 'MISSING', ('VOLUME_RATIO_PARTIAL',))
        metric = [point.volume_ratio for point in window]
    else:
        metric = [_low_volume_metric(points, start + index) for index in range(5)]
        if any(value is None for value in metric):
            return FeatureValue(None, 'MISSING', ('VOLUME_RATIO_FALLBACK_UNAVAILABLE',))
        reasons.append('ASSUMPTION_LEFT_WASH_VOLUME_RATIO_FALLBACK')
    hits = sum(1 for index in range(5)
               if metric[index] < 0.65 and amplitudes[index] < 0.04)
    active = _base_is_active(points, start - 55, start)
    if active is None:
        return FeatureValue(None, 'MISSING', ('BASE_WINDOW_UNKNOWN',))
    if not active:
        return FeatureValue(0.0, 'READY', tuple(reasons))     # B 无活跃 → 活跃收敛分为 0
    # 文档 I(condition) 为 100/0；2.5*I/100 等价于命中时直接加 2.5。
    bonus = (2.5 * float(mean(metric[3:]) < mean(metric[:3]))
             + 2.5 * float(mean(amplitudes[3:]) < mean(amplitudes[:3])))
    return FeatureValue(5.0 * (LEFT_WASH_DAY_SCORE[hits] + bonus), 'READY', tuple(reasons))


def _base_is_active(points: list[Point], base_start: int, base_end: int) -> bool | None:
    """docs/03 §5.1：B 内至少一天涨幅≥5% 且换手满足市值分档；市值未知返回 None。"""
    for index in range(base_start, base_end):
        point, previous = points[index], points[index - 1]
        if point.close is None or previous.close in (None, 0):
            return None
        if point.close / previous.close - 1.0 < 0.05:
            continue
        if point.circ_mv_cny is None or point.turnover_ratio is None:
            return None
        threshold = 0.05 if point.circ_mv_cny < 1e10 else (0.03 if point.circ_mv_cny < 5e10 else 0.015)
        if point.turnover_ratio >= threshold:
            return True
    return False


def convergence_score(points: list[Point]) -> FeatureValue:
    """docs/03 §5.1 波动成交收敛 V：均使用 A 窗口，最近 2 日 vs 前 3 日。"""
    # A 的第一天振幅也需要其前一完成日收盘，六个切片点不足以提供真实前收。
    if len(points) < 7:
        return FeatureValue(None, 'MISSING', ('INSUFFICIENT_HISTORY_FOR_A',))
    start = len(points) - 6
    window = points[start:start + 5]
    amplitudes = [amplitude(window[index], points[start + index - 1].close) for index in range(5)]
    volumes = [point.volume for point in window]
    if any(value is None for value in amplitudes) or any(value is None for value in volumes):
        return FeatureValue(None, 'MISSING', ('A_WINDOW_INPUT_MISSING',))
    if mean(volumes[:3]) <= 0 or mean(amplitudes[:3]) <= 0:
        return FeatureValue(None, 'MISSING', ('A_WINDOW_ZERO_BASELINE',))
    volume_ratio = R(mean(volumes[3:]) / mean(volumes[:3]), 0.5, 1.2)
    amplitude_ratio = R(mean(amplitudes[3:]) / mean(amplitudes[:3]), 0.5, 1.2)
    if volume_ratio.value is None or amplitude_ratio.value is None:
        return FeatureValue(None, 'MISSING', ('A_WINDOW_INPUT_MISSING',))
    value = 0.5 * (100.0 - volume_ratio.value) + 0.5 * (100.0 - amplitude_ratio.value)
    return FeatureValue(value, 'READY')


def relative_strength_score(ret5_stock: float | None, ret5_sector: float | None) -> FeatureValue:
    """docs/03 §5.1：RS=R(ret5_stock-ret5_sector,-0.02,0.04)，板块关系缺失为 NULL。"""
    if ret5_sector is None:
        return FeatureValue(None, 'MISSING', ('SECTOR_RETURN_UNAVAILABLE',))
    if ret5_stock is None:
        return FeatureValue(None, 'MISSING', ('STOCK_RETURN_UNAVAILABLE',))
    return R(ret5_stock - ret5_sector, -0.02, 0.04)


def weekly_score(weekly_closes: list[float] | None) -> FeatureValue:
    """docs/03 §3.1 周分 W：EMA12/EMA26/DEA9/HIST，不足 26 周为 NULL。"""
    if not weekly_closes or len(weekly_closes) < WEEKLY_MIN_WEEKS:
        return FeatureValue(None, 'MISSING', ('WEEKLY_INSUFFICIENT_COMPLETED_WEEKS',))
    if any(value is None or value <= 0 for value in weekly_closes):
        return FeatureValue(None, 'MISSING', ('WEEKLY_CLOSE_INVALID',))

    def ema_series(values: list[float], window: int) -> list[float]:
        factor = 2.0 / (window + 1)
        out = [values[0]]
        for value in values[1:]:
            out.append(factor * value + (1 - factor) * out[-1])
        return out

    ema12 = ema_series(weekly_closes, 12)
    ema26 = ema_series(weekly_closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = ema_series(dif, 9)
    hist = [2.0 * (d - e) for d, e in zip(dif, dea)]
    close = weekly_closes[-1]
    last_dif, last_dea, last_hist, prior_hist = dif[-1], dea[-1], hist[-1], hist[-2]
    crossed_up = dif[-2] <= dea[-2] and last_dif > last_dea
    if crossed_up and abs(last_dif / close) <= 0.01:
        return FeatureValue(100.0, 'READY')
    if last_hist > 0 and last_hist > prior_hist:
        return FeatureValue(70.0, 'READY')
    if abs(last_dif / close) <= 0.005 and abs(last_dea / close) <= 0.005:
        return FeatureValue(50.0, 'READY')
    return FeatureValue(0.0, 'READY')


@dataclass(frozen=True)
class ModelInputs:
    left: dict[str, FeatureValue]
    right: dict[str, FeatureValue]
    values: dict[str, float | None]
    reasons: tuple[str, ...]


def build_inputs(points: list[Point], *, cost85: float | None = None,
                 sector_return: float | None = None,
                 weekly_closes: list[float] | None = None) -> ModelInputs:
    """把一只股票的已完成日线窗口转成左侧/右侧模型的七项与六项输入。"""
    closes, close_reasons = adjusted_closes(points)
    if closes is None:
        missing = FeatureValue(None, 'MISSING', tuple(close_reasons))
        keys_left = ('left_wash', 'convergence', 'safety', 'resistance', 'ma_state',
                     'weekly', 'relative_strength')
        keys_right = ('trend', 'slope', 'resistance', 'safety', 'memory', 'efficiency')
        return ModelInputs({name: missing for name in keys_left},
                           {name: missing for name in keys_right}, {}, tuple(close_reasons))

    ma5 = moving_average(closes, 5)
    ma10 = moving_average(closes, 10)
    ma20 = moving_average(closes, 20)
    ma60 = moving_average(closes, 60)
    close = closes[-1]
    ret5 = closes[-1] / closes[-6] - 1.0 if len(closes) >= 6 else None
    ma20_slope5 = ma20 / moving_average(closes[:-5], 20) - 1.0 if len(closes) >= 25 else None
    ma60_slope10 = ma60 / moving_average(closes[:-10], 60) - 1.0 if len(closes) >= 70 else None

    left = {
        'left_wash': left_wash_score(points),
        'convergence': convergence_score(points),
        'safety': safety_score(close, ma20),
        'resistance': resistance_score(cost85, close),
        'ma_state': ma_state_score(close, ma5, ma10, ma20, ma60),
        'weekly': weekly_score(weekly_closes),
        'relative_strength': relative_strength_score(ret5, sector_return),
    }
    right = {
        'trend': trend_score(close, ma20, ma60),
        'slope': slope_score(ma20_slope5, ma60_slope10),
        'resistance': resistance_score(cost85, close),
        'safety': safety_score(close, ma20),
        'memory': memory_score(closes),
        'efficiency': efficiency_score(points),
    }
    values = {'ma5': ma5, 'ma10': ma10, 'ma20': ma20, 'ma60': ma60, 'close_adj': close,
              'ret5': ret5, 'ma20_slope5': ma20_slope5, 'ma60_slope10': ma60_slope10,
              'cost85': cost85}
    values.update({f'left.{name}': part.value for name, part in left.items()})
    values.update({f'right.{name}': part.value for name, part in right.items()})
    reasons = tuple(sorted({reason for part in list(left.values()) + list(right.values())
                            for reason in part.reasons}))
    return ModelInputs(left, right, values, reasons)
