"""Explicit SQLite backup publication; pruning follows durable verification."""

import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup_database(source: Path, directory: Path, *, keep: int | None = None) -> Path:
    if keep is not None and (type(keep) is not int or keep < 1):
        raise ValueError("keep must be a positive integer")
    if not source.is_file():
        raise FileNotFoundError(source)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (
        "flatfinder-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        + ".sqlite3"
    )
    with tempfile.NamedTemporaryFile(
        prefix=".backup-", dir=directory, delete=False
    ) as temporary:
        staging = Path(temporary.name)
    try:
        with closing(
            sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        ) as original:
            with closing(sqlite3.connect(staging)) as copy:
                original.backup(copy)
                if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise sqlite3.DatabaseError("backup integrity check failed")
                if copy.execute("PRAGMA foreign_key_check").fetchall():
                    raise sqlite3.DatabaseError("backup foreign keys failed")
        with staging.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(staging, target)
        _sync_directory(directory)
        if keep is not None:
            candidates = sorted(
                path
                for path in directory.glob("flatfinder-*.sqlite3")
                if path.resolve() not in {source.resolve(), target.resolve()}
            )
            removable = candidates if keep == 1 else candidates[: -(keep - 1)]
            for older in removable:
                older.unlink()
            _sync_directory(directory)
        return target
    finally:
        staging.unlink(missing_ok=True)
