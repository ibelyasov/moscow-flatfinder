"""Pure scoring formulas for measured nearby-place observations."""

from __future__ import annotations

import math


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _smoothstep(value: float) -> float:
    value = _clamp(value)
    return value * value * (3 - 2 * value)


def park_quality(area_hectares: float | None) -> float:
    if area_hectares is None or not math.isfinite(area_hectares):
        return 0.5
    lower, upper = 0.3, 5.0
    if area_hectares <= lower:
        return 0.3
    if area_hectares >= upper:
        return 1.0
    position = math.log(area_hectares / lower) / math.log(upper / lower)
    return 0.3 + 0.7 * _smoothstep(position)


def park_score(minutes: float | None, area_hectares: float | None) -> float:
    if minutes is None or not math.isfinite(minutes) or minutes < 0:
        return 0.0
    position = _clamp((minutes - 10) / 15)
    access = 1 - _smoothstep(position)
    return round(9 * park_quality(area_hectares) * access, 2)


def fitness_quality(rating: float | None, review_count: int | None) -> float:
    if (
        rating is None
        or review_count is None
        or not math.isfinite(rating)
        or review_count < 0
    ):
        return 0.0
    return _smoothstep((rating - 4.0) / 0.5) * _smoothstep(review_count / 20)


def fitness_score(
    minutes: float | None,
    rating: float | None,
    review_count: int | None,
    sauna: bool,
) -> float:
    if minutes is None or not math.isfinite(minutes) or minutes < 0:
        return 0.0
    quality = fitness_quality(rating, review_count)
    venue_score = 2 + 2 * quality * (1 + int(bool(sauna)))
    access = 1 - _smoothstep((minutes - 10) / 15)
    return round(venue_score * access, 2)


__all__ = ["fitness_quality", "fitness_score", "park_quality", "park_score"]
