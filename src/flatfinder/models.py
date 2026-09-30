"""Typed facts and strict serialization at the domain boundary."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ValueStatus(StrEnum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    ABSENT = "absent"


VISION_SCHEMA_VERSION = "vision-owner-v2"
VISION_RUBRIC_VERSION = "vision-owner-v10"
EQUIPMENT_NAMES = frozenset({"furnished", "ac", "dishwasher", "fridge", "washer"})
FACT_FIELD_NAMES = frozenset(
    {
        "title",
        "address",
        "metro_station",
        "location_point",
        "price_monthly",
        "utilities",
        "commission",
        "deposit",
        "move_in_total",
        "area_m2",
        "rooms",
        "floor",
        "total_floors",
        "building_year",
        "layout",
        "repair",
        "furnished",
        "appliances",
        "route",
        "route_minutes",
        "park",
        "noise",
        "fitness",
        "building",
        "entrance",
        "lease_term",
        "move_in_date",
        "restrictions",
        "photos_total",
        "photos_observed",
        "photos",
        "light_view",
    }
)


def _text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{name} must be {'text' if empty else 'non-empty text'}")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")  # noqa: TRY004 - schema violations use ValueError.
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _json_value(value: Any, path: str) -> Any:
    """Copy JSON values without stringification, aliases, or implicit defaults."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)):
        _number(value, path)
        return value
    if isinstance(value, list):
        return [
            _json_value(item, f"{path}[{index}]") for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            _text(key, f"{path} key")
            result[key] = _json_value(item, f"{path}.{key}")
        return result
    raise ValueError(f"{path} must contain only JSON values")


@dataclass(slots=True, frozen=True)
class Evidence:
    source: str
    detail: str
    captured_at: str

    def __post_init__(self) -> None:
        _text(self.source, "evidence source")
        _text(self.detail, "evidence detail", empty=True)
        _text(self.captured_at, "evidence captured_at", empty=True)


@dataclass(slots=True)
class FieldValue:
    value: Any
    status: ValueStatus
    evidence: list[Evidence] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.status, ValueStatus):
            raise TypeError("field status must be ValueStatus")
        if not isinstance(self.evidence, list) or any(
            not isinstance(item, Evidence) for item in self.evidence
        ):
            raise ValueError("field evidence must be a list of Evidence")
        _json_value(self.value, "field value")


@dataclass(slots=True)
class ListingFacts:
    source_listing_id: str
    source_url: str
    fields: dict[str, FieldValue]
    source: str
    measurement_context: str | None = None

    def __post_init__(self) -> None:
        _text(self.source, "listing source")
        _text(self.source_listing_id, "source listing id")
        _text(self.source_url, "source URL")
        if self.measurement_context is not None and (
            not isinstance(self.measurement_context, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.measurement_context)
        ):
            raise ValueError(
                "facts measurement_context must be a lowercase SHA256 digest or None"
            )
        if not isinstance(self.fields, dict):
            raise TypeError("listing fields must be a dict of FieldValue")
        for name, value in self.fields.items():
            if name not in FACT_FIELD_NAMES:
                raise ValueError(f"unknown canonical field: {name!r}")
            if not isinstance(value, FieldValue):
                raise TypeError(f"canonical field {name!r} must be FieldValue")


@dataclass(slots=True, frozen=True)
class PhotoInput:
    listing_id: int
    image_index: int
    source_url: str
    local_path: str | None = None
    sha256: str | None = None
    dhash: str | None = None
    status: str = "indexed"
    error: str | None = None
    raw_source_url: str | None = None
    duplicate_of_index: int | None = None


def facts_to_dict(facts: ListingFacts) -> dict[str, Any]:
    """Encode only the canonical typed model, preserving provenance verbatim."""
    if not isinstance(facts, ListingFacts):
        raise TypeError("facts_to_dict requires ListingFacts")
    facts.__post_init__()
    fields: dict[str, Any] = {}
    for name, value in facts.fields.items():
        value.__post_init__()
        fields[name] = {
            "value": _json_value(value.value, f"fields.{name}.value"),
            "status": value.status.value,
            "evidence": [
                {
                    "source": item.source,
                    "detail": item.detail,
                    "captured_at": item.captured_at,
                }
                for item in value.evidence
            ],
        }
    return {
        "source": facts.source,
        "source_listing_id": facts.source_listing_id,
        "source_url": facts.source_url,
        "fields": fields,
        "measurement_context": facts.measurement_context,
    }


def facts_from_dict(payload: Mapping[str, Any]) -> ListingFacts:
    """Decode exact canonical keys; malformed persisted facts fail visibly."""
    required = {
        "source",
        "source_listing_id",
        "source_url",
        "fields",
        "measurement_context",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ValueError("listing facts keys are invalid")
    raw_fields = payload["fields"]
    if not isinstance(raw_fields, Mapping):
        raise TypeError("listing facts fields must be an object")
    fields = {}
    for name, value in raw_fields.items():
        if name not in FACT_FIELD_NAMES:
            raise ValueError(f"unknown canonical field: {name!r}")
        if not isinstance(value, Mapping) or set(value) != {
            "value",
            "status",
            "evidence",
        }:
            raise ValueError(f"canonical field {name!r} keys are invalid")
        try:
            status = ValueStatus(value["status"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"canonical field {name!r} status is invalid") from exc
        raw_evidence = value["evidence"]
        if not isinstance(raw_evidence, list):
            raise TypeError(f"canonical field {name!r} evidence must be an array")
        evidence = []
        for item in raw_evidence:
            if not isinstance(item, Mapping) or set(item) != {
                "source",
                "detail",
                "captured_at",
            }:
                raise ValueError(f"canonical field {name!r} evidence keys are invalid")
            evidence.append(
                Evidence(item["source"], item["detail"], item["captured_at"])
            )
        fields[name] = FieldValue(
            _json_value(value["value"], f"fields.{name}.value"), status, evidence
        )
    return ListingFacts(
        payload["source_listing_id"],
        payload["source_url"],
        fields,
        payload["source"],
        payload["measurement_context"],
    )


def _image_indices(value: Any, name: str) -> list[int]:
    # Check element types before hashing: lists/dicts must produce a domain error.
    if not isinstance(value, (list, tuple)) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in value
    ):
        raise ValueError(f"{name} must contain nonnegative integer image indices")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} image indices must be unique")
    return list(value)


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
    status = value["status"]
    if not isinstance(status, str) or status not in {"scoreable", "unknown"}:
        raise ValueError(f"{name} status is invalid")
    score = value["score"]
    if status == "scoreable":
        score = _number(score, f"{name} score")
        if not 0 <= score <= maximum:
            raise ValueError(f"{name} score must be inside [0,{maximum:g}]")
    elif score is not None:
        raise ValueError(f"unknown {name} must have score=null")
    indices = _image_indices(value["evidence_indices"], f"{name} evidence_indices")
    if any(item not in allowed for item in indices):
        raise ValueError(f"{name} evidence_indices are outside the current photo set")
    if status == "scoreable" and not indices:
        raise ValueError(f"scoreable {name} requires evidence_indices")
    unknowns = value["unknowns"]
    if not isinstance(unknowns, list) or any(
        not isinstance(item, str) or not item.strip() or len(item.strip()) > 160
        for item in unknowns
    ):
        raise ValueError(f"{name} unknowns are invalid")
    summary = _text(value["summary"], f"{name} summary").strip()
    if len(summary) > 600:
        raise ValueError(f"{name} summary must contain 1..600 characters")
    result = {
        "status": status,
        "score": score,
        "evidence_indices": indices,
        "unknowns": [item.strip() for item in unknowns],
        "summary": summary,
    }
    if repair:
        interval = value["interval"]
        if not isinstance(interval, list) or len(interval) != 2:
            raise ValueError("repair interval must contain two numbers")
        low, high = (_number(item, "repair interval") for item in interval)
        if not 0 <= low <= high <= maximum or (
            score is not None and not low <= score <= high
        ):
            raise ValueError("repair interval is invalid")
        worst_zone = value["worst_zone"]
        if worst_zone is not None:
            worst_zone = _text(worst_zone, "repair worst_zone").strip()
        result.update(interval=[low, high], worst_zone=worst_zone)
    return result


def validate_visual_payload(value: Any, allowed_image_indices: Any) -> dict[str, Any]:
    """Validate the sole owner photo rubric, including a valid all-unknown result."""
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
    if (
        value["schema_version"] != VISION_SCHEMA_VERSION
        or value["rubric_version"] != VISION_RUBRIC_VERSION
    ):
        raise ValueError("visual schema/rubric version is unsupported")
    level = value["model_level"]
    if not isinstance(level, str) or level not in {
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise ValueError("visual model level is invalid")
    allowed = set(_image_indices(allowed_image_indices, "allowed_image_indices"))
    return {
        "schema_version": VISION_SCHEMA_VERSION,
        "rubric_version": VISION_RUBRIC_VERSION,
        "model_level": level,
        "repair": _visual_component(
            value["repair"], "repair", 16, allowed, repair=True
        ),
        "layout": _visual_component(value["layout"], "layout", 3, allowed),
        "light_view": _visual_component(value["light_view"], "light_view", 2, allowed),
    }
