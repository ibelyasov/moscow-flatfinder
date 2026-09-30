"""Pure numerical formulas over the canonical facts emitted by source adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from itertools import pairwise
from math import fsum, isfinite
from typing import Any

from .geo_scoring import fitness_score, park_score
from .models import EQUIPMENT_NAMES, FieldValue, ListingFacts, ValueStatus

MAX_SCORES = {
    "noise": 6,
    "park": 9,
    "equipment": 15,
    "repair": 16,
    "price": 16,
    "commute": 9,
    "area": 4,
    "visual_layout": 3,
    "floor": 2,
    "light_view": 2,
    "building": 2,
    "personal": 10,
    "fitness": 6,
}
AUTOMATIC_MAX = sum(value for name, value in MAX_SCORES.items() if name != "personal")
TOTAL_MAX = sum(MAX_SCORES.values())
DEFAULT_SCORING_PARAMETERS = {
    "price_best_monthly_total": 90_000.0,
    "price_zero_monthly_total": 115_000.0,
    "commission_amortization_months": 12.0,
    "utilities_meters_monthly": 2_500.0,
    "utilities_full_bill_monthly": 10_000.0,
    "commute_best_minutes": 25.0,
    "commute_zero_minutes": 45.0,
    "area_start_m2": 35.0,
    "area_good_m2": 40.0,
    "area_full_m2": 50.0,
}
CRITERION_INPUT_FIELDS = {
    "noise": ("noise",),
    "park": ("park",),
    "equipment": ("appliances", "furnished"),
    "repair": (),
    "price": ("price_monthly", "commission", "utilities"),
    "commute": ("route", "route_minutes"),
    "area": ("area_m2",),
    "visual_layout": (),
    "floor": ("floor", "total_floors"),
    "light_view": (),
    "building": ("building_year",),
    "fitness": ("fitness",),
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if isfinite(number) else None


def _mapping(values: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    if values is None:
        return {}
    if not isinstance(values, Mapping) or any(
        not isinstance(key, str) for key in values
    ):
        raise ValueError(f"{name} must be an object with text keys")
    return values


def normalized_max_scores(
    values: Mapping[str, float] | None = None,
) -> dict[str, float]:
    values = _mapping(values, "scoring.max_points")
    unknown = sorted(set(values) - set(MAX_SCORES))
    if unknown:
        raise ValueError(f"unknown scoring criteria: {', '.join(unknown)}")
    result = {name: float(maximum) for name, maximum in MAX_SCORES.items()}
    for name, value in values.items():
        number = _number(value)
        if number is None or number < 0:
            raise ValueError(f"scoring.max_points.{name} must be a finite number >= 0")
        result[name] = number
    if not isfinite(sum(result.values())):
        raise ValueError("sum of scoring maxima must be finite")
    return result


def normalized_scoring_parameters(
    values: Mapping[str, float] | None = None,
) -> dict[str, float]:
    values = _mapping(values, "scoring.parameters")
    unknown = sorted(set(values) - set(DEFAULT_SCORING_PARAMETERS))
    if unknown:
        raise ValueError(f"unknown scoring parameters: {', '.join(unknown)}")
    result = dict(DEFAULT_SCORING_PARAMETERS)
    for name, value in values.items():
        number = _number(value)
        if number is None or number < 0:
            raise ValueError(f"scoring.parameters.{name} must be a finite number >= 0")
        result[name] = number
    for group in (
        ("price_best_monthly_total", "price_zero_monthly_total"),
        ("commute_best_minutes", "commute_zero_minutes"),
        ("area_start_m2", "area_good_m2", "area_full_m2"),
    ):
        if any(result[left] >= result[right] for left, right in pairwise(group)):
            raise ValueError(f"scoring parameters must increase: {', '.join(group)}")
    if result["commission_amortization_months"] <= 0:
        raise ValueError("commission_amortization_months must be greater than 0")
    return result


def score_maxima(
    values: Mapping[str, float] | None = None,
) -> tuple[float, float, float]:
    maxima = normalized_max_scores(values)
    automatic = sum(value for name, value in maxima.items() if name != "personal")
    return automatic, maxima["personal"], automatic + maxima["personal"]


def _unwrap(value: Any, *, partial: bool = False) -> Any:
    if isinstance(value, FieldValue):
        statuses = (
            {ValueStatus.CONFIRMED, ValueStatus.PARTIAL}
            if partial
            else {ValueStatus.CONFIRMED}
        )
        return value.value if value.status in statuses else None
    return value


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _smoothstep(value: float) -> float:
    value = _clamp(value)
    return value * value * (3 - 2 * value)


def round_half(value: float) -> float:
    return round(value * 2) / 2


def _linear(number: float, points: Sequence[tuple[float, float]]) -> float:
    if number <= points[0][0]:
        return points[0][1]
    for (left_x, left_y), (right_x, right_y) in pairwise(points):
        if number <= right_x:
            fraction = (number - left_x) / (right_x - left_x)
            return _clamp(
                round_half(left_y + fraction * (right_y - left_y)),
                0,
                max(score for _, score in points),
            )
    return points[-1][1]


def score_noise(observation: FieldValue | Mapping[str, Any] | None = None) -> float:
    raw = _unwrap(observation, partial=True)
    score = _number(raw.get("score")) if isinstance(raw, Mapping) else None
    return _clamp(score, 0, 6) if score is not None else 0.0


def score_park(observation: FieldValue | Mapping[str, Any] | None = None) -> float:
    raw = _unwrap(observation, partial=True)
    if not isinstance(raw, Mapping):
        return 0.0
    return park_score(
        _number(raw.get("walking_minutes")), _number(raw.get("area_hectares"))
    )


def equipment_values(facts: ListingFacts) -> dict[str, tuple[bool | None, ValueStatus]]:
    field = facts.fields.get("appliances")
    appliances = (
        field.value
        if isinstance(field, FieldValue) and isinstance(field.value, Mapping)
        else {}
    )
    parent_status = (
        field.status if isinstance(field, FieldValue) else ValueStatus.UNKNOWN
    )
    result = {}
    for name in EQUIPMENT_NAMES:
        raw = appliances.get(name)
        status = parent_status if isinstance(raw, bool) else ValueStatus.UNKNOWN
        if parent_status == ValueStatus.ABSENT:
            raw, status = False, ValueStatus.ABSENT
        # The two canonical producers also expose furniture as its own fact.
        if name == "furnished" and not isinstance(raw, bool):
            furniture = facts.fields.get("furnished")
            if isinstance(furniture, FieldValue):
                if furniture.status == ValueStatus.ABSENT:
                    raw, status = False, ValueStatus.ABSENT
                elif isinstance(furniture.value, bool):
                    raw, status = furniture.value, furniture.status
        result[name] = (raw if isinstance(raw, bool) else None, status)
    return result


def score_equipment(appliances: FieldValue | Mapping[str, Any] | None = None) -> float:
    raw = _unwrap(appliances)
    if not isinstance(raw, Mapping):
        return 0.0
    return float(3 * sum(raw.get(name) is True for name in EQUIPMENT_NAMES))


def _confirmed_cost(
    raw: Any, status: ValueStatus, *, price: float, commission: bool
) -> float | None:
    if status == ValueStatus.ABSENT:
        return 0.0
    if status != ValueStatus.CONFIRMED or not isinstance(raw, Mapping):
        return None
    amount = _number(raw.get("amount"))
    if commission:
        if amount is not None and 0 <= amount <= price:
            return amount
        percent = _number(raw.get("percent"))
        return (
            price * percent / 100
            if percent is not None and 0 <= percent <= 100
            else None
        )
    # A quoted base bill (including zero) does not confirm excluded meter usage.
    # Interpret the canonical payment mode before any quoted bill amount.
    mode = raw.get("mode")
    if mode == "included":
        return 0.0
    if mode == "full_bill" and amount is not None and amount >= 0:
        return amount
    return None


def monthly_cost(
    price: FieldValue | float | None = None,
    commission: FieldValue | Mapping[str, Any] | None = None,
    utilities: FieldValue | Mapping[str, Any] | None = None,
    parameters: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Keep soft estimates separate from confirmed costs used by hard budgets."""
    configured = DEFAULT_SCORING_PARAMETERS if parameters is None else parameters
    rent = _number(_unwrap(price))
    if rent is None or rent < 0:
        return {
            "estimated_monthly_total": None,
            "confirmed_monthly_total": None,
            "confirmed_monthly_lower_bound": None,
            "confidence": "unknown",
            "assumptions": [],
        }
    commission_raw = (
        commission.value if isinstance(commission, FieldValue) else commission
    )
    commission_status = (
        commission.status
        if isinstance(commission, FieldValue)
        else (ValueStatus.CONFIRMED if commission is not None else ValueStatus.UNKNOWN)
    )
    utilities_raw = utilities.value if isinstance(utilities, FieldValue) else utilities
    utilities_status = (
        utilities.status
        if isinstance(utilities, FieldValue)
        else (ValueStatus.CONFIRMED if utilities is not None else ValueStatus.UNKNOWN)
    )
    fee = _confirmed_cost(
        commission_raw, commission_status, price=rent, commission=True
    )
    utility_cost = _confirmed_cost(
        utilities_raw, utilities_status, price=rent, commission=False
    )
    assumptions = []
    estimated_fee = fee
    if estimated_fee is None:
        estimated_fee = rent
        assumptions.append(
            "Комиссия не подтверждена: для рейтинга принята одна месячная аренда."
        )
    estimated_utilities = utility_cost
    if estimated_utilities is None:
        mode = (
            utilities_raw.get("mode")
            if isinstance(utilities_raw, Mapping)
            and utilities_status == ValueStatus.CONFIRMED
            else None
        )
        parameter = (
            "utilities_meters_monthly"
            if mode == "meters_only"
            else "utilities_full_bill_monthly"
        )
        estimated_utilities = configured[parameter]
        assumptions.append(
            f"Коммунальные расходы не подтверждены: для рейтинга принято {estimated_utilities:g} ₽/мес."
        )
    months = configured["commission_amortization_months"]
    estimate = rent + estimated_utilities + estimated_fee / months
    confirmed = (
        rent + utility_cost + fee / months
        if utility_cost is not None and fee is not None
        else None
    )
    lower_bound = rent + (utility_cost or 0.0) + (fee or 0.0) / months
    if (
        not isfinite(estimate)
        or not isfinite(lower_bound)
        or (confirmed is not None and not isfinite(confirmed))
    ):
        raise ValueError("monthly cost calculation must remain finite")
    return {
        "estimated_monthly_total": estimate,
        "confirmed_monthly_total": confirmed,
        "confirmed_monthly_lower_bound": lower_bound,
        "rent": rent,
        "utilities_monthly": estimated_utilities,
        "commission_amount": estimated_fee,
        "commission_monthly": estimated_fee / months,
        "confidence": "partial" if assumptions else "confirmed",
        "assumptions": assumptions,
    }


def estimated_monthly_total(
    price: Any = None,
    commission: Any = None,
    utilities: Any = None,
    parameters: Mapping[str, float] | None = None,
) -> float | None:
    return monthly_cost(price, commission, utilities, parameters)[
        "estimated_monthly_total"
    ]


def score_price(
    monthly_total: Any = None, parameters: Mapping[str, float] | None = None
) -> float:
    number = _number(_unwrap(monthly_total))
    if number is None or number < 0:
        return 0.0
    configured = DEFAULT_SCORING_PARAMETERS if parameters is None else parameters
    position = (number - configured["price_best_monthly_total"]) / (
        configured["price_zero_monthly_total"] - configured["price_best_monthly_total"]
    )
    return 16 * (1 - _smoothstep(position))


def score_commute(
    minutes: Any = None, parameters: Mapping[str, float] | None = None
) -> float:
    number = _number(_unwrap(minutes, partial=True))
    if number is None or number < 0:
        return 0.0
    configured = DEFAULT_SCORING_PARAMETERS if parameters is None else parameters
    normalized = 25 + (number - configured["commute_best_minutes"]) * 20 / (
        configured["commute_zero_minutes"] - configured["commute_best_minutes"]
    )
    return _linear(normalized, ((25, 9), (30, 7), (35, 5), (40, 2), (45, 0)))


def score_area(
    area_m2: Any = None, parameters: Mapping[str, float] | None = None
) -> float:
    number = _number(_unwrap(area_m2))
    if number is None or number < 0:
        return 0.0
    configured = DEFAULT_SCORING_PARAMETERS if parameters is None else parameters
    start, good, full = (
        configured[name] for name in ("area_start_m2", "area_good_m2", "area_full_m2")
    )
    return 3 * _smoothstep((number - start) / (good - start)) + _smoothstep(
        (number - good) / (full - good)
    )


def score_floor(floor: Any = None, total_floors: Any = None) -> float:
    number = _number(_unwrap(floor))
    total = _number(_unwrap(total_floors))
    if number is None or number <= 1 or not number.is_integer():
        return 0.0
    return 1.0 if total is not None and number == total else 2.0


def score_building(observation: Any = None) -> float:
    year = _number(_unwrap(observation))
    if year is None or year <= 0:
        return 1.0
    return (
        2.0
        if year >= 2020
        else 1.5
        if year >= 2010
        else 1.0
        if year >= 2000
        else 0.5
        if year >= 1980
        else 0.0
    )


def score_fitness(observation: FieldValue | Mapping[str, Any] | None = None) -> float:
    raw = _unwrap(observation, partial=True)
    if not isinstance(raw, Mapping):
        return 0.0
    reviews = _number(raw.get("review_count"))
    if reviews is not None and (reviews < 0 or not reviews.is_integer()):
        return 0.0
    return fitness_score(
        _number(raw.get("walking_minutes")),
        _number(raw.get("rating")),
        int(reviews) if reviews is not None else None,
        raw.get("sauna") is True,
    )


def route_minutes(facts: ListingFacts) -> FieldValue | None:
    route = facts.fields.get("route")
    if isinstance(route, FieldValue) and isinstance(route.value, Mapping):
        value = route.value.get("average_minutes")
        if _number(value) is not None:
            return FieldValue(value, route.status, route.evidence)
    # route_minutes is also emitted by the Geo producer, not a user alias.
    minutes = facts.fields.get("route_minutes")
    return minutes if isinstance(minutes, FieldValue) else None


def _visual_score(visual_result: Mapping[str, Any] | None, component: str) -> float:
    if visual_result is None:
        return 0.0
    value = visual_result[component]
    return float(value["score"]) if value["status"] == "scoreable" else 0.0


def _scaled_score(criterion: str, value: float, maxima: Mapping[str, float]) -> float:
    maximum = maxima[criterion]
    # Integral weights retain the one-decimal rubric; fractional weights keep
    # their configured precision instead of disappearing or exceeding the cap.
    precision = max(1, -Decimal(str(maximum)).as_tuple().exponent)
    return _clamp(round(value / MAX_SCORES[criterion] * maximum, precision), 0, maximum)


def score_listing(
    facts: ListingFacts,
    *,
    max_scores: Mapping[str, float],
    parameters: Mapping[str, float],
    visual_result: Mapping[str, Any] | None = None,
) -> dict[str, float]:
    if not isinstance(facts, ListingFacts):
        raise TypeError("score_listing requires ListingFacts")
    fields = facts.fields
    cost = monthly_cost(
        fields.get("price_monthly"),
        fields.get("commission"),
        fields.get("utilities"),
        parameters,
    )
    equipment = equipment_values(facts)
    base = {
        "noise": score_noise(fields.get("noise")),
        "park": score_park(fields.get("park")),
        "equipment": float(
            3
            * sum(
                present is True and status == ValueStatus.CONFIRMED
                for present, status in equipment.values()
            )
        ),
        "repair": _visual_score(visual_result, "repair"),
        "price": score_price(cost["estimated_monthly_total"], parameters),
        "commute": score_commute(route_minutes(facts), parameters),
        "area": score_area(fields.get("area_m2"), parameters),
        "visual_layout": _visual_score(visual_result, "layout"),
        "floor": score_floor(fields.get("floor"), fields.get("total_floors")),
        "light_view": _visual_score(visual_result, "light_view"),
        "building": score_building(fields.get("building_year")),
        "fitness": score_fitness(fields.get("fitness")),
    }
    return {
        name: _scaled_score(name, value, max_scores)
        for name, value in base.items()
        if max_scores[name] > 0
    }


def _constraint_number(value: FieldValue | None) -> float | None:
    number = _number(_unwrap(value))
    return number if number is not None and number >= 0 else None


def evaluate_hard_constraints(
    facts: ListingFacts,
    constraints: Mapping[str, Any],
    parameters: Mapping[str, float],
    *,
    visual_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    checks = []

    def numeric(name: str, actual: float | None, operator: str) -> None:
        expected = constraints[name]
        status = (
            "needs_review"
            if actual is None
            else "pass"
            if (actual <= expected if operator == "max" else actual >= expected)
            else "fail"
        )
        checks.append(
            {
                "criterion": name,
                "status": status,
                "actual": actual,
                "expected": expected,
            }
        )

    if "max_monthly_total" in constraints:
        cost = monthly_cost(
            facts.fields.get("price_monthly"),
            facts.fields.get("commission"),
            facts.fields.get("utilities"),
            parameters,
        )
        actual = cost["confirmed_monthly_total"]
        lower_bound = cost["confirmed_monthly_lower_bound"]
        if (
            actual is None
            and lower_bound is not None
            and lower_bound > constraints["max_monthly_total"]
        ):
            numeric("max_monthly_total", lower_bound, "max")
            checks[-1]["actual_kind"] = "confirmed_lower_bound"
        else:
            numeric("max_monthly_total", actual, "max")
    for name, field, operator in (
        ("min_area_m2", "area_m2", "min"),
        ("min_floor", "floor", "min"),
    ):
        if name in constraints:
            numeric(name, _constraint_number(facts.fields.get(field)), operator)
    if "max_commute_minutes" in constraints:
        numeric("max_commute_minutes", _constraint_number(route_minutes(facts)), "max")
    if "min_repair_score" in constraints:
        component = visual_result["repair"] if visual_result is not None else None
        actual = (
            float(component["score"])
            if component is not None and component["status"] == "scoreable"
            else None
        )
        numeric("min_repair_score", actual, "min")
    if "required_equipment" in constraints:
        equipment = equipment_values(facts)
        for name in constraints["required_equipment"]:
            actual, status = equipment[name]
            state = (
                "needs_review"
                if status not in {ValueStatus.CONFIRMED, ValueStatus.ABSENT}
                or actual is None
                else "pass"
                if actual
                else "fail"
            )
            checks.append(
                {
                    "criterion": "required_equipment",
                    "item": name,
                    "status": state,
                    "actual": actual if state != "needs_review" else None,
                    "expected": True,
                }
            )
    status = (
        "rejected"
        if any(check["status"] == "fail" for check in checks)
        else "needs_review"
        if any(check["status"] == "needs_review" for check in checks)
        else "eligible"
    )
    return {"status": status, "checks": checks}


def score_bucket(
    auto_score: float, thresholds: Mapping[str, float] | None = None
) -> str:
    number = _number(auto_score)
    if number is None or number < 0:
        raise ValueError("automatic score must be a finite number >= 0")
    values = (
        {"priority": 80.0, "good": 70.0, "reserve": 60.0}
        if thresholds is None
        else thresholds
    )
    if set(values) != {"priority", "good", "reserve"} or any(
        _number(value) is None or value < 0 for value in values.values()
    ):
        raise ValueError("scoring thresholds are invalid")
    if not values["priority"] >= values["good"] >= values["reserve"]:
        raise ValueError("scoring thresholds must satisfy priority >= good >= reserve")
    for name in ("priority", "good", "reserve"):
        if number >= values[name]:
            return name
    return "skip"


def score_total(scores: Sequence[float], maximum: float = TOTAL_MAX) -> float:
    maximum_number = _number(maximum)
    numbers = [_number(score) for score in scores]
    if (
        maximum_number is None
        or maximum_number < 0
        or any(number is None or number < 0 for number in numbers)
    ):
        raise ValueError("scores and maximum must be finite nonnegative numbers")
    raw = fsum(numbers)
    if not isfinite(raw) or raw > maximum_number + 1e-9:
        raise ValueError(f"score out of range: {raw}")
    # Contributions already carry the rubric precision. A second rounding here
    # changes their sum and can make a valid assessment impossible to persist.
    return min(maximum_number, raw)
