"""One persisted photo evaluation with atomic database completion."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .photos import photo_input_hash
from .vision import VisionRuntime, validate_photo_files

if TYPE_CHECKING:
    from .database import Database


@dataclass(frozen=True, slots=True)
class VisionRunResult:
    status: str
    run_id: int | None = None
    visual_coverage: float = 0.0
    error: str | None = None


def _coverage(result: Mapping[str, Any]) -> float:
    return (
        sum(
            result[name]["status"] == "scoreable"
            for name in ("repair", "layout", "light_view")
        )
        / 3
    )


def run_listing_vision(
    database: Database,
    listing_id: int,
    *,
    runtime: VisionRuntime,
    policy: Mapping[str, Any],
    auto_accept: bool = False,
    force: bool = False,
) -> VisionRunResult:
    """Infer outside a transaction, then persist result and assessment together."""

    if runtime is None:
        raise TypeError(
            "Vision runtime is required; load failures must be reported by the caller"
        )
    if not isinstance(auto_accept, bool) or not isinstance(force, bool):
        raise TypeError("auto_accept and force must be booleans")
    if database.listing(listing_id) is None:
        raise ValueError(f"listing {listing_id} does not exist")
    photos = database.photos(listing_id)
    input_hash = photo_input_hash(photos)
    cached = database.current_vision(listing_id, runtime.contract, input_hash)
    file_error = None
    try:
        validate_photo_files(listing_id, photos)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        file_error = error
    if (
        not force
        and file_error is None
        and cached is not None
        and cached["status"] in {"pending", "accepted", "rejected"}
        and cached["result"] is not None
    ):
        return VisionRunResult("skipped", cached["id"], _coverage(cached["result"]))
    run_id = database.begin_vision(listing_id, runtime.contract, input_hash)
    try:
        if file_error is not None:
            raise file_error
        result = runtime.evaluate(listing_id, photos)
        # Database owns the complete transaction, including dependent assessment.
        database.complete_vision(run_id, result, auto_accept, policy)
    except (
        OSError,
        sqlite3.Error,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        message = str(error)[:1000] or error.__class__.__name__
        try:
            database.fail_vision(run_id, message)
        except (
            sqlite3.Error,
            OverflowError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as persistence_error:
            message += f"; failure status could not be persisted: {str(persistence_error)[:400]}"
        return VisionRunResult("failed", run_id, error=message)
    return VisionRunResult(
        "accepted" if auto_accept else "pending", run_id, _coverage(result)
    )


__all__ = ["VisionRunResult", "run_listing_vision"]
