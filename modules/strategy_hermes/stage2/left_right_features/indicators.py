from __future__ import annotations

from math import isfinite
from typing import Any, Mapping


def as_float(row: Mapping[str, Any], key: str, default: float | None = None) -> float | None:
    """Return a finite float from a row-like object, or the supplied default."""
    try:
        value = row[key]
        if value is None or str(value).strip() == "":
            return default
        result = float(value)
        return result if isfinite(result) else default
    except (KeyError, IndexError, TypeError, ValueError):
        return default
