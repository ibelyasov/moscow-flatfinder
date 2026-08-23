import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from itertools import pairwise
from math import isfinite
from numbers import Real
from typing import Any

from .models import (
    VISION_SCHEMA_VERSION,
    Evidence,
    FieldValue,
    ListingFacts,
    ValueStatus,
    proposal_is_scoreable,
    validate_visual_payload,
)

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
    "equipment": (
        "appliances",
        "equipment",
        "furnished",
        "bed",
        "ac",
        "dishwasher",
        "fridge",
        "washer",
    ),
    "repair": ("repair",),
    "price": ("price_monthly", "price", "commission", "utilities"),
    "commute": ("route", "route_minutes", "commute"),
    "area": ("area_m2", "area"),
    "visual_layout": ("layout",),
    "floor": ("floor", "total_floors"),
    "light_view": ("light_view",),
    "building": ("building_year",),
    "fitness": ("fitness",),
}
CRITERION_MODEL_VERSIONS = {
    "noise": 1,
    "park": 2,
    "equipment": 2,
    "repair": 4,
    "price": 2,
    "commute": 1,
    "area": 4,
    "visual_layout": 4,
    "floor": 1,
    "light_view": 1,
    "building": 3,
    "fitness": 1,
}
_VISUAL_CRITERIA = frozenset({"repair", "visual_layout", "light_view"})
_HASH_IGNORED_KEYS = frozenset(
    {
        "evidence",
        "captured_at",
        "created_at",
        "updated_at",
        "fetched_at",
        "started_at",
        "finished_at",
        "input_hash",
    }
)


def round_half(value: float) -> float:
    return round(value * 2) / 2


def _unwrap(value: Any) -> tuple[Any, ValueStatus]:
    if isinstance(value, FieldValue):
        return value.value, value.status
    if value is None:
        return None, ValueStatus.UNKNOWN
    return value, ValueStatus.CONFIRMED


def _status_name(status: ValueStatus | str) -> str:
    return status.value if isinstance(status, ValueStatus) else str(status)


def _status_default(status: ValueStatus | str) -> float | None:
    state = _status_name(status)
    if state in {ValueStatus.UNKNOWN.value, ValueStatus.PARTIAL.value}:
        return 0
    if state not in {ValueStatus.CONFIRMED.value, ValueStatus.ABSENT.value}:
        return 0
    if state == ValueStatus.ABSENT.value:
        return 0
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, Real) and not isinstance(value, bool):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return None
        return number if isfinite(number) else None
    return None


def _normalise(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return "_".join(value.strip().lower().replace("-", " ").split())


def _stable_input(value: Any) -> Any:
    if isinstance(value, FieldValue):
        return {
            "status": _status_name(value.status),
            "value": _stable_input(value.value),
        }
    if isinstance(value, Mapping):
        return {
            str(key): _stable_input(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _HASH_IGNORED_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_stable_input(item) for item in value]
    if isinstance(value, set):
        items = [_stable_input(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, default=str
            ),
        )
    if isinstance(value, Real) and not isinstance(value, bool):
        return float(value) if isfinite(float(value)) else None
    if isinstance(value, (str, bool)) or value is None:
        return value
    return str(value)


def _input_hash(value: Any) -> str:
    payload = json.dumps(
        _stable_input(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def visual_input_hash(photos: Sequence[Any]) -> str:
    identities: set[str] = set()
    for photo in photos or ():
        if isinstance(photo, Mapping):
            identity = (
                photo.get("sha256") or photo.get("source_url") or photo.get("url")
            )
        else:
            identity = (
                getattr(photo, "sha256", None)
                or getattr(photo, "source_url", None)
                or getattr(photo, "url", None)
            )
        if identity:
            identities.add(str(identity))
    return _input_hash({"photos": sorted(identities)})


def criterion_input_hashes(
    facts: Any,
    *,
    visual_hash: str | None = None,
    max_scores: Mapping[str, float] | None = None,
    parameters: Mapping[str, float] | None = None,
) -> dict[str, str]:
    raw_fields = getattr(facts, "fields", None)
    if not isinstance(raw_fields, Mapping) and isinstance(facts, Mapping):
        raw_fields = facts.get("fields")
    if not isinstance(raw_fields, Mapping):
        raise ValueError("criterion hashes require canonical fields")
    fields = raw_fields
    result: dict[str, str] = {}
    maxima = normalized_max_scores(max_scores)
    normalized_parameters = normalized_scoring_parameters(parameters)
    for criterion, names in CRITERION_INPUT_FIELDS.items():
        if maxima[criterion] <= 0:
            continue
        payload: dict[str, Any] = {"fields": {name: fields.get(name) for name in names}}
        payload["model_version"] = CRITERION_MODEL_VERSIONS[criterion]
        payload["maximum"] = maxima[criterion]
        payload["parameters"] = normalized_parameters
        if criterion in _VISUAL_CRITERIA:
            payload["visual_hash"] = visual_hash
        result[criterion] = _input_hash(payload)
    return result


def reuse_unchanged_criteria(
    scores: Mapping[str, float],
    assessment: Mapping[str, Any],
    previous: Mapping[str, Any],
    input_hashes: Mapping[str, str],
    *,
    max_scores: Mapping[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    maxima = normalized_max_scores(max_scores)
    merged_scores = {str(name): float(value) for name, value in scores.items()}
    merged_assessment = deepcopy(dict(assessment))
    for criterion, input_hash in input_hashes.items():
        current = merged_assessment.get(criterion)
        if not isinstance(current, Mapping):
            current = {
                "score": merged_scores.get(criterion, 0.0),
                "evidence": [],
                "confidence": "unknown",
            }
        prior = previous.get(criterion) if isinstance(previous, Mapping) else None
        prior_score = (
            _number(prior.get("score")) if isinstance(prior, Mapping) else None
        )
        if (
            isinstance(prior, Mapping)
            and prior.get("input_hash") == input_hash
            and prior_score is not None
            and 0 <= prior_score <= maxima[criterion]
        ):
            merged_assessment[criterion] = deepcopy(dict(prior))
            merged_scores[criterion] = prior_score
            continue
        detail = deepcopy(dict(current))
        detail["input_hash"] = input_hash
        detail["score"] = merged_scores.get(criterion, 0.0)
        merged_assessment[criterion] = detail
    return merged_scores, merged_assessment


def _bounded(value: float, maximum: float) -> float:
    return max(0, min(maximum, round_half(value)))


def normalized_max_scores(
    values: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """Return the fixed criterion set with validated configurable maxima."""

    result = {name: float(maximum) for name, maximum in MAX_SCORES.items()}
    if values is None:
        return result
    unknown = sorted(set(values) - set(MAX_SCORES))
    if unknown:
        raise ValueError(f"unknown scoring criteria: {', '.join(unknown)}")
    for name, value in values.items():
        number = _number(value)
        if number is None or number < 0:
            raise ValueError(f"scoring.max_points.{name} must be a finite number >= 0")
        result[name] = float(number)
    return result


def score_maxima(
    values: Mapping[str, float] | None = None,
) -> tuple[float, float, float]:
    maxima = normalized_max_scores(values)
    automatic = sum(value for name, value in maxima.items() if name != "personal")
    personal = maxima["personal"]
    return automatic, personal, automatic + personal


def normalized_scoring_parameters(
    values: Mapping[str, float] | None = None,
) -> dict[str, float]:
    result = dict(DEFAULT_SCORING_PARAMETERS)
    if values is None:
        return result
    unknown = sorted(set(values) - set(result))
    if unknown:
        raise ValueError(f"unknown scoring parameters: {', '.join(unknown)}")
    for name, value in values.items():
        number = _number(value)
        if number is None or number < 0:
            raise ValueError(f"scoring.parameters.{name} must be a finite number >= 0")
        result[name] = number
    ordered_groups = (
        ("price_best_monthly_total", "price_zero_monthly_total"),
        ("commute_best_minutes", "commute_zero_minutes"),
        ("area_start_m2", "area_good_m2", "area_full_m2"),
    )
    for group in ordered_groups:
        numbers = [result[name] for name in group]
        if any(left >= right for left, right in pairwise(numbers)):
            raise ValueError(f"scoring parameters must increase: {', '.join(group)}")
    if result["commission_amortization_months"] <= 0:
        raise ValueError("commission_amortization_months must be greater than 0")
    return result


def _scaled_score(
    criterion: str,
    value: float,
    maxima: Mapping[str, float],
) -> float:
    maximum = float(maxima[criterion])
    if maximum <= 0:
        return 0.0
    base_maximum = float(MAX_SCORES[criterion])
    return round(max(0.0, min(maximum, value / base_maximum * maximum)), 1)


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _smoothstep(value: float) -> float:
    value = _clamp(value)
    return value * value * (3 - 2 * value)


def _linear(value: Any, points: Sequence[tuple[float, float]]) -> float:
    raw, status = _unwrap(value)
    status_value = _status_default(status)
    if status_value is not None:
        return status_value
    number = _number(raw)
    maximum = max(point[1] for point in points)
    if number is None:
        return 0
    if number <= points[0][0]:
        return points[0][1]
    for (left_x, left_y), (right_x, right_y) in pairwise(points):
        if number <= right_x:
            fraction = (number - left_x) / (right_x - left_x)
            return _bounded(left_y + fraction * (right_y - left_y), maximum)
    return points[-1][1]


def _lookup(value: Any, table: Mapping[str, float]) -> float:
    raw, status = _unwrap(value)
    status_value = _status_default(status)
    if status_value is not None:
        return status_value
    maximum = max(table.values(), default=0)
    number = _number(raw)
    if number is not None:
        return _bounded(number, maximum)
    return _bounded(table.get(_normalise(raw) or "", 0), maximum)


def score_noise(observation: Any = None) -> float:
    raw, status = _unwrap(observation)
    status_value = _status_default(status)
    if status_value is not None:
        return status_value
    if isinstance(raw, Mapping):
        score = _number(raw.get("score"))
        if score is not None:
            return max(0.0, min(float(MAX_SCORES["noise"]), score))
    return _lookup(
        observation,
        {
            "quiet": 6,
            "quiet_courtyard": 6,
            "protected": 4.5,
            "shielded": 4.5,
            "potential": 1.5,
            "source_present": 1.5,
            "loud": 0,
            "strong": 0,
        },
    )


def score_park(observation: Any = None) -> float:
    raw, status = _unwrap(observation)
    status_value = _status_default(status)
    if status_value is not None:
        return status_value
    if isinstance(raw, Mapping):
        numeric = _number(raw.get("score"))
        if numeric is not None:
            return max(0.0, min(9.0, numeric))
    return _lookup(
        raw,
        {
            "good": 9,
            "suitable": 9,
            "limited": 6,
            "small_or_busy": 6,
            "greenery": 2,
            "only_greenery": 2,
            "none": 0,
        },
    )


def score_equipment(appliances: Mapping[str, Any] | None = None) -> float:
    raw_appliances, parent_status = _unwrap(appliances)
    if not isinstance(raw_appliances, Mapping):
        raw_appliances = {}
    names = ("furnished", "ac", "dishwasher", "fridge", "washer")
    total = 0
    for name in names:
        item = raw_appliances.get(name)
        if _status_name(parent_status) != ValueStatus.CONFIRMED.value:
            item = FieldValue(None, parent_status)
        raw, status = _unwrap(item)
        state = _status_name(status)
        if state == ValueStatus.ABSENT.value:
            continue
        if state in {ValueStatus.UNKNOWN.value, ValueStatus.PARTIAL.value}:
            continue
        if state != ValueStatus.CONFIRMED.value or raw is None:
            continue
        present = (
            raw
            if isinstance(raw, bool)
            else _normalise(raw)
            in {
                "yes",
                "true",
                "present",
                "confirmed",
            }
        )
        if present:
            total += 3
    return float(total)


def _owner_visual_component(observation: Any, name: str) -> Mapping[str, Any] | None:
    raw, _ = _unwrap(observation)
    if isinstance(raw, Mapping) and raw.get("schema_version") == VISION_SCHEMA_VERSION:
        component = raw.get(name)
        return component if isinstance(component, Mapping) else None
    return None


def score_repair(observation: Any = None) -> float:
    component = _owner_visual_component(observation, "repair")
    score = (
        component.get("score")
        if component and component.get("status") == "scoreable"
        else None
    )
    if (
        isinstance(score, Real)
        and not isinstance(score, bool)
        and isfinite(float(score))
    ):
        return _clamp(float(score), 0.0, 16.0)
    return 0.0


def _commission_amount(
    price: float, commission: Any, *, estimate_unknown: bool
) -> float | None:
    raw, status = _unwrap(commission)
    if _status_name(status) in {ValueStatus.UNKNOWN.value, ValueStatus.PARTIAL.value}:
        return price if estimate_unknown else None
    if _status_name(status) == ValueStatus.ABSENT.value:
        return 0.0
    if isinstance(raw, Mapping):
        amount = _number(raw.get("amount"))
        if amount is not None and 0 <= amount <= price:
            return amount
        percent = _number(raw.get("percent"))
        if percent is not None and 0 <= percent <= 100:
            return price * percent / 100
    amount = _number(raw)
    if amount is not None and 0 <= amount <= price:
        return amount
    return price if estimate_unknown else None


def _utilities_amount(
    utilities: Any,
    parameters: Mapping[str, float] | None = None,
) -> float:
    configured = normalized_scoring_parameters(parameters)
    utilities_monthly = {
        "included": 0.0,
        "meters_only": configured["utilities_meters_monthly"],
        "full_bill": configured["utilities_full_bill_monthly"],
    }
    raw, status = _unwrap(utilities)
    if _status_name(status) in {ValueStatus.UNKNOWN.value, ValueStatus.PARTIAL.value}:
        return utilities_monthly["full_bill"]
    if _status_name(status) == ValueStatus.ABSENT.value:
        return 0.0
    if isinstance(raw, Mapping):
        amount = _number(raw.get("amount"))
        if amount is not None and amount >= 0:
            return amount
        raw = raw.get("mode")
    return utilities_monthly.get(_normalise(raw) or "", utilities_monthly["full_bill"])


def estimated_monthly_total(
    price: Any = None,
    commission: Any = None,
    utilities: Any = None,
    parameters: Mapping[str, float] | None = None,
) -> float | None:
    configured = normalized_scoring_parameters(parameters)
    raw_price, price_status = _unwrap(price)
    if _status_default(price_status) is not None:
        return None
    price_number = _number(raw_price)
    if price_number is None or price_number < 0:
        return None
    commission_amount = _commission_amount(
        price_number, commission, estimate_unknown=True
    )
    if commission_amount is None:
        return None
    return (
        price_number
        + _utilities_amount(utilities, configured)
        + commission_amount / configured["commission_amortization_months"]
    )


def score_price(
    monthly_total: Any = None,
    parameters: Mapping[str, float] | None = None,
) -> float:
    raw, status = _unwrap(monthly_total)
    status_value = _status_default(status)
    if status_value is not None:
        return status_value
    number = _number(raw)
    if number is None:
        return 0
    configured = normalized_scoring_parameters(parameters)
    best = configured["price_best_monthly_total"]
    zero = configured["price_zero_monthly_total"]
    position = _clamp((number - best) / (zero - best))
    return 16 * (1 - _smoothstep(position))


def score_commute(
    minutes: Any = None,
    parameters: Mapping[str, float] | None = None,
) -> float:
    configured = normalized_scoring_parameters(parameters)
    raw, status = _unwrap(minutes)
    number = _number(raw)
    if _status_default(status) is not None or number is None:
        return 0.0
    best = configured["commute_best_minutes"]
    zero = configured["commute_zero_minutes"]
    normalized_minutes = 25 + (number - best) * 20 / (zero - best)
    return _linear(
        normalized_minutes,
        ((25, 9), (30, 7), (35, 5), (40, 2), (45, 0)),
    )


def score_area(
    area_m2: Any = None,
    parameters: Mapping[str, float] | None = None,
) -> float:
    area, area_status = _unwrap(area_m2)
    area_number = _number(area)
    area_status_value = _status_default(area_status)
    if area_status_value is not None:
        area_score = area_status_value
    elif area_number is None:
        area_score = 0
    else:
        configured = normalized_scoring_parameters(parameters)
        start = configured["area_start_m2"]
        good = configured["area_good_m2"]
        full = configured["area_full_m2"]
        area_score = 3 * _smoothstep(
            (area_number - start) / (good - start)
        ) + _smoothstep((area_number - good) / (full - good))
    return float(area_score)


def score_visual_layout(layout: Any = None) -> float:
    component = _owner_visual_component(layout, "layout")
    layout_score = (
        _number(component.get("score"))
        if component and component.get("status") == "scoreable"
        else None
    )
    if layout_score is None:
        layout_score = (
            0.0
            if component is not None
            else _lookup(
                layout,
                {
                    "comfortable": 3,
                    "good_two_room": 3,
                    "comfortable_two_room": 3,
                    "good_one_room": 2,
                    "good_one_bedroom": 2,
                    "disputed_two_room": 2,
                    "tight": 0,
                    "walk_through": 0,
                    "inconvenient": 0,
                },
            )
        )
    return float(layout_score)


def score_layout(area_m2: Any = None, layout: Any = None) -> float:
    """Compatibility helper for callers that still need the old composite."""

    return score_area(area_m2) + score_visual_layout(layout)


def score_floor(floor: Any = None, total_floors: Any = None) -> float:
    floor_raw, floor_status = _unwrap(floor)
    floor_name = _normalise(floor_raw)
    floor_number = _number(floor_raw)
    total_floors_raw, total_floors_status = _unwrap(total_floors)
    total_floors_number = (
        _number(total_floors_raw)
        if _status_default(total_floors_status) is None
        else None
    )
    floor_status_value = _status_default(floor_status)
    if floor_status_value is not None:
        floor_score = floor_status_value
    elif floor_raw is None:
        floor_score = 0
    elif isinstance(floor_raw, Real) and not isinstance(floor_raw, bool):
        if floor_number is None or floor_number < 1 or floor_number == 1:
            floor_score = 0
        elif total_floors_number is not None and floor_number == total_floors_number:
            floor_score = 1
        else:
            floor_score = 2
    elif (
        floor_name
        in {
            "nan",
            "inf",
            "+inf",
            "-inf",
            "infinity",
            "+infinity",
            "-infinity",
        }
        or floor_name == "first"
        or floor_name in {"unknown", "unclear"}
    ):
        floor_score = 0
    elif floor_name == "last":
        floor_score = 1
    elif floor_name and floor_name.isdigit():
        floor_value = int(floor_name)
        floor_score = (
            0 if floor_value == 1 else 1 if total_floors_number == floor_value else 2
        )
    else:
        floor_score = 0

    return float(floor_score)


def score_light_view(observation: Any = None) -> float:
    component = _owner_visual_component(observation, "light_view")
    score = (
        _number(component.get("score"))
        if component and component.get("status") == "scoreable"
        else None
    )
    return float(score or 0.0)


def score_building(observation: Any = None) -> float:
    raw, status = _unwrap(observation)
    year = _number(raw) if _status_default(status) is None else None
    if year is None:
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


def score_personal(_: Any = None) -> float:
    return 0.0


def score_fitness(observation: Any = None) -> float:
    raw, status = _unwrap(observation)
    status_value = _status_default(status)
    if status_value is not None:
        return status_value
    if isinstance(raw, Mapping):
        numeric = _number(raw.get("score"))
        if numeric is not None:
            return max(0.0, min(6.0, numeric))
    return 0.0


def _field_value(
    facts: ListingFacts,
    observations: Mapping[str, Any],
    *names: str,
) -> Any:
    if not isinstance(observations, Mapping):
        observations = {}
    for name in names:
        if name in observations:
            return observations[name]
    raw_fields = getattr(facts, "fields", {})
    fields = raw_fields if isinstance(raw_fields, Mapping) else {}
    for name in names:
        if name in fields:
            return fields[name]
    return None


def _nested(value: Any, key: str) -> Any:
    raw, parent_status = _unwrap(value)
    if isinstance(raw, Mapping):
        nested = raw.get(key)
        if _status_name(parent_status) != ValueStatus.CONFIRMED.value:
            nested_raw, _ = _unwrap(nested)
            return FieldValue(nested_raw, parent_status)
        return nested
    return None


def _proposal_mapping(proposal: Any) -> dict[str, Any] | None:
    if isinstance(proposal, Mapping):
        return dict(proposal)
    keys = getattr(proposal, "keys", None)
    if not callable(keys):
        return None
    try:
        return {key: proposal[key] for key in keys()}
    except (KeyError, TypeError, AttributeError):
        return None


def _empty_conflicts(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False


def _decode_proposal(proposal: Any) -> dict[str, Any] | None:
    normalized = _proposal_mapping(proposal)
    if normalized is None:
        return None
    for source, target in (
        ("value_json", "value"),
        ("evidence_json", "evidence"),
        ("image_indices_json", "image_indices"),
    ):
        if target in normalized or source not in normalized:
            continue
        try:
            normalized[target] = json.loads(normalized[source])
        except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
            return None
    if "conflicts" not in normalized and "conflicts_json" in normalized:
        raw_conflicts = normalized["conflicts_json"]
        if _empty_conflicts(raw_conflicts):
            normalized["conflicts"] = []
        elif isinstance(raw_conflicts, (list, tuple, dict)):
            return None
        else:
            try:
                normalized["conflicts"] = json.loads(raw_conflicts)
            except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
                return None
    if "conflicts" in normalized and not _empty_conflicts(normalized["conflicts"]):
        return None
    image_indices = normalized.get("image_indices")
    if image_indices is not None:
        if not isinstance(image_indices, (list, tuple)) or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in image_indices
        ):
            return None
    value = normalized.get("value")
    if (
        isinstance(value, Mapping)
        and value.get("schema_version") == VISION_SCHEMA_VERSION
    ):
        try:
            normalized["value"] = validate_visual_payload(value, image_indices or [])
        except ValueError:
            return None
    return (
        normalized
        if isinstance(value, Mapping)
        and value.get("schema_version") == VISION_SCHEMA_VERSION
        else None
    )


def apply_validated_vision(
    facts: ListingFacts,
    proposals: Sequence[Mapping[str, Any]],
) -> ListingFacts:
    """Apply the one current validated Luna visual assessment."""

    raw_fields = getattr(facts, "fields", {})
    fields = deepcopy(raw_fields) if isinstance(raw_fields, Mapping) else {}
    structured_visuals: list[dict[str, Any]] = []
    for proposal in proposals if isinstance(proposals, Sequence) else ():
        normalized_proposal = _decode_proposal(proposal)
        if normalized_proposal is None or not proposal_is_scoreable(
            normalized_proposal
        ):
            continue
        image_indices = normalized_proposal.get("image_indices")
        if not isinstance(image_indices, (list, tuple)) or not image_indices:
            continue
        value = normalized_proposal.get("value")
        if (
            isinstance(value, Mapping)
            and value.get("schema_version") == VISION_SCHEMA_VERSION
        ):
            structured_visuals.append(dict(value))

    if len(structured_visuals) == 1:
        payload = structured_visuals[0]
        for component_name, field_name in (
            ("repair", "repair"),
            ("layout", "layout"),
            ("light_view", "light_view"),
        ):
            component = payload[component_name]
            images = ", ".join(
                str(index) for index in component.get("evidence_indices", [])
            )
            evidence = [
                Evidence(
                    source="vision:model",
                    detail=f"{component.get('summary')} [images: {images}]",
                    captured_at="",
                )
            ]
            fields[field_name] = FieldValue(
                payload,
                ValueStatus.CONFIRMED
                if component.get("status") == "scoreable"
                else ValueStatus.UNKNOWN,
                evidence,
            )

    source = getattr(facts, "source", None)
    if not isinstance(source, str) or not source.strip():
        raise ValueError("validated visual facts require a non-empty source")
    return ListingFacts(
        getattr(facts, "source_listing_id", ""),
        getattr(facts, "source_url", ""),
        fields,
        source,
    )


def score_listing(
    facts: ListingFacts,
    observations: Mapping[str, Any] | None = None,
    *,
    max_scores: Mapping[str, float] | None = None,
    parameters: Mapping[str, float] | None = None,
) -> dict[str, float]:
    observations = observations if isinstance(observations, Mapping) else {}
    equipment = _field_value(facts, observations, "equipment", "appliances")
    equipment_raw, equipment_status = _unwrap(equipment)
    if isinstance(equipment_raw, Mapping):
        equipment_raw = dict(equipment_raw)
        equipment_raw.setdefault(
            "furnished", _field_value(facts, observations, "furnished")
        )
        equipment = (
            equipment_raw
            if _status_name(equipment_status) == ValueStatus.CONFIRMED.value
            else FieldValue(equipment_raw, equipment_status)
        )
    else:
        equipment = {
            name: _field_value(facts, observations, name)
            for name in ("furnished", "ac", "dishwasher", "fridge", "washer")
        }

    route = _field_value(facts, observations, "route", "route_minutes", "commute")
    route_minutes = _nested(route, "average_minutes")
    if route_minutes is None:
        route_minutes = _nested(route, "minutes")
    if route_minutes is None:
        route_minutes = route
    route_score = _number(_nested(route, "average_score"))
    floor = _field_value(facts, observations, "floor")
    total_floors = _field_value(facts, observations, "total_floors")
    light_view = _field_value(facts, observations, "light_view", "view")

    price = _field_value(facts, observations, "price_monthly", "price")
    commission = _field_value(facts, observations, "commission")
    utilities = _field_value(facts, observations, "utilities")
    configured_parameters = normalized_scoring_parameters(parameters)
    monthly_total = estimated_monthly_total(
        price, commission, utilities, configured_parameters
    )

    area = _field_value(facts, observations, "area_m2", "area")
    layout = _field_value(facts, observations, "layout")
    base_scores = {
        "noise": score_noise(_field_value(facts, observations, "noise")),
        "park": score_park(_field_value(facts, observations, "park")),
        "equipment": score_equipment(equipment),
        "repair": score_repair(_field_value(facts, observations, "repair")),
        "price": score_price(monthly_total, configured_parameters),
        "commute": route_score
        if route_score is not None
        else score_commute(route_minutes, configured_parameters),
        "area": score_area(area, configured_parameters),
        "visual_layout": score_visual_layout(layout),
        "floor": score_floor(floor, total_floors),
        "light_view": score_light_view(light_view),
        "building": score_building(_field_value(facts, observations, "building_year")),
        "personal": score_personal(),
        "fitness": score_fitness(_field_value(facts, observations, "fitness")),
    }
    maxima = normalized_max_scores(max_scores)
    return {
        name: 0.0 if name == "personal" else _scaled_score(name, value, maxima)
        for name, value in base_scores.items()
        if maxima[name] > 0
    }


def _constraint_number(value: Any) -> float | None:
    raw, status = _unwrap(value)
    if _status_default(status) is not None:
        return None
    return _number(raw)


def evaluate_hard_constraints(
    facts: ListingFacts,
    constraints: Mapping[str, Any] | None,
    parameters: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Evaluate the fixed public constraint set without inventing new rules."""

    configured = constraints if isinstance(constraints, Mapping) else {}
    checks: list[dict[str, Any]] = []

    def numeric_check(
        name: str,
        actual: float | None,
        expected: Any,
        operator: str,
    ) -> None:
        limit = _number(expected)
        if limit is None:
            raise ValueError(f"hard_constraints.{name} must be a finite number")
        if actual is None:
            state = "needs_review"
        elif operator == "max":
            state = "pass" if actual <= limit else "fail"
        else:
            state = "pass" if actual >= limit else "fail"
        checks.append(
            {
                "criterion": name,
                "status": state,
                "actual": actual,
                "expected": limit,
            }
        )

    if "max_monthly_total" in configured:
        monthly_total = estimated_monthly_total(
            _field_value(facts, {}, "price_monthly", "price"),
            _field_value(facts, {}, "commission"),
            _field_value(facts, {}, "utilities"),
            parameters,
        )
        numeric_check(
            "max_monthly_total",
            monthly_total,
            configured["max_monthly_total"],
            "max",
        )
    if "min_area_m2" in configured:
        numeric_check(
            "min_area_m2",
            _constraint_number(_field_value(facts, {}, "area_m2", "area")),
            configured["min_area_m2"],
            "min",
        )
    if "min_floor" in configured:
        numeric_check(
            "min_floor",
            _constraint_number(_field_value(facts, {}, "floor")),
            configured["min_floor"],
            "min",
        )
    if "max_commute_minutes" in configured:
        route = _field_value(facts, {}, "route", "route_minutes", "commute")
        minutes = _constraint_number(_nested(route, "average_minutes"))
        if minutes is None:
            minutes = _constraint_number(_nested(route, "minutes"))
        if minutes is None:
            minutes = _constraint_number(route)
        numeric_check(
            "max_commute_minutes",
            minutes,
            configured["max_commute_minutes"],
            "max",
        )
    if "min_repair_score" in configured:
        repair = score_repair(_field_value(facts, {}, "repair"))
        repair_field = _field_value(facts, {}, "repair")
        component = _owner_visual_component(repair_field, "repair")
        numeric_check(
            "min_repair_score",
            repair if component and component.get("status") == "scoreable" else None,
            configured["min_repair_score"],
            "min",
        )
    if "required_equipment" in configured:
        required = configured["required_equipment"]
        if not isinstance(required, list) or not all(
            isinstance(name, str) and name.strip() for name in required
        ):
            raise ValueError(
                "hard_constraints.required_equipment must be an array of names"
            )
        equipment = _field_value(facts, {}, "equipment", "appliances")
        raw_equipment, parent_status = _unwrap(equipment)
        raw_equipment = raw_equipment if isinstance(raw_equipment, Mapping) else {}
        for raw_name in required:
            name = raw_name.strip()
            value = raw_equipment.get(name, _field_value(facts, {}, name))
            raw, status = _unwrap(value)
            if _status_name(parent_status) != ValueStatus.CONFIRMED.value:
                status = parent_status
            normalized = _normalise(raw)
            present = raw is True or normalized in {
                "yes",
                "true",
                "present",
                "confirmed",
            }
            state = (
                "needs_review"
                if _status_name(status)
                in {ValueStatus.UNKNOWN.value, ValueStatus.PARTIAL.value}
                or raw is None
                else "pass"
                if present
                else "fail"
            )
            checks.append(
                {
                    "criterion": "required_equipment",
                    "item": name,
                    "status": state,
                    "actual": present if state != "needs_review" else None,
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
    auto_score: float,
    thresholds: Mapping[str, float] | None = None,
) -> str:
    values = {"priority": 80.0, "good": 70.0, "reserve": 60.0}
    if thresholds is not None:
        unknown = sorted(set(thresholds) - set(values))
        if unknown:
            raise ValueError(f"unknown scoring thresholds: {', '.join(unknown)}")
        for name, value in thresholds.items():
            number = _number(value)
            if number is None or number < 0:
                raise ValueError(
                    f"scoring.thresholds.{name} must be a finite number >= 0"
                )
            values[name] = number
    if not values["priority"] >= values["good"] >= values["reserve"]:
        raise ValueError("scoring thresholds must satisfy priority >= good >= reserve")
    if auto_score >= values["priority"]:
        return "priority"
    if auto_score >= values["good"]:
        return "good"
    if auto_score >= values["reserve"]:
        return "reserve"
    return "skip"


def score_total(scores: list[float], maximum: float = TOTAL_MAX) -> float:
    maximum_number = _number(maximum)
    if maximum_number is None or maximum_number < 0:
        raise ValueError("score maximum must be a finite number >= 0")
    numbers = [_number(score) for score in scores]
    if any(number is None for number in numbers):
        raise ValueError("score out of range: non-finite or malformed value")
    raw_total = sum(number for number in numbers if number is not None)
    if not isfinite(raw_total) or not 0 <= raw_total <= maximum_number:
        raise ValueError(f"score out of range: {raw_total}")
    total = round(raw_total, 1)
    if not 0 <= total <= maximum_number:
        raise ValueError(f"score out of range: {total}")
    return total
