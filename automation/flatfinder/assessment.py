"""Pure listing assessment: facts in, one complete decision bundle out."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from typing import Any

from .models import (
    VISION_SCHEMA_VERSION,
    Evidence,
    FieldValue,
    ListingFacts,
    ValueStatus,
)
from .scoring import (
    CRITERION_INPUT_FIELDS,
    criterion_input_hashes,
    evaluate_hard_constraints,
    reuse_unchanged_criteria,
    score_bucket,
    score_listing,
    score_maxima,
    score_total,
)
from .scoring_policy import normalize_policy
from .vision_contract import VisionContractLike

_CRITERION_FIELDS = CRITERION_INPUT_FIELDS
_VISUAL_COMPONENT = {
    "repair": "repair",
    "visual_layout": "layout",
    "light_view": "light_view",
}


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    scores: dict[str, float]
    assessment: dict[str, Any]
    auto_score: float
    total: float
    personal_score: float
    status: str


def _status(value: Any, default: str = ValueStatus.UNKNOWN.value) -> str:
    raw = getattr(value, "value", value)
    text = str(raw)
    return text if text in {item.value for item in ValueStatus} else default


def _evidence_list(
    value: Any,
    source: str = "snapshot",
    fallback_confidence: str = ValueStatus.UNKNOWN.value,
) -> list[dict[str, str]]:
    values = value if isinstance(value, list) else [value] if value not in (None, "") else []
    result: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, Evidence):
            result.append(
                {
                    "source": item.source,
                    "detail": item.detail,
                    "captured_at": item.captured_at,
                    "confidence": fallback_confidence,
                }
            )
        elif isinstance(item, Mapping):
            result.append(
                {
                    "source": str(item.get("source", source)),
                    "detail": str(item.get("detail", item)),
                    "captured_at": str(item.get("captured_at", "")),
                    "confidence": _status(item.get("confidence"), fallback_confidence),
                }
            )
        else:
            result.append(
                {"source": source, "detail": str(item), "captured_at": "", "confidence": fallback_confidence}
            )
    return result


def normalize_facts(
    payload: Mapping[str, Any] | ListingFacts,
    source_listing_id: str = "",
    source_url: str = "",
    *,
    source: str | None = None,
) -> dict[str, Any]:
    """Normalize canonical snapshot facts while preserving source provenance."""

    if isinstance(payload, ListingFacts):
        fields = {
            str(name): {
                "value": deepcopy(field.value),
                "status": _status(field.status),
                "evidence": _evidence_list(field.evidence),
            }
            for name, field in payload.fields.items()
        }
        data: dict[str, Any] = {
            "source": payload.source,
            "source_listing_id": payload.source_listing_id,
            "source_url": payload.source_url,
            "fields": fields,
        }
    elif isinstance(payload, Mapping) and isinstance(payload.get("fields"), Mapping):
        data = deepcopy(dict(payload))
    else:
        raise ValueError("canonical facts require a fields object")

    payload_source = data.get("source")
    explicit_source = source.strip() if isinstance(source, str) and source.strip() else None
    if source is not None and explicit_source is None:
        raise ValueError("canonical facts source must be non-empty text")
    if payload_source in (None, ""):
        source_value = explicit_source
    elif isinstance(payload_source, str) and payload_source.strip():
        source_value = payload_source.strip()
        if explicit_source is not None and source_value != explicit_source:
            raise ValueError("canonical facts source conflicts with its context")
    else:
        raise ValueError("canonical facts source must be non-empty text")
    if source_value is None:
        raise ValueError("canonical facts require a non-empty source")

    fields: dict[str, dict[str, Any]] = {}
    for name, raw in data["fields"].items():
        if isinstance(raw, FieldValue):
            raw = {"value": raw.value, "status": raw.status, "evidence": raw.evidence}
        if not isinstance(raw, Mapping):
            raise ValueError(f"canonical field {name!r} must be an object")
        field = deepcopy(dict(raw))
        field["status"] = _status(
            field.get("status"),
            ValueStatus.CONFIRMED.value if field.get("value") is not None else ValueStatus.UNKNOWN.value,
        )
        field["evidence"] = _evidence_list(field.get("evidence"))
        fields[str(name)] = field
    result = {key: deepcopy(value) for key, value in data.items() if key not in {"fields", "source_listing_id", "source_url"}}
    result.update(
        source=source_value,
        source_listing_id=str(data.get("source_listing_id") or source_listing_id),
        source_url=str(data.get("source_url") or source_url),
        fields=fields,
    )
    route = fields.get("route")
    if "route_minutes" not in fields and isinstance(route, Mapping):
        route_value = route.get("value")
        if isinstance(route_value, Mapping) and route_value.get("minutes") is not None:
            fields["route_minutes"] = {
                "value": route_value["minutes"],
                "status": route.get("status", ValueStatus.CONFIRMED.value),
                "evidence": deepcopy(route.get("evidence", [])),
            }
    return result


def facts_model(
    payload: Mapping[str, Any] | ListingFacts,
    source_listing_id: str = "",
    source_url: str = "",
    *,
    source: str | None = None,
) -> ListingFacts:
    """Convert canonical snapshot facts to the scoring model."""

    if isinstance(payload, ListingFacts) and source is None and not source_listing_id and not source_url:
        return payload
    normalized = normalize_facts(payload, source_listing_id, source_url, source=source)
    fields: dict[str, FieldValue] = {}
    for name, raw in normalized["fields"].items():
        state = _status(raw.get("status"))
        evidence = [
            Evidence(item["source"], item["detail"], item["captured_at"])
            for item in _evidence_list(raw.get("evidence"))
        ]
        fields[name] = FieldValue(deepcopy(raw.get("value")), ValueStatus(state), evidence)
    return ListingFacts(
        str(normalized.get("source_listing_id", "")),
        str(normalized.get("source_url", "")),
        fields,
        str(normalized["source"]),
    )


def _field_evidence(field: FieldValue, *, nested: bool = False) -> list[dict[str, str]]:
    result = [
        {"source": item.source, "detail": item.detail, "captured_at": item.captured_at, "confidence": _status(field.status)}
        for item in field.evidence
    ]
    if nested and isinstance(field.value, Mapping):
        for child in field.value.values():
            if isinstance(child, FieldValue):
                result.extend(_field_evidence(child))
            elif isinstance(child, Mapping):
                child_status = _status(child.get("status"))
                for item in _evidence_list(child.get("evidence")):
                    item["confidence"] = child_status
                    result.append(item)
    return result


def build_assessment(
    facts: ListingFacts,
    scores: Mapping[str, float],
) -> dict[str, Any]:
    """Build the one structured criterion evidence contract."""

    result: dict[str, Any] = {}
    for criterion, score in scores.items():
        if criterion == "personal":
            continue
        evidence: list[dict[str, str]] = []
        states: list[str] = []
        for field_name in _CRITERION_FIELDS.get(criterion, ()):
            field = facts.fields.get(field_name)
            if not isinstance(field, FieldValue):
                continue
            component_name = _VISUAL_COMPONENT.get(criterion)
            if component_name and not (
                isinstance(field.value, Mapping)
                and field.value.get("schema_version") == VISION_SCHEMA_VERSION
                and isinstance(field.value.get(component_name), Mapping)
            ):
                continue
            field_evidence = _field_evidence(field, nested=criterion == "equipment")
            if component_name and not any(
                item["source"].startswith("vision:") for item in field_evidence
            ):
                continue
            states.append(_status(field.status))
            evidence.extend(field_evidence)
        confidence = (
            ValueStatus.CONFIRMED.value
            if states and all(state == ValueStatus.CONFIRMED.value for state in states)
            else ValueStatus.UNKNOWN.value
            if states
            and all(
                state in {ValueStatus.UNKNOWN.value, ValueStatus.ABSENT.value}
                for state in states
            )
            else ValueStatus.PARTIAL.value
            if evidence or any(state == ValueStatus.PARTIAL.value for state in states)
            else ValueStatus.UNKNOWN.value
        )
        detail: dict[str, Any] = {
            "score": float(score),
            "evidence": evidence,
            "confidence": confidence,
        }
        component_name = _VISUAL_COMPONENT.get(criterion)
        field_name = _CRITERION_FIELDS.get(criterion, (None,))[0]
        visual_field = facts.fields.get(field_name) if field_name else None
        payload = visual_field.value if isinstance(visual_field, FieldValue) else None
        if component_name and isinstance(payload, Mapping) and payload.get("schema_version") == VISION_SCHEMA_VERSION:
            component = payload.get(component_name)
            if isinstance(component, Mapping):
                detail["details"] = deepcopy(dict(component))
        result[criterion] = detail
    return result


def evaluate_listing(
    facts: ListingFacts,
    previous: Mapping[str, Any],
    personal_score: float,
    *,
    max_scores: Mapping[str, float] | None = None,
    parameters: Mapping[str, float] | None = None,
    thresholds: Mapping[str, float] | None = None,
    hard_constraints: Mapping[str, Any] | None = None,
    visual_hash: str | None = None,
    vision_scoring_enabled: bool = False,
    vision_contract: VisionContractLike | None = None,
) -> AssessmentResult:
    """Evaluate facts using one explicit policy, without I/O."""

    if not isinstance(facts, ListingFacts):
        raise TypeError("evaluate_listing requires ListingFacts")
    previous = previous if isinstance(previous, Mapping) else {}
    policy = normalize_policy(
        max_scores=max_scores,
        parameters=parameters,
        thresholds=thresholds,
        hard_constraints=hard_constraints,
        vision_scoring_enabled=vision_scoring_enabled,
        vision_contract=vision_contract,
    )
    scores = score_listing(
        facts,
        {},
        max_scores=policy["max_scores"],
        parameters=policy["parameters"],
    )
    assessment = build_assessment(facts, scores)
    assessment["eligibility"] = evaluate_hard_constraints(
        facts, policy["hard_constraints"], policy["parameters"]
    )
    scores, assessment = reuse_unchanged_criteria(
        scores,
        assessment,
        previous,
        criterion_input_hashes(
            facts,
            visual_hash=visual_hash,
            max_scores=policy["max_scores"],
            parameters=policy["parameters"],
        ),
        max_scores=policy["max_scores"],
    )
    for criterion in scores:
        if criterion == "personal":
            continue
        detail = assessment.get(criterion)
        if not isinstance(detail, Mapping):
            continue
        normalized_detail = deepcopy(dict(detail))
        confidence = _status(normalized_detail.get("confidence"))
        evidence = _evidence_list(
            normalized_detail.get("evidence"), fallback_confidence=confidence
        )
        normalized_detail["evidence"] = evidence
        normalized_detail["confidence"] = confidence
        assessment[criterion] = normalized_detail
    previous_personal = previous.get("personal")
    if isinstance(previous_personal, Mapping):
        personal_detail = deepcopy(dict(previous_personal))
        personal_detail["score"] = float(personal_score)
        assessment["personal"] = personal_detail
    elif policy["max_scores"].get("personal", 0) > 0:
        assessment["personal"] = {
            "score": float(personal_score),
            "evidence": [],
            "confidence": ValueStatus.UNKNOWN.value,
        }
    assessment["_policy"] = deepcopy(policy)
    auto_score = sum(value for name, value in scores.items() if name != "personal")
    automatic_max, _personal_max, _ = score_maxima(policy["max_scores"])
    if not isfinite(float(personal_score)) or not 0 <= float(personal_score):
        raise ValueError("personal score must be a finite number >= 0")
    total = score_total(list(scores.values()), automatic_max) + float(personal_score)
    return AssessmentResult(
        scores=dict(scores),
        assessment=assessment,
        auto_score=float(auto_score),
        total=float(total),
        personal_score=float(personal_score),
        status=score_bucket(auto_score, policy["thresholds"]),
    )


__all__ = ["AssessmentResult", "build_assessment", "evaluate_listing", "facts_model", "normalize_facts"]
