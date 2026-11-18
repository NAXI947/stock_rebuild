"""Pure daily feature calculations with explicit missing reasons."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class FeatureValue:
    value: float | None
    status: str
    reasons: tuple[str, ...] = ()
