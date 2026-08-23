"""Visible-page top-N environment enrichment and score persistence."""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

from .models import (
    VISION_SCHEMA_VERSION,
    Evidence,
    FieldValue,
    ListingFacts,
    ValueStatus,
)
from .scoring import (
    _decode_proposal,
    apply_validated_vision,
    criterion_input_hashes,
    evaluate_hard_constraints,
    reuse_unchanged_criteria,
    score_bucket,
    score_listing,
    score_maxima,
    score_total,
)
from .storage import persist_enrichment_bundle, visual_score_input_hash

_VALID_STATUS = {item.value for item in ValueStatus}
_BLOCKER_RE = re.compile(
    r"(?:captcha|капч|robot|робот|access\s+denied|доступ\s+ограничен|sign\s*in|войти|2fa)",
    re.IGNORECASE,
)
_RISK_PATTERNS = {
    "highway": r"(?:магистраль|шоссе|трасс|автомагистраль|highway|motorway)",
    "railway": r"(?:железн(?:ая|ой|ую)|ж\.?д\.?|railway|railroad)",
    "stadium": r"(?:стадион|arena|stadium)",
    "construction": r"(?:стройк|ремонт дороги|construction)",
    "nightlife": r"(?:ночн(?:ой|ые)|клуб|бар|night ?club|late[- ]night)",
}
_CRITERION_FIELDS = {
    "noise": ("noise",),
    "park": ("park",),
    "equipment": (
        "appliances",
        "equipment",
        "furnished",
        "bed",
        "fridge",
        "washer",
        "ac",
        "dishwasher",
    ),
    "repair": ("repair",),
    "price": ("price_monthly", "price", "commission", "utilities"),
    "commute": ("route_minutes", "route"),
    "area": ("area_m2",),
    "visual_layout": ("layout",),
    "floor": ("floor", "total_floors"),
    "light_view": ("light_view",),
    "building": ("building_year",),
    "fitness": ("fitness",),
}
_BASE_CRITERION_FIELDS = {
    **_CRITERION_FIELDS,
    "equipment": ("appliances", "equipment"),
    "floor": ("floor", "total_floors"),
}


@dataclass(frozen=True, slots=True)
class EnvironmentResult:
    noise_risks: list[str]
    entrance: str | None
    windows: str | None
    evidence: list[Any]
    status: str = ValueStatus.CONFIRMED.value
    field_evidence: Mapping[str, list[Any]] = field(default_factory=dict)


def _value(item: Any, *names: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        for name in names:
            if name in item:
                return item[name]
    keys = getattr(item, "keys", None)
    if callable(keys):
        available = set(keys())
        for name in names:
            if name in available:
                return item[name]
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _number(item: Any, *names: str, default: float = 0.0) -> float:
    try:
        return float(_value(item, *names, default=default))
    except (TypeError, ValueError, OverflowError):
        return default


def _status(value: Any, default: str = ValueStatus.UNKNOWN.value) -> str:
    raw = getattr(value, "value", value)
    raw = str(raw)
    return raw if raw in _VALID_STATUS else default


def _evidence_list(value: Any, source: str = "snapshot") -> list[dict[str, Any]]:
    values = (
        value if isinstance(value, list) else [value] if value not in (None, "") else []
    )
    result: list[dict[str, Any]] = []
    for item in values:
        if isinstance(item, Mapping):
            result.append(
                {
                    "source": str(item.get("source", source)),
                    "detail": str(item.get("detail", item)),
                    "captured_at": str(item.get("captured_at", "")),
                    **(
                        {"confidence": str(item["confidence"])}
                        if item.get("confidence") is not None
                        else {}
                    ),
                }
            )
        else:
            result.append({"source": source, "detail": str(item), "captured_at": ""})
    return result


def normalize_facts(
    payload: Mapping[str, Any],
    source_listing_id: str = "",
    source_url: str = "",
    *,
    source: str | None = None,
) -> dict[str, Any]:
    """Normalize the current canonical snapshot contract.

    ``source`` is part of the trust boundary: old snapshots without it must
    fail closed instead of being silently classified as Yandex listings.
    Callers that already know the source may pass it explicitly while loading
    a legacy-shaped payload; a conflicting payload value is still rejected.
    """
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("fields"), Mapping
    ):
        raise ValueError("canonical facts require a fields object")
    data = json.loads(json.dumps(dict(payload), ensure_ascii=False))
    payload_source = data.get("source")
    explicit_source = None
    if source is not None:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("canonical facts source must be non-empty text")
        explicit_source = source.strip()
    if payload_source in (None, ""):
        source_value = explicit_source
    else:
        if not isinstance(payload_source, str) or not payload_source.strip():
            raise ValueError("canonical facts source must be non-empty text")
        source_value = payload_source.strip()
        if explicit_source is not None and source_value != explicit_source:
            raise ValueError("canonical facts source conflicts with its context")
    if source_value is None:
        raise ValueError("canonical facts require a non-empty source")
    raw_fields = data["fields"]
    fields: dict[str, dict[str, Any]] = {}
    for name, raw in raw_fields.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"canonical field {name!r} must be an object")
        field_value = dict(raw)
        field_value["status"] = _status(
            field_value.get("status"),
            ValueStatus.CONFIRMED.value
            if field_value.get("value") is not None
            else ValueStatus.UNKNOWN.value,
        )
        field_value["evidence"] = _evidence_list(
            field_value.get("evidence"), "snapshot"
        )
        fields[str(name)] = field_value
    result = {
        key: value
        for key, value in data.items()
        if key not in {"fields", "source_listing_id", "source_url"}
    }
    result["source_listing_id"] = str(
        data.get("source_listing_id") or source_listing_id
    )
    result["source_url"] = str(data.get("source_url") or source_url)
    result["source"] = source_value
    result["fields"] = fields
    route = fields.get("route")
    if "route_minutes" not in fields and isinstance(route, Mapping):
        route_value = route.get("value")
        if isinstance(route_value, Mapping) and route_value.get("minutes") is not None:
            fields["route_minutes"] = {
                "value": route_value["minutes"],
                "status": route.get("status", ValueStatus.CONFIRMED.value),
                "evidence": list(route.get("evidence", [])),
            }
    return result


def _priority(item: Any) -> int:
    bucket = str(_value(item, "priority", "bucket", "tier", default="reserve")).lower()
    return {"priority": 0, "good": 1, "reserve": 2}.get(bucket, 3)


def _published(item: Any) -> float:
    raw = _value(
        item,
        "published_timestamp",
        "published_at",
        "publication_time",
        "created_at",
        default="",
    )
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return (
            parsed.replace(tzinfo=timezone.utc).timestamp()
            if parsed.tzinfo is None
            else parsed.timestamp()
        )
    except (TypeError, ValueError, OverflowError):
        return 0.0


def select_top_candidates(
    candidates: Iterable[Any], limit: int = 10, top_n: int | None = None
) -> list[Any]:
    if top_n is not None:
        limit = top_n
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return []
    if limit <= 0:
        return []
    rows = list(candidates)
    rows.sort(
        key=lambda item: (
            _priority(item),
            -_number(item, "auto_score", "score", "total_score"),
            -_number(item, "completeness", "coverage"),
            -_published(item),
            str(
                _value(
                    item,
                    "source_listing_id",
                    "listing_id",
                    "id",
                    "source_url",
                    "url",
                    default="",
                )
            ),
        )
    )
    return rows[:limit]


def _facts(item: Any) -> dict[str, Any]:
    raw = _value(item, "facts", default=item if isinstance(item, Mapping) else {})
    source_id = str(_value(item, "source_listing_id", default=""))
    source_url = str(_value(item, "source_url", default=""))
    source = _value(item, "source", default=None)
    return normalize_facts(
        raw if isinstance(raw, Mapping) else {},
        source_id,
        source_url,
        source=source if isinstance(source, str) else None,
    )


def _field(item: Any, name: str) -> Any:
    fields = _facts(item).get("fields", {})
    raw = fields.get(name) if isinstance(fields, Mapping) else None
    return raw.get("value") if isinstance(raw, Mapping) else raw


def _address(item: Any) -> str:
    value = _value(item, "address", "normalized_address", "location", default=None)
    if value is None:
        value = _field(item, "address")
    if isinstance(value, Mapping):
        value = value.get("value") or value.get("text") or value.get("full")
    return str(value or "").strip()


def _listing_text(item: Any) -> str:
    pieces: list[str] = []
    for name in ("location_facts", "location", "address", "description", "text"):
        value = _value(item, name, default=None)
        if value is not None:
            pieces.extend(str(part) for part in value.values()) if isinstance(
                value, Mapping
            ) else pieces.append(str(value))
    fields = _facts(item).get("fields", {})
    if isinstance(fields, Mapping):
        for name in (
            "address",
            "park",
            "noise",
            "fitness",
            "sauna",
            "entrance",
            "windows",
            "description",
        ):
            value = fields.get(name)
            value = value.get("value") if isinstance(value, Mapping) else value
            if value is not None:
                pieces.append(str(value))
    return " ".join(pieces)


async def _page_text(page: Any) -> str:
    locator = getattr(page, "locator", None)
    if callable(locator):
        inner_text = getattr(locator("body"), "inner_text", None)
        if callable(inner_text):
            value = inner_text()
            return str(await value if inspect.isawaitable(value) else value)
    return str(getattr(page, "body_text", ""))


async def _visible_page(page: Any, url: str) -> tuple[str, dict[str, Any]]:
    goto = getattr(page, "goto", None)
    if not callable(goto):
        return "", {"url": url, "error": "page.goto is unavailable"}
    try:
        try:
            result = goto(url, wait_until="domcontentloaded")
        except TypeError:
            result = goto(url)
        if inspect.isawaitable(result):
            await result
        text = await _page_text(page)
    except Exception as exc:
        return "", {"url": url, "error": str(exc)}
    return text, {"url": url, "excerpt": text[:500]}


def _blocked(text: str, page: Any) -> bool:
    return bool(
        _BLOCKER_RE.search(text) or _BLOCKER_RE.search(str(getattr(page, "url", "")))
    )


def _risks(text: str) -> list[str]:
    return [
        name
        for name, pattern in _RISK_PATTERNS.items()
        if re.search(pattern, text, re.IGNORECASE)
    ]


async def enrich_environment(page: Any, listing: Any) -> EnvironmentResult:
    card_text = _listing_text(listing)
    card_evidence = (
        [
            {
                "source": "listing_card",
                "detail": card_text[:500],
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "confidence": ValueStatus.CONFIRMED.value,
            }
        ]
        if card_text
        else []
    )
    risks = _risks(card_text)
    entrance = (
        "confirmed"
        if re.search(r"(?:подъезд|вход|entrance)", card_text, re.IGNORECASE)
        else None
    )
    windows = (
        "confirmed"
        if re.search(r"(?:окн|window|вид из окна)", card_text, re.IGNORECASE)
        else None
    )
    evidence = list(card_evidence)
    location = _address(listing)
    map_text = ""
    if location:
        map_text, map_evidence = await _visible_page(
            page, f"https://yandex.ru/maps/?text={quote_plus(location)}"
        )
        if map_evidence.get("error") or _blocked(map_text, page):
            evidence.append(
                {
                    "source": "yandex_maps_visible",
                    "detail": json.dumps(map_evidence, ensure_ascii=False),
                    "captured_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "confidence": ValueStatus.UNKNOWN.value,
                }
            )
            field_evidence = {
                name: list(card_evidence) for name in ("noise", "entrance", "windows")
            }
            return EnvironmentResult(
                risks,
                entrance,
                windows,
                evidence,
                ValueStatus.UNKNOWN.value,
                field_evidence,
            )
        risks.extend(risk for risk in _risks(map_text) if risk not in risks)
        map_item = {
            "source": "yandex_maps_visible",
            "detail": map_text[:500],
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "confidence": ValueStatus.CONFIRMED.value,
        }
        evidence.append(map_item)
    field_evidence = {
        "noise": list(evidence),
        "entrance": list(card_evidence if entrance else []),
        "windows": list(card_evidence if windows else []),
    }
    return EnvironmentResult(
        risks, entrance, windows, evidence, ValueStatus.CONFIRMED.value, field_evidence
    )


def _field_status(raw: Any, default: str = ValueStatus.UNKNOWN.value) -> str:
    if isinstance(raw, FieldValue):
        return _status(raw.status, default)
    if isinstance(raw, Mapping):
        return _status(raw.get("status"), default)
    return _status(getattr(raw, "status", default), default)


def _field_evidence(
    raw: Any, fallback_confidence: str = ValueStatus.UNKNOWN.value
) -> list[dict[str, Any]]:
    if isinstance(raw, FieldValue):
        confidence = _field_status(raw, fallback_confidence)
        return [
            {
                "source": str(getattr(item, "source", "")),
                "detail": str(getattr(item, "detail", item)),
                "captured_at": str(getattr(item, "captured_at", "")),
                "confidence": confidence,
            }
            for item in (raw.evidence or ())
        ]
    if not isinstance(raw, Mapping):
        return []
    confidence = _field_status(raw, fallback_confidence)
    result = _evidence_list(raw.get("evidence"), "snapshot")
    for item in result:
        item.setdefault("confidence", confidence)
    return result


def apply_enrichment(
    facts: Mapping[str, Any], environment: EnvironmentResult | None = None
) -> dict[str, Any]:
    result = normalize_facts(facts)
    fields = result["fields"]
    if environment is not None:
        for name, value in {
            "entrance": environment.entrance,
            "windows": environment.windows,
        }.items():
            if (
                value in (None, "unknown", [])
                and _field_status(fields.get(name)) == ValueStatus.CONFIRMED.value
            ):
                continue
            field_evidence = environment.field_evidence.get(name, environment.evidence)
            field_status = (
                ValueStatus.CONFIRMED.value
                if value not in (None, "unknown", []) and field_evidence
                else ValueStatus.UNKNOWN.value
            )
            fields[name] = {
                "value": value,
                "status": field_status,
                "evidence": _evidence_list(field_evidence, "environment"),
            }
        fields["environment"] = {
            "value": {
                "noise_risks": environment.noise_risks,
                "entrance": environment.entrance,
                "windows": environment.windows,
            },
            "status": _status(environment.status),
            "evidence": _evidence_list(environment.evidence, "environment"),
        }
    return result


def _facts_model(payload: Mapping[str, Any]) -> ListingFacts:
    normalized = normalize_facts(payload)
    fields: dict[str, FieldValue] = {}
    for name, raw in normalized["fields"].items():
        value = raw.get("value") if isinstance(raw, Mapping) else raw
        state = _status(raw.get("status") if isinstance(raw, Mapping) else None)
        evidence = []
        for item in _field_evidence(raw, state):
            evidence.append(
                Evidence(
                    str(item.get("source", "snapshot")),
                    str(item.get("detail", "")),
                    str(item.get("captured_at", "")),
                )
            )
        fields[str(name)] = FieldValue(value, ValueStatus(state), evidence)
    return ListingFacts(
        str(normalized.get("source_listing_id", "")),
        str(normalized.get("source_url", "")),
        fields,
        str(normalized["source"]),
    )


def _latest_facts(
    conn: sqlite3.Connection, listing_id: int
) -> tuple[dict[str, Any], int]:
    row = conn.execute(
        """
        SELECT s.id, s.facts_json, l.source
        FROM listing_snapshots AS s
        JOIN listings AS l ON l.id = s.listing_id
        WHERE s.listing_id = ?
        ORDER BY s.id DESC LIMIT 1
        """,
        (int(listing_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"listing {listing_id} has no facts snapshot")
    try:
        return normalize_facts(json.loads(row[1]), source=str(row[2])), int(row[0])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"listing {listing_id} has invalid facts snapshot") from exc


def _existing_assessment(
    conn: sqlite3.Connection, listing_id: int
) -> tuple[dict[str, Any], float, float, float, str]:
    row = conn.execute(
        "SELECT assessment_json, personal_score, completeness, total_score, status FROM assessments WHERE listing_id = ?",
        (int(listing_id),),
    ).fetchone()
    if row is None:
        return {}, 0.0, 0.0, 0.0, "reserve"
    try:
        assessment = json.loads(row[0]) if row[0] else {}
    except (TypeError, ValueError):
        assessment = {}
    return (
        assessment if isinstance(assessment, dict) else {},
        float(row[1] or 0),
        float(row[2] or 0),
        float(row[3] or 0),
        str(row[4] or "reserve"),
    )


def _apply_validated_proposals(
    conn: sqlite3.Connection,
    listing_id: int,
    normalized: dict[str, Any],
    vision_contract: tuple[str, str, str, str] | None = None,
) -> ListingFacts:
    """Feed the current validated Vision assessment into scoring."""

    from .storage import _current_vision_contract
    from .vision import PRODUCTION_PASS_CRITERIA

    provider, model_name, reasoning_effort, prompt_version = (
        vision_contract or _current_vision_contract()
    )
    proposals = conn.execute(
        """
        WITH current_runs AS (
          SELECT vr.id,
                 ROW_NUMBER() OVER (ORDER BY vr.id DESC) AS run_rank
          FROM vision_runs AS vr
          JOIN listings AS l ON l.id = vr.listing_id
          WHERE vr.listing_id = ?
            AND vr.status = 'success'
            AND vr.schema_valid = 1
            AND vr.provider = ?
            AND vr.model_name = ?
            AND vr.model_version = ?
            AND vr.reasoning_effort = ?
            AND vr.prompt_version = ?
            AND l.vision_content_hash IS NOT NULL
            AND vr.content_hash = l.vision_content_hash
        )
        SELECT vp.*
        FROM vision_proposals AS vp
        JOIN current_runs AS cr ON cr.id = vp.vision_run_id AND cr.run_rank = 1
        WHERE vp.listing_id = ?
          AND vp.review_status = 'validated'
          AND vp.result_status = 'category'
          AND vp.model_name = ?
          AND vp.model_version = ?
          AND vp.prompt_version = ?
        ORDER BY id
        """,
        (
            int(listing_id),
            provider,
            model_name,
            model_name,
            reasoning_effort,
            prompt_version,
            int(listing_id),
            model_name,
            model_name,
            prompt_version,
        ),
    ).fetchall()
    filtered_proposals: list[dict[str, Any]] = []
    for item in proposals:
        row = dict(item)
        pass_name = str(row.get("pass_name", ""))
        criterion = str(row.get("criterion", ""))
        if (
            pass_name not in PRODUCTION_PASS_CRITERIA
            or criterion not in PRODUCTION_PASS_CRITERIA[pass_name]
            or str(row.get("model_name", "")) != model_name
            or str(row.get("model_version", "")) != model_name
            or str(row.get("prompt_version", "")) != prompt_version
        ):
            continue
        decoded = _decode_proposal(row)
        if decoded is not None:
            filtered_proposals.append(decoded)
    proposals = filtered_proposals
    model = _facts_model(normalized)
    if not proposals:
        return model
    return apply_validated_vision(model, proposals)


def _fact_fields(facts: Mapping[str, Any] | ListingFacts) -> Mapping[str, Any]:
    if isinstance(facts, ListingFacts):
        return facts.fields
    if isinstance(facts, Mapping):
        fields = facts.get("fields", {})
        return fields if isinstance(fields, Mapping) else {}
    return {}


def _assessment_field_evidence(
    raw: Any, *, nested: bool = False
) -> list[dict[str, Any]]:
    evidences = _field_evidence(raw, _field_status(raw))
    if not nested:
        return evidences
    value = (
        raw.value
        if isinstance(raw, FieldValue)
        else raw.get("value")
        if isinstance(raw, Mapping)
        else None
    )
    if isinstance(value, Mapping):
        for child in value.values():
            evidences.extend(_field_evidence(child, _field_status(child)))
    return evidences


def _assessment_for(
    facts: Mapping[str, Any] | ListingFacts,
    scores: dict[str, float],
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    fields = _fact_fields(facts)
    score_view = isinstance(facts, ListingFacts)
    criterion_fields = _CRITERION_FIELDS if score_view else _BASE_CRITERION_FIELDS
    result: dict[str, Any] = {}
    for criterion, score in scores.items():
        if criterion == "personal":
            continue
        evidences: list[dict[str, Any]] = []
        for name in criterion_fields.get(criterion, ()):
            raw = fields.get(name) if isinstance(fields, Mapping) else None
            if raw is not None:
                field_evidence = _assessment_field_evidence(
                    raw, nested=score_view and criterion == "equipment"
                )
                if score_view and criterion == "light_view":
                    if not any(
                        str(item.get("source", "")).startswith("vision:")
                        for item in field_evidence
                    ):
                        continue
                evidences.extend(field_evidence)
        if not evidences and isinstance(previous.get(criterion), Mapping):
            old = previous[criterion]
            evidences = _evidence_list(old.get("evidence"), "previous_assessment")
            old_confidence = _status(old.get("confidence"), ValueStatus.UNKNOWN.value)
        else:
            old_confidence = ""
        confidence_values = {
            str(item.get("confidence", ValueStatus.UNKNOWN.value)) for item in evidences
        }
        confidence = old_confidence or (
            ValueStatus.CONFIRMED.value
            if confidence_values == {ValueStatus.CONFIRMED.value}
            else ValueStatus.UNKNOWN.value
            if confidence_values
            <= {ValueStatus.UNKNOWN.value, ValueStatus.ABSENT.value}
            else ValueStatus.PARTIAL.value
        )
        result[criterion] = {
            "score": float(score),
            "evidence": evidences,
            "confidence": confidence,
        }
        component_name = {
            "repair": "repair",
            "visual_layout": "layout",
            "light_view": "light_view",
        }.get(criterion)
        if component_name and score_view:
            raw = fields.get(criterion)
            payload = raw.value if isinstance(raw, FieldValue) else None
            if (
                isinstance(payload, Mapping)
                and payload.get("schema_version") == VISION_SCHEMA_VERSION
            ):
                result[criterion]["details"] = json.loads(
                    json.dumps(payload[component_name], ensure_ascii=False)
                )
    if isinstance(previous.get("personal"), Mapping):
        result["personal"] = json.loads(
            json.dumps(previous["personal"], ensure_ascii=False)
        )
    return result


def _build_bundle(
    conn: sqlite3.Connection,
    listing_id: int,
    facts: Mapping[str, Any],
    *,
    vision_scoring_enabled: bool = False,
    max_scores: Mapping[str, float] | None = None,
    parameters: Mapping[str, float] | None = None,
    thresholds: Mapping[str, float] | None = None,
    hard_constraints: Mapping[str, Any] | None = None,
    vision_contract: tuple[str, str, str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], float, float, float, float, str]:
    normalized = normalize_facts(facts)
    previous, personal, completeness, _old_total, _old_status = _existing_assessment(
        conn, listing_id
    )
    facts_model = _facts_model(normalized)
    if vision_scoring_enabled:
        facts_model = _apply_validated_proposals(
            conn, listing_id, normalized, vision_contract
        )
    scores = score_listing(
        facts_model, {}, max_scores=max_scores, parameters=parameters
    )
    assessment = _assessment_for(
        facts_model if vision_scoring_enabled else normalized, scores, previous
    )
    assessment["eligibility"] = evaluate_hard_constraints(
        facts_model, hard_constraints, parameters
    )
    scores, assessment = reuse_unchanged_criteria(
        scores,
        assessment,
        previous,
        criterion_input_hashes(
            normalized,
            visual_hash=visual_score_input_hash(conn, listing_id, vision_contract),
            max_scores=max_scores,
            parameters=parameters,
        ),
        max_scores=max_scores,
    )
    auto_score = sum(value for name, value in scores.items() if name != "personal")
    automatic_max, _personal_max, _total_max = score_maxima(max_scores)
    total = score_total(list(scores.values()), automatic_max) + personal
    return (
        normalized,
        assessment,
        float(auto_score),
        float(total),
        float(personal),
        float(completeness),
        score_bucket(auto_score, thresholds),
    )


def persist_enrichment(
    conn: sqlite3.Connection,
    listing_id: int,
    facts: Mapping[str, Any],
    *,
    vision_scoring_enabled: bool = False,
    max_scores: Mapping[str, float] | None = None,
    parameters: Mapping[str, float] | None = None,
    thresholds: Mapping[str, float] | None = None,
    hard_constraints: Mapping[str, Any] | None = None,
    vision_contract: tuple[str, str, str, str] | None = None,
) -> dict[str, Any]:
    normalized, assessment, auto_score, total, personal, completeness, status = (
        _build_bundle(
            conn,
            listing_id,
            facts,
            vision_scoring_enabled=vision_scoring_enabled,
            max_scores=max_scores,
            parameters=parameters,
            thresholds=thresholds,
            hard_constraints=hard_constraints,
            vision_contract=vision_contract,
        )
    )
    snapshot_id, inserted = persist_enrichment_bundle(
        conn,
        listing_id,
        normalized,
        assessment,
        auto_score,
        total,
        personal,
        completeness,
        status,
        append_snapshot=True,
        max_scores=max_scores,
    )
    return {
        "listing_id": int(listing_id),
        "snapshot_id": snapshot_id,
        "inserted": inserted,
        "assessment": assessment,
        "total_score": total,
    }


def recompute_assessment(
    conn: sqlite3.Connection,
    listing_id: int,
    *,
    vision_scoring_enabled: bool = False,
    max_scores: Mapping[str, float] | None = None,
    parameters: Mapping[str, float] | None = None,
    thresholds: Mapping[str, float] | None = None,
    hard_constraints: Mapping[str, Any] | None = None,
    vision_contract: tuple[str, str, str, str] | None = None,
) -> dict[str, Any]:
    facts, _ = _latest_facts(conn, listing_id)
    normalized, assessment, auto_score, total, personal, completeness, status = (
        _build_bundle(
            conn,
            listing_id,
            facts,
            vision_scoring_enabled=vision_scoring_enabled,
            max_scores=max_scores,
            parameters=parameters,
            thresholds=thresholds,
            hard_constraints=hard_constraints,
            vision_contract=vision_contract,
        )
    )
    snapshot_id, _ = persist_enrichment_bundle(
        conn,
        listing_id,
        normalized,
        assessment,
        auto_score,
        total,
        personal,
        completeness,
        status,
        append_snapshot=False,
        max_scores=max_scores,
    )
    return {
        "listing_id": int(listing_id),
        "snapshot_id": snapshot_id,
        "assessment": assessment,
        "total_score": total,
    }


__all__ = [
    "EnvironmentResult",
    "apply_enrichment",
    "enrich_environment",
    "normalize_facts",
    "persist_enrichment",
    "recompute_assessment",
    "select_top_candidates",
]
