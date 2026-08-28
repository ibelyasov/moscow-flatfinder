"""SQLite domain storage for FlatFinder."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    FullTextRecord,
    PhotoInput,
    ResultStatus,
    ReviewStatus,
    VisionProposal,
    proposal_is_scoreable,
    validate_visual_payload,
)
from .scoring import score_maxima

MAX_SQLITE_ID = (1 << 63) - 1

_DUPLICATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS listing_duplicates (
  listing_id INTEGER PRIMARY KEY REFERENCES listings(id),
  canonical_listing_id INTEGER NOT NULL REFERENCES listings(id),
  method TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  evidence_json TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  CHECK(listing_id != canonical_listing_id)
)
"""

_LISTING_STATE_TIMESTAMP_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS listings_set_inactive_at
AFTER UPDATE OF state ON listings
WHEN OLD.state IS NOT NEW.state
BEGIN
  UPDATE listings
  SET inactive_at = CASE
    WHEN NEW.state = 'inactive'
    THEN strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
    ELSE NULL
  END
  WHERE id = NEW.id;
END
"""


def _current_vision_contract() -> tuple[str, str, str, str]:
    """Return the backward-compatible default Vision contract."""

    from .vision import DEFAULT_PROMPT_VERSION, MODEL_NAME

    return "codex", MODEL_NAME, "medium", DEFAULT_PROMPT_VERSION


def current_vision_run_id(
    conn: sqlite3.Connection,
    listing_id: int,
    vision_contract: tuple[str, str, str, str] | None = None,
) -> int | None:
    """Return the latest successful run matching the current content contract."""

    listing_id = int(listing_id)
    provider, model_name, reasoning_effort, prompt_version = (
        vision_contract or _current_vision_contract()
    )
    row = conn.execute(
        """
        SELECT vr.id
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
        ORDER BY vr.id DESC
        LIMIT 1
        """,
        (
            listing_id,
            provider,
            model_name,
            model_name,
            reasoning_effort,
            prompt_version,
        ),
    ).fetchone()
    return int(row[0]) if row is not None else None


def visual_score_input_hash(
    conn: sqlite3.Connection,
    listing_id: int,
    vision_contract: tuple[str, str, str, str] | None = None,
) -> str:
    row = conn.execute(
        "SELECT vision_content_hash FROM listings WHERE id = ?", (int(listing_id),)
    ).fetchone()
    photo_hash = str(row[0]) if row is not None and row[0] else None
    run_id = current_vision_run_id(conn, listing_id, vision_contract)
    proposals = (
        []
        if run_id is None
        else [
            list(item)
            for item in conn.execute(
                """
            SELECT id, criterion, value_json, confidence, review_status,
                   result_status, image_indices_json, evidence_json, updated_at
            FROM vision_proposals
            WHERE vision_run_id = ?
              AND review_status = 'validated'
              AND result_status = 'category'
            ORDER BY id
            """,
                (run_id,),
            )
        ]
    )
    return _hash_json(
        {"photo_hash": photo_hash, "vision_run_id": run_id, "proposals": proposals}
    )


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      parser_version TEXT NOT NULL,
      status TEXT NOT NULL,
      blocked_reason TEXT,
      cards_found INTEGER NOT NULL DEFAULT 0,
      cards_new INTEGER NOT NULL DEFAULT 0,
      cards_failed INTEGER NOT NULL DEFAULT 0,
      field_coverage REAL,
      summary_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listings (
      id INTEGER PRIMARY KEY,
      source TEXT NOT NULL,
      source_listing_id TEXT NOT NULL,
      source_url TEXT NOT NULL,
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      content_sha256 TEXT,
      vision_content_hash TEXT,
      parser_version TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'active',
      inactive_at TEXT,
      UNIQUE(source, source_listing_id)
    )
    """,
    _LISTING_STATE_TIMESTAMP_TRIGGER,
    """
    CREATE TABLE IF NOT EXISTS listing_snapshots (
      id INTEGER PRIMARY KEY,
      listing_id INTEGER NOT NULL REFERENCES listings(id),
      captured_at TEXT NOT NULL,
      facts_json TEXT NOT NULL,
      content_sha256 TEXT NOT NULL,
      UNIQUE(listing_id, content_sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assessments (
      listing_id INTEGER PRIMARY KEY REFERENCES listings(id),
      auto_score REAL NOT NULL,
      personal_score REAL NOT NULL DEFAULT 0,
      total_score REAL NOT NULL,
      completeness REAL NOT NULL,
      fact_coverage REAL NOT NULL DEFAULT 0,
      visual_coverage REAL NOT NULL DEFAULT 0,
      personal_rated_at TEXT,
      disliked_at TEXT,
      favorited_at TEXT,
      status TEXT NOT NULL,
      assessment_json TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence (
      id INTEGER PRIMARY KEY,
      snapshot_id INTEGER NOT NULL REFERENCES listing_snapshots(id),
      field_name TEXT NOT NULL,
      source_kind TEXT NOT NULL,
      detail TEXT NOT NULL,
      confidence TEXT NOT NULL,
      captured_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS commute_checks (
      id INTEGER PRIMARY KEY,
      listing_id INTEGER NOT NULL REFERENCES listings(id),
      address_sha256 TEXT NOT NULL,
      address TEXT NOT NULL,
      destination_sha256 TEXT NOT NULL,
      destination TEXT NOT NULL,
      service_date TEXT NOT NULL,
      provider TEXT NOT NULL DEFAULT 'yandex_maps',
      status TEXT NOT NULL,
      gate_status TEXT NOT NULL,
      home_lat REAL,
      home_lon REAL,
      point_kind TEXT,
      building_id TEXT,
      entrance_id TEXT,
      geocode_precision TEXT,
      office_lat REAL,
      office_lon REAL,
      home_to_work_minutes REAL,
      work_to_home_minutes REAL,
      home_to_work_score REAL,
      work_to_home_score REAL,
      average_minutes REAL,
      average_score REAL,
      commute_score REAL NOT NULL DEFAULT 0,
      error TEXT,
      payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS park_checks (
      id INTEGER PRIMARY KEY,
      listing_id INTEGER NOT NULL REFERENCES listings(id),
      address_sha256 TEXT NOT NULL,
      address TEXT NOT NULL,
      provider TEXT NOT NULL DEFAULT '2gis',
      status TEXT NOT NULL,
      home_lat REAL,
      home_lon REAL,
      place_id TEXT,
      place_name TEXT,
      place_type TEXT,
      place_lat REAL,
      place_lon REAL,
      area_hectares REAL,
      quality REAL,
      walking_minutes REAL,
      walking_distance_m REAL,
      park_score REAL NOT NULL DEFAULT 0,
      error TEXT,
      payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fitness_checks (
      id INTEGER PRIMARY KEY,
      listing_id INTEGER NOT NULL REFERENCES listings(id),
      address_sha256 TEXT NOT NULL,
      address TEXT NOT NULL,
      provider TEXT NOT NULL DEFAULT '2gis',
      status TEXT NOT NULL,
      home_lat REAL,
      home_lon REAL,
      place_id TEXT,
      place_name TEXT,
      place_lat REAL,
      place_lon REAL,
      rating REAL,
      review_count INTEGER,
      sauna INTEGER NOT NULL DEFAULT 0,
      quality REAL NOT NULL DEFAULT 0,
      walking_minutes REAL,
      walking_distance_m REAL,
      fitness_score REAL NOT NULL DEFAULT 0,
      error TEXT,
      payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS photos (
      id INTEGER PRIMARY KEY,
      listing_id INTEGER NOT NULL REFERENCES listings(id),
      source_url TEXT NOT NULL,
      sha256 TEXT,
      dhash TEXT,
      role TEXT,
      retained INTEGER NOT NULL DEFAULT 0,
      UNIQUE(listing_id, source_url)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS photo_ingestion (
      id INTEGER PRIMARY KEY,
      listing_id INTEGER NOT NULL REFERENCES listings(id),
      image_index INTEGER NOT NULL,
      source_url TEXT NOT NULL,
      raw_source_url TEXT,
      local_path TEXT,
      sha256 TEXT,
      dhash TEXT,
      duplicate_of INTEGER REFERENCES photo_ingestion(id),
      fetched_at TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'indexed',
      error TEXT,
      UNIQUE(listing_id, image_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS full_text (
      listing_id INTEGER PRIMARY KEY REFERENCES listings(id),
      text TEXT NOT NULL,
      quotes_json TEXT NOT NULL DEFAULT '[]',
      content_sha256 TEXT NOT NULL,
      captured_at TEXT NOT NULL
    )
    """,
    _DUPLICATE_SCHEMA,
    """
    CREATE TABLE IF NOT EXISTS vision_runs (
      id INTEGER PRIMARY KEY,
      listing_id INTEGER NOT NULL REFERENCES listings(id),
      content_hash TEXT,
      provider TEXT NOT NULL DEFAULT 'codex',
      model_name TEXT NOT NULL,
      model_version TEXT NOT NULL,
      reasoning_effort TEXT NOT NULL DEFAULT 'medium',
      prompt_version TEXT NOT NULL,
      status TEXT NOT NULL,
      schema_valid INTEGER NOT NULL DEFAULT 0,
      retry_count INTEGER NOT NULL DEFAULT 0,
      visual_coverage REAL NOT NULL DEFAULT 0,
      error TEXT,
      started_at TEXT NOT NULL,
      finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vision_proposals (
      id INTEGER PRIMARY KEY,
      listing_id INTEGER NOT NULL REFERENCES listings(id),
      vision_run_id INTEGER NOT NULL REFERENCES vision_runs(id),
      pass_name TEXT NOT NULL,
      criterion TEXT NOT NULL,
      value_json TEXT,
      confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
      review_status TEXT NOT NULL CHECK(review_status IN ('pending', 'validated', 'rejected')),
      result_status TEXT NOT NULL CHECK(result_status IN ('category', 'unknown', 'conflict')),
      model_name TEXT NOT NULL,
      model_version TEXT NOT NULL,
      prompt_version TEXT NOT NULL,
      image_indices_json TEXT NOT NULL DEFAULT '[]',
      text_quotes_json TEXT NOT NULL DEFAULT '[]',
      evidence_json TEXT NOT NULL DEFAULT '[]',
      conflicts_json TEXT NOT NULL DEFAULT '[]',
      review_category TEXT,
      review_reason TEXT,
      reviewed_at TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(vision_run_id, pass_name, criterion)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_vision_proposals_listing_review
      ON vision_proposals(listing_id, review_status, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_commute_checks_listing_address
      ON commute_checks(listing_id, address_sha256, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_commute_checks_destination
      ON commute_checks(destination_sha256, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_park_checks_listing_address
      ON park_checks(listing_id, address_sha256, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fitness_checks_listing_address
      ON fitness_checks(listing_id, address_sha256, id)
    """,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text_time(value: Any, default: str | None = None) -> str:
    if value is None:
        if default is None:
            return _now()
        return default
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="seconds")
    return str(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _stable_enrichment_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_enrichment_value(item)
            for key, item in value.items()
            if key != "captured_at"
        }
    if isinstance(value, list):
        return [_stable_enrichment_value(item) for item in value]
    return value


def _stable_content_value(value: Any) -> Any:
    stable = _stable_enrichment_value(value)
    if isinstance(stable, dict) and isinstance(stable.get("fields"), dict):
        stable["fields"].pop("photos_observed", None)
    return stable


def _evidence_detail(value: Any) -> str:
    return (
        _json(_stable_enrichment_value(value))
        if isinstance(value, dict)
        else str(value)
    )


def _begin_immediate(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise sqlite3.OperationalError("storage operation requires a clean connection")
    conn.execute("BEGIN IMMEDIATE")


def _bounded_float(value: Any, name: str, upper: float) -> float:
    message = f"{name} must be a finite number from 0 to {upper:g}"
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(message) from exc
    if not math.isfinite(number) or not 0 <= number <= upper:
        raise ValueError(message)
    return number


def _validate_coverage(value: Any, name: str = "coverage") -> float:
    return _bounded_float(value, name, 100)


def _enum_name(enum_type: type[Any], value: Any, name: str) -> str:
    raw = getattr(value, "value", value)
    try:
        return enum_type(str(raw)).value
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{name} must be one of: {allowed}") from exc


def _confidence(value: Any) -> float:
    return _bounded_float(value, "confidence", 1)


@contextmanager
def _write_transaction(conn: sqlite3.Connection):
    _begin_immediate(conn)
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def connect_db(path: str | Path) -> sqlite3.Connection:
    """Open the single writer connection used by FlatFinder."""

    database = str(path)
    if database != ":memory:":
        Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    # Vision inference runs in ``asyncio.to_thread`` while the collector keeps
    # its single writer connection.  Calls remain serial, but SQLite must allow
    # that hand-off between the event-loop and worker thread.
    conn = sqlite3.connect(
        database, timeout=5.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create schema 17 or migrate a supported predecessor."""

    if conn.in_transaction:
        raise sqlite3.OperationalError("migrate requires a clean connection")
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise sqlite3.OperationalError("SQLite foreign key enforcement is unavailable")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current == 17:
        return
    if current not in {0, 15, 16}:
        raise RuntimeError(
            f"unsupported database schema version: {current}; expected 15, 16, or 17"
        )
    with _write_transaction(conn):
        if current == 15:
            conn.execute(
                "ALTER TABLE vision_runs ADD COLUMN provider TEXT NOT NULL DEFAULT 'codex'"
            )
            conn.execute(
                "ALTER TABLE vision_runs ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT 'medium'"
            )
        if current in {15, 16}:
            conn.execute("ALTER TABLE assessments ADD COLUMN favorited_at TEXT")
        else:
            for statement in _SCHEMA:
                conn.execute(statement)
        conn.execute("PRAGMA user_version = 17")


def _photo_values(photo: Any) -> tuple[str, str | None, str | None, str | None, int]:
    if isinstance(photo, dict):
        url = photo.get("url") or photo.get("canonical_url") or photo.get("source_url")
        if not url:
            raise ValueError("photo is missing source_url")
        return (
            str(url),
            str(photo["sha256"]) if photo.get("sha256") is not None else None,
            str(photo["dhash"]) if photo.get("dhash") is not None else None,
            str(photo["role"]) if photo.get("role") is not None else None,
            int(bool(photo.get("retained", False))),
        )
    if not isinstance(photo, str) or not photo:
        raise ValueError("photo must be a non-empty URL")
    return photo, None, None, None, 0


def _insert_evidence(
    conn: sqlite3.Connection,
    snapshot_id: int,
    assessment: dict[str, Any],
    captured_at: str,
) -> None:
    for field_name, value in assessment.items():
        if not isinstance(value, dict):
            continue
        details = value.get("evidence", [])
        if not isinstance(details, list):
            continue
        confidence = str(value.get("confidence", "unknown"))
        for detail in details:
            if not isinstance(detail, str):
                detail = _json(detail)
            conn.execute(
                """
                INSERT INTO evidence
                  (snapshot_id, field_name, source_kind, detail, confidence, captured_at)
                VALUES (?, ?, 'assessment', ?, ?, ?)
                """,
                (snapshot_id, str(field_name), detail, confidence, captured_at),
            )


def _upsert_assessment(
    conn: sqlite3.Connection,
    listing_id: int,
    auto_score: float,
    personal_score: float,
    total_score: float,
    completeness: float,
    fact_coverage: float,
    visual_coverage: float,
    status: str,
    assessment_json: str,
    updated_at: str,
    max_scores: Mapping[str, float] | None = None,
) -> None:
    automatic_max, personal_max, total_max = score_maxima(max_scores)
    auto_score = _bounded_float(auto_score, "automatic score", automatic_max)
    personal_score = _bounded_float(personal_score, "personal score", personal_max)
    total_score = _bounded_float(total_score, "total score", total_max)
    if not math.isclose(total_score, auto_score + personal_score, abs_tol=1e-9):
        raise ValueError("total score must equal automatic score plus personal score")
    conn.execute(
        """
        INSERT INTO assessments
          (listing_id, auto_score, personal_score, total_score, completeness,
           fact_coverage, visual_coverage, status, assessment_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(listing_id) DO UPDATE SET
          auto_score = excluded.auto_score,
          personal_score = excluded.personal_score,
          total_score = excluded.total_score,
          completeness = excluded.completeness,
          fact_coverage = excluded.fact_coverage,
          visual_coverage = assessments.visual_coverage,
          status = excluded.status,
          assessment_json = excluded.assessment_json,
          updated_at = excluded.updated_at
        """,
        (
            int(listing_id),
            float(auto_score),
            float(personal_score),
            float(total_score),
            float(completeness),
            float(fact_coverage),
            float(visual_coverage),
            str(status),
            assessment_json,
            updated_at,
        ),
    )


def _insert_photos(
    conn: sqlite3.Connection,
    listing_id: int,
    photos: Any,
) -> None:
    if not isinstance(photos, list):
        return
    for photo in photos:
        url, sha256, dhash, role, retained = _photo_values(photo)
        conn.execute(
            """
            INSERT INTO photos (listing_id, source_url, sha256, dhash, role, retained)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id, source_url) DO UPDATE SET
              sha256 = excluded.sha256,
              dhash = excluded.dhash,
              role = excluded.role,
              retained = excluded.retained
            """,
            (listing_id, url, sha256, dhash, role, retained),
        )


def _photo_input(
    value: PhotoInput | Mapping[str, Any],
) -> tuple[PhotoInput, str, str, str | None, int | None]:
    raw_source_url: str | None = None
    error: str | None = None
    duplicate_of_index: int | None = None
    if isinstance(value, PhotoInput):
        photo = value
        status = str(
            photo.status
            or ("duplicate" if photo.duplicate_of is not None else "indexed")
        )
        raw_source_url = photo.raw_source_url or photo.source_url
        error = photo.error
        duplicate_of_index = photo.duplicate_of_index
    elif isinstance(value, Mapping):
        try:
            photo = PhotoInput(
                listing_id=int(value["listing_id"]),
                image_index=int(value["image_index"]),
                source_url=str(value["source_url"]),
                local_path=str(value["local_path"])
                if value.get("local_path") is not None
                else None,
                sha256=str(value["sha256"])
                if value.get("sha256") is not None
                else None,
                dhash=str(value["dhash"]) if value.get("dhash") is not None else None,
                duplicate_of=int(value["duplicate_of"])
                if value.get("duplicate_of") is not None
                else None,
                status=str(value.get("status") or "indexed"),
                error=str(value.get("error"))[:240]
                if value.get("error") is not None
                else None,
                raw_source_url=str(value.get("raw_source_url") or value["source_url"]),
                duplicate_of_index=int(value["duplicate_of_index"])
                if value.get("duplicate_of_index") is not None
                else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("photo ingestion record is malformed") from exc
        status = str(
            photo.status
            or ("duplicate" if photo.duplicate_of is not None else "indexed")
        )
        raw_source_url = photo.raw_source_url or photo.source_url
        error = photo.error
        duplicate_of_index = photo.duplicate_of_index
    else:
        raise TypeError("photo must be PhotoInput or a mapping")
    if photo.listing_id <= 0 or photo.image_index < 0 or not photo.source_url.strip():
        raise ValueError("photo listing_id, image_index and source_url are required")
    if photo.duplicate_of is not None and photo.duplicate_of <= 0:
        raise ValueError("duplicate_of must be a positive photo id")
    if duplicate_of_index is not None and duplicate_of_index < 0:
        raise ValueError("duplicate_of_index must be non-negative")
    if not status.strip():
        raise ValueError("photo status must not be empty")
    return photo, status, raw_source_url or photo.source_url, error, duplicate_of_index


def upsert_photo_ingestion(
    conn: sqlite3.Connection,
    photos: PhotoInput | Mapping[str, Any] | Sequence[PhotoInput | Mapping[str, Any]],
    *,
    listing_id: int | None = None,
    replace: bool = False,
) -> int | list[int]:
    """Persist one or more deterministic photo-ingestion records atomically.

    ``replace=True`` reconciles one listing's current gallery in this same
    transaction, removing indices that disappeared from the latest batch.
    """

    if isinstance(photos, (PhotoInput, Mapping)):
        items = [photos]
        many = False
    elif isinstance(photos, Sequence) and not isinstance(
        photos, (str, bytes, bytearray)
    ):
        items = list(photos)
        many = True
    else:
        raise TypeError("photos must be a PhotoInput, mapping, or sequence")
    if not items and not replace:
        return [] if many else 0
    prepared = [_photo_input(item) for item in items]
    prepared_listing_ids = {photo.listing_id for photo, *_ in prepared}
    if listing_id is None:
        if len(prepared_listing_ids) != 1:
            if not prepared:
                raise ValueError(
                    "listing_id is required when replacing an empty photo batch"
                )
            raise ValueError("photo ingestion batch must contain one listing")
        listing_id = next(iter(prepared_listing_ids))
    listing_id = int(listing_id)
    if listing_id <= 0 or any(photo.listing_id != listing_id for photo, *_ in prepared):
        raise ValueError("photo ingestion listing_id does not match the batch")
    ids: list[int] = []
    with _write_transaction(conn):
        if replace:
            indices = [photo.image_index for photo, *_ in prepared]
            if indices:
                placeholders = ",".join("?" for _ in indices)
                stale_ids = conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- only the parameter placeholder count is dynamic.
                    f"SELECT id FROM photo_ingestion WHERE listing_id = ? AND image_index NOT IN ({placeholders})",
                    (listing_id, *indices),
                ).fetchall()
                if stale_ids:
                    stale_placeholders = ",".join("?" for _ in stale_ids)
                    conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- only the parameter placeholder count is dynamic.
                        f"DELETE FROM photo_ingestion WHERE listing_id = ? AND duplicate_of IN ({stale_placeholders})",
                        (listing_id, *(int(row[0]) for row in stale_ids)),
                    )
                conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- only the parameter placeholder count is dynamic.
                    f"DELETE FROM photo_ingestion WHERE listing_id = ? AND image_index NOT IN ({placeholders})",
                    (listing_id, *indices),
                )
            else:
                conn.execute(
                    "DELETE FROM photo_ingestion WHERE listing_id = ?", (listing_id,)
                )
        for photo, status, raw_source_url, error, duplicate_of_index in prepared:
            duplicate_of = photo.duplicate_of
            if duplicate_of_index is not None:
                row = conn.execute(
                    "SELECT id FROM photo_ingestion WHERE listing_id = ? AND image_index = ?",
                    (photo.listing_id, duplicate_of_index),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"duplicate_of_index {duplicate_of_index} has no prior photo row"
                    )
                duplicate_of = int(row[0])
            conn.execute(
                """
                INSERT INTO photo_ingestion
                  (listing_id, image_index, source_url, raw_source_url, local_path,
                   sha256, dhash, duplicate_of, fetched_at, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(listing_id, image_index) DO UPDATE SET
                  source_url = excluded.source_url,
                  raw_source_url = excluded.raw_source_url,
                  local_path = excluded.local_path,
                  sha256 = excluded.sha256,
                  dhash = excluded.dhash,
                  duplicate_of = excluded.duplicate_of,
                  fetched_at = excluded.fetched_at,
                  status = excluded.status,
                  error = excluded.error
                """,
                (
                    photo.listing_id,
                    photo.image_index,
                    photo.source_url,
                    raw_source_url,
                    photo.local_path,
                    photo.sha256,
                    photo.dhash,
                    duplicate_of,
                    _now(),
                    status,
                    error,
                ),
            )
            row = conn.execute(
                "SELECT id FROM photo_ingestion WHERE listing_id = ? AND image_index = ?",
                (photo.listing_id, photo.image_index),
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError(
                    "photo ingestion upsert did not return a row"
                )
            ids.append(int(row[0]))
    return ids if many else ids[0]


def _duplicate_key(facts: Mapping[str, Any]) -> tuple[str, int, int, float] | None:
    fields = facts.get("fields")
    if not isinstance(fields, Mapping):
        return None

    def value(name: str) -> Any:
        raw = fields.get(name)
        return raw.get("value") if isinstance(raw, Mapping) else raw

    point = value("location_point")
    building_id = (
        str(point.get("building_id") or "").strip()
        if isinstance(point, Mapping)
        else ""
    )
    try:
        rooms = int(value("rooms"))
        floor = int(value("floor"))
        area = float(value("area_m2"))
    except (TypeError, ValueError, OverflowError):
        return None
    return (building_id, rooms, floor, area) if building_id and area > 0 else None


def _listing_hashes(conn: sqlite3.Connection, listing_id: int) -> list[str]:
    return list(
        dict.fromkeys(
            str(row[0])
            for row in conn.execute(
                "SELECT dhash FROM photo_ingestion WHERE listing_id = ? AND status = 'indexed' AND dhash IS NOT NULL ORDER BY image_index",
                (int(listing_id),),
            ).fetchall()
            if row[0]
        )
    )


def _visual_matches(left: Sequence[str], right: Sequence[str]) -> int:
    available = set(range(len(right)))
    matches = 0
    for left_hash in left:
        distances: list[tuple[int, int]] = []
        for index in available:
            try:
                distance = (int(left_hash, 16) ^ int(right[index], 16)).bit_count()
            except ValueError:
                continue
            if distance <= 6:
                distances.append((distance, index))
        if distances:
            _, index = min(distances)
            available.remove(index)
            matches += 1
    return matches


def _canonical_metric(value: Any) -> float:
    """Return a bounded quality metric suitable for deterministic ranking."""

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(100.0, number))


def _canonical_timestamp(value: Any) -> tuple[int, int]:
    """Normalize an ISO timestamp without using an internal database id."""

    text = str(value or "").strip()
    if not text:
        return (0, 0)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (1, int(parsed.timestamp() * 1_000_000))
    except (TypeError, ValueError, OverflowError, OSError):
        return (0, 0)


def _canonical_sort_key(conn: sqlite3.Connection, listing_id: int) -> tuple[Any, ...]:
    """Rank a listing by quality, freshness, then stable source identity.

    The tuple is intentionally independent of the SQLite row id:
    ``(completeness, fact_coverage, visual_coverage)`` descending,
    the latest content snapshot timestamp descending, and
    ``(source, source_listing_id)`` ascending.  ``last_seen_at`` is deliberately
    excluded: a crawl observing unchanged content must not beat another source
    merely because that source ran later in the configured order.
    """

    row = conn.execute(
        """
        SELECT l.source, l.source_listing_id,
               (
                 SELECT latest.captured_at
                 FROM listing_snapshots AS latest
                 WHERE latest.listing_id = l.id
                 ORDER BY latest.captured_at DESC, latest.content_sha256 DESC
                 LIMIT 1
               ) AS snapshot_captured_at,
               COALESCE(a.completeness, 0),
               COALESCE(a.fact_coverage, 0),
               COALESCE(a.visual_coverage, 0)
        FROM listings AS l
        LEFT JOIN assessments AS a ON a.listing_id = l.id
        WHERE l.id = ?
        """,
        (int(listing_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"listing {listing_id} does not exist")
    quality = (
        _canonical_metric(row[3]),
        _canonical_metric(row[4]),
        _canonical_metric(row[5]),
    )
    freshness = _canonical_timestamp(row[2])
    return (
        -quality[0],
        -quality[1],
        -quality[2],
        -freshness[0],
        -freshness[1],
        str(row[0] or ""),
        str(row[1] or ""),
    )


def _duplicate_component(conn: sqlite3.Connection, seeds: Sequence[int]) -> set[int]:
    """Return the connected duplicate component around ``seeds``.

    Duplicate rows are stored as directed ``listing -> canonical`` links, but
    re-parenting needs the whole undirected component.  Walking both ends also
    repairs databases left with a stale chain or a cycle by the old logic.
    """

    component = {int(value) for value in seeds if int(value) > 0}
    if not component:
        return set()
    edges = conn.execute(
        "SELECT listing_id, canonical_listing_id FROM listing_duplicates"
    ).fetchall()
    adjacent: dict[int, set[int]] = {}
    for row in edges:
        left, right = int(row[0]), int(row[1])
        adjacent.setdefault(left, set()).add(right)
        adjacent.setdefault(right, set()).add(left)
    frontier = list(component)
    while frontier:
        current = frontier.pop()
        for neighbour in adjacent.get(current, ()):
            if neighbour not in component:
                component.add(neighbour)
                frontier.append(neighbour)
    return component


def _write_duplicate_component(
    conn: sqlite3.Connection,
    members: set[int],
    canonical_id: int,
    *,
    method: str,
    confidence: float,
    evidence_json: str,
) -> None:
    """Flatten one duplicate component onto its selected canonical listing."""

    if not members or canonical_id not in members:
        raise ValueError("duplicate component has no selected canonical listing")
    placeholders = ", ".join("?" for _ in members)
    old_rows = conn.execute(
        f"SELECT listing_id, method, confidence, evidence_json, detected_at "
        f"FROM listing_duplicates WHERE listing_id IN ({placeholders})",
        tuple(sorted(members)),
    ).fetchall()
    old_by_listing = {int(row[0]): row for row in old_rows}
    conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- placeholders contain only generated question marks; member ids remain parameterized.
        f"DELETE FROM listing_duplicates WHERE listing_id IN ({placeholders})",
        tuple(sorted(members)),
    )
    for member_id in sorted(members):
        if member_id == canonical_id:
            continue
        old = old_by_listing.get(member_id)
        if old is None:
            row_method = method
            row_confidence = confidence
            row_evidence = evidence_json
            detected_at = _now()
        else:
            row_method = str(old[1])
            row_confidence = float(old[2])
            row_evidence = str(old[3])
            detected_at = str(old[4])
        conn.execute(
            """
            INSERT INTO listing_duplicates
              (listing_id, canonical_listing_id, method, confidence, evidence_json, detected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                member_id,
                canonical_id,
                row_method,
                row_confidence,
                row_evidence,
                detected_at,
            ),
        )


def detect_listing_duplicate(
    conn: sqlite3.Connection, listing_id: int
) -> sqlite3.Row | None:
    """Confirm a cross-source duplicate using an exact building and shared photos."""

    listing_id = int(listing_id)
    row = conn.execute(
        """
        SELECT l.source, s.facts_json
        FROM listings AS l
        JOIN listing_snapshots AS s ON s.id = (
          SELECT latest.id FROM listing_snapshots AS latest
          WHERE latest.listing_id = l.id ORDER BY latest.id DESC LIMIT 1
        )
        WHERE l.id = ?
        """,
        (listing_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"listing {listing_id} has no current facts")
    try:
        key = _duplicate_key(json.loads(row[1]))
    except (TypeError, ValueError, json.JSONDecodeError):
        key = None
    hashes = _listing_hashes(conn, listing_id)
    best: tuple[int, float, str, str, int] | None = None
    best_score: tuple[int, float, str, str] | None = None
    qualified_candidate_ids: list[int] = []
    if key is not None and len(hashes) >= 3:
        building_id, rooms, floor, area = key
        candidates = conn.execute(
            """
            SELECT l.id, l.source, l.source_listing_id, s.facts_json
            FROM listings AS l
            JOIN listing_snapshots AS s ON s.id = (
              SELECT latest.id FROM listing_snapshots AS latest
              WHERE latest.listing_id = l.id ORDER BY latest.id DESC LIMIT 1
            )
            WHERE l.id != ? AND l.source != ?
            """,
            (listing_id, str(row[0])),
        ).fetchall()
        for candidate in candidates:
            try:
                candidate_key = _duplicate_key(json.loads(candidate[3]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if candidate_key is None:
                continue
            candidate_building, candidate_rooms, candidate_floor, candidate_area = (
                candidate_key
            )
            if (
                candidate_building != building_id
                or candidate_rooms != rooms
                or candidate_floor != floor
                or abs(candidate_area - area) > 1.0
            ):
                continue
            candidate_hashes = _listing_hashes(conn, int(candidate[0]))
            matches = _visual_matches(hashes, candidate_hashes)
            overlap = (
                matches / min(len(hashes), len(candidate_hashes))
                if candidate_hashes
                else 0.0
            )
            if matches >= 3 and overlap >= 0.35:
                candidate_source = str(candidate[1] or "")
                candidate_source_id = str(candidate[2] or "")
                candidate_id = int(candidate[0])
                qualified_candidate_ids.append(candidate_id)
                score = (
                    -matches,
                    -overlap,
                    candidate_source,
                    candidate_source_id,
                )
                if best_score is None or score < best_score:
                    best_score = score
                    best = (
                        matches,
                        overlap,
                        candidate_source,
                        candidate_source_id,
                        candidate_id,
                    )
    with _write_transaction(conn):
        if best is None:
            has_children = conn.execute(
                "SELECT 1 FROM listing_duplicates WHERE canonical_listing_id = ? LIMIT 1",
                (listing_id,),
            ).fetchone()
            members = (
                _duplicate_component(conn, (listing_id,))
                if has_children is not None
                else {listing_id}
            )
            placeholders = ", ".join("?" for _ in members)
            conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- placeholders contain only generated question marks; member ids remain parameterized.
                f"DELETE FROM listing_duplicates WHERE listing_id IN ({placeholders})",
                tuple(sorted(members)),
            )
        else:
            matches, overlap, _source, _source_id, candidate_id = best
            evidence = {
                "building_id": key[0],
                "rooms": key[1],
                "floor": key[2],
                "area_m2": key[3],
                "photo_matches": matches,
                "gallery_overlap": round(overlap, 4),
                "gallery_sizes": [
                    len(hashes),
                    len(_listing_hashes(conn, candidate_id)),
                ],
            }
            members = {listing_id}
            for qualified_id in qualified_candidate_ids:
                members.update(_duplicate_component(conn, (listing_id, qualified_id)))
            canonical_id = min(
                members, key=lambda value: _canonical_sort_key(conn, value)
            )
            _write_duplicate_component(
                conn,
                members,
                canonical_id,
                method="building_rooms_floor_area_photos",
                confidence=min(0.99, 0.9 + overlap / 10),
                evidence_json=_json(evidence),
            )
    return conn.execute(
        "SELECT * FROM listing_duplicates WHERE listing_id = ?", (listing_id,)
    ).fetchone()


def _full_text_record(value: FullTextRecord | Mapping[str, Any]) -> FullTextRecord:
    if isinstance(value, FullTextRecord):
        record = value
    elif isinstance(value, Mapping):
        try:
            record = FullTextRecord(
                listing_id=int(value["listing_id"]),
                text=str(value.get("text") or ""),
                quotes=list(value.get("quotes") or []),
                captured_at=_text_time(value.get("captured_at")),
                content_sha256=str(value["content_sha256"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("full-text record is malformed") from exc
    else:
        raise TypeError("full-text record must be FullTextRecord or a mapping")
    if record.listing_id <= 0 or not isinstance(record.text, str):
        raise ValueError("full-text listing_id and text are required")
    if not isinstance(record.quotes, list) or any(
        not isinstance(item, Mapping)
        or any(
            not isinstance(key, str) or not isinstance(val, str)
            for key, val in item.items()
        )
        for item in record.quotes
    ):
        raise ValueError("full-text quotes must be a list of string mappings")
    if not record.content_sha256.strip():
        raise ValueError("full-text content_sha256 is required")
    return record


def upsert_full_text(
    conn: sqlite3.Connection,
    record: FullTextRecord | Mapping[str, Any],
) -> sqlite3.Row:
    """Replace a listing's full-text channel in one transaction."""

    record = _full_text_record(record)
    with _write_transaction(conn):
        conn.execute(
            """
            INSERT INTO full_text
              (listing_id, text, quotes_json, content_sha256, captured_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
              text = excluded.text,
              quotes_json = excluded.quotes_json,
              content_sha256 = excluded.content_sha256,
              captured_at = excluded.captured_at
            """,
            (
                record.listing_id,
                record.text,
                _json(record.quotes),
                record.content_sha256,
                _text_time(record.captured_at),
            ),
        )
        row = conn.execute(
            "SELECT * FROM full_text WHERE listing_id = ?", (record.listing_id,)
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("full-text upsert did not return a row")
    return row


def _run_text(value: Any, name: str) -> str:
    text = str(value) if value is not None else ""
    if not text.strip():
        raise ValueError(f"{name} is required")
    return text


def create_vision_run(
    conn: sqlite3.Connection,
    listing_id: int,
    model_name: str,
    model_version: str,
    prompt_version: str,
    *,
    provider: str = "codex",
    reasoning_effort: str = "medium",
    status: str = "running",
    schema_valid: bool = False,
    retry_count: int = 0,
    visual_coverage: float = 0,
    error: str | None = None,
    started_at: Any = None,
    content_hash: str | None = None,
) -> int:
    """Create a VLM run record without touching deterministic assessment data."""

    listing_id = int(listing_id)
    status = _run_text(status, "status")
    model_name = _run_text(model_name, "model_name")
    model_version = _run_text(model_version, "model_version")
    prompt_version = _run_text(prompt_version, "prompt_version")
    provider = _run_text(provider, "provider")
    reasoning_effort = _run_text(reasoning_effort, "reasoning_effort")
    if content_hash is not None:
        content_hash = _run_text(content_hash, "content_hash")
    if not isinstance(schema_valid, bool):
        raise ValueError("schema_valid must be boolean")
    if isinstance(retry_count, bool) or int(retry_count) < 0:
        raise ValueError("retry_count must be a non-negative integer")
    visual_coverage = _validate_coverage(visual_coverage, "visual_coverage")
    with _write_transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO vision_runs
              (listing_id, content_hash, provider, model_name, model_version,
               reasoning_effort, prompt_version, status, schema_valid, retry_count,
               visual_coverage, error, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                content_hash,
                provider,
                model_name,
                model_version,
                reasoning_effort,
                prompt_version,
                status,
                int(schema_valid),
                int(retry_count),
                visual_coverage,
                str(error) if error is not None else None,
                _text_time(started_at),
            ),
        )
        return int(cursor.lastrowid)


def finish_vision_run(
    conn: sqlite3.Connection,
    vision_run_id: int,
    status: str,
    *,
    schema_valid: bool | None = None,
    retry_count: int | None = None,
    visual_coverage: float | None = None,
    error: str | None = None,
    finished_at: Any = None,
) -> sqlite3.Row:
    """Finish a run while preserving omitted values from its initial record."""

    status = _run_text(status, "status")
    with _write_transaction(conn):
        current = conn.execute(
            "SELECT * FROM vision_runs WHERE id = ?", (int(vision_run_id),)
        ).fetchone()
        if current is None:
            raise ValueError(f"vision run {vision_run_id} does not exist")
        if current["finished_at"] is not None:
            raise ValueError(f"vision run {vision_run_id} is already finished")
        if schema_valid is None:
            schema_valid = bool(current["schema_valid"])
        elif not isinstance(schema_valid, bool):
            raise ValueError("schema_valid must be boolean")
        if retry_count is None:
            retry_count = int(current["retry_count"])
        if isinstance(retry_count, bool) or int(retry_count) < 0:
            raise ValueError("retry_count must be a non-negative integer")
        if visual_coverage is None:
            visual_coverage = float(current["visual_coverage"])
        visual_coverage = _validate_coverage(visual_coverage, "visual_coverage")
        conn.execute(
            """
            UPDATE vision_runs
            SET status = ?, schema_valid = ?, retry_count = ?, visual_coverage = ?,
                error = ?, finished_at = ?
            WHERE id = ? AND finished_at IS NULL
            """,
            (
                status,
                int(schema_valid),
                int(retry_count),
                visual_coverage,
                str(error) if error is not None else None,
                _text_time(finished_at),
                int(vision_run_id),
            ),
        )
        row = conn.execute(
            "SELECT * FROM vision_runs WHERE id = ?", (int(vision_run_id),)
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("vision run update did not return a row")
    return row


def mark_vision_content(
    conn: sqlite3.Connection,
    listing_id: int,
    content_hash: str,
    visual_coverage: float,
) -> sqlite3.Row:
    """Commit the last evaluated content hash and coverage without touching score."""

    content_hash = _run_text(content_hash, "content_hash")
    visual_coverage = _validate_coverage(visual_coverage, "visual_coverage")
    with _write_transaction(conn):
        changed = conn.execute(
            "UPDATE listings SET vision_content_hash = ? WHERE id = ?",
            (content_hash, int(listing_id)),
        ).rowcount
        if changed != 1:
            raise ValueError(f"listing {listing_id} does not exist")
        conn.execute(
            "UPDATE assessments SET visual_coverage = ? WHERE listing_id = ?",
            (visual_coverage, int(listing_id)),
        )
        row = conn.execute(
            "SELECT * FROM listings WHERE id = ?", (int(listing_id),)
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("vision content update did not return a row")
    return row


def set_vision_content_hash(
    conn: sqlite3.Connection,
    listing_id: int,
    content_hash: str,
) -> sqlite3.Row:
    """Bind the current deterministic content without changing coverage."""

    content_hash = _run_text(content_hash, "content_hash")
    with _write_transaction(conn):
        current = conn.execute(
            "SELECT vision_content_hash FROM listings WHERE id = ?", (int(listing_id),)
        ).fetchone()
        if current is None:
            raise ValueError(f"listing {listing_id} does not exist")
        changed = conn.execute(
            "UPDATE listings SET vision_content_hash = ? WHERE id = ?",
            (content_hash, int(listing_id)),
        ).rowcount
        if changed != 1:
            raise ValueError(f"listing {listing_id} does not exist")
        if current[0] != content_hash:
            conn.execute(
                "UPDATE assessments SET visual_coverage = 0 WHERE listing_id = ?",
                (int(listing_id),),
            )
        row = conn.execute(
            "SELECT * FROM listings WHERE id = ?", (int(listing_id),)
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(
                "vision content hash update did not return a row"
            )
    return row


def vision_manual_review_count(
    conn: sqlite3.Connection,
    listing_id: int | None = None,
    vision_contract: tuple[str, str, str, str] | None = None,
) -> int:
    """Count pending proposals plus failed runs requiring a manual retry/review."""

    provider, model_name, reasoning_effort, prompt_version = (
        vision_contract or _current_vision_contract()
    )
    listing_clause = " AND vr.listing_id = ?" if listing_id is not None else ""
    args = (provider, model_name, model_name, reasoning_effort, prompt_version)
    if listing_id is not None:
        args += (int(listing_id),)
    pending = conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- the optional clause is fixed and its value remains parameterized.
        """
        WITH current_runs AS (
          SELECT vr.id, vr.listing_id,
                 ROW_NUMBER() OVER (
                   PARTITION BY vr.listing_id
                   ORDER BY vr.id DESC
                 ) AS run_rank
          FROM vision_runs AS vr
          JOIN listings AS l ON l.id = vr.listing_id
          WHERE vr.status = 'success'
            AND vr.schema_valid = 1
            AND vr.provider = ?
            AND vr.model_name = ?
            AND vr.model_version = ?
            AND vr.reasoning_effort = ?
            AND vr.prompt_version = ?
            AND l.vision_content_hash IS NOT NULL
            AND vr.content_hash = l.vision_content_hash
        """
        + listing_clause
        + """
        )
        SELECT COUNT(*)
        FROM vision_proposals AS vp
        JOIN current_runs AS cr ON cr.id = vp.vision_run_id AND cr.run_rank = 1
        WHERE vp.review_status = 'pending'
          AND vp.model_name = ?
          AND vp.model_version = ?
          AND vp.prompt_version = ?
        """,
        args + (model_name, model_name, prompt_version),
    ).fetchone()
    latest_args = (provider, model_name, model_name, reasoning_effort, prompt_version)
    if listing_id is not None:
        latest_args += (int(listing_id),)
    failed = conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- the optional clause is fixed and its value remains parameterized.
        """
        WITH latest_runs AS (
          SELECT vr.status, vr.schema_valid,
                 ROW_NUMBER() OVER (
                   PARTITION BY vr.listing_id
                   ORDER BY vr.id DESC
                 ) AS run_rank
          FROM vision_runs AS vr
          JOIN listings AS l ON l.id = vr.listing_id
          WHERE vr.provider = ?
            AND vr.model_name = ?
            AND vr.model_version = ?
            AND vr.reasoning_effort = ?
            AND vr.prompt_version = ?
            AND l.vision_content_hash IS NOT NULL
            AND vr.content_hash = l.vision_content_hash
        """
        + (" AND vr.listing_id = ?" if listing_id is not None else "")
        + """
        )
        SELECT COUNT(*) FROM latest_runs
        WHERE run_rank = 1 AND (status = 'failed' OR schema_valid = 0)
        """,
        latest_args,
    ).fetchone()
    return int((pending[0] if pending else 0) or 0) + int(
        (failed[0] if failed else 0) or 0
    )


def _string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"{name} must be a list of strings")
    return [item for item in value]


def _conflicts(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise ValueError("conflicts must be a list of objects")
    return [dict(item) for item in value]


def _image_indices(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("image_indices must be a list of integers")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError("image_indices must be a list of non-negative integers")
        result.append(int(item))
    return result


def _proposal_data(value: VisionProposal | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, VisionProposal):
        raw = {
            name: getattr(value, name)
            for name in (
                "listing_id",
                "vision_run_id",
                "pass_name",
                "criterion",
                "value",
                "confidence",
                "review_status",
                "result_status",
                "model_name",
                "model_version",
                "prompt_version",
                "image_indices",
                "text_quotes",
                "evidence",
                "conflicts",
            )
        }
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise TypeError("proposal must be VisionProposal or a mapping")
    try:
        listing_id = int(raw["listing_id"])
        vision_run_id = int(raw["vision_run_id"])
        pass_name = _run_text(raw["pass_name"], "pass_name")
        criterion = _run_text(raw["criterion"], "criterion")
        model_name = _run_text(raw["model_name"], "model_name")
        model_version = _run_text(raw["model_version"], "model_version")
        prompt_version = _run_text(raw["prompt_version"], "prompt_version")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("vision proposal identity is malformed") from exc
    result_status = _enum_name(ResultStatus, raw.get("result_status"), "result_status")
    review_status = _enum_name(ReviewStatus, raw.get("review_status"), "review_status")
    confidence = _confidence(raw.get("confidence"))
    value_data = raw.get("value")
    image_indices = _image_indices(raw.get("image_indices"))
    if pass_name != "visual" or criterion != "owner_visual_assessment":
        raise ValueError("only the current owner visual assessment is supported")
    if result_status != ResultStatus.CATEGORY.value:
        raise ValueError("owner visual assessment must be scoreable")
    if not image_indices:
        raise ValueError("owner visual assessment requires image_indices")
    value_data = validate_visual_payload(value_data, image_indices)
    text_quotes = _string_list(raw.get("text_quotes"), "text_quotes")
    evidence = _string_list(raw.get("evidence"), "evidence")
    conflicts = _conflicts(raw.get("conflicts"))
    if conflicts:
        raise ValueError("owner visual assessment cannot contain conflicts")
    normalized = {
        "listing_id": listing_id,
        "vision_run_id": vision_run_id,
        "pass_name": pass_name,
        "criterion": criterion,
        "value": value_data,
        "confidence": confidence,
        "review_status": review_status,
        "result_status": result_status,
        "model_name": model_name,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "image_indices": image_indices,
        "text_quotes": text_quotes,
        "evidence": evidence,
        "conflicts": conflicts,
    }
    if review_status == ReviewStatus.VALIDATED.value and not proposal_is_scoreable(
        normalized
    ):
        raise ValueError(
            "validated proposal must contain a scoreable visual assessment"
        )
    return normalized


def insert_vision_proposals(
    conn: sqlite3.Connection,
    proposals: VisionProposal
    | Mapping[str, Any]
    | Sequence[VisionProposal | Mapping[str, Any]],
) -> list[int]:
    """Insert proposals atomically, checking the run/listing trust boundary."""

    if isinstance(proposals, (VisionProposal, Mapping)):
        items = [proposals]
    elif isinstance(proposals, Sequence) and not isinstance(
        proposals, (str, bytes, bytearray)
    ):
        items = list(proposals)
    else:
        raise TypeError("proposals must be a VisionProposal, mapping, or sequence")
    prepared = [_proposal_data(item) for item in items]
    ids: list[int] = []
    with _write_transaction(conn):
        for proposal in prepared:
            run = conn.execute(
                "SELECT listing_id FROM vision_runs WHERE id = ?",
                (proposal["vision_run_id"],),
            ).fetchone()
            if run is None:
                raise ValueError(
                    f"vision run {proposal['vision_run_id']} does not exist"
                )
            if int(run[0]) != proposal["listing_id"]:
                raise ValueError("proposal listing_id does not match its vision run")
            now = _now()
            cursor = conn.execute(
                """
                INSERT INTO vision_proposals
                  (listing_id, vision_run_id, pass_name, criterion, value_json,
                   confidence, review_status, result_status, model_name, model_version,
                   prompt_version, image_indices_json, text_quotes_json, evidence_json,
                   conflicts_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal["listing_id"],
                    proposal["vision_run_id"],
                    proposal["pass_name"],
                    proposal["criterion"],
                    _json(proposal["value"]) if proposal["value"] is not None else None,
                    proposal["confidence"],
                    proposal["review_status"],
                    proposal["result_status"],
                    proposal["model_name"],
                    proposal["model_version"],
                    proposal["prompt_version"],
                    _json(proposal["image_indices"]),
                    _json(proposal["text_quotes"]),
                    _json(proposal["evidence"]),
                    _json(proposal["conflicts"]),
                    now,
                    now,
                ),
            )
            ids.append(int(cursor.lastrowid))
    return ids


def review_proposal(
    conn: sqlite3.Connection,
    proposal_id: int,
    review_status: ReviewStatus,
    *,
    reason: str | None = None,
    vision_contract: tuple[str, str, str, str] | None = None,
) -> sqlite3.Row:
    """Validate or reject one current visual assessment."""

    review_status = _enum_name(ReviewStatus, review_status, "review_status")
    try:
        proposal_id = int(proposal_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("proposal_id must be a positive SQLite integer") from exc
    if proposal_id <= 0 or proposal_id > MAX_SQLITE_ID:
        raise ValueError("proposal_id must be a positive SQLite integer")
    reason_text = str(reason).strip() if reason is not None else ""
    if len(reason_text) > 2000:
        raise ValueError("reason must be at most 2000 characters")
    with _write_transaction(conn):
        row = conn.execute(
            "SELECT * FROM vision_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"vision proposal {proposal_id} does not exist")
        if row["review_status"] != ReviewStatus.PENDING.value:
            raise ValueError(f"vision proposal {proposal_id} has already been reviewed")
        _provider, model_name, _reasoning_effort, prompt_version = (
            vision_contract or _current_vision_contract()
        )
        if (
            current_vision_run_id(conn, int(row["listing_id"]), vision_contract)
            != int(row["vision_run_id"])
            or row["model_name"] != model_name
            or row["model_version"] != model_name
            or row["prompt_version"] != prompt_version
        ):
            raise ValueError(
                f"vision proposal {proposal_id} is stale and cannot be reviewed"
            )
        review_reason: str | None = None
        if review_status == ReviewStatus.VALIDATED.value:
            try:
                original_value = json.loads(row["value_json"] or "null")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("proposal value is malformed") from exc
            if (
                row["pass_name"] != "visual"
                or row["criterion"] != "owner_visual_assessment"
            ):
                raise ValueError(
                    "only the current owner visual assessment can be validated"
                )
            if row["result_status"] != ResultStatus.CATEGORY.value:
                raise ValueError("only a scoreable visual assessment can be validated")
            try:
                stored_image_indices = _image_indices(
                    json.loads(row["image_indices_json"] or "[]")
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("proposal image_indices are malformed") from exc
            value = validate_visual_payload(original_value, stored_image_indices)
            proposal = {
                "review_status": review_status,
                "result_status": ResultStatus.CATEGORY.value,
                "confidence": row["confidence"],
                "value": value,
            }
            if not proposal_is_scoreable(proposal):
                raise ValueError("only a scoreable visual assessment can be validated")
        elif review_status == ReviewStatus.REJECTED.value:
            if not reason_text:
                raise ValueError("rejected visual assessment requires a reason")
            review_reason = reason_text
        else:
            raise ValueError("manual review status must be validated or rejected")
        conn.execute(
            """
            UPDATE vision_proposals
            SET review_status = ?, review_reason = ?,
                reviewed_at = ?, updated_at = ?
            WHERE id = ? AND review_status = 'pending'
            """,
            (
                review_status,
                review_reason,
                _now(),
                _now(),
                proposal_id,
            ),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise sqlite3.IntegrityError(
                "vision proposal review changed an unexpected number of rows"
            )
        updated = conn.execute(
            "SELECT * FROM vision_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if updated is None:
            raise sqlite3.IntegrityError("vision proposal review did not return a row")
    return updated


def create_run(conn: sqlite3.Connection, parser_version: str) -> int:
    """Create one run record before any browser work starts."""

    with _write_transaction(conn):
        cursor = conn.execute(
            "INSERT INTO runs (started_at, parser_version, status) VALUES (?, ?, 'running')",
            (_now(), str(parser_version)),
        )
        return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    blocked_reason: str | None = None,
    cards_found: int = 0,
    cards_new: int = 0,
    cards_failed: int = 0,
    field_coverage: float | None = None,
    summary: dict[str, Any] | None = None,
) -> sqlite3.Row:
    """Close a run and atomically record its counters and structured summary."""

    if summary is None:
        summary = {}
    elif not isinstance(summary, dict):
        raise ValueError("summary must be a JSON object")
    summary_json = _json(summary)

    with _write_transaction(conn):
        changed = conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, blocked_reason = ?,
                cards_found = ?, cards_new = ?, cards_failed = ?, field_coverage = ?,
                summary_json = ?
            WHERE id = ? AND finished_at IS NULL
            """,
            (
                _now(),
                str(status),
                str(blocked_reason) if blocked_reason is not None else None,
                int(cards_found),
                int(cards_new),
                int(cards_failed),
                float(field_coverage) if field_coverage is not None else None,
                summary_json,
                int(run_id),
            ),
        ).rowcount
        if changed != 1:
            raise ValueError(f"run {run_id} is missing or already finished")
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("run update did not return a row")
    return row


_RUN_SUMMARY_FIELDS = frozenset(
    {
        "parser_version",
        "cards_found",
        "cards_new",
        "cards_changed",
        "cards_failed",
        "retries",
        "blocker",
        "field_coverage_p50",
        "photos_processed",
        "top_n_checks",
        "vision_attempts",
        "vision_failed",
        "visual_coverage",
        "manual_review_count",
        "json_export_sha256",
    }
)


def merge_run_summary(
    conn: sqlite3.Connection,
    run_id: int,
    updates: dict[str, Any],
) -> sqlite3.Row:
    """Merge known summary fields without dropping an earlier run metric."""

    if not isinstance(updates, dict):
        raise ValueError("summary updates must be a JSON object")
    unknown = set(updates) - _RUN_SUMMARY_FIELDS
    if unknown:
        raise ValueError(
            f"unsupported run summary fields: {', '.join(sorted(unknown))}"
        )
    with _write_transaction(conn):
        row = conn.execute(
            "SELECT summary_json FROM runs WHERE id = ?", (int(run_id),)
        ).fetchone()
        if row is None:
            raise ValueError(f"run {run_id} does not exist")
        try:
            current = json.loads(row[0] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"run {run_id} has invalid summary_json") from exc
        if not isinstance(current, dict):
            raise ValueError(f"run {run_id} summary_json is not an object")
        current.update({key: updates[key] for key in sorted(updates)})
        conn.execute(
            "UPDATE runs SET summary_json = ? WHERE id = ?",
            (_json(current), int(run_id)),
        )
        updated = conn.execute(
            "SELECT * FROM runs WHERE id = ?", (int(run_id),)
        ).fetchone()
        if updated is None:
            raise sqlite3.IntegrityError("run summary update did not return a row")
    return updated


def _facts_payload(facts: Any) -> dict[str, Any]:
    """Convert ListingFacts into the stable JSON shape used by snapshots."""

    source = str(getattr(facts, "source", "")).strip()
    if not source:
        raise ValueError("facts must contain a non-empty source")
    fields = getattr(facts, "fields", {})
    payload: dict[str, Any] = {}
    if isinstance(fields, dict):
        for name, field in fields.items():
            value = getattr(field, "value", field)
            status = getattr(field, "status", "confirmed")
            status = getattr(status, "value", status)
            evidence = []
            for item in getattr(field, "evidence", ()) or ():
                evidence.append(
                    {
                        "source": str(getattr(item, "source", "")),
                        "detail": str(getattr(item, "detail", item)),
                        "captured_at": str(getattr(item, "captured_at", "")),
                    }
                )
            payload[str(name)] = {
                "value": value,
                "status": str(status),
                "evidence": evidence,
            }
    return {
        "source": source,
        "source_listing_id": str(getattr(facts, "source_listing_id", "")),
        "source_url": str(getattr(facts, "source_url", "")),
        "fields": payload,
    }


def _resolve_enrichment_snapshot(
    conn: sqlite3.Connection,
    listing_id: int,
    facts: dict[str, Any],
    content_sha256: str,
    captured_at: str,
    append_snapshot: bool,
) -> tuple[int, bool]:
    if append_snapshot:
        existing = conn.execute(
            "SELECT id FROM listing_snapshots WHERE listing_id = ? AND content_sha256 = ?",
            (int(listing_id), content_sha256),
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                "INSERT INTO listing_snapshots (listing_id, captured_at, facts_json, content_sha256) VALUES (?, ?, ?, ?)",
                (int(listing_id), captured_at, _json(facts), content_sha256),
            )
            return int(cursor.lastrowid), True
        return int(existing[0]), False

    current = conn.execute(
        "SELECT id FROM listing_snapshots WHERE listing_id = ? ORDER BY id DESC LIMIT 1",
        (int(listing_id),),
    ).fetchone()
    if current is None:
        raise ValueError(f"listing {listing_id} has no facts snapshot")
    return int(current[0]), False


def _persist_enrichment_fields(
    conn: sqlite3.Connection,
    listing_id: int,
    snapshot_id: int,
    fields: dict[str, Any],
    captured_at: str,
) -> None:
    for field_name, raw in fields.items():
        if not isinstance(raw, dict):
            continue
        details = raw.get("evidence", [])
        if not isinstance(details, list):
            details = [details]
        confidence = str(raw.get("status", "unknown"))
        for detail in details:
            if isinstance(detail, dict):
                detail_confidence = str(detail.get("confidence", confidence))
                detail_text = _json(detail)
            else:
                detail_confidence, detail_text = confidence, str(detail)
            conn.execute(
                "INSERT INTO evidence (snapshot_id, field_name, source_kind, detail, confidence, captured_at) VALUES (?, ?, 'field', ?, ?, ?)",
                (
                    snapshot_id,
                    str(field_name),
                    detail_text,
                    detail_confidence,
                    captured_at,
                ),
            )
    photos = (
        fields.get("photos", {}).get("value")
        if isinstance(fields.get("photos"), dict)
        else None
    )
    _insert_photos(conn, int(listing_id), photos)


def _insert_enrichment_assessment_evidence(
    conn: sqlite3.Connection,
    snapshot_id: int,
    assessment: dict[str, Any],
    captured_at: str,
) -> None:
    for criterion, detail in assessment.items():
        if not isinstance(detail, dict):
            continue
        for evidence in detail.get("evidence", []) or []:
            evidence_text = _evidence_detail(evidence)
            confidence = str(detail.get("confidence", "unknown"))
            exists = conn.execute(
                "SELECT 1 FROM evidence WHERE snapshot_id = ? AND field_name = ? AND source_kind = 'assessment' AND detail = ? LIMIT 1",
                (snapshot_id, str(criterion), evidence_text),
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO evidence (snapshot_id, field_name, source_kind, detail, confidence, captured_at) VALUES (?, ?, 'assessment', ?, ?, ?)",
                    (
                        snapshot_id,
                        str(criterion),
                        evidence_text,
                        confidence,
                        captured_at,
                    ),
                )


def persist_enrichment_bundle(
    conn: sqlite3.Connection,
    listing_id: int,
    facts: dict[str, Any],
    assessment: dict[str, Any],
    auto_score: float,
    total_score: float,
    personal_score: float,
    completeness: float | None = None,
    status: str | None = None,
    append_snapshot: bool = True,
    max_scores: Mapping[str, float] | None = None,
) -> tuple[int, bool]:
    """Atomically persist facts, evidence and the recalculated assessment."""
    if not isinstance(facts, dict) or not isinstance(assessment, dict):
        raise ValueError("facts and assessment must be JSON objects")
    content_sha256 = _hash_json(_stable_content_value(facts))
    captured_at = _now()
    fields = facts.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    with _write_transaction(conn):
        listing = conn.execute(
            "SELECT id FROM listings WHERE id = ?", (int(listing_id),)
        ).fetchone()
        if listing is None:
            raise ValueError(f"listing {listing_id} does not exist")
        existing_assessment = conn.execute(
            "SELECT completeness, fact_coverage, visual_coverage, status FROM assessments WHERE listing_id = ?",
            (int(listing_id),),
        ).fetchone()
        snapshot_id, inserted = _resolve_enrichment_snapshot(
            conn,
            int(listing_id),
            facts,
            content_sha256,
            captured_at,
            append_snapshot,
        )
        if inserted:
            _persist_enrichment_fields(
                conn,
                int(listing_id),
                snapshot_id,
                fields,
                captured_at,
            )
        _insert_enrichment_assessment_evidence(
            conn,
            snapshot_id,
            assessment,
            captured_at,
        )
        completeness = float(
            completeness
            if completeness is not None
            else (existing_assessment[0] if existing_assessment else 0.0)
        )
        _validate_coverage(completeness, "completeness")
        fact_coverage = completeness
        visual_coverage = (
            0.0 if existing_assessment is None else float(existing_assessment[2])
        )
        status = str(
            status
            if status is not None
            else (existing_assessment[3] if existing_assessment else "reserve")
        )
        _upsert_assessment(
            conn,
            int(listing_id),
            float(auto_score),
            float(personal_score),
            float(total_score),
            completeness,
            fact_coverage,
            visual_coverage,
            status,
            _json(assessment),
            captured_at,
            max_scores,
        )
    return snapshot_id, inserted


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in {"api_key", "apikey", "twogis_api_key"}
            or _contains_secret_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_key(item) for item in value)
    return False


def record_commute_check(
    conn: sqlite3.Connection, listing_id: int, payload: Mapping[str, Any]
) -> sqlite3.Row:
    """Append one complete Yandex Maps route attempt without storing secrets."""

    if not isinstance(payload, Mapping):
        raise ValueError("commute payload must be an object")
    if _contains_secret_key(payload):
        raise ValueError("commute payload contains an API key")
    address = str(payload.get("address") or "").strip()
    address_sha256 = str(payload.get("address_sha256") or "").strip()
    destination = str(payload.get("destination") or "").strip()
    destination_sha256 = hashlib.sha256(
        " ".join(destination.casefold().split()).encode("utf-8")
    ).hexdigest()
    service_date = str(payload.get("service_date") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    status = str(payload.get("status") or "unknown")
    gate_status = str(payload.get("gate_status") or "unknown")
    if (
        not address
        or len(address_sha256) != 64
        or not destination
        or not service_date
        or provider != "yandex_maps"
    ):
        raise ValueError(
            "commute payload is missing address, provider, or service date"
        )
    if status not in {"success", "unknown"} or gate_status not in {
        "passed",
        "failed",
        "unknown",
    }:
        raise ValueError("commute payload has an invalid status")
    created_at = _text_time(payload.get("captured_at"))
    with _write_transaction(conn):
        if (
            conn.execute(
                "SELECT 1 FROM listings WHERE id = ?", (int(listing_id),)
            ).fetchone()
            is None
        ):
            raise ValueError(f"listing {listing_id} does not exist")
        cursor = conn.execute(
            """
            INSERT INTO commute_checks
              (listing_id, address_sha256, address, destination_sha256, destination,
               service_date, provider, status, gate_status,
               home_lat, home_lon, point_kind, building_id, entrance_id,
               geocode_precision, office_lat, office_lon, home_to_work_minutes,
               work_to_home_minutes, home_to_work_score, work_to_home_score,
               average_minutes, average_score, commute_score,
               error, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(listing_id),
                address_sha256,
                address,
                destination_sha256,
                destination,
                service_date,
                provider,
                status,
                gate_status,
                payload.get("home_lat"),
                payload.get("home_lon"),
                payload.get("point_kind"),
                payload.get("building_id"),
                payload.get("entrance_id"),
                payload.get("geocode_precision"),
                payload.get("office_lat"),
                payload.get("office_lon"),
                payload.get("home_to_work_minutes"),
                payload.get("work_to_home_minutes"),
                payload.get("home_to_work_score"),
                payload.get("work_to_home_score"),
                payload.get("average_minutes"),
                payload.get("average_score"),
                float(payload.get("commute_score") or 0),
                payload.get("error"),
                _json(dict(payload)),
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM commute_checks WHERE id = ?", (int(cursor.lastrowid),)
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("commute insert did not return a row")
    return row


def latest_office_point(
    conn: sqlite3.Connection, destination_sha256: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT office_lat, office_lon
        FROM commute_checks
        WHERE destination_sha256 = ? AND provider = 'yandex_maps'
          AND office_lat IS NOT NULL AND office_lon IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """,
        (str(destination_sha256),),
    ).fetchone()
    if row is None:
        return None
    return {"office_lat": row[0], "office_lon": row[1]}


def latest_commute_check(
    conn: sqlite3.Connection,
    listing_id: int,
    *,
    address_sha256: str | None = None,
    successful_only: bool = False,
) -> dict[str, Any] | None:
    clauses = ["listing_id = ?", "provider = 'yandex_maps'"]
    params: list[Any] = [int(listing_id)]
    if address_sha256 is not None:
        clauses.append("address_sha256 = ?")
        params.append(str(address_sha256))
    if successful_only:
        clauses.append("status = 'success'")
    row = conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- clauses are selected from fixed predicates; values remain parameterized.
        f"SELECT id, payload_json, created_at FROM commute_checks WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[1])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"commute check {row[0]} has invalid payload_json") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"commute check {row[0]} payload is not an object")
    payload["id"] = int(row[0])
    payload.setdefault("captured_at", str(row[2]))
    return payload


def commute_history(conn: sqlite3.Connection, listing_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, payload_json, created_at FROM commute_checks WHERE listing_id = ? ORDER BY id",
        (int(listing_id),),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row[1])
        if not isinstance(payload, dict):
            raise ValueError(f"commute check {row[0]} payload is not an object")
        payload["id"] = int(row[0])
        payload.setdefault("captured_at", str(row[2]))
        result.append(payload)
    return result


def record_park_check(
    conn: sqlite3.Connection, listing_id: int, payload: Mapping[str, Any]
) -> sqlite3.Row:
    """Append one 2GIS place plus Yandex Maps walking attempt."""

    if not isinstance(payload, Mapping):
        raise ValueError("park payload must be an object")
    if _contains_secret_key(payload):
        raise ValueError("park payload contains an API key")
    address = str(payload.get("address") or "").strip()
    address_sha256 = str(payload.get("address_sha256") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    status = str(payload.get("status") or "unknown")
    if (
        not address
        or len(address_sha256) != 64
        or provider != "2gis"
        or payload.get("route_provider") != "yandex_maps"
    ):
        raise ValueError("park payload is missing address or provider")
    if status not in {"success", "unknown"}:
        raise ValueError("park payload has an invalid status")
    created_at = _text_time(payload.get("captured_at"))
    with _write_transaction(conn):
        if (
            conn.execute(
                "SELECT 1 FROM listings WHERE id = ?", (int(listing_id),)
            ).fetchone()
            is None
        ):
            raise ValueError(f"listing {listing_id} does not exist")
        cursor = conn.execute(
            """
            INSERT INTO park_checks
              (listing_id, address_sha256, address, provider, status,
               home_lat, home_lon, place_id, place_name, place_type,
               place_lat, place_lon, area_hectares, quality, walking_minutes,
               walking_distance_m, park_score, error, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(listing_id),
                address_sha256,
                address,
                provider,
                status,
                payload.get("home_lat"),
                payload.get("home_lon"),
                payload.get("place_id"),
                payload.get("place_name"),
                payload.get("place_type"),
                payload.get("place_lat"),
                payload.get("place_lon"),
                payload.get("area_hectares"),
                payload.get("quality"),
                payload.get("walking_minutes"),
                payload.get("walking_distance_m"),
                float(payload.get("park_score") or 0),
                payload.get("error"),
                _json(dict(payload)),
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM park_checks WHERE id = ?", (int(cursor.lastrowid),)
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("park insert did not return a row")
    return row


def latest_park_check(
    conn: sqlite3.Connection,
    listing_id: int,
    *,
    address_sha256: str | None = None,
    successful_only: bool = False,
) -> dict[str, Any] | None:
    clauses = [
        "listing_id = ?",
        "json_extract(payload_json, '$.route_provider') = 'yandex_maps'",
    ]
    params: list[Any] = [int(listing_id)]
    if address_sha256 is not None:
        clauses.append("address_sha256 = ?")
        params.append(str(address_sha256))
    if successful_only:
        clauses.append("status = 'success'")
    row = conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- clauses are selected from fixed predicates; values remain parameterized.
        f"SELECT id, payload_json, created_at FROM park_checks WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[1])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"park check {row[0]} has invalid payload_json") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"park check {row[0]} payload is not an object")
    payload["id"] = int(row[0])
    payload.setdefault("captured_at", str(row[2]))
    return payload


def record_fitness_check(
    conn: sqlite3.Connection, listing_id: int, payload: Mapping[str, Any]
) -> sqlite3.Row:
    """Append one 2GIS place plus Yandex Maps walking attempt."""

    if not isinstance(payload, Mapping):
        raise ValueError("fitness payload must be an object")
    if _contains_secret_key(payload):
        raise ValueError("fitness payload contains an API key")
    address = str(payload.get("address") or "").strip()
    address_sha256 = str(payload.get("address_sha256") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    status = str(payload.get("status") or "unknown")
    if (
        not address
        or len(address_sha256) != 64
        or provider != "2gis"
        or payload.get("route_provider") != "yandex_maps"
    ):
        raise ValueError("fitness payload is missing address or provider")
    if status not in {"success", "unknown"}:
        raise ValueError("fitness payload has an invalid status")
    created_at = _text_time(payload.get("captured_at"))
    with _write_transaction(conn):
        if (
            conn.execute(
                "SELECT 1 FROM listings WHERE id = ?", (int(listing_id),)
            ).fetchone()
            is None
        ):
            raise ValueError(f"listing {listing_id} does not exist")
        cursor = conn.execute(
            """
            INSERT INTO fitness_checks
              (listing_id, address_sha256, address, provider, status,
               home_lat, home_lon, place_id, place_name, place_lat, place_lon,
               rating, review_count, sauna, quality, walking_minutes,
               walking_distance_m, fitness_score, error, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(listing_id),
                address_sha256,
                address,
                provider,
                status,
                payload.get("home_lat"),
                payload.get("home_lon"),
                payload.get("place_id"),
                payload.get("place_name"),
                payload.get("place_lat"),
                payload.get("place_lon"),
                payload.get("rating"),
                payload.get("review_count"),
                int(bool(payload.get("sauna"))),
                float(payload.get("quality") or 0),
                payload.get("walking_minutes"),
                payload.get("walking_distance_m"),
                float(payload.get("fitness_score") or 0),
                payload.get("error"),
                _json(dict(payload)),
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM fitness_checks WHERE id = ?", (int(cursor.lastrowid),)
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("fitness insert did not return a row")
    return row


def latest_fitness_check(
    conn: sqlite3.Connection,
    listing_id: int,
    *,
    address_sha256: str | None = None,
    successful_only: bool = False,
) -> dict[str, Any] | None:
    clauses = [
        "listing_id = ?",
        "json_extract(payload_json, '$.route_provider') = 'yandex_maps'",
    ]
    params: list[Any] = [int(listing_id)]
    if address_sha256 is not None:
        clauses.append("address_sha256 = ?")
        params.append(str(address_sha256))
    if successful_only:
        clauses.append("status = 'success'")
    row = conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- clauses are selected from fixed predicates; values remain parameterized.
        f"SELECT id, payload_json, created_at FROM fitness_checks WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[1])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"fitness check {row[0]} has invalid payload_json") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"fitness check {row[0]} payload is not an object")
    payload["id"] = int(row[0])
    payload.setdefault("captured_at", str(row[2]))
    return payload


def _latest_check_at_point(
    conn: sqlite3.Connection,
    table: str,
    lat: float,
    lon: float,
) -> dict[str, Any] | None:
    if table not in {"park_checks", "fitness_checks"}:
        raise ValueError("unsupported amenity check table")
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("amenity coordinates must be finite numbers") from exc
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise ValueError("amenity coordinates must be finite numbers")
    row = conn.execute(
        f"SELECT id, payload_json, created_at FROM {table} "
        "WHERE status = 'success' AND home_lat = ? AND home_lon = ? "
        "AND json_extract(payload_json, '$.route_provider') = 'yandex_maps' "
        "ORDER BY id DESC LIMIT 1",
        (lat, lon),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[1])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{table} check {row[0]} has invalid payload_json") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{table} check {row[0]} payload is not an object")
    payload["id"] = int(row[0])
    payload.setdefault("captured_at", str(row[2]))
    return payload


def latest_park_check_at_point(
    conn: sqlite3.Connection, lat: float, lon: float
) -> dict[str, Any] | None:
    return _latest_check_at_point(conn, "park_checks", lat, lon)


def latest_fitness_check_at_point(
    conn: sqlite3.Connection, lat: float, lon: float
) -> dict[str, Any] | None:
    return _latest_check_at_point(conn, "fitness_checks", lat, lon)


def persist_listing(
    conn: sqlite3.Connection,
    facts: Any,
    scores: dict[str, float],
    total_score: float,
    completeness: float,
    assessment: dict[str, Any],
    parser_version: str,
    personal_score: float = 0.0,
    status: str = "reserve",
    max_scores: Mapping[str, float] | None = None,
) -> int:
    """Persist one extracted listing, snapshot, evidence, photos and score.

    The complete write is one transaction so a blocker observed immediately
    before this call cannot leave a half-written listing behind.
    """

    source_id = str(getattr(facts, "source_listing_id", ""))
    source_url = str(getattr(facts, "source_url", ""))
    if not source_id or not source_url:
        raise ValueError("facts must contain source_listing_id and source_url")
    _validate_coverage(completeness, "completeness")
    facts_payload = _facts_payload(facts)
    content_sha256 = _hash_json(_stable_content_value(facts_payload))
    captured_at = _now()
    source = str(facts_payload["source"])
    assessment_json = _json(assessment)
    with _write_transaction(conn):
        conn.execute(
            """
            INSERT INTO listings
              (source, source_listing_id, source_url, first_seen_at, last_seen_at,
               content_sha256, parser_version, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(source, source_listing_id) DO UPDATE SET
              source_url = excluded.source_url,
              last_seen_at = excluded.last_seen_at,
              content_sha256 = excluded.content_sha256,
              parser_version = excluded.parser_version,
              state = 'active'
            """,
            (
                source,
                source_id,
                source_url,
                captured_at,
                captured_at,
                content_sha256,
                str(parser_version),
            ),
        )
        listing = conn.execute(
            "SELECT id FROM listings WHERE source = ? AND source_listing_id = ?",
            (source, source_id),
        ).fetchone()
        if listing is None:
            raise sqlite3.IntegrityError("listing upsert did not return a row")
        listing_id = int(listing[0])
        snapshot = conn.execute(
            "SELECT id FROM listing_snapshots WHERE listing_id = ? AND content_sha256 = ?",
            (listing_id, content_sha256),
        ).fetchone()
        if snapshot is None:
            cursor = conn.execute(
                """
                INSERT INTO listing_snapshots
                  (listing_id, captured_at, facts_json, content_sha256)
                VALUES (?, ?, ?, ?)
                """,
                (listing_id, captured_at, _json(facts_payload), content_sha256),
            )
            snapshot_id = int(cursor.lastrowid)
            _insert_evidence(conn, snapshot_id, assessment, captured_at)
            fields = facts_payload.get("fields", {})
            photos = (
                fields.get("photos", {}).get("value")
                if isinstance(fields, dict)
                else None
            )
            _insert_photos(conn, listing_id, photos)
        _upsert_assessment(
            conn,
            listing_id,
            float(sum(value for name, value in scores.items() if name != "personal")),
            float(personal_score),
            float(total_score),
            float(completeness),
            float(completeness),
            0.0,
            str(status),
            assessment_json,
            captured_at,
            max_scores,
        )
    return listing_id


def reconcile_listing_states(
    conn: sqlite3.Connection,
    source: str,
    seen_source_ids: Sequence[str],
) -> tuple[int, int]:
    """Activate seen listings and mark other active source listings inactive."""

    source = str(source).strip()
    source_ids = tuple(
        dict.fromkeys(
            str(value).strip() for value in seen_source_ids if str(value).strip()
        )
    )
    if not source or not source_ids:
        raise ValueError(
            "state reconciliation requires a source and non-empty discovery"
        )
    placeholders = ",".join("?" for _ in source_ids)
    with _write_transaction(conn):
        reactivated = conn.execute(
            f"SELECT COUNT(*) FROM listings WHERE source = ? AND state != 'active' "
            f"AND source_listing_id IN ({placeholders})",
            (source, *source_ids),
        ).fetchone()[0]
        unpublished = conn.execute(
            f"SELECT COUNT(*) FROM listings WHERE source = ? AND state = 'active' "
            f"AND source_listing_id NOT IN ({placeholders})",
            (source, *source_ids),
        ).fetchone()[0]
        conn.execute(
            f"UPDATE listings SET state = 'active' WHERE source = ? "
            f"AND source_listing_id IN ({placeholders})",
            (source, *source_ids),
        )
        conn.execute(
            f"UPDATE listings SET state = 'inactive' WHERE source = ? AND state = 'active' "
            f"AND source_listing_id NOT IN ({placeholders})",
            (source, *source_ids),
        )
    return int(reactivated), int(unpublished)


def _personal_assessment_row(
    conn: sqlite3.Connection, identifier: int | str
) -> sqlite3.Row:
    try:
        listing_id = int(identifier)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("listing_id must be a positive internal id") from exc
    if listing_id <= 0:
        raise ValueError("listing_id must be a positive internal id")
    row = conn.execute(
        """
        SELECT a.listing_id, a.auto_score, a.assessment_json
        FROM assessments AS a
        WHERE a.listing_id = ?
        """,
        (listing_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"listing {identifier!r} has no assessment")
    return row


def update_personal_score(
    conn: sqlite3.Connection,
    identifier: int | str,
    score: float,
    *,
    max_scores: Mapping[str, float] | None = None,
) -> sqlite3.Row:
    """Set a bounded manual score, mark the listing rated, and clear dislike."""

    automatic_max, personal_max, _total_max = score_maxima(max_scores)
    value = _bounded_float(score, "personal score", personal_max)
    with _write_transaction(conn):
        row = _personal_assessment_row(conn, identifier)
        assessment_json = (
            row["assessment_json"] if isinstance(row, sqlite3.Row) else row[2]
        )
        listing_id = int(row["listing_id"] if isinstance(row, sqlite3.Row) else row[0])
        auto_score = _bounded_float(
            row["auto_score"] if isinstance(row, sqlite3.Row) else row[1],
            "automatic score",
            automatic_max,
        )
        assessment = json.loads(assessment_json)
        if not isinstance(assessment, dict):
            assessment = {}
        personal = assessment.get("personal")
        if isinstance(personal, dict):
            personal.update(
                {
                    "score": value,
                    "evidence": ["Manual score set by user."],
                    "confidence": "confirmed",
                }
            )
        else:
            assessment["personal"] = {
                "score": value,
                "evidence": ["Manual score set by user."],
                "confidence": "confirmed",
            }
        total = auto_score + value
        now = _now()
        conn.execute(
            """
            UPDATE assessments
            SET personal_score = ?, total_score = ?, assessment_json = ?,
                personal_rated_at = ?, disliked_at = NULL, updated_at = ?
            WHERE listing_id = ?
            """,
            (value, total, _json(assessment), now, now, listing_id),
        )
        updated = conn.execute(
            "SELECT * FROM assessments WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        if updated is None:
            raise sqlite3.IntegrityError("personal score update did not return a row")
    return updated


def set_listing_disliked(
    conn: sqlite3.Connection, identifier: int | str, disliked: bool
) -> sqlite3.Row:
    """Hide or restore one listing without changing its score or rating."""

    if not isinstance(disliked, bool):
        raise ValueError("disliked must be a boolean")
    with _write_transaction(conn):
        row = _personal_assessment_row(conn, identifier)
        listing_id = int(row["listing_id"] if isinstance(row, sqlite3.Row) else row[0])
        now = _now()
        conn.execute(
            """
            UPDATE assessments
            SET disliked_at = ?, favorited_at = CASE WHEN ? THEN NULL ELSE favorited_at END,
                updated_at = ?
            WHERE listing_id = ?
            """,
            (now if disliked else None, disliked, now, listing_id),
        )
        updated = conn.execute(
            "SELECT * FROM assessments WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        if updated is None:
            raise sqlite3.IntegrityError("listing dislike update did not return a row")
    return updated


def set_listing_favorited(
    conn: sqlite3.Connection, identifier: int | str, favorited: bool
) -> sqlite3.Row:
    """Add or remove one listing from favorites, restoring it when added."""

    if not isinstance(favorited, bool):
        raise ValueError("favorited must be a boolean")
    with _write_transaction(conn):
        row = _personal_assessment_row(conn, identifier)
        listing_id = int(row["listing_id"] if isinstance(row, sqlite3.Row) else row[0])
        now = _now()
        conn.execute(
            """
            UPDATE assessments
            SET favorited_at = ?, disliked_at = CASE WHEN ? THEN NULL ELSE disliked_at END,
                updated_at = ?
            WHERE listing_id = ?
            """,
            (now if favorited else None, favorited, now, listing_id),
        )
        updated = conn.execute(
            "SELECT * FROM assessments WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        if updated is None:
            raise sqlite3.IntegrityError("listing favorite update did not return a row")
    return updated
