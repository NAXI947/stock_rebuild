"""v3.1.2 adjusted daily inputs; the frozen v3.1.0/v3.1.1 helpers stay intact.

Every price-based daily component sees the same end-date OHLC basis. Volumes,
turnover and market value keep their original units. This module only constructs
model inputs; execution eligibility and displayed raw quotes are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .daily import FeatureValue
from .model_inputs import ModelInputs, Point, build_inputs as legacy_build_inputs

INVALID_PRICE = 'INVALID_OR_MISSING_PRICE_FACTOR'
LEFT_PARTS = ('left_wash', 'convergence', 'safety', 'resistance', 'ma_state',
              'weekly', 'relative_strength')
RIGHT_PARTS = ('trend', 'slope', 'resistance', 'safety', 'memory', 'efficiency')


@dataclass(frozen=True)
class AdjustedPoint(Point):
    """A model-only view with OHLC normalized and factor reset to one.

Resetting the factor prevents the unchanged moving-average helper from applying
the corporate action a second time. The source Point is never modified.
"""


def _positive(value) -> bool:
    try:
        return not isinstance(value, bool) and isfinite(value) and value > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_ohlc(open_, high, low, close) -> bool:
    return (all(_positive(value) for value in (open_, high, low, close))
            and low <= min(open_, close) <= max(open_, close) <= high)


def adjusted_points(points: list[Point]) -> tuple[list[AdjustedPoint] | None, tuple[str, ...]]:
    """Use f_i/f_T for all OHLC; reject invalid inputs and adjusted overflow."""
    if not points:
        return None, ('NO_BARS',)
    anchor = points[-1].adj_factor
    if not _positive(anchor):
        return None, (INVALID_PRICE,)
    result = []
    for point in points:
        if not _positive(point.adj_factor) or not _valid_ohlc(
                point.open, point.high, point.low, point.close):
            return None, (INVALID_PRICE,)
        ratio = point.adj_factor / anchor
        o, h, l, c = (value * ratio for value in
                      (point.open, point.high, point.low, point.close))
        if not _valid_ohlc(o, h, l, c):
            return None, (INVALID_PRICE,)
        result.append(AdjustedPoint(
            point.trade_date, o, h, l, c, point.volume, point.turnover_ratio,
            point.volume_ratio, 1.0, point.circ_mv_cny))
    return result, ()


def build_inputs(points: list[Point], *, cost85: float | None = None,
                 sector_return: float | None = None,
                 weekly_closes: list[float] | None = None) -> ModelInputs:
    adjusted, reasons = adjusted_points(points)
    if adjusted is None:
        missing = FeatureValue(None, 'MISSING', reasons)
        return ModelInputs({name: missing for name in LEFT_PARTS},
                           {name: missing for name in RIGHT_PARTS}, {}, reasons)
    # The reused builder feeds dictionary OHLC and turnover_ratio * 100 into
    # chaos, preserving its current window and percent turnover convention.
    return legacy_build_inputs(adjusted, cost85=cost85, sector_return=sector_return,
                               weekly_closes=weekly_closes)
