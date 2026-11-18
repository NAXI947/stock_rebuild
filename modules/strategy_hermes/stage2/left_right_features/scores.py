"""Documented v3.1.1 score primitives. Missing input stays NULL, never zero."""
from __future__ import annotations
from dataclasses import dataclass, field
from math import isfinite

from .daily import FeatureValue


def valid(*values) -> bool:
    return all(value is not None and isfinite(float(value)) for value in values)


def clipped(value, low, high):
    return min(max(value, low), high)


def R(x, a, b) -> FeatureValue:
    if not valid(x, a, b):
        return FeatureValue(None, 'MISSING', ('INPUT_MISSING',))
    if b <= a:
        return FeatureValue(None, 'MISSING', ('INVALID_BOUNDS',))
    return FeatureValue(100 * clipped((x - a) / (b - a), 0, 1), 'READY')


def indicator(condition: bool | None) -> FeatureValue:
    if condition is None:
        return FeatureValue(None, 'MISSING', ('CONDITION_UNKNOWN',))
    return FeatureValue(100.0 if condition else 0.0, 'READY')


def combine(parts: dict[str, FeatureValue], weights: dict[str, float]) -> FeatureValue:
    if set(parts) != set(weights):
        raise ValueError('parts and weights must share the same keys')
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError('weights must sum to 1')
    missing = [name for name, part in parts.items() if part.value is None]
    if missing:
        return FeatureValue(None, 'MISSING', tuple(sorted(missing)))
    total = sum(weights[name] * parts[name].value for name in parts)
    return FeatureValue(total, 'READY')


@dataclass(frozen=True)
class LeftSetup:
    left_wash: FeatureValue
    convergence: FeatureValue
    safety: FeatureValue
    resistance: FeatureValue
    ma_state: FeatureValue
    weekly: FeatureValue
    relative_strength: FeatureValue
    weights: dict = field(default_factory=lambda: {
        'left_wash': .20, 'convergence': .15, 'safety': .15, 'resistance': .15,
        'ma_state': .10, 'weekly': .10, 'relative_strength': .15})

    def score(self) -> FeatureValue:
        return combine({'left_wash': self.left_wash, 'convergence': self.convergence,
            'safety': self.safety, 'resistance': self.resistance, 'ma_state': self.ma_state,
            'weekly': self.weekly, 'relative_strength': self.relative_strength}, self.weights)


@dataclass(frozen=True)
class RightSetup:
    trend: FeatureValue
    slope: FeatureValue
    resistance: FeatureValue
    safety: FeatureValue
    memory: FeatureValue
    efficiency: FeatureValue
    weights: dict = field(default_factory=lambda: {
        'trend': .20, 'slope': .15, 'resistance': .20,
        'safety': .15, 'memory': .15, 'efficiency': .15})

    def score(self) -> FeatureValue:
        return combine({'trend': self.trend, 'slope': self.slope, 'resistance': self.resistance,
            'safety': self.safety, 'memory': self.memory, 'efficiency': self.efficiency}, self.weights)
