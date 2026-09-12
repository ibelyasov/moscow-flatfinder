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

from .assessment import evaluate_listing, facts_model, normalize_facts
from .models import FieldValue, ListingFacts, ValueStatus
from .queries import assessment_row, latest_facts_row, validated_vision_proposal_rows
from .scoring import _decode_proposal, apply_validated_vision
from .storage import persist_enrichment_bundle, visual_score_input_hash
from .vision_contract import (
    PRODUCTION_PASS_CRITERIA,
)
from .vision_contract import vision_contract as resolve_vision_contract

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


def _latest_facts(
    conn: sqlite3.Connection, listing_id: int
) -> tuple[dict[str, Any], int]:
    row = latest_facts_row(conn, listing_id)
    if row is None:
        raise ValueError(f"listing {listing_id} has no facts snapshot")
    try:
        return normalize_facts(json.loads(row[1]), source=str(row[2])), int(row[0])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"listing {listing_id} has invalid facts snapshot") from exc


def _existing_assessment(
    conn: sqlite3.Connection, listing_id: int
) -> tuple[dict[str, Any], float, float, float, str]:
    row = assessment_row(conn, listing_id)
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

    provider, model_name, reasoning_effort, prompt_version = resolve_vision_contract(
        vision_contract
    ).as_tuple()
    proposals = validated_vision_proposal_rows(
        conn,
        listing_id,
        (provider, model_name, reasoning_effort, prompt_version),
    )
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
    model = facts_model(normalized)
    if not proposals:
        return model
    return apply_validated_vision(model, proposals)


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
    model = facts_model(normalized)
    if vision_scoring_enabled:
        model = _apply_validated_proposals(
            conn, listing_id, normalized, vision_contract
        )
    result = evaluate_listing(
        model,
        previous,
        personal,
        max_scores=max_scores,
        parameters=parameters,
        thresholds=thresholds,
        hard_constraints=hard_constraints,
        visual_hash=visual_score_input_hash(conn, listing_id, vision_contract),
        vision_scoring_enabled=vision_scoring_enabled,
        vision_contract=vision_contract,
    )
    return (
        normalized,
        result.assessment,
        result.auto_score,
        result.total,
        result.personal_score,
        float(completeness),
        result.status,
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
