"""Canonical projections shared by UI and deterministic JSON export."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from .database import Database
from .models import facts_to_dict
from .photos import photo_input_hash
from .scoring import route_minutes
from .scoring_policy import criterion_metadata, validate_policy
from .vision_contract import VisionContract


def _rubric(policy: Mapping[str, Any]) -> dict[str, Any]:
    metadata = criterion_metadata(policy["parameters"])
    maxima = policy["max_scores"]
    criteria = {
        name: {**metadata[name], "max": maximum}
        for name, maximum in maxima.items()
        if maximum > 0
    }
    personal = float(maxima["personal"])
    automatic = sum(
        float(maximum) for name, maximum in maxima.items() if name != "personal"
    )
    return {
        "criteria": criteria,
        "automatic_max": automatic,
        "personal_max": personal,
        "total_max": automatic + personal,
        "parameters": dict(policy["parameters"]),
    }


def _vision_view(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {**run, "contract": run["contract"].to_dict()}


def _listing_view(
    database: Database,
    row: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    detail: bool,
) -> dict[str, Any]:
    listing_id = row["id"]
    facts = facts_to_dict(row["facts"])
    saved_policy = row["assessment"]["_policy"]
    policy_stale = saved_policy["fingerprint"] != policy["fingerprint"]
    measurement_stale = facts["measurement_context"] != policy["measurement_context"]
    uses_measurements = (
        any(
            policy["max_scores"][name] > 0
            for name in ("commute", "park", "fitness", "noise")
        )
        or "max_commute_minutes" in policy["hard_constraints"]
    )
    stale = policy_stale or (measurement_stale and uses_measurements)
    photos = database.photos(listing_id)
    current = accepted = pending = None
    raw_contract = policy["vision_contract"]
    if raw_contract is not None:
        contract = VisionContract.from_dict(raw_contract)
        current = database.current_vision(listing_id, contract)
        accepted = database.accepted_vision(listing_id, contract)
        pending = database.pending_vision(listing_id, contract)
    latest = database.latest_vision(listing_id)
    commute = route_minutes(row["facts"])
    price_details = row["assessment"].get("price", {}).get("details", {})
    item = {
        "id": listing_id,
        "source": row["source"],
        "source_listing_id": row["source_listing_id"],
        "source_url": row["source_url"],
        "availability": row["availability"],
        "in_search": row["in_search"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "captured_at": row["captured_at"],
        "current_observation_id": row["current_observation_id"],
        "fields": facts["fields"],
        "assessment": row["assessment"],
        "scores": row["scores"],
        "auto_score": row["auto_score"],
        "personal_score": row["personal_score"],
        "total_score": row["total_score"],
        "status": row["status"],
        "eligibility_status": row["assessment"]["eligibility"]["status"],
        "favorite": row["favorite"],
        "disliked": row["disliked"],
        "personal_rated_at": row["personal_rated_at"],
        "favorited_at": row["favorited_at"],
        "disliked_at": row["disliked_at"],
        "is_new": not any(
            row[name] for name in ("personal_rated_at", "favorited_at", "disliked_at")
        ),
        "estimated_monthly_total": price_details.get("estimated_monthly_total"),
        "average_commute_minutes": commute.value
        if commute is not None and not measurement_stale
        else None,
        "measurement_stale": measurement_stale,
        "facts_measurement_context": facts["measurement_context"],
        "saved_measurement_context": saved_policy["measurement_context"],
        "current_measurement_context": policy["measurement_context"],
        "assessment_stale": stale,
        "assessment_stale_reason": "measurement_context_changed"
        if measurement_stale
        else "policy_changed"
        if policy_stale
        else None,
        "saved_policy": saved_policy,
        "rubric": _rubric(saved_policy),
        "updated_at": max(
            row[name] or ""
            for name in (
                "updated_at",
                "last_seen_at",
                "personal_rated_at",
                "favorited_at",
                "disliked_at",
            )
        ),
        "photos": [asdict(photo) for photo in photos],
        "photo_input_hash": photo_input_hash(photos),
        "accepted_vision_id": row["accepted_vision_id"],
        "vision": {
            "current": _vision_view(current),
            "accepted": _vision_view(accepted),
            "pending": _vision_view(pending),
            "latest": _vision_view(latest),
        },
        "manual_review_count": int(pending is not None),
        "duplicate_links": database.duplicate_links(listing_id),
    }
    if detail:
        item["observation_history"] = [
            {
                **{key: value for key, value in observation.items() if key != "facts"},
                "fields": facts_to_dict(observation["facts"])["fields"],
                "measurement_context": observation["facts"].measurement_context,
            }
            for observation in database.observation_history(listing_id)
        ]
        item["assessment_history"] = database.assessment_history(listing_id)
        item["checks"] = database.checks(listing_id)
        item["technical_archive"] = database.archive_history(listing_id)
    return item


def dashboard_payload(
    database: Database,
    policy: Mapping[str, Any],
    listing_id: int | None = None,
    *,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Project one read snapshot; invalid persisted JSON is a visible error."""
    validate_policy(policy, effective=True)
    with database.read_snapshot():
        if listing_id is None:
            rows = database.listings(include_inactive=include_inactive)
        else:
            row = database.listing(listing_id)
            rows = [row] if row is not None else []
        items = [
            _listing_view(database, row, policy, detail=listing_id is not None)
            for row in rows
        ]
        fresh = sum(not item["assessment_stale"] for item in items)
        stale = len(items) - fresh
        return {
            "version": 8,
            "rubric": _rubric(policy),
            "current_policy_fingerprint": policy["fingerprint"],
            "assessment_freshness": {"fresh": fresh, "stale": stale},
            "scores_comparable": stale == 0,
            "updated_at": max((item["updated_at"] for item in items), default=""),
            "manual_review_count": sum(item["manual_review_count"] for item in items),
            "searches": database.searches(),
            "listings": items,
        }


__all__ = ["dashboard_payload"]
