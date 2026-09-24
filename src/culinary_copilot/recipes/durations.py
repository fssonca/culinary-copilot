"""Shared total-duration policy for repository filtering and evidence.

One policy, applied before ranking and ``LIMIT``, so standalone search and
the retrieval flow agree:

- Only a finite positive reported total can satisfy a maximum-duration
  filter. Missing (``NULL``), unverified zero, negative, and non-finite
  values are unknown for filtering and presentation.
- Raw reported values are always preserved separately; instruction times
  are never extracted or summed here, and cook time alone is never
  substituted for the total.
- Invalid JSON numeric types (strings, booleids, objects) are never
  coerced: they classify as ``MISSING``.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any


class DurationStatus(str, Enum):
    """Usability of a reported total duration for filtering/presentation."""

    REPORTED_POSITIVE = "reported_positive"
    REPORTED_ZERO_UNVERIFIED = "reported_zero_unverified"
    MISSING = "missing"


def classify_total(value: Any) -> tuple[DurationStatus, float | None]:
    """Classify a stored total-minutes value.

    Returns ``(status, usable_value)`` where ``usable_value`` is the finite
    positive total or ``None``. Booleans, strings, and every other
    non-numeric JSON type classify as ``MISSING`` without coercion.
    """
    if isinstance(value, bool):
        return DurationStatus.MISSING, None
    if not isinstance(value, (int, float)):
        return DurationStatus.MISSING, None
    number = float(value)
    if not math.isfinite(number):
        return DurationStatus.MISSING, None
    if number > 0:
        return DurationStatus.REPORTED_POSITIVE, number
    if number == 0:
        return DurationStatus.REPORTED_ZERO_UNVERIFIED, None
    return DurationStatus.MISSING, None


def satisfies_ceiling(value: Any, ceiling: float) -> bool:
    """True only when ``value`` is a finite positive total within ``ceiling``."""
    status, usable = classify_total(value)
    if status is not DurationStatus.REPORTED_POSITIVE or usable is None:
        return False
    if not math.isfinite(ceiling) or ceiling <= 0:
        return False
    return usable <= ceiling
