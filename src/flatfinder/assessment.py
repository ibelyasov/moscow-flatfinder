"""One complete pure calculation: typed facts and policy in, assessment out."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from math import fsum
from typing import Any

from .models import (
    FieldValue,
    ListingFacts,
    ValueStatus,
    facts_to_dict,
    validate_visual_payload,
)
from .scoring import (
    CRITERION_INPUT_FIELDS,
    _number,
    equipment_values,
    evaluate_hard_constraints,
    monthly_cost,
    route_minutes,
    score_bucket,
    score_listing,
    score_total,
)
from .scoring_policy import validate_policy

_VISUAL_COMPONENTS = {
    "repair": "repair",
    "visual_layout": "layout",
    "light_view": "light_view",
}
_MEASUREMENT_FIELDS = {
    "commute": ("route", "route_minutes"),
    "park": ("park",),
    "fitness": ("fitness",),
    "noise": ("noise",),
}


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    scores: dict[str, float]
    assessment: dict[str, Any]
    auto_score: float
    total: float
    personal_score: float
    status: str


def _accepted_visual(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate payload structure; storage owns accepted status and photo currentness."""
    if not isinstance(value, Mapping):
        raise TypeError("accepted visual_result must be the owner visual payload")
    indices = []
    for name in ("repair", "layout", "light_view"):
        component = value.get(name)
        items = (
            component.get("evidence_indices")
            if isinstance(component, Mapping)
            else None
        )
        if not isinstance(items, list) or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in items
        ):
            raise ValueError(f"visual_result {name} evidence_indices are invalid")
        indices.extend(items)
    # Element validation precedes hashing; current-gallery validation happened in storage.
    return validate_visual_payload(value, list(dict.fromkeys(indices)))


def _field_evidence(field: FieldValue) -> list[dict[str, str]]:
    return [
        {
            "source": item.source,
            "detail": item.detail,
            "captured_at": item.captured_at,
            "confidence": field.status.value,
        }
        for item in field.evidence
    ]


def _confidence(fields: list[FieldValue]) -> str:
    if fields and all(
        field.status in {ValueStatus.CONFIRMED, ValueStatus.ABSENT} for field in fields
    ):
        return "confirmed"
    if any(
        field.status in {ValueStatus.CONFIRMED, ValueStatus.PARTIAL, ValueStatus.ABSENT}
        for field in fields
    ):
        return "partial"
    return "unknown"


def _measurement(field: FieldValue | None, key: str) -> float | None:
    if (
        field is None
        or field.status not in {ValueStatus.CONFIRMED, ValueStatus.PARTIAL}
        or not isinstance(field.value, Mapping)
    ):
        return None
    return _number(field.value.get(key))


def build_assessment(
    facts: ListingFacts,
    scores: Mapping[str, float],
    *,
    policy: Mapping[str, Any],
    visual_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for criterion, score in scores.items():
        if criterion == "personal":
            continue
        fields = [
            facts.fields[name]
            for name in CRITERION_INPUT_FIELDS[criterion]
            if name in facts.fields
        ]
        detail: dict[str, Any] = {
            "score": float(score),
            "confidence": _confidence(fields),
            "evidence": [item for field in fields for item in _field_evidence(field)],
        }
        assumptions = []
        if criterion in _VISUAL_COMPONENTS:
            component = (
                visual_result[_VISUAL_COMPONENTS[criterion]]
                if visual_result is not None
                else None
            )
            detail["confidence"] = (
                "confirmed"
                if component is not None and component["status"] == "scoreable"
                else "unknown"
            )
            if component is not None:
                detail["details"] = deepcopy(dict(component))
                # Photo timestamps and exact currentness are available on the stored Vision run.
                detail["evidence"] = [
                    {
                        "source": "vision:accepted",
                        "detail": component["summary"],
                        "captured_at": "",
                        "confidence": detail["confidence"],
                    }
                ]
        elif criterion == "price":
            cost = monthly_cost(
                facts.fields.get("price_monthly"),
                facts.fields.get("commission"),
                facts.fields.get("utilities"),
                policy["parameters"],
            )
            detail["details"] = cost
            detail["confidence"] = cost["confidence"]
            assumptions.extend(cost["assumptions"])
        elif criterion == "equipment":
            values = equipment_values(facts)
            states = [status for _, status in values.values()]
            detail["confidence"] = (
                "confirmed"
                if all(
                    status in {ValueStatus.CONFIRMED, ValueStatus.ABSENT}
                    for status in states
                )
                else "partial"
                if any(
                    status
                    in {ValueStatus.CONFIRMED, ValueStatus.ABSENT, ValueStatus.PARTIAL}
                    for status in states
                )
                else "unknown"
            )
            detail["details"] = {
                name: {"present": present, "status": status.value}
                for name, (present, status) in sorted(values.items())
            }
        elif criterion == "building":
            year = facts.fields.get("building_year")
            known = (
                _number(year.value)
                if year is not None and year.status == ValueStatus.CONFIRMED
                else None
            )
            if known is None or known <= 0:
                assumptions.append(
                    "Год постройки неизвестен: в базовой шкале рейтинга принято 1 из 2 баллов."
                )
            detail["details"] = {"building_year": known}
        elif criterion in {"noise", "park", "fitness"}:
            field = facts.fields.get(criterion)
            if field is not None and isinstance(field.value, Mapping):
                detail["details"] = deepcopy(dict(field.value))
                declared = field.value.get("assumptions", [])
                if isinstance(declared, list) and all(
                    isinstance(item, str) for item in declared
                ):
                    assumptions.extend(declared)
            measurement_key = "score" if criterion == "noise" else "walking_minutes"
            if _measurement(field, measurement_key) is None:
                detail["confidence"] = "unknown"
            elif criterion == "park" and _measurement(field, "area_hectares") is None:
                assumptions.append(
                    "Площадь парка неизвестна: для рейтинга принята quality=0,5."
                )
            elif criterion == "fitness" and (
                _measurement(field, "rating") is None
                or _measurement(field, "review_count") is None
            ):
                assumptions.append(
                    "Качество зала неизвестно: для рейтинга принят базовый балл 2 из 6 до поправки на время пути."
                )
        elif criterion == "commute":
            minutes = route_minutes(facts)
            if (
                minutes is None
                or _number(minutes.value) is None
                or minutes.status in {ValueStatus.UNKNOWN, ValueStatus.ABSENT}
            ):
                detail["confidence"] = "unknown"
            else:
                detail["confidence"] = minutes.status.value
                if minutes.status == ValueStatus.PARTIAL:
                    assumptions.append(
                        "Маршрут использует приблизительные исходные точки; время пригодно для soft ranking."
                    )
        elif criterion == "floor":
            floor, total = facts.fields.get("floor"), facts.fields.get("total_floors")
            floor_number = (
                _number(floor.value)
                if floor is not None and floor.status == ValueStatus.CONFIRMED
                else None
            )
            total_number = (
                _number(total.value)
                if total is not None and total.status == ValueStatus.CONFIRMED
                else None
            )
            if floor_number is not None and floor_number > 1 and total_number is None:
                assumptions.append(
                    "Этажность дома неизвестна: этаж выше первого оценён как промежуточный."
                )
        if assumptions:
            detail["assumptions"] = list(dict.fromkeys(assumptions))
            detail["confidence"] = "partial"
        result[criterion] = detail
    return result


def evaluate_listing(
    facts: ListingFacts,
    *,
    policy: Mapping[str, Any],
    personal_score: float = 0,
    visual_result: Mapping[str, Any] | None = None,
) -> AssessmentResult:
    """Recalculate everything from current inputs; no storage, providers, or reuse."""
    if not isinstance(facts, ListingFacts):
        raise TypeError("evaluate_listing requires ListingFacts")
    facts_to_dict(facts)  # Revalidate mutable provider fields at the domain boundary.
    validate_policy(policy, effective=True)
    measurement_stale = facts.measurement_context != policy["measurement_context"]
    calculation_facts = facts
    if measurement_stale:
        # Reassessment cannot rewrite acquisition identity or promote old evidence.
        calculation_facts = deepcopy(facts)
        for names in _MEASUREMENT_FIELDS.values():
            for name in names:
                field = calculation_facts.fields.get(name)
                if field is not None:
                    field.status = ValueStatus.UNKNOWN
    personal = _number(personal_score)
    if personal is None or not 0 <= personal <= policy["max_scores"]["personal"]:
        raise ValueError("personal score must be inside its configured finite range")
    visual = _accepted_visual(visual_result) if visual_result is not None else None
    if (
        visual is not None
        and policy["vision_scoring_enabled"]
        and visual["model_level"] != policy["vision_contract"]["reasoning_effort"]
    ):
        raise ValueError("accepted visual model_level differs from the policy contract")
    if not policy["vision_scoring_enabled"]:
        visual = None
    scores = score_listing(
        calculation_facts,
        max_scores=policy["max_scores"],
        parameters=policy["parameters"],
        visual_result=visual,
    )
    assessment = build_assessment(
        calculation_facts, scores, policy=policy, visual_result=visual
    )
    for criterion in _MEASUREMENT_FIELDS:
        if criterion in assessment:
            detail = assessment[criterion]
            detail["measurement_stale"] = measurement_stale
            if measurement_stale:
                detail["confidence"] = ValueStatus.UNKNOWN.value
                detail["measurement_stale_reason"] = "measurement_context_changed"
    automatic_max = fsum(
        maximum for name, maximum in policy["max_scores"].items() if name != "personal"
    )
    auto_score = score_total(list(scores.values()), automatic_max)
    total = score_total(
        [*scores.values(), personal], fsum(policy["max_scores"].values())
    )
    if policy["max_scores"]["personal"] > 0:
        scores["personal"] = personal
        assessment["personal"] = {
            "score": personal,
            "evidence": [],
            "confidence": "confirmed",
        }
    assessment["eligibility"] = evaluate_hard_constraints(
        calculation_facts,
        policy["hard_constraints"],
        policy["parameters"],
        visual_result=visual,
    )
    assessment["_policy"] = deepcopy(dict(policy))
    return AssessmentResult(
        scores=scores,
        assessment=assessment,
        auto_score=auto_score,
        total=total,
        personal_score=personal,
        status=score_bucket(auto_score, policy["thresholds"]),
    )


__all__ = ["AssessmentResult", "build_assessment", "evaluate_listing"]
