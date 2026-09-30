"""Explicit offline import into a new database; never opens private data implicitly."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

from flatfinder.database import Database
from flatfinder.models import FACT_FIELD_NAMES, PhotoInput, facts_from_dict
from flatfinder.scoring_policy import normalize_policy
from flatfinder.sources.common import collect_photo_urls

# These measurements require a new input/provider identity. The complete original
# field and its evidence remain in the technical archive.
_DERIVED_FIELDS = {
    "route",
    "route_minutes",
    "commute",
    "park",
    "fitness",
    "noise",
    "repair_visual",
    "layout_visual",
    "light_view",
    "visual_coverage",
}


def read_source(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Offline immutable read boundary; live WAL/journal sources are rejected."""
    source = Path(path).expanduser().resolve()
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("offline v17 source must have no pending WAL or journal")
    conn = sqlite3.connect(
        source.as_uri() + "?mode=ro&immutable=1", uri=True, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != 17:
            raise ValueError("offline source must use schema 17")
        if (
            conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or conn.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("offline source fails SQLite integrity checks")
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        result = {}
        for name in tables:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("unsupported legacy table name")
            records = []
            for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
                records.append(dict(row))
            result[name] = records
        return result
    finally:
        conn.close()


def _archive_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$sqlite_blob_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        return {"$sqlite_float": str(value)}
    if isinstance(value, dict):
        return {key: _archive_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_archive_value(item) for item in value]
    return value


def _facts(snapshot: dict[str, Any], listing: dict[str, Any]):
    payload = json.loads(snapshot["facts_json"])
    payload["measurement_context"] = None
    for key in ("source", "source_listing_id"):
        if payload.get(key) != listing[key]:
            raise ValueError(
                f"snapshot identity disagrees with listing {listing['id']}"
            )
    payload["fields"] = {
        key: value
        for key, value in payload["fields"].items()
        if key in FACT_FIELD_NAMES
        and key not in _DERIVED_FIELDS
        and not any(
            str(item.get("source", ""))
            .lower()
            .startswith(("2gis", "twogis", "yandex_route", "noise", "vision"))
            for item in value.get("evidence", [])
        )
    }
    for value in payload["fields"].values():
        for item in value.get("evidence", []):
            item.pop("confidence", None)
    return facts_from_dict(payload)


def transform(rows: dict[str, list[dict[str, Any]]], *, photo_root: Path | None = None):
    """Convert one readonly legacy snapshot without inferring lost chronology."""
    listings = {int(row["id"]): row for row in rows["listings"]}
    snapshots: dict[int, list[dict[str, Any]]] = {}
    for row in rows["listing_snapshots"]:
        snapshots.setdefault(int(row["listing_id"]), []).append(row)
    manual = {int(row["listing_id"]): row for row in rows.get("assessments", [])}
    ingestions = {
        (int(row["listing_id"]), int(row["image_index"])): row
        for row in rows.get("photo_ingestion", [])
    }
    root = photo_root.expanduser().resolve() if photo_root is not None else None
    records, archive = [], []
    withheld_paths = 0
    for listing_id, listing in sorted(listings.items()):
        old = sorted(snapshots.get(listing_id, []), key=lambda row: int(row["id"]))
        matching = [
            index
            for index, row in enumerate(old)
            if row["content_sha256"] == listing["content_sha256"]
        ]
        if len(matching) != 1:
            raise ValueError(
                f"listing {listing_id} has no unique current-hash snapshot"
            )
        observations = [
            {
                "facts": _facts(row, listing),
                "kind": "import",
                "parser_version": "v17-content-snapshot:no-recurrence-history",
                "captured_at": row["captured_at"],
            }
            for row in old
        ]
        current_index = matching[0]
        current = observations[current_index]["facts"]
        gallery = current.fields.get("photos")
        entries = (
            gallery.value if gallery is not None and gallery.value is not None else []
        )
        urls = collect_photo_urls(current)
        raw_urls: dict[str, str] = {}
        for entry in entries:
            if isinstance(entry, str):
                url = raw_url = entry
            else:
                url, raw_url = entry["url"], entry["source_url"]
            raw_urls.setdefault(url, raw_url)
        photos = []
        for index, url in enumerate(urls):
            raw_url = raw_urls[url]
            entry = ingestions.get((listing_id, index), {})
            matched = bool(
                {url, raw_url} & {entry.get("source_url"), entry.get("raw_source_url")}
            )
            local_path = entry.get("local_path") if matched else None
            if local_path:
                path = Path(local_path).expanduser()
                if (
                    root is None
                    or not path.is_absolute()
                    or not path.resolve().is_relative_to(root)
                ):
                    withheld_paths += 1
                    local_path = None
            photos.append(
                PhotoInput(
                    listing_id=listing_id,
                    image_index=index,
                    source_url=url,
                    raw_source_url=entry.get("raw_source_url") or raw_url
                    if matched
                    else raw_url,
                    local_path=local_path,
                    sha256=entry.get("sha256") if local_path else None,
                    dhash=entry.get("dhash") if local_path else None,
                    status="indexed"
                    if local_path and entry.get("sha256")
                    else "failed",
                    error=None
                    if local_path and entry.get("sha256")
                    else "not_downloaded",
                )
            )
        decision = manual.get(listing_id, {})
        records.append(
            {
                "id": listing_id,
                "facts": current,
                "observations": observations,
                "current_observation_index": current_index,
                # v17 inactive meant source-wide reconciliation, not proven withdrawal.
                "availability": "available"
                if listing["state"] == "active"
                else "unknown",
                "first_seen_at": listing["first_seen_at"],
                "last_seen_at": listing["last_seen_at"],
                "manual": {
                    "personal_score": decision.get("personal_score", 0),
                    "favorite": bool(decision.get("favorited_at")),
                    "disliked": bool(decision.get("disliked_at")),
                    "personal_rated_at": decision.get("personal_rated_at"),
                    "favorited_at": decision.get("favorited_at"),
                    "disliked_at": decision.get("disliked_at"),
                },
                "photos": photos,
            }
        )
    snapshot_listing = {
        row["id"]: row["listing_id"] for row in rows["listing_snapshots"]
    }
    for table, items in sorted(rows.items()):
        for index, row in enumerate(items):
            listing_id = row.get("listing_id")
            if table == "listings":
                listing_id = row["id"]
            elif table == "evidence":
                listing_id = snapshot_listing.get(row.get("snapshot_id"))
            archive.append(
                {
                    "source_schema": "17",
                    "source_table": table,
                    "source_key": f"{index}:{row.get('id', row.get('listing_id', 'row'))}",
                    "listing_id": listing_id if listing_id in listings else None,
                    "payload": _archive_value(row),
                }
            )
    return records, archive, withheld_paths


def publish_new_database(target: Path, populate: Callable[[Database], Any]) -> Any:
    """Publish a completed, checked database atomically without replacing any file."""
    target = target.expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"refusing to overwrite: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".flatfinder-import-", dir=target.parent
    ) as staging:
        staged = Path(staging) / "database.sqlite3"
        with Database.initialize(staged) as database:
            result = populate(database)
            health = database.health()
            if health["integrity"] != "ok" or health["foreign_key_errors"]:
                raise ValueError("new database failed integrity or foreign-key checks")
        with staged.open("rb") as file:
            os.fsync(file.fileno())
        # A hard link is an atomic no-clobber publication on the same filesystem.
        os.link(staged, target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return result


def import_v17(
    source: Path, target: Path, *, photo_root: Path | None = None
) -> dict[str, Any]:
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("source must be an existing database file")

    def source_hash():
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise ValueError(
                    "source must be offline with no pending WAL or rollback journal"
                )
        with source.open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()

    before = source_hash()
    rows = read_source(source)
    records, archive, withheld_paths = transform(rows, photo_root=photo_root)
    policy = normalize_policy(vision_scoring_enabled=False)
    personal_max = max(
        [policy["max_scores"]["personal"]]
        + [record["manual"]["personal_score"] for record in records]
    )
    policy = normalize_policy(
        max_scores={"personal": personal_max}, vision_scoring_enabled=False
    )

    def populate(database):
        database.import_records(records, archive, policy)
        after = source_hash()
        if after != before:
            raise ValueError(
                "source changed during offline import; target was not published"
            )

    publish_new_database(target, populate)
    return {
        "target": str(target),
        "source_sha256": before,
        "listings": len(records),
        "archived_rows": len(archive),
        "import_policy_fingerprint": policy["fingerprint"],
        "personal_max": personal_max,
        "withheld_local_photo_references": withheld_paths,
        "limitations": [
            "v17 deduplicated content snapshots cannot recover recurrence chronology",
            "legacy Geo, Vision, assessments and search presence remain archived",
            "local photo references require explicit --photo-root; files are never copied",
        ],
        "integrity": "ok",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument(
        "--photo-root",
        type=Path,
        help="explicit same-runtime root for retained local references; never copies photos",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            import_v17(args.source, args.target, photo_root=args.photo_root),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
