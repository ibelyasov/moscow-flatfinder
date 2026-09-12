"""Persisted photo assessment workflow shared by collection and explicit commands."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from . import queries
from .enrich import recompute_assessment
from .models import FullTextRecord, PhotoInput, ResultStatus, ReviewStatus
from .scoring import visual_input_hash
from .storage import (
    create_vision_run,
    finish_vision_run,
    insert_vision_proposals,
    mark_vision_content,
    review_proposal,
)


def vision_content_hash(
    facts: Mapping[str, Any],
    full_text: FullTextRecord | Mapping[str, Any],
    photos: Sequence[PhotoInput],
) -> str:
    """Hash only photo identities; fact changes do not invalidate Vision."""

    del facts, full_text
    return visual_input_hash(photos)


def _photo_inputs(rows: Sequence[Any], listing_id: int) -> list[PhotoInput]:
    return [
        PhotoInput(
            listing_id=int(listing_id),
            image_index=int(row["image_index"] if hasattr(row, "keys") else row[1]),
            source_url=str(row["source_url"] if hasattr(row, "keys") else row[2]),
            local_path=(row["local_path"] if hasattr(row, "keys") else row[3]),
            sha256=(row["sha256"] if hasattr(row, "keys") else row[4]),
            dhash=(row["dhash"] if hasattr(row, "keys") else row[5]),
            duplicate_of=(
                int(row["duplicate_of"])
                if hasattr(row, "keys") and row["duplicate_of"] is not None
                else int(row[6])
                if not hasattr(row, "keys") and row[6] is not None
                else None
            ),
            status=str(row["status"] if hasattr(row, "keys") else row[7]),
            error=(row["error"] if hasattr(row, "keys") else row[8]),
            raw_source_url=(row["raw_source_url"] if hasattr(row, "keys") else row[9]),
        )
        for row in rows
    ]


def run_listing_vision(
    conn: Any,
    runtime: Any,
    listing_id: int,
    *,
    force: bool = False,
    auto_validate: bool = False,
    vision_scoring_enabled: bool = False,
    max_scores: Mapping[str, float] | None = None,
    parameters: Mapping[str, float] | None = None,
    thresholds: Mapping[str, float] | None = None,
    hard_constraints: Mapping[str, Any] | None = None,
) -> Any:
    """Evaluate one listing and optionally apply its validated visual assessment."""

    from .vision import DEFAULT_PROMPT_VERSION, MODEL_NAME, VisionRunResult, run_passes

    listing_id = int(listing_id)
    listing = queries.listing_vision_state(conn, listing_id)
    if listing is None:
        raise ValueError(f"listing {listing_id} does not exist")
    state = listing["state"] if hasattr(listing, "keys") else listing[1]
    if state != "active":
        return VisionRunResult(status="skipped", error="listing is not published")
    inputs = queries.vision_input_rows(conn, listing_id)
    snapshot = inputs.snapshot
    if snapshot is None:
        raise ValueError(f"listing {listing_id} has no facts snapshot")
    try:
        facts = json.loads(
            snapshot["facts_json"] if hasattr(snapshot, "keys") else snapshot[0]
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"listing {listing_id} has invalid facts snapshot") from error
    if not isinstance(facts, Mapping):
        # Stored-payload validation intentionally uses the project's ValueError API.
        raise ValueError(  # noqa: TRY004
            f"listing {listing_id} facts snapshot is not an object"
        )
    text_row = inputs.full_text
    if text_row is None:
        full_text = FullTextRecord(
            listing_id=listing_id,
            text="",
            quotes=[],
            captured_at="",
            content_sha256=hashlib.sha256(b"").hexdigest(),
        )
    else:
        try:
            quotes = json.loads(
                text_row["quotes_json"] if hasattr(text_row, "keys") else text_row[1]
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            quotes = []
        full_text = FullTextRecord(
            listing_id=listing_id,
            text=str(text_row["text"] if hasattr(text_row, "keys") else text_row[0]),
            quotes=quotes if isinstance(quotes, list) else [],
            captured_at=str(
                text_row["captured_at"] if hasattr(text_row, "keys") else text_row[3]
            ),
            content_sha256=str(
                text_row["content_sha256"] if hasattr(text_row, "keys") else text_row[2]
            ),
        )
    photos = _photo_inputs(inputs.photos, listing_id)
    content_hash = vision_content_hash(facts, full_text, photos)
    model_name = str(getattr(runtime, "model_name", MODEL_NAME) or MODEL_NAME)
    model_version = str(getattr(runtime, "model_version", MODEL_NAME) or MODEL_NAME)
    provider = str(getattr(runtime, "provider", "codex") or "codex")
    reasoning_effort = str(getattr(runtime, "reasoning_effort", "medium") or "medium")
    prompt_version = str(
        getattr(runtime, "prompt_version", DEFAULT_PROMPT_VERSION)
        or DEFAULT_PROMPT_VERSION
    )
    latest = queries.latest_vision_run_metadata(conn, listing_id)
    prior_hash = (
        listing["vision_content_hash"] if hasattr(listing, "keys") else listing[0]
    )
    latest_values = (
        (
            latest["content_hash"],
            latest["status"],
            latest["schema_valid"],
            latest["provider"],
            latest["model_name"],
            latest["model_version"],
            latest["reasoning_effort"],
            latest["prompt_version"],
            latest["visual_coverage"],
        )
        if latest is not None and hasattr(latest, "keys")
        else tuple(latest)
        if latest is not None
        else ()
    )
    if (
        not force
        and prior_hash == content_hash
        and latest_values[:8]
        == (
            content_hash,
            "success",
            1,
            provider,
            model_name,
            model_version,
            reasoning_effort,
            prompt_version,
        )
    ):
        coverage = float(latest_values[8]) / 100.0
        if vision_scoring_enabled:
            recompute_assessment(
                conn,
                listing_id,
                vision_scoring_enabled=True,
                max_scores=max_scores,
                parameters=parameters,
                thresholds=thresholds,
                hard_constraints=hard_constraints,
                vision_contract=(
                    provider,
                    model_name,
                    reasoning_effort,
                    prompt_version,
                ),
            )
        return VisionRunResult(
            status="skipped", visual_coverage=max(0.0, min(1.0, coverage))
        )

    # Move the current content hash before inference so proposals from an old
    # snapshot stop being scoreable/manual immediately, even when this run fails.
    contract_changed = bool(
        latest_values
        and latest_values[3:8]
        != (provider, model_name, model_version, reasoning_effort, prompt_version)
    )
    if prior_hash != content_hash or contract_changed:
        mark_vision_content(conn, listing_id, content_hash, 0.0)
    run_id = create_vision_run(
        conn,
        listing_id,
        model_name,
        model_version,
        prompt_version,
        provider=provider,
        reasoning_effort=reasoning_effort,
        content_hash=content_hash,
    )
    if runtime is None:
        error = "Luna photo-scoring runtime is unavailable; manual review required"
        finish_vision_run(conn, run_id, "failed", schema_valid=False, error=error)
        return VisionRunResult(
            status="failed", schema_valid=False, error=error, visual_coverage=0.0
        )
    try:
        result = run_passes(
            runtime,
            listing_id,
            photos,
            full_text,
            facts,
            model_version=model_version,
            prompt_version=prompt_version,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        message = str(error)[:1000] or error.__class__.__name__
        finish_vision_run(conn, run_id, "failed", schema_valid=False, error=message)
        return VisionRunResult(
            status="failed", schema_valid=False, error=message, visual_coverage=0.0
        )
    proposals = [
        replace(proposal, vision_run_id=run_id) for proposal in result.proposals
    ]
    coverage = max(0.0, min(1.0, float(result.visual_coverage)))
    status = "success" if result.status == "success" else "failed"
    apply_scores = bool(
        auto_validate
        and vision_scoring_enabled
        and status == "success"
        and result.schema_valid
    )
    try:
        proposal_ids: list[int] = []
        if proposals:
            proposal_ids = insert_vision_proposals(conn, proposals)
        finish_vision_run(
            conn,
            run_id,
            status,
            schema_valid=bool(result.schema_valid),
            retry_count=int(result.retry_count),
            visual_coverage=coverage * 100.0,
            error=result.error,
        )
        if status == "success":
            if apply_scores:
                for proposal, proposal_id in zip(proposals, proposal_ids, strict=True):
                    if proposal.result_status == ResultStatus.CATEGORY:
                        review_proposal(
                            conn,
                            proposal_id,
                            ReviewStatus.VALIDATED,
                            vision_contract=(
                                provider,
                                model_name,
                                reasoning_effort,
                                prompt_version,
                            ),
                        )
                result.proposals = [
                    replace(proposal, review_status=ReviewStatus.VALIDATED)
                    if proposal.result_status == ResultStatus.CATEGORY
                    else proposal
                    for proposal in proposals
                ]
            mark_vision_content(conn, listing_id, content_hash, coverage * 100.0)
            if apply_scores:
                recompute_assessment(
                    conn,
                    listing_id,
                    vision_scoring_enabled=True,
                    max_scores=max_scores,
                    parameters=parameters,
                    thresholds=thresholds,
                    hard_constraints=hard_constraints,
                    vision_contract=(
                        provider,
                        model_name,
                        reasoning_effort,
                        prompt_version,
                    ),
                )
    except (sqlite3.Error, OverflowError, RuntimeError, TypeError, ValueError) as error:
        message = str(error)[:1000] or error.__class__.__name__
        try:
            finish_vision_run(conn, run_id, "failed", schema_valid=False, error=message)
        except (
            sqlite3.Error,
            OverflowError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as persistence_error:
            message = (
                f"{message}; failure status persistence failed ({persistence_error})"
            )
        return VisionRunResult(
            status="failed",
            proposals=[],
            schema_valid=False,
            error=message,
            visual_coverage=coverage,
        )
    return result


__all__ = ["run_listing_vision", "vision_content_hash"]
