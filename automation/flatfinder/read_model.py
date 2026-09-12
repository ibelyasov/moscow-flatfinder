"""Batch SQLite read model shared by JSON export and Streamlit."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any

from .scoring import (
    estimated_monthly_total,
    normalized_max_scores,
    score_maxima,
    score_park,
)
from .scoring_policy import criterion_metadata
from .vision_contract import VisionContractLike
from .vision_contract import vision_contract as _vision_contract


def _json(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _copy_object(raw: Any) -> dict[str, Any]:
    value = _json(raw, {})
    if not isinstance(value, Mapping):
        return {}
    return json.loads(json.dumps(value, ensure_ascii=False))


def _value(raw: Any) -> Any:
    return raw.get("value") if isinstance(raw, Mapping) and "value" in raw else raw


def _facts_value(facts: Mapping[str, Any], name: str, *aliases: str) -> Any:
    fields = facts.get("fields")
    if isinstance(fields, Mapping):
        for key in (name, *aliases):
            if key in fields:
                return _value(fields[key])
    return None


def _facts_value_and_status(
    facts: Mapping[str, Any], name: str, *aliases: str
) -> tuple[Any, str]:
    fields = facts.get("fields")
    if not isinstance(fields, Mapping):
        return None, "unknown"
    for key in (name, *aliases):
        if key not in fields:
            continue
        raw = fields[key]
        if isinstance(raw, Mapping) and "value" in raw:
            return raw["value"], str(raw.get("status", "unknown")).lower()
        return raw, "unknown"
    return None, "unknown"


def _finite_measurement(value: Any, lower: float, upper: float) -> bool:
    return (
        isinstance(value, Real)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and lower <= float(value) <= upper
    )


def _complete_park_observation(value: Any, status: str) -> bool:
    if status != "confirmed" or not isinstance(value, Mapping):
        return False
    coordinates = value.get("coordinates")
    return (
        bool(str(value.get("name") or "").strip())
        and bool(str(value.get("route_provider") or "").strip())
        and isinstance(coordinates, Mapping)
        and _finite_measurement(coordinates.get("lat"), -90, 90)
        and _finite_measurement(coordinates.get("lon"), -180, 180)
        and _finite_measurement(value.get("walking_minutes"), 0, 1440)
    )


def _photo_list(facts: Mapping[str, Any]) -> list[Any]:
    value = _facts_value(facts, "photos", "images", "photo_urls")
    return list(value) if isinstance(value, list) else []


def _required_source(raw: Any, context: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{context} requires a non-empty source")
    return raw.strip()


def _criteria(
    max_scores: Mapping[str, float] | None,
    parameters: Mapping[str, float] | None,
) -> dict[str, dict[str, Any]]:
    metadata = criterion_metadata(parameters)
    return {
        name: {**metadata[name], "max": maximum}
        for name, maximum in normalized_max_scores(max_scores).items()
        if maximum > 0
    }


def _rubric(
    max_scores: Mapping[str, float] | None,
    parameters: Mapping[str, float] | None,
) -> dict[str, Any]:
    criteria = _criteria(max_scores, parameters)
    automatic_max, personal_max, total_max = score_maxima(max_scores)
    result = json.loads(json.dumps(criteria, ensure_ascii=False))
    result.update(
        {
            "version": 3,
            "automatic_max": automatic_max,
            "personal_max": personal_max,
            "total_max": total_max,
            "criteria": criteria,
            "parameters": dict(parameters or {}),
        }
    )
    return result


def _unknowns(assessment: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for name in normalized_max_scores():
        detail = assessment.get(name)
        if not isinstance(detail, Mapping):
            result.append(name)
            continue
        confidence = str(detail.get("confidence", "unknown")).lower()
        if confidence in {"unknown", "partial", "absent"} or not detail.get("evidence"):
            result.append(name)
    return result


def _placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _group_rows(
    conn: sqlite3.Connection, query: str, ids: Sequence[int], key: str = "listing_id"
) -> dict[int, list[sqlite3.Row]]:
    if not ids:
        return {}
    grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(query.format(ids=_placeholders(ids)), tuple(ids)):
        grouped[int(row[key])].append(row)
    return dict(grouped)


def _latest_payloads(
    conn: sqlite3.Connection, table: str, ids: Sequence[int], predicate: str
) -> dict[int, dict[str, Any]]:
    if table not in {"commute_checks", "fitness_checks"}:
        raise ValueError("unsupported read-model history table")
    rows = _group_rows(
        conn,
        f"""
        SELECT listing_id, id, payload_json, created_at FROM (
          SELECT listing_id, id, payload_json, created_at,
                 ROW_NUMBER() OVER (PARTITION BY listing_id ORDER BY id DESC) AS rank
          FROM {table} WHERE listing_id IN ({{ids}}) AND {predicate}
        ) WHERE rank = 1
        """,
        ids,
    )
    result: dict[int, dict[str, Any]] = {}
    for listing_id, values in rows.items():
        row = values[0]
        payload = _json(row["payload_json"], None)
        if not isinstance(payload, dict):
            raise ValueError(  # noqa: TRY004 - persisted JSON contract violation
                f"{table} check {row['id']} payload is not an object"
            )
        payload["id"] = int(row["id"])
        payload.setdefault("captured_at", str(row["created_at"]))
        result[listing_id] = payload
    return result


def _policy_view(
    assessment: Mapping[str, Any],
    current_policy: Mapping[str, Any] | None,
    fallback_max_scores: Mapping[str, float] | None,
    fallback_parameters: Mapping[str, float] | None,
) -> dict[str, Any]:
    raw_saved = assessment.get("_policy")
    saved = dict(raw_saved) if isinstance(raw_saved, Mapping) else None
    saved_fingerprint = (
        str(saved.get("fingerprint"))
        if saved is not None and saved.get("fingerprint")
        else None
    )
    current_fingerprint = (
        str(current_policy.get("fingerprint"))
        if isinstance(current_policy, Mapping) and current_policy.get("fingerprint")
        else None
    )
    known = saved_fingerprint is not None
    if not known:
        reason = "legacy_policy_unknown"
    elif current_fingerprint is None:
        reason = "current_policy_unknown"
    elif saved_fingerprint != current_fingerprint:
        reason = "policy_changed"
    else:
        reason = None
    saved_max_scores = saved.get("max_scores") if saved is not None else None
    saved_parameters = saved.get("parameters") if saved is not None else None
    display_max_scores = (
        saved_max_scores
        if isinstance(saved_max_scores, Mapping)
        else fallback_max_scores
    )
    display_parameters = (
        saved_parameters
        if isinstance(saved_parameters, Mapping)
        else fallback_parameters
    )
    return {
        "known": known,
        "stale": reason is not None,
        "reason": reason,
        "saved_fingerprint": saved_fingerprint,
        "current_fingerprint": current_fingerprint,
        "max_scores": display_max_scores,
        "parameters": display_parameters,
    }


def _load_related(
    conn: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    contract_value: VisionContractLike | None,
) -> dict[str, Any]:
    ids = [int(row["listing_id"]) for row in rows]
    snapshot_ids = [
        int(row["snapshot_id"]) for row in rows if row["snapshot_id"] is not None
    ]
    evidence_by_snapshot = _group_rows(
        conn,
        """SELECT snapshot_id AS listing_id, field_name, source_kind, detail,
                  confidence, captured_at, id
           FROM evidence WHERE snapshot_id IN ({ids}) ORDER BY id""",
        snapshot_ids,
    )
    photos = _group_rows(
        conn,
        """SELECT listing_id, source_url, sha256, dhash, role, retained, id
           FROM photos WHERE listing_id IN ({ids}) ORDER BY id""",
        ids,
    )
    ingestion = _group_rows(
        conn,
        """SELECT listing_id, image_index, source_url, raw_source_url, sha256,
                  dhash, duplicate_of, status, error
           FROM photo_ingestion WHERE listing_id IN ({ids}) ORDER BY image_index""",
        ids,
    )
    full_text_rows = _group_rows(
        conn,
        """SELECT listing_id, text, quotes_json, content_sha256, captured_at
           FROM full_text WHERE listing_id IN ({ids})""",
        ids,
    )
    vision_runs = _group_rows(
        conn,
        """SELECT listing_id, id, content_hash, provider, model_name, model_version,
                  reasoning_effort, prompt_version, status, schema_valid, retry_count,
                  visual_coverage, error, started_at, finished_at
           FROM vision_runs WHERE listing_id IN ({ids}) ORDER BY id""",
        ids,
    )
    proposals = _group_rows(
        conn,
        """SELECT listing_id, id, vision_run_id, pass_name, criterion, value_json,
                  confidence, review_status, result_status, model_name, model_version,
                  prompt_version, image_indices_json, text_quotes_json, evidence_json,
                  conflicts_json, review_category, review_reason, reviewed_at,
                  created_at, updated_at
           FROM vision_proposals WHERE listing_id IN ({ids}) ORDER BY id""",
        ids,
    )
    canonical_ids = sorted(
        {int(row["canonical_listing_id"] or row["listing_id"]) for row in rows}
    )
    offers = _group_rows(
        conn,
        """SELECT COALESCE(d.canonical_listing_id, l.id) AS listing_id,
                  l.id AS offer_listing_id, l.source, l.source_listing_id, l.source_url
           FROM listings AS l
           LEFT JOIN listing_duplicates AS d ON d.listing_id = l.id
           WHERE COALESCE(d.canonical_listing_id, l.id) IN ({ids})
           ORDER BY l.id""",
        canonical_ids,
    )
    commute = _latest_payloads(conn, "commute_checks", ids, "provider = 'yandex_maps'")
    fitness = _latest_payloads(
        conn,
        "fitness_checks",
        ids,
        "json_extract(payload_json, '$.route_provider') = 'yandex_maps'",
    )
    contract = _vision_contract(contract_value)
    content_hashes = {
        int(row["listing_id"]): row["vision_content_hash"] for row in rows
    }
    current_runs: dict[int, int] = {}
    latest_contract_runs: dict[int, sqlite3.Row] = {}
    for listing_id, run_rows in vision_runs.items():
        for run in run_rows:
            matches = (
                run["provider"] == contract.provider
                and run["model_name"] == contract.model_name
                and run["model_version"] == contract.model_name
                and run["reasoning_effort"] == contract.reasoning_effort
                and run["prompt_version"] == contract.prompt_version
                and content_hashes.get(listing_id) is not None
                and run["content_hash"] == content_hashes.get(listing_id)
            )
            if matches:
                latest_contract_runs[listing_id] = run
                if run["status"] == "success" and int(run["schema_valid"] or 0) == 1:
                    current_runs[listing_id] = int(run["id"])
    manual_counts: dict[int, int] = {}
    for listing_id in ids:
        current_run_id = current_runs.get(listing_id)
        pending = sum(
            1
            for proposal in proposals.get(listing_id, [])
            if int(proposal["vision_run_id"]) == current_run_id
            and proposal["review_status"] == "pending"
            and proposal["model_name"] == contract.model_name
            and proposal["model_version"] == contract.model_name
            and proposal["prompt_version"] == contract.prompt_version
        )
        latest = latest_contract_runs.get(listing_id)
        failed = int(
            latest is not None
            and (latest["status"] == "failed" or int(latest["schema_valid"] or 0) == 0)
        )
        manual_counts[listing_id] = pending + failed
    return {
        "evidence": evidence_by_snapshot,
        "photos": photos,
        "ingestion": ingestion,
        "full_text": full_text_rows,
        "vision_runs": vision_runs,
        "proposals": proposals,
        "offers": offers,
        "commute": commute,
        "fitness": fitness,
        "current_runs": current_runs,
        "manual_counts": manual_counts,
    }


def _listing_payload(
    row: sqlite3.Row,
    related: Mapping[str, Any],
    current_policy: Mapping[str, Any] | None,
    max_scores: Mapping[str, float] | None,
    scoring_parameters: Mapping[str, float] | None,
) -> dict[str, Any]:
    facts = _copy_object(row["facts_json"])
    assessment = _copy_object(row["assessment_json"])
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
    photo_rows = related["photos"].get(listing_id, [])
    photos = [
        {
            "source_url": str(item["source_url"]),
            "sha256": item["sha256"],
            "dhash": item["dhash"],
            "role": item["role"],
            "retained": bool(item["retained"]),
        }
        for item in photo_rows
    ]
    photo_urls = [photo["source_url"] for photo in photos]
    if not photo_urls:
        photo_urls = [
            str(value)
            for item in _photo_list(facts)
            for value in [item if isinstance(item, str) else item.get("source_url")]
            if value
        ]
    ingestion = [
        {
            "image_index": int(item["image_index"]),
            "source_url": str(item["source_url"]),
            "raw_source_url": item["raw_source_url"],
            "sha256": item["sha256"],
            "dhash": item["dhash"],
            "duplicate_of": item["duplicate_of"],
            "status": str(item["status"]),
            "error": item["error"],
        }
        for item in related["ingestion"].get(listing_id, [])
    ]
    full_rows = related["full_text"].get(listing_id, [])
    full_text = None
    if full_rows:
        item = full_rows[0]
        full_text = {
            "text": str(item["text"]),
            "quotes": _json(item["quotes_json"], []),
            "content_sha256": str(item["content_sha256"]),
            "captured_at": str(item["captured_at"]),
        }
    vision_runs = [
        {
            "id": int(item["id"]),
            "provider": str(item["provider"]),
            "model_name": str(item["model_name"]),
            "model_version": str(item["model_version"]),
            "reasoning_effort": str(item["reasoning_effort"]),
            "prompt_version": str(item["prompt_version"]),
            "status": str(item["status"]),
            "schema_valid": bool(item["schema_valid"]),
            "retry_count": int(item["retry_count"]),
            "visual_coverage": float(item["visual_coverage"] or 0),
            "error": item["error"],
            "started_at": str(item["started_at"]),
            "finished_at": item["finished_at"],
        }
        for item in related["vision_runs"].get(listing_id, [])
    ]
    current_run = related["current_runs"].get(listing_id)
    proposals = [
        {
            "id": int(item["id"]),
            "vision_run_id": int(item["vision_run_id"]),
            "is_current": current_run == int(item["vision_run_id"]),
            "pass_name": str(item["pass_name"]),
            "criterion": str(item["criterion"]),
            "value": _json(item["value_json"], None),
            "confidence": float(item["confidence"]),
            "review_status": str(item["review_status"]),
            "result_status": str(item["result_status"]),
            "model_name": str(item["model_name"]),
            "model_version": str(item["model_version"]),
            "prompt_version": str(item["prompt_version"]),
            "image_indices": _json(item["image_indices_json"], []),
            "text_quotes": _json(item["text_quotes_json"], []),
            "evidence": _json(item["evidence_json"], []),
            "conflicts": _json(item["conflicts_json"], []),
            "review_category": item["review_category"],
            "review_reason": item["review_reason"],
            "reviewed_at": item["reviewed_at"],
            "created_at": str(item["created_at"]),
            "updated_at": str(item["updated_at"]),
        }
        for item in related["proposals"].get(listing_id, [])
    ]
    commute_payload = related["commute"].get(listing_id)
    commute_keys = (
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
    commute = (
        None
        if commute_payload is None
        else {key: commute_payload.get(key) for key in commute_keys}
    )
    fitness_payload = related["fitness"].get(listing_id)
    fitness_keys = (
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
    fitness = (
        None
        if fitness_payload is None
        else {key: fitness_payload.get(key) for key in fitness_keys}
    )
    park_value, park_field_status = _facts_value_and_status(facts, "park")
    park_is_complete = _complete_park_observation(park_value, park_field_status)
    park_coordinates = (
        park_value.get("coordinates") if isinstance(park_value, Mapping) else {}
    )
    park = (
        None
        if not isinstance(park_value, Mapping)
        else {
            "provider": park_value.get("provider") or "unknown",
            "status": "success" if park_is_complete else "unknown",
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
    policy_view = _policy_view(
        assessment, current_policy, max_scores, scoring_parameters
    )
    display_rubric = _rubric(policy_view["max_scores"], policy_view["parameters"])
    price_monthly = _facts_value(facts, "price_monthly", "price")
    canonical_id = int(row["canonical_listing_id"] or listing_id)
    evidence = (
        [
            {
                "field_name": str(item["field_name"]),
                "source_kind": str(item["source_kind"]),
                "detail": str(item["detail"]),
                "confidence": str(item["confidence"]),
                "captured_at": str(item["captured_at"]),
            }
            for item in related["evidence"].get(int(row["snapshot_id"]), [])
        ]
        if row["snapshot_id"] is not None
        else []
    )
    personal_rated_at = row["personal_rated_at"] or None
    disliked_at = row["disliked_at"] or None
    favorited_at = row["favorited_at"] or None
    field_values = (
        facts.get("fields") if isinstance(facts.get("fields"), Mapping) else {}
    )
    return {
        "listing_id": listing_id,
        "state": str(row["state"] or "active"),
        "id": f"{prefix}-{source_id}",
        "source": source,
        "source_listing_id": source_id,
        "source_url": str(row["source_url"]),
        "source_offers": [
            {
                "listing_id": int(item["offer_listing_id"]),
                "source": _required_source(item["source"], "source offer"),
                "source_listing_id": str(item["source_listing_id"]),
                "source_url": str(item["source_url"]),
            }
            for item in related["offers"].get(canonical_id, [])
        ],
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
            policy_view["parameters"],
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
        "is_new": personal_rated_at is None
        and disliked_at is None
        and favorited_at is None,
        "facts": facts,
        "field_values": field_values,
        "assessment": assessment,
        "assessment_stale": policy_view["stale"],
        "assessment_stale_reason": policy_view["reason"],
        "assessment_policy_known": policy_view["known"],
        "assessment_policy_fingerprint": policy_view["saved_fingerprint"],
        "current_policy_fingerprint": policy_view["current_fingerprint"],
        "rubric": display_rubric,
        "eligibility_status": str(
            assessment.get("eligibility", {}).get("status", "eligible")
        )
        if isinstance(assessment.get("eligibility"), Mapping)
        else "eligible",
        "confidence": {
            name: str(assessment[name].get("confidence", "unknown"))
            for name in normalized_max_scores()
            if isinstance(assessment.get(name), Mapping)
        },
        "evidence": evidence,
        "photos": photos,
        "photo_urls": photo_urls,
        "photo_ingestion": ingestion,
        "full_text": full_text,
        "vision_runs": vision_runs,
        "vision_proposals": proposals,
        "vision_content_hash": row["vision_content_hash"],
        "visual_coverage": float(row["visual_coverage"] or 0),
        "manual_review_count": related["manual_counts"].get(listing_id, 0),
        "commute": commute,
        "park": park,
        "fitness": fitness,
        "average_commute_minutes": commute.get("average_minutes")
        if commute and commute.get("status") == "success"
        else None,
        "contact_sheet": _facts_value(facts, "contact_sheet", "contact_sheet_path"),
        "auto_score": float(row["auto_score"] or 0),
        "personal_score": float(row["personal_score"] or 0),
        "total_score": float(row["total_score"] or 0),
        "completeness": float(row["completeness"] or 0),
        "fact_coverage": float(row["fact_coverage"] or row["completeness"] or 0),
        "status": str(row["status"] or "unknown"),
        "unknowns": _unknowns(assessment),
        "updated_at": str(row["assessment_updated_at"] or row["last_seen_at"] or ""),
    }


def dashboard_payload(
    conn: sqlite3.Connection,
    listing_id: int | None = None,
    *,
    include_inactive: bool = False,
    policy: Mapping[str, Any] | None = None,
    max_scores: Mapping[str, float] | None = None,
    scoring_parameters: Mapping[str, float] | None = None,
    vision_contract: VisionContractLike | None = None,
) -> dict[str, Any]:
    """Build the shared, read-only JSON/Streamlit view in a bounded query count."""

    rows = conn.execute(
        """
        SELECT l.id AS listing_id, l.source, l.source_listing_id, l.source_url, l.state,
               l.first_seen_at, l.last_seen_at, l.inactive_at, s.id AS snapshot_id,
               s.captured_at, s.facts_json, a.auto_score, a.personal_score,
               a.total_score, a.completeness, a.fact_coverage, a.visual_coverage,
               a.status, a.assessment_json, a.personal_rated_at, a.disliked_at,
               a.favorited_at, a.updated_at AS assessment_updated_at,
               l.vision_content_hash, d.canonical_listing_id,
               d.method AS duplicate_method, d.confidence AS duplicate_confidence,
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
    current_max_scores = (
        policy.get("max_scores")
        if isinstance(policy, Mapping) and isinstance(policy.get("max_scores"), Mapping)
        else max_scores
    )
    current_parameters = (
        policy.get("parameters")
        if isinstance(policy, Mapping) and isinstance(policy.get("parameters"), Mapping)
        else scoring_parameters
    )
    related = _load_related(conn, rows, vision_contract)
    listings = [
        _listing_payload(row, related, policy, current_max_scores, current_parameters)
        for row in rows
    ]
    freshness = {
        "fresh": sum(not item["assessment_stale"] for item in listings),
        "stale": sum(item["assessment_stale"] for item in listings),
        "legacy_unknown": sum(
            item["assessment_stale_reason"] == "legacy_policy_unknown"
            for item in listings
        ),
        "current_unknown": sum(
            item["assessment_stale_reason"] == "current_policy_unknown"
            for item in listings
        ),
    }
    return {
        "version": 7,
        "rubric": _rubric(current_max_scores, current_parameters),
        "current_policy_fingerprint": str(policy.get("fingerprint"))
        if isinstance(policy, Mapping) and policy.get("fingerprint")
        else None,
        "assessment_freshness": freshness,
        "updated_at": max(
            (item.get("updated_at", "") for item in listings), default=""
        ),
        "manual_review_count": sum(
            int(item.get("manual_review_count", 0)) for item in listings
        ),
        "listings": listings,
    }


__all__ = ["dashboard_payload"]
