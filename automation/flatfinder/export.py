"""Deterministic serialization for the shared FlatFinder read model."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .read_model import dashboard_payload
from .vision_contract import VisionContractLike


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


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def export_json(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    policy: Mapping[str, Any] | None = None,
    max_scores: Mapping[str, float] | None = None,
    scoring_parameters: Mapping[str, float] | None = None,
    vision_contract: VisionContractLike | None = None,
) -> dict[str, Any]:
    """Write the SQLite read model as deterministic, atomically replaced JSON."""

    payload = dashboard_payload(
        conn,
        policy=policy,
        max_scores=max_scores,
        scoring_parameters=scoring_parameters,
        vision_contract=vision_contract,
    )
    _atomic_write(path, _dump(payload) + "\n")
    return payload


__all__ = ["dashboard_payload", "export_json"]
