"""Deterministic JSON export and dashboard payload for FlatFinder."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .scoring import (
    estimated_monthly_total,
    normalized_max_scores,
    score_maxima,
    score_park,
)
from .storage import (
    current_vision_run_id,
    latest_commute_check,
    latest_fitness_check,
    vision_manual_review_count,
)

_LABELS = {
    "noise": "Тишина",
    "park": "Парк и прогулки",
    "equipment": "Оснащение и мебель",
    "repair": "Ремонт",
    "price": "Полная стоимость",
    "commute": "Дорога",
    "area": "Площадь",
    "visual_layout": "Планировка по фото",
    "floor": "Этаж",
    "light_view": "Свет и вид",
    "building": "Год дома",
    "personal": "Хочу здесь жить",
    "fitness": "Зал с сауной",
}


def _criteria(max_scores: Mapping[str, float] | None) -> dict[str, dict[str, Any]]:
    return {
        name: {"label": _LABELS[name], "max": maximum}
        for name, maximum in normalized_max_scores(max_scores).items()
        if maximum > 0
    }


def _json(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return value


def _value(raw: Any) -> Any:
    if isinstance(raw, Mapping) and "value" in raw:
        return raw["value"]
    return raw


def _required_source(raw: Any, context: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{context} requires a non-empty source")
    return raw.strip()


def _facts_value(facts: Mapping[str, Any], name: str, *aliases: str) -> Any:
    fields = facts.get("fields")
    if isinstance(fields, Mapping):
        for key in (name, *aliases):
            if key in fields:
                return _value(fields[key])
    return None


def _photo_list(facts: Mapping[str, Any]) -> list[Any]:
    value = _facts_value(facts, "photos", "images", "photo_urls")
    return list(value) if isinstance(value, list) else []


def _atomic_write(path: str | Path, content: str) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _evidence(
    conn: sqlite3.Connection, snapshot_id: int | None
) -> list[dict[str, Any]]:
    if snapshot_id is None:
        return []
    rows = conn.execute(
        """
        SELECT field_name, source_kind, detail, confidence, captured_at
        FROM evidence WHERE snapshot_id = ? ORDER BY id
        """,
        (snapshot_id,),
    ).fetchall()
    return [
        {
            "field_name": str(row[0]),
            "source_kind": str(row[1]),
            "detail": str(row[2]),
            "confidence": str(row[3]),
            "captured_at": str(row[4]),
        }
        for row in rows
    ]


def _photos(conn: sqlite3.Connection, listing_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT source_url, sha256, dhash, role, retained
        FROM photos WHERE listing_id = ? ORDER BY id
        """,
        (listing_id,),
    ).fetchall()
    return [
        {
            "source_url": str(row[0]),
            "sha256": row[1],
            "dhash": row[2],
            "role": row[3],
            "retained": bool(row[4]),
        }
        for row in rows
    ]


def _photo_ingestion(conn: sqlite3.Connection, listing_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT image_index, source_url, raw_source_url, sha256, dhash,
               duplicate_of, status, error
        FROM photo_ingestion WHERE listing_id = ? ORDER BY image_index
        """,
        (int(listing_id),),
    ).fetchall()
    return [
        {
            "image_index": int(row[0]),
            "source_url": str(row[1]),
            "raw_source_url": row[2],
            "sha256": row[3],
            "dhash": row[4],
            "duplicate_of": row[5],
            "status": str(row[6]),
            "error": row[7],
        }
        for row in rows
    ]


def _full_text(conn: sqlite3.Connection, listing_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT text, quotes_json, content_sha256, captured_at FROM full_text WHERE listing_id = ?",
        (int(listing_id),),
    ).fetchone()
    if row is None:
        return None
    return {
        "text": str(row[0]),
        "quotes": _json(row[1], []),
        "content_sha256": str(row[2]),
        "captured_at": str(row[3]),
    }


def _vision_runs(conn: sqlite3.Connection, listing_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, provider, model_name, model_version, reasoning_effort,
               prompt_version, status, schema_valid, retry_count, visual_coverage, error,
               started_at, finished_at
        FROM vision_runs WHERE listing_id = ? ORDER BY id
        """,
        (int(listing_id),),
    ).fetchall()
    return [
        {
            "id": int(row[0]),
            "provider": str(row[1]),
            "model_name": str(row[2]),
            "model_version": str(row[3]),
            "reasoning_effort": str(row[4]),
            "prompt_version": str(row[5]),
            "status": str(row[6]),
            "schema_valid": bool(row[7]),
            "retry_count": int(row[8]),
            "visual_coverage": float(row[9] or 0),
            "error": row[10],
            "started_at": str(row[11]),
            "finished_at": row[12],
        }
        for row in rows
    ]


def _vision_proposals(
    conn: sqlite3.Connection,
    listing_id: int,
    vision_contract: tuple[str, str, str, str] | None = None,
) -> list[dict[str, Any]]:
    current_run_id = current_vision_run_id(conn, listing_id, vision_contract)
    rows = conn.execute(
        """
        SELECT id, vision_run_id, pass_name, criterion, value_json,
               confidence, review_status, result_status, model_name,
               model_version, prompt_version, image_indices_json,
               text_quotes_json, evidence_json, conflicts_json,
               review_category, review_reason, reviewed_at,
               created_at, updated_at
        FROM vision_proposals WHERE listing_id = ? ORDER BY id
        """,
        (int(listing_id),),
    ).fetchall()
    return [
        {
            "id": int(row[0]),
            "vision_run_id": int(row[1]),
            "is_current": current_run_id == int(row[1]),
            "pass_name": str(row[2]),
            "criterion": str(row[3]),
            "value": _json(row[4], None),
            "confidence": float(row[5]),
            "review_status": str(row[6]),
            "result_status": str(row[7]),
            "model_name": str(row[8]),
            "model_version": str(row[9]),
            "prompt_version": str(row[10]),
            "image_indices": _json(row[11], []),
            "text_quotes": _json(row[12], []),
            "evidence": _json(row[13], []),
            "conflicts": _json(row[14], []),
            "review_category": row[15],
            "review_reason": row[16],
            "reviewed_at": row[17],
            "created_at": str(row[18]),
            "updated_at": str(row[19]),
        }
        for row in rows
    ]


def _unknowns(assessment: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for name in _LABELS:
        detail = assessment.get(name)
        if not isinstance(detail, Mapping):
            result.append(name)
            continue
        confidence = str(detail.get("confidence", "unknown")).lower()
        if confidence in {"unknown", "partial", "absent"} or not detail.get("evidence"):
            result.append(name)
    return result


def _source_offers(conn: sqlite3.Connection, listing_id: int) -> list[dict[str, Any]]:
    duplicate = conn.execute(
        "SELECT canonical_listing_id FROM listing_duplicates WHERE listing_id = ?",
        (int(listing_id),),
    ).fetchone()
    canonical_id = int(duplicate[0]) if duplicate is not None else int(listing_id)
    rows = conn.execute(
        """
        SELECT id, source, source_listing_id, source_url
        FROM listings
        WHERE id = ? OR id IN (
          SELECT listing_id FROM listing_duplicates WHERE canonical_listing_id = ?
        )
        ORDER BY id
        """,
        (canonical_id, canonical_id),
    ).fetchall()
    return [
        {
            "listing_id": int(row[0]),
            "source": _required_source(row[1], "source offer"),
            "source_listing_id": str(row[2]),
            "source_url": str(row[3]),
        }
        for row in rows
    ]


def _listing_payload(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    vision_contract: tuple[str, str, str, str] | None = None,
    scoring_parameters: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    facts = _json(row["facts_json"], {})
    if not isinstance(facts, Mapping):
        facts = {}
    facts = json.loads(json.dumps(facts, ensure_ascii=False))
    assessment = _json(row["assessment_json"], {})
    if not isinstance(assessment, Mapping):
        assessment = {}
    assessment = json.loads(json.dumps(assessment, ensure_ascii=False))
    listing_id = int(row["listing_id"])
    source = _required_source(row["source"], "listing export")
    source_id = str(row["source_listing_id"])
    raw_facts_source = facts.get("source")
    if raw_facts_source in (None, ""):
        facts["source"] = source
    elif _required_source(raw_facts_source, "listing facts export") != source:
        raise ValueError("listing source and facts source do not match")
    prefix = (
        "yandex"
        if source.lower().startswith("yandex")
        else source.lower().replace(" ", "_")
    )
    photos = _photos(conn, listing_id)
    photo_ingestion = _photo_ingestion(conn, listing_id)
    full_text = _full_text(conn, listing_id)
    vision_runs = _vision_runs(conn, listing_id)
    vision_proposals = _vision_proposals(conn, listing_id, vision_contract)
    photo_urls = [photo["source_url"] for photo in photos]
    if not photo_urls:
        photo_urls = [
            item if isinstance(item, str) else item.get("source_url")
            for item in _photo_list(facts)
            if isinstance(item, (str, Mapping))
        ]
        photo_urls = [str(item) for item in photo_urls if item]
    field_values = (
        facts.get("fields") if isinstance(facts.get("fields"), Mapping) else {}
    )
    contact_sheet = _facts_value(facts, "contact_sheet", "contact_sheet_path")
    personal_rated_at = row["personal_rated_at"] or None
    disliked_at = row["disliked_at"] or None
    favorited_at = row["favorited_at"] or None
    commute_payload = latest_commute_check(conn, listing_id)
    commute = (
        None
        if commute_payload is None
        else {
            key: commute_payload.get(key)
            for key in (
                "id",
                "provider",
                "status",
                "error",
                "service_date",
                "gate_status",
                "captured_at",
                "home_lat",
                "home_lon",
                "point_kind",
                "building_id",
                "entrance_id",
                "geocode_precision",
                "office_lat",
                "office_lon",
                "home_to_work_minutes",
                "work_to_home_minutes",
                "home_to_work_score",
                "work_to_home_score",
                "average_minutes",
                "average_score",
                "commute_score",
            )
        }
    )
    park_value = _facts_value(facts, "park")
    park_coordinates = (
        park_value.get("coordinates") if isinstance(park_value, Mapping) else {}
    )
    park = (
        None
        if not isinstance(park_value, Mapping)
        else {
            "provider": park_value.get("provider") or "unknown",
            "status": "success",
            "place_id": park_value.get("place_id"),
            "place_name": park_value.get("name"),
            "place_type": park_value.get("place_type"),
            "place_lat": park_coordinates.get("lat")
            if isinstance(park_coordinates, Mapping)
            else None,
            "place_lon": park_coordinates.get("lon")
            if isinstance(park_coordinates, Mapping)
            else None,
            "walking_minutes": park_value.get("walking_minutes"),
            "walking_distance_m": park_value.get("walking_distance_m"),
            "park_score": score_park(park_value),
        }
    )
    fitness_payload = latest_fitness_check(conn, listing_id)
    fitness = (
        None
        if fitness_payload is None
        else {
            key: fitness_payload.get(key)
            for key in (
                "id",
                "provider",
                "status",
                "error",
                "captured_at",
                "home_lat",
                "home_lon",
                "route_provider",
                "place_id",
                "place_name",
                "place_lat",
                "place_lon",
                "rating",
                "review_count",
                "sauna",
                "quality",
                "walking_minutes",
                "walking_distance_m",
                "fitness_score",
            )
        }
    )
    price_monthly = _facts_value(facts, "price_monthly", "price")
    result = {
        "listing_id": listing_id,
        "state": str(row["state"] or "active"),
        "id": f"{prefix}-{source_id}",
        "source": source,
        "source_listing_id": source_id,
        "source_url": str(row["source_url"]),
        "source_offers": _source_offers(conn, listing_id),
        "duplicate_of_listing_id": row["canonical_listing_id"],
        "duplicate": None
        if row["canonical_listing_id"] is None
        else {
            "method": str(row["duplicate_method"]),
            "confidence": float(row["duplicate_confidence"]),
            "evidence": _json(row["duplicate_evidence_json"], {}),
        },
        "title": _facts_value(facts, "title") or "",
        "address": _facts_value(facts, "address", "location") or "",
        "metro_station": _facts_value(facts, "metro_station", "metro") or "",
        "location_point": _facts_value(facts, "location_point") or None,
        "price_monthly": price_monthly,
        "estimated_monthly_total": estimated_monthly_total(
            price_monthly,
            _facts_value(facts, "commission"),
            _facts_value(facts, "utilities"),
            scoring_parameters,
        ),
        "area_m2": _facts_value(facts, "area_m2", "area"),
        "rooms": _facts_value(facts, "rooms", "rooms_total"),
        "property_type": _facts_value(facts, "property_type", "type"),
        "captured_at": str(row["captured_at"] or row["last_seen_at"] or ""),
        "first_seen_at": str(row["first_seen_at"] or ""),
        "last_seen_at": str(row["last_seen_at"] or ""),
        "inactive_at": str(row["inactive_at"] or ""),
        "personal_rated_at": personal_rated_at,
        "disliked_at": disliked_at,
        "favorited_at": favorited_at,
        "is_new": (
            personal_rated_at is None and disliked_at is None and favorited_at is None
        ),
        "facts": facts,
        "field_values": field_values,
        "assessment": assessment,
        "eligibility_status": str(
            assessment.get("eligibility", {}).get("status", "eligible")
        )
        if isinstance(assessment.get("eligibility"), Mapping)
        else "eligible",
        "confidence": {
            name: str(detail.get("confidence", "unknown"))
            for name, detail in assessment.items()
            if isinstance(detail, Mapping)
        },
        "evidence": _evidence(conn, row["snapshot_id"]),
        "photos": photos,
        "photo_urls": photo_urls,
        "photo_ingestion": photo_ingestion,
        "full_text": full_text,
        "vision_runs": vision_runs,
        "vision_proposals": vision_proposals,
        "vision_content_hash": row["vision_content_hash"],
        "visual_coverage": float(row["visual_coverage"] or 0),
        "manual_review_count": vision_manual_review_count(
            conn, listing_id, vision_contract
        ),
        "commute": commute,
        "park": park,
        "fitness": fitness,
        "average_commute_minutes": commute.get("average_minutes")
        if commute and commute.get("status") == "success"
        else None,
        "contact_sheet": contact_sheet,
        "auto_score": float(row["auto_score"] or 0),
        "personal_score": float(row["personal_score"] or 0),
        "total_score": float(row["total_score"] or 0),
        "completeness": float(row["completeness"] or 0),
        "fact_coverage": float(row["fact_coverage"] or row["completeness"] or 0),
        "status": str(row["status"] or "unknown"),
        "unknowns": _unknowns(assessment),
        "updated_at": str(row["assessment_updated_at"] or row["last_seen_at"] or ""),
    }
    return result


def dashboard_payload(
    conn: sqlite3.Connection,
    listing_id: int | None = None,
    *,
    include_inactive: bool = False,
    max_scores: Mapping[str, float] | None = None,
    scoring_parameters: Mapping[str, float] | None = None,
    vision_contract: tuple[str, str, str, str] | None = None,
) -> dict[str, Any]:
    """Build the shared JSON/Streamlit view of current listing state."""

    rows = conn.execute(
        """
        SELECT l.id AS listing_id, l.source, l.source_listing_id, l.source_url, l.state,
               l.first_seen_at, l.last_seen_at, l.inactive_at, s.id AS snapshot_id,
               s.captured_at, s.facts_json,
               a.auto_score, a.personal_score, a.total_score, a.completeness,
               a.fact_coverage, a.visual_coverage, a.status, a.assessment_json,
               a.personal_rated_at, a.disliked_at, a.favorited_at,
               a.updated_at AS assessment_updated_at, l.vision_content_hash,
               d.canonical_listing_id, d.method AS duplicate_method,
               d.confidence AS duplicate_confidence,
               d.evidence_json AS duplicate_evidence_json
        FROM listings AS l
        LEFT JOIN listing_snapshots AS s ON s.id = (
          SELECT latest.id FROM listing_snapshots AS latest
          WHERE latest.listing_id = l.id ORDER BY latest.id DESC LIMIT 1
        )
        LEFT JOIN assessments AS a ON a.listing_id = l.id
        LEFT JOIN listing_duplicates AS d ON d.listing_id = l.id
        LEFT JOIN listings AS canonical ON canonical.id = d.canonical_listing_id
        WHERE (? IS NULL OR l.id = ?)
          AND (? OR l.state = 'active')
          AND (? IS NOT NULL OR d.listing_id IS NULL OR canonical.state != 'active')
        ORDER BY l.source, l.source_listing_id
        """,
        (listing_id, listing_id, int(include_inactive), listing_id),
    ).fetchall()
    criteria = _criteria(max_scores)
    automatic_max, personal_max, total_max = score_maxima(max_scores)
    rubric = json.loads(json.dumps(criteria, ensure_ascii=False))
    rubric.update(
        {
            "version": 3,
            "automatic_max": automatic_max,
            "personal_max": personal_max,
            "total_max": total_max,
            "criteria": criteria,
        }
    )
    listings = [
        _listing_payload(conn, row, vision_contract, scoring_parameters) for row in rows
    ]
    return {
        "version": 7,
        "rubric": rubric,
        "updated_at": max(
            (item.get("updated_at", "") for item in listings), default=""
        ),
        "manual_review_count": sum(
            int(item.get("manual_review_count", 0)) for item in listings
        ),
        "listings": listings,
    }


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def export_json(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    max_scores: Mapping[str, float] | None = None,
    scoring_parameters: Mapping[str, float] | None = None,
    vision_contract: tuple[str, str, str, str] | None = None,
) -> dict[str, Any]:
    """Write the SQLite snapshot as a deterministic, atomically replaced JSON file."""

    payload = dashboard_payload(
        conn,
        max_scores=max_scores,
        scoring_parameters=scoring_parameters,
        vision_contract=vision_contract,
    )
    _atomic_write(path, _dump(payload) + "\n")
    return payload


__all__ = ["dashboard_payload", "export_json"]
