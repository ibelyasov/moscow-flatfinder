"""Create a screenshot-safe MoscowFlatFinder database copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_PHOTO_TABLES = ("photo_ingestion", "photos")
_TEXT_KEYS = {
    "address",
    "address_name",
    "destination",
    "description",
    "evidence",
    "full_text",
    "full_address_name",
    "full_name",
    "location",
    "name",
    "place_name",
    "q",
    "source_listing_id",
    "source_url",
    "title",
    "url",
}
_PHOTO_KEYS = {"images", "photo_urls", "photos", "photos_observed"}


def _quote_identifier(value: str) -> str:
    """Quote a SQLite identifier discovered from an untrusted database schema."""
    return '"' + value.replace('"', '""') + '"'


def _execute_schema_sql(
    conn: sqlite3.Connection, statement: str, parameters: tuple[Any, ...] = ()
) -> sqlite3.Cursor:
    """Execute SQL whose dynamic identifiers were quoted by `_quote_identifier`."""
    # Values remain bound parameters; only reviewed, quoted identifiers reach here.
    return conn.execute(
        statement, parameters
    )  # nosemgrep: sqlalchemy-execute-raw-query


def _executemany_schema_sql(
    conn: sqlite3.Connection,
    statement: str,
    parameters: Iterable[tuple[Any, ...]],
) -> sqlite3.Cursor:
    """Execute repeated SQL with identifiers quoted by `_quote_identifier`."""
    # Values remain bound parameters; only reviewed, quoted identifiers reach here.
    return conn.executemany(  # nosemgrep: sqlalchemy-execute-raw-query
        statement, parameters
    )


def _synthetic_point(index: int) -> tuple[float, float]:
    row, column = divmod(max(0, index - 1), 8)
    return 55.735 + row * 0.004, 37.585 + column * 0.006


def _demo_text(key: str, index: int) -> str:
    if "url" in key:
        return f"https://example.invalid/listing/demo-{index:03d}"
    if key == "source_listing_id":
        return f"DEMO-{index:03d}"
    if key == "title":
        return f"Демо-квартира {index:03d}"
    if key in {"name", "place_name"}:
        return f"Демо-место {index:03d}"
    if key in {
        "address",
        "address_name",
        "full_address_name",
        "full_name",
        "location",
        "q",
    }:
        return f"Демо-адрес {index:03d}, Москва"
    if key == "destination":
        return "Демо-точка назначения, Москва"
    return "Санитизированный текст"


def _sanitize_json(value: Any, index: int) -> Any:
    if isinstance(value, list):
        return [_sanitize_json(item, index) for item in value]
    if isinstance(value, str) and ("http://" in value or "https://" in value):
        return _demo_text("url", index)
    if not isinstance(value, Mapping):
        return value
    result: dict[str, Any] = {}
    lat, lon = _synthetic_point(index)
    for raw_key, item in value.items():
        key = str(raw_key)
        lowered = key.casefold()
        if lowered in _PHOTO_KEYS:
            if isinstance(item, Mapping) and "value" in item:
                sanitized = _sanitize_json(item, index)
                sanitized["value"] = []
                result[key] = sanitized
            else:
                result[key] = []
        elif lowered in _TEXT_KEYS:
            if isinstance(item, Mapping) and "value" in item:
                sanitized = _sanitize_json(item, index)
                sanitized["value"] = _demo_text(lowered, index)
                result[key] = sanitized
            elif isinstance(item, list):
                result[key] = ["Санитизированное подтверждение"]
            else:
                result[key] = _demo_text(lowered, index)
        elif lowered.endswith("_url") and isinstance(item, str):
            result[key] = _demo_text("url", index)
        elif lowered in {"lat", "latitude", "home_lat", "office_lat", "place_lat"}:
            result[key] = lat
        elif lowered in {
            "lon",
            "lng",
            "longitude",
            "home_lon",
            "office_lon",
            "place_lon",
        }:
            result[key] = lon
        else:
            result[key] = _sanitize_json(item, index)
    return result


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in _execute_schema_sql(
            conn, f"PRAGMA table_info({_quote_identifier(table)})"
        )
    ]


def _remap_listing_ids(
    conn: sqlite3.Connection, mapping: dict[int, int], tables: list[str]
) -> None:
    for table in tables:
        quoted_table = _quote_identifier(table)
        for foreign_key in _execute_schema_sql(
            conn, f"PRAGMA foreign_key_list({quoted_table})"
        ):
            if str(foreign_key[2]) != "listings":
                continue
            column = str(foreign_key[3])
            quoted_column = _quote_identifier(column)
            _executemany_schema_sql(
                conn,
                f"UPDATE {quoted_table} SET {quoted_column} = ? WHERE {quoted_column} = ?",
                ((new, old) for old, new in mapping.items()),
            )
    conn.executemany(
        'UPDATE "listings" SET id = ? WHERE id = ?',
        ((new, old) for old, new in mapping.items()),
    )


def _sanitize_json_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    reverse_mapping: dict[int, int],
) -> None:
    json_columns = [name for name in columns if name.endswith("_json")]
    if not json_columns:
        return
    has_listing_id = "listing_id" in columns
    quoted_table = _quote_identifier(table)
    for column in json_columns:
        quoted_column = _quote_identifier(column)
        selected = (
            f"rowid, listing_id, {quoted_column}"
            if has_listing_id
            else f"rowid, {quoted_column}"
        )
        for row in _execute_schema_sql(
            conn, f"SELECT {selected} FROM {quoted_table}"
        ).fetchall():
            rowid = int(row[0])
            listing_id = int(row[1]) if has_listing_id and row[1] is not None else None
            raw = row[2] if has_listing_id else row[1]
            if raw in {None, ""}:
                continue
            try:
                value = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                value = "Санитизированный текст"
            index = reverse_mapping.get(listing_id, (rowid % 999) + 1)
            sanitized = json.dumps(
                _sanitize_json(value, index),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            _execute_schema_sql(
                conn,
                f"UPDATE {quoted_table} SET {quoted_column} = ? WHERE rowid = ?",
                (sanitized, rowid),
            )


def sanitize(source: Path, target: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    if target.exists():
        raise FileExistsError(f"refusing to overwrite: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        source_conn.close()
        target_conn.close()

    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        tables = _tables(conn)
        old_ids = [
            int(row[0]) for row in conn.execute("SELECT id FROM listings ORDER BY id")
        ]
        mapping = {old: 10_000 + index for index, old in enumerate(old_ids, 1)}
        _remap_listing_ids(conn, mapping, tables)
        reverse_mapping = {new: index for index, new in enumerate(mapping.values(), 1)}

        for index, listing_id in enumerate(mapping.values(), 1):
            conn.execute(
                "UPDATE listings SET source_listing_id = ?, source_url = ? WHERE id = ?",
                (
                    f"DEMO-{index:03d}",
                    f"https://example.invalid/listing/demo-{index:03d}",
                    listing_id,
                ),
            )

        for table in tables:
            columns = _columns(conn, table)
            _sanitize_json_columns(conn, table, columns, reverse_mapping)
            has_listing_id = "listing_id" in columns
            quoted_table = _quote_identifier(table)
            for column in columns:
                lowered = column.casefold()
                if not lowered.endswith(("_lat", "_lon")):
                    continue
                selected = "rowid, listing_id" if has_listing_id else "rowid"
                for row in _execute_schema_sql(
                    conn, f"SELECT {selected} FROM {quoted_table}"
                ).fetchall():
                    rowid = int(row[0])
                    listing_id = (
                        int(row[1]) if has_listing_id and row[1] is not None else None
                    )
                    index = reverse_mapping.get(listing_id, (rowid % 999) + 1)
                    lat, lon = _synthetic_point(index)
                    quoted_column = _quote_identifier(column)
                    _execute_schema_sql(
                        conn,
                        f"UPDATE {quoted_table} SET {quoted_column} = ? WHERE rowid = ?",
                        (lat if lowered.endswith("_lat") else lon, rowid),
                    )
            if "address" in columns:
                _execute_schema_sql(
                    conn,
                    f"UPDATE {quoted_table} SET address = ?",
                    ("Демо-адрес, Москва",),
                )
            if "destination" in columns:
                _execute_schema_sql(
                    conn,
                    f"UPDATE {quoted_table} SET destination = ?",
                    ("Демо-точка, Москва",),
                )
            if "place_name" in columns:
                _execute_schema_sql(
                    conn, f"UPDATE {quoted_table} SET place_name = ?", ("Демо-место",)
                )
            if "blocked_reason" in columns:
                _execute_schema_sql(
                    conn,
                    f"UPDATE {quoted_table} SET blocked_reason = ? WHERE blocked_reason IS NOT NULL",
                    ("Санитизированная причина",),
                )
            for column in columns:
                if column.endswith("_sha256"):
                    quoted_column = _quote_identifier(column)
                    for row in _execute_schema_sql(
                        conn, f"SELECT rowid FROM {quoted_table}"
                    ).fetchall():
                        rowid = int(row[0])
                        digest = hashlib.sha256(
                            f"demo:{table}:{column}:{rowid}".encode()
                        ).hexdigest()
                        _execute_schema_sql(
                            conn,
                            f"UPDATE {quoted_table} SET {quoted_column} = ? WHERE rowid = ?",
                            (digest, rowid),
                        )

        if "full_text" in tables:
            conn.execute(
                "UPDATE full_text SET text = 'Санитизированный текст объявления', quotes_json = '[]'"
            )
        if "evidence" in tables:
            conn.execute(
                "UPDATE evidence SET detail = 'Санитизированное подтверждение'"
            )
        for table in _PHOTO_TABLES:
            if table in tables:
                _execute_schema_sql(conn, f"DELETE FROM {_quote_identifier(table)}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if foreign_key_errors or integrity != "ok":
            raise RuntimeError(
                f"sanitized database is invalid: integrity={integrity}, foreign_keys={len(foreign_key_errors)}"
            )
        conn.execute("VACUUM")
        listing_count = int(conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0])
    finally:
        conn.close()
    return {"target": str(target), "listings": listing_count, "integrity": "ok"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    print(json.dumps(sanitize(args.source, args.target), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
