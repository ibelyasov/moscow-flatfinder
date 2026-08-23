import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ValueStatus(StrEnum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    ABSENT = "absent"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    VALIDATED = "validated"
    REJECTED = "rejected"


class ResultStatus(StrEnum):
    CATEGORY = "category"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


VISION_SCHEMA_VERSION = "vision-owner-v2"
VISION_RUBRIC_VERSION = "vision-owner-v10"


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _visual_component(
    value: Any,
    name: str,
    maximum: float,
    allowed: set[int],
    *,
    repair: bool = False,
) -> dict[str, Any]:
    required = {"status", "score", "evidence_indices", "unknowns", "summary"}
    if repair:
        required |= {"interval", "worst_zone"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError(f"{name} component keys are invalid")
    status = value.get("status")
    if status not in {"scoreable", "unknown"}:
        raise ValueError(f"{name} status is invalid")
    score = value.get("score")
    if status == "scoreable":
        score = _number(score, f"{name} score")
        if not 0 <= score <= maximum:
            raise ValueError(f"{name} score must be inside [0,{maximum:g}]")
    elif score is not None:
        raise ValueError(f"unknown {name} must have score=null")
    allowed = {
        int(item)
        for item in allowed
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
    }
    indices = value.get("evidence_indices")
    if (
        not isinstance(indices, list)
        or len(indices) != len(set(indices))
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item not in allowed
            for item in indices
        )
    ):
        raise ValueError(f"{name} evidence_indices are invalid")
    if status == "scoreable" and not indices:
        raise ValueError(f"scoreable {name} requires evidence_indices")
    unknowns = value.get("unknowns")
    if not isinstance(unknowns, list) or any(
        not isinstance(item, str) or not item.strip() or len(item.strip()) > 160
        for item in unknowns
    ):
        raise ValueError(f"{name} unknowns are invalid")
    summary = value.get("summary")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary.strip()) > 600
    ):
        raise ValueError(f"{name} summary must contain 1..600 characters")
    result = {
        **dict(value),
        "score": round(score, 1) if score is not None else None,
        "summary": summary.strip(),
        "unknowns": [item.strip() for item in unknowns],
    }
    if repair:
        interval = value.get("interval")
        if not isinstance(interval, list) or len(interval) != 2:
            raise ValueError("repair interval must contain two numbers")
        low, high = (_number(item, "repair interval") for item in interval)
        if not 0 <= low <= high <= maximum or (
            score is not None and not low <= score <= high
        ):
            raise ValueError("repair interval is invalid")
        worst_zone = value.get("worst_zone")
        if worst_zone is not None and (
            not isinstance(worst_zone, str) or not worst_zone.strip()
        ):
            raise ValueError("repair worst_zone must be null or non-empty text")
        result.update(
            interval=[round(low, 1), round(high, 1)],
            worst_zone=worst_zone.strip() if isinstance(worst_zone, str) else None,
        )
    return result


def validate_visual_payload(value: Any, allowed_image_indices: Any) -> dict[str, Any]:
    """Validate the sole Luna multi-criterion photo contract."""

    required = {
        "schema_version",
        "rubric_version",
        "model_level",
        "repair",
        "layout",
        "light_view",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("visual payload keys are invalid")
    if value.get("schema_version") != VISION_SCHEMA_VERSION:
        raise ValueError(
            f"visual payload requires schema_version={VISION_SCHEMA_VERSION!r}"
        )
    model_level = value.get("model_level")
    if value.get("rubric_version") != VISION_RUBRIC_VERSION or model_level not in {
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    }:
        raise ValueError("visual rubric/model level is invalid")
    allowed = {
        int(item)
        for item in allowed_image_indices
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
    }
    result = {
        "schema_version": VISION_SCHEMA_VERSION,
        "rubric_version": VISION_RUBRIC_VERSION,
        "model_level": model_level,
        "repair": _visual_component(
            value.get("repair"), "repair", 16, allowed, repair=True
        ),
        "layout": _visual_component(value.get("layout"), "layout", 3, allowed),
        "light_view": _visual_component(
            value.get("light_view"), "light_view", 2, allowed
        ),
    }
    if not any(
        result[name]["status"] == "scoreable"
        for name in ("repair", "layout", "light_view")
    ):
        raise ValueError("visual payload must score at least one criterion")
    return result


@dataclass(slots=True, frozen=True)
class Evidence:
    source: str
    detail: str
    captured_at: str


@dataclass(slots=True)
class FieldValue:
    value: Any
    status: ValueStatus
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(slots=True)
class ListingFacts:
    source_listing_id: str
    source_url: str
    fields: dict[str, FieldValue]
    source: str | None = None

    def __post_init__(self) -> None:
        """Reject source-less facts before they can cross the model boundary."""

        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("listing facts require a non-empty source")
        self.source = self.source.strip()


@dataclass(slots=True, frozen=True)
class PhotoInput:
    listing_id: int
    image_index: int
    source_url: str
    local_path: str | None = None
    sha256: str | None = None
    dhash: str | None = None
    duplicate_of: int | None = None
    status: str = "indexed"
    error: str | None = None
    raw_source_url: str | None = None
    duplicate_of_index: int | None = None


@dataclass(slots=True, frozen=True)
class FullTextRecord:
    listing_id: int
    text: str
    quotes: list[dict[str, str]]
    captured_at: str
    content_sha256: str


@dataclass(slots=True, frozen=True)
class VisionProposal:
    listing_id: int
    vision_run_id: int
    pass_name: str
    criterion: str
    value: dict[str, Any] | None
    confidence: float
    review_status: ReviewStatus
    result_status: ResultStatus
    model_name: str
    model_version: str
    prompt_version: str
    image_indices: list[int]
    text_quotes: list[str]
    evidence: list[str]
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def proposal_is_scoreable(proposal: Mapping[str, Any]) -> bool:
    if not isinstance(proposal, Mapping):
        return False
    if _enum_value(proposal.get("review_status")) != ReviewStatus.VALIDATED.value:
        return False
    if _enum_value(proposal.get("result_status")) != ResultStatus.CATEGORY.value:
        return False
    confidence = proposal.get("confidence")
    if isinstance(confidence, bool):
        return False
    try:
        confidence = float(confidence)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        return False
    value = proposal.get("value")
    return (
        isinstance(value, Mapping)
        and value.get("schema_version") == VISION_SCHEMA_VERSION
        and any(
            isinstance(value.get(name), Mapping)
            and value[name].get("status") == "scoreable"
            and isinstance(value[name].get("score"), (int, float))
            for name in ("repair", "layout", "light_view")
        )
    )
