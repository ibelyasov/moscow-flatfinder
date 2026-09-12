"""Named SQLite queries used by FlatFinder application workflows."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VisionInputRows:
    """Raw persisted inputs needed to construct one Vision request."""

    snapshot: sqlite3.Row | None
    full_text: sqlite3.Row | None
    photos: list[sqlite3.Row]


def recent_coverage_rows(
    conn: sqlite3.Connection, parser_version: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT field_coverage
        FROM runs
        WHERE parser_version = ? AND field_coverage IS NOT NULL
        ORDER BY id DESC
        LIMIT 5
        """,
        (str(parser_version),),
    ).fetchall()


def listing_id_by_source(
    conn: sqlite3.Connection, source: str, source_listing_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id
        FROM listings
        WHERE source = ? AND source_listing_id = ?
        LIMIT 1
        """,
        (str(source), str(source_listing_id)),
    ).fetchone()


def previous_listing_assessment(
    conn: sqlite3.Connection, source: str, source_listing_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT l.id, l.content_sha256, a.personal_score, a.assessment_json
        FROM listings AS l
        LEFT JOIN assessments AS a ON a.listing_id = l.id
        WHERE l.source = ? AND l.source_listing_id = ?
        """,
        (str(source), str(source_listing_id)),
    ).fetchone()


def processable_listing_links(
    conn: sqlite3.Connection,
    source: str,
    links: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Keep hidden and inactive listings out of detail processing."""

    rows = conn.execute(
        """
        SELECT l.source_listing_id
        FROM listings AS l
        LEFT JOIN assessments AS a ON a.listing_id = l.id
        WHERE l.source = ? AND (a.disliked_at IS NOT NULL OR l.state != 'active')
        """,
        (str(source),),
    ).fetchall()
    skipped = {str(row[0]) for row in rows}
    return [(source_id, url) for source_id, url in links if source_id not in skipped]


def enrichment_candidate_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT l.id AS listing_id, l.source_listing_id, l.source_url, l.first_seen_at,
               a.auto_score, a.completeness, a.status,
               (SELECT s.facts_json FROM listing_snapshots AS s
                WHERE s.listing_id = l.id
                  AND s.content_sha256 = l.content_sha256
                ORDER BY s.id DESC LIMIT 1) AS facts_json,
               l.source AS source
        FROM assessments AS a JOIN listings AS l ON l.id = a.listing_id
        LEFT JOIN listing_duplicates AS d ON d.listing_id = l.id
        LEFT JOIN listings AS canonical ON canonical.id = d.canonical_listing_id
        WHERE l.state = 'active' AND a.disliked_at IS NULL
          AND (d.listing_id IS NULL OR canonical.state != 'active')
        """
    ).fetchall()


def listing_content_hash(
    conn: sqlite3.Connection, listing_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT content_sha256 FROM listings WHERE id = ?", (int(listing_id),)
    ).fetchone()


def listing_vision_state(
    conn: sqlite3.Connection, listing_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT vision_content_hash, state FROM listings WHERE id = ?",
        (int(listing_id),),
    ).fetchone()


def latest_vision_status(
    conn: sqlite3.Connection, listing_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT status
        FROM vision_runs
        WHERE listing_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(listing_id),),
    ).fetchone()


def latest_vision_run_metadata(
    conn: sqlite3.Connection, listing_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT content_hash, status, schema_valid, provider, model_name, model_version,
               reasoning_effort, prompt_version, visual_coverage
        FROM vision_runs
        WHERE listing_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(listing_id),),
    ).fetchone()


def vision_input_rows(conn: sqlite3.Connection, listing_id: int) -> VisionInputRows:
    listing_id = int(listing_id)
    snapshot = conn.execute(
        """
        SELECT facts_json
        FROM listing_snapshots
        WHERE listing_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (listing_id,),
    ).fetchone()
    full_text = conn.execute(
        """
        SELECT text, quotes_json, content_sha256, captured_at
        FROM full_text
        WHERE listing_id = ?
        """,
        (listing_id,),
    ).fetchone()
    photos = conn.execute(
        """
        SELECT id, image_index, source_url, local_path, sha256, dhash,
               duplicate_of, status, error, raw_source_url
        FROM photo_ingestion
        WHERE listing_id = ?
        ORDER BY image_index
        """,
        (listing_id,),
    ).fetchall()
    return VisionInputRows(snapshot=snapshot, full_text=full_text, photos=photos)


def mark_listing_inactive(
    conn: sqlite3.Connection, source: str, source_listing_id: str
) -> int:
    """Mark one listing inactive without committing the caller's transaction."""

    cursor = conn.execute(
        """
        UPDATE listings
        SET state = 'inactive'
        WHERE source = ? AND source_listing_id = ? AND state != 'inactive'
        """,
        (str(source), str(source_listing_id)),
    )
    return max(0, int(cursor.rowcount))


def latest_facts_row(conn: sqlite3.Connection, listing_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT s.id, s.facts_json, l.source
        FROM listing_snapshots AS s
        JOIN listings AS l ON l.id = s.listing_id
        WHERE s.listing_id = ?
        ORDER BY s.id DESC
        LIMIT 1
        """,
        (int(listing_id),),
    ).fetchone()


def assessment_row(conn: sqlite3.Connection, listing_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT assessment_json, personal_score, completeness, total_score, status
        FROM assessments
        WHERE listing_id = ?
        """,
        (int(listing_id),),
    ).fetchone()


def validated_vision_proposal_rows(
    conn: sqlite3.Connection,
    listing_id: int,
    vision_contract: tuple[str, str, str, str],
) -> list[sqlite3.Row]:
    from .storage import current_vision_run_id

    _, model_name, _, prompt_version = vision_contract
    vision_run_id = current_vision_run_id(conn, listing_id, vision_contract)
    if vision_run_id is None:
        return []
    return conn.execute(
        """
        SELECT vp.*
        FROM vision_proposals AS vp
        WHERE vp.vision_run_id = ?
          AND vp.listing_id = ?
          AND vp.review_status = 'validated'
          AND vp.result_status = 'category'
          AND vp.model_name = ?
          AND vp.model_version = ?
          AND vp.prompt_version = ?
        ORDER BY id
        """,
        (
            vision_run_id,
            int(listing_id),
            model_name,
            model_name,
            prompt_version,
        ),
    ).fetchall()


def retry_route_rows(
    conn: sqlite3.Connection, listing_id: int | None = None
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT l.id, l.source_listing_id, l.source_url,
               (SELECT s.facts_json FROM listing_snapshots AS s
                WHERE s.listing_id = l.id ORDER BY s.id DESC LIMIT 1) AS facts_json,
               l.source AS source
        FROM listings AS l
        JOIN assessments AS a ON a.listing_id = l.id
        WHERE l.state = 'active'
          AND (a.disliked_at IS NULL OR ? IS NOT NULL)
          AND (? IS NULL OR l.id = ?)
        ORDER BY l.id
        """,
        (listing_id, listing_id, listing_id),
    ).fetchall()


def coordinate_rows(
    conn: sqlite3.Connection,
    listing_id: int | None = None,
    after_id: int | None = None,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT l.id, l.source_listing_id, l.source_url,
               (SELECT s.facts_json FROM listing_snapshots AS s
                WHERE s.listing_id = l.id ORDER BY s.id DESC LIMIT 1) AS facts_json,
               l.source AS source
        FROM listings AS l
        JOIN assessments AS a ON a.listing_id = l.id
        WHERE l.state = 'active'
          AND a.disliked_at IS NULL
          AND (? IS NULL OR l.id = ?)
          AND (? IS NULL OR l.id > ?)
        ORDER BY l.id
        """,
        (listing_id, listing_id, after_id, after_id),
    ).fetchall()


def new_candidate_count(conn: sqlite3.Connection, run_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT l.id)
        FROM listings AS l
        JOIN assessments AS a ON a.listing_id = l.id
        JOIN runs AS r ON r.id = ?
        LEFT JOIN listing_duplicates AS d ON d.listing_id = l.id
        LEFT JOIN listings AS canonical ON canonical.id = d.canonical_listing_id
        WHERE l.first_seen_at > r.started_at
          AND (r.finished_at IS NULL OR l.first_seen_at <= r.finished_at)
          AND a.status IN ('priority', 'good')
          AND (d.listing_id IS NULL OR canonical.state != 'active')
        """,
        (int(run_id),),
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0


def has_new_three_failed_run_streak(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT status
        FROM runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 4
        """
    ).fetchall()
    statuses = [str(row[0]) for row in rows]
    return (
        len(statuses) >= 3
        and statuses[:3] == ["failed"] * 3
        and (len(statuses) == 3 or statuses[3] != "failed")
    )


def database_health(conn: sqlite3.Connection) -> tuple[str, int]:
    """Return SQLite integrity and schema version for the doctor command."""

    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    schema = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return integrity, schema


def reassessment_listing_ids(
    conn: sqlite3.Connection, listing_id: int | None = None
) -> list[int]:
    """Select all active listings or one explicitly requested existing listing."""

    rows = conn.execute(
        "SELECT id FROM listings WHERE (? IS NULL AND state = 'active') OR id = ? ORDER BY id",
        (listing_id, listing_id),
    ).fetchall()
    if listing_id is not None and not rows:
        raise ValueError(f"listing {listing_id!r} was not found")
    return [int(row[0]) for row in rows]
