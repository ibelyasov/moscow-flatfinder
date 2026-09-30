"""Current SQLite schema and complete atomic write intents.

Normal opening validates the current schema and never migrates or initializes it.
Provider work belongs outside this module and outside SQLite transactions.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .assessment import evaluate_listing
from .models import (
    ListingFacts,
    PhotoInput,
    ValueStatus,
    facts_from_dict,
    facts_to_dict,
    validate_visual_payload,
)
from .photos import normalize_photo_url, photo_input_hash
from .scoring import score_bucket
from .scoring_policy import validate_policy
from .vision_contract import VisionContract

SCHEMA_VERSION = 18
_SCHEMA = Path(__file__).with_name("schema.sql")


class DatabaseError(ValueError):
    """The database does not satisfy the application's persisted contract."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must use UTC")
    return value


def _dump(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _load(value: Any, context: str, kind: type | tuple[type, ...] = dict) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = item
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant {value}")

    try:
        result = json.loads(
            value, parse_constant=reject_constant, object_pairs_hook=object_pairs
        )
    except (TypeError, ValueError) as error:
        raise DatabaseError(f"invalid JSON in {context}") from error
    if not isinstance(result, kind):
        names = (
            kind.__name__
            if isinstance(kind, type)
            else "/".join(item.__name__ for item in kind)
        )
        raise DatabaseError(f"{context} must be a JSON {names}")
    return result


def _policy(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _load(_dump(dict(value)), "policy")
    validate_policy(result, effective=True)
    return result


def _statements() -> Iterator[str]:
    buffer = ""
    for line in _SCHEMA.read_text(encoding="utf-8").splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    if buffer.strip():
        raise DatabaseError("incomplete schema SQL")


def _signature(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        (row[0], row[1], row[2], re.sub(r"\s+", " ", row[3] or "").strip())
        for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]


def _expected_signature() -> list[tuple[Any, ...]]:
    with sqlite3.connect(":memory:") as conn:
        for statement in _statements():
            conn.execute(statement)
        return _signature(conn)


def _photo_values(
    listing_id: int, photos: Sequence[PhotoInput]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for photo in photos:
        if not isinstance(photo, PhotoInput) or photo.listing_id != listing_id:
            raise ValueError("photos must be typed inputs for this listing")
        result.append(asdict(photo))
    ordered = sorted(result, key=lambda row: row["image_index"])
    photo_input_hash([PhotoInput(**row) for row in ordered])
    indexed = {row["image_index"] for row in ordered if row["status"] == "indexed"}
    for row in ordered:
        if row["status"] == "duplicate" and row["duplicate_of_index"] not in indexed:
            raise ValueError(
                "duplicate photo must refer to an indexed image in this set"
            )
    _dump(ordered)
    return ordered


class Database:
    def __init__(self, path: str | Path, readonly: bool = False):
        if str(path) == ":memory:":
            raise DatabaseError(
                "use Database.initialize(':memory:') for an empty database"
            )
        uri = Path(path).expanduser().resolve().as_uri() + (
            "?mode=ro" if readonly else "?mode=rw"
        )
        self.path = Path(path).expanduser()
        self.readonly = bool(readonly)
        self._snapshot_depth = 0
        self._fixed_time = None
        self._conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._validate_schema()
        except BaseException:
            self._conn.close()
            raise

    @classmethod
    def initialize(cls, path: str | Path) -> Database:
        """Explicitly initialize an empty database; existing tables are rejected."""
        if str(path) != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        database = cls.__new__(cls)
        database.path = Path(path).expanduser()
        database.readonly = False
        database._snapshot_depth = 0
        database._fixed_time = None
        database._conn = sqlite3.connect(
            str(database.path), isolation_level=None, timeout=10
        )
        database._conn.row_factory = sqlite3.Row
        database._conn.execute("PRAGMA foreign_keys=ON")
        try:
            with database._write():
                version = int(
                    database._conn.execute("PRAGMA user_version").fetchone()[0]
                )
                if version != 0 or _signature(database._conn):
                    raise DatabaseError(
                        "initialization requires an empty schema-zero database"
                    )
                for statement in _statements():
                    database._conn.execute(statement)
                database._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                database._validate_schema()
            if str(path) != ":memory:":
                database._conn.execute("PRAGMA journal_mode=WAL")
            return database
        except BaseException:
            database.close()
            raise

    def _now(self) -> str:
        return self._fixed_time or _now()

    @contextmanager
    def _operation_time(self, timestamp: str | None) -> Iterator[None]:
        if timestamp is not None:
            timestamp = _timestamp(timestamp, "operation timestamp")
        previous = self._fixed_time
        self._fixed_time = timestamp
        try:
            yield
        finally:
            self._fixed_time = previous

    def _validate_schema(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise DatabaseError(
                f"schema {version} is unsupported; expected {SCHEMA_VERSION}; import v17 offline into a new database"
            )
        if _signature(self._conn) != _expected_signature():
            raise DatabaseError("current database schema has an incompatible shape")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[None]:
        if self.readonly:
            raise DatabaseError("database is read-only")
        if self._conn.in_transaction or self._snapshot_depth:
            raise DatabaseError("a write intent cannot nest another transaction")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.commit()
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    @contextmanager
    def read_snapshot(self) -> Iterator[Database]:
        """All selectors in this scope observe one committed SQLite snapshot."""
        if self._snapshot_depth:
            self._snapshot_depth += 1
            try:
                yield self
            finally:
                self._snapshot_depth -= 1
            return
        if self._conn.in_transaction:
            raise DatabaseError("read snapshot requires a clean connection")
        self._conn.execute("BEGIN")
        self._snapshot_depth = 1
        try:
            yield self
            self._conn.commit()
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        finally:
            self._snapshot_depth = 0

    def _require_listing(self, listing_id: int) -> sqlite3.Row:
        listing_id = _integer(listing_id, "listing_id")
        row = self._conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"listing {listing_id} does not exist")
        return row

    def register_searches(self, searches: Sequence[str]) -> None:
        urls = tuple(dict.fromkeys(_text(url, "search URL") for url in searches))
        with self._write():
            self._conn.execute("UPDATE searches SET enabled=0")
            self._conn.executemany(
                "INSERT INTO searches(url,enabled) VALUES (?,1) ON CONFLICT(url) DO UPDATE SET enabled=1",
                [(url,) for url in urls],
            )

    def record_search(self, url: str, links: Sequence[str], complete: bool) -> None:
        url = _text(url, "search URL")
        if type(complete) is not bool:
            raise ValueError("complete must be a boolean")
        links = tuple(dict.fromkeys(_text(link, "offer URL") for link in links))
        now = self._now()
        with self._write():
            if (
                self._conn.execute(
                    "SELECT enabled FROM searches WHERE url=?", (url,)
                ).fetchone()
                is None
            ):
                raise ValueError("register the search before recording its result")
            if complete:
                self._conn.execute(
                    "DELETE FROM search_memberships WHERE search_url=?", (url,)
                )
            self._conn.executemany(
                "INSERT INTO search_memberships(search_url,source_url,seen_at) VALUES (?,?,?) ON CONFLICT(search_url,source_url) DO UPDATE SET seen_at=excluded.seen_at",
                [(url, link, now) for link in links],
            )
            self._conn.execute(
                "UPDATE searches SET checked_at=?,complete=? WHERE url=?",
                (now, int(complete), url),
            )

    def searches(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._conn.execute("SELECT * FROM searches ORDER BY url")
        ]

    def _save_observation(
        self,
        facts: ListingFacts,
        parser_version: str,
        listing_id: int | None,
        *,
        kind: str = "collection",
        captured_at: str | None = None,
    ) -> int:
        payload = facts_to_dict(facts)
        # Round trip validates nested values before they become persisted facts.
        facts_from_dict(payload)
        parser_version = _text(parser_version, "parser_version")
        now = (
            _timestamp(captured_at, "observation captured_at")
            if captured_at is not None
            else self._now()
        )
        existing = self._conn.execute(
            "SELECT * FROM listings WHERE source=? AND source_listing_id=?",
            (facts.source, facts.source_listing_id),
        ).fetchone()
        if listing_id is not None:
            row = self._require_listing(listing_id)
            if (
                row["source"] != facts.source
                or row["source_listing_id"] != facts.source_listing_id
            ):
                raise ValueError("observation identity does not match listing")
        elif existing is not None:
            listing_id = int(existing["id"])
        else:
            cursor = self._conn.execute(
                "INSERT INTO listings(source,source_listing_id,source_url,first_seen_at,last_seen_at) VALUES (?,?,?,?,?)",
                (facts.source, facts.source_listing_id, facts.source_url, now, now),
            )
            listing_id = int(cursor.lastrowid)
            self._conn.execute(
                "INSERT INTO manual_decisions(listing_id) VALUES (?)", (listing_id,)
            )
        raw = _dump(payload)
        cursor = self._conn.execute(
            "INSERT INTO observations(listing_id,kind,parser_version,content_sha256,facts_json,captured_at) VALUES (?,?,?,?,?,?)",
            (
                listing_id,
                kind,
                parser_version,
                hashlib.sha256(raw.encode()).hexdigest(),
                raw,
                now,
            ),
        )
        availability = (
            "available"
            if kind == "collection"
            else self._require_listing(listing_id)["availability"]
        )
        self._conn.execute(
            "UPDATE listings SET source_url=?,last_seen_at=?,current_observation_id=?,availability=? WHERE id=?",
            (facts.source_url, now, cursor.lastrowid, availability, listing_id),
        )
        self._sync_gallery(listing_id, facts)
        return listing_id

    def _sync_gallery(self, listing_id: int, facts: ListingFacts) -> None:
        field = facts.fields.get("photos")
        entries = field.value if field is not None else []
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise ValueError("photos field must contain an ordered list")
        gallery: dict[str, str] = {}
        for entry in entries:
            if isinstance(entry, str):
                url = raw = _text(entry, "photo URL")
            elif isinstance(entry, Mapping) and set(entry) == {"url", "source_url"}:
                url = _text(entry["url"], "canonical photo URL")
                raw = _text(entry["source_url"], "raw photo URL")
            else:
                raise ValueError("photo entries require canonical url and source_url")
            gallery.setdefault(normalize_photo_url(url), raw)
        urls = list(gallery)
        current = self.photos(listing_id)
        if (
            self._photo_set(listing_id) is not None
            and [photo.source_url for photo in current] == urls
        ):
            return
        self._save_photos(
            listing_id,
            [
                PhotoInput(
                    listing_id,
                    index,
                    url,
                    raw_source_url=gallery[url],
                    status="failed",
                    error="not_downloaded",
                )
                for index, url in enumerate(urls)
            ],
        )

    def save_observation(
        self,
        facts: ListingFacts,
        parser_version: str,
        policy: Mapping[str, Any],
        listing_id: int | None = None,
    ) -> int:
        policy = _policy(policy)
        with self._write():
            listing_id = self._save_observation(facts, parser_version, listing_id)
            self._reassess(listing_id, policy)
        return listing_id

    def save_enrichment(
        self,
        listing_id: int,
        facts: ListingFacts,
        checks: Sequence[Mapping[str, Any]],
        policy: Mapping[str, Any],
    ) -> None:
        policy = _policy(policy)
        checks = tuple(checks)
        if not checks:
            raise ValueError("completed enrichment requires measurement checks")
        checks_by_kind = {}
        for check in checks:
            if (
                not isinstance(check, Mapping)
                or set(check)
                != {"kind", "input_hash", "status", "payload", "captured_at"}
                or check["status"]
                not in {"success", "partial", "unknown", "failed", "blocked"}
                or not isinstance(check["payload"], Mapping)
            ):
                raise ValueError("invalid enrichment check")
            kind = _text(check["kind"], "check kind")
            if kind in checks_by_kind:
                raise ValueError("completed enrichment requires one check per kind")
            checks_by_kind[kind] = check
        context_changed = facts.measurement_context != policy["measurement_context"]
        acquired_facts = replace(
            deepcopy(facts), measurement_context=policy["measurement_context"]
        )
        for kind, names in {
            "commute": ("route", "route_minutes"),
            "park": ("park",),
            "fitness": ("fitness",),
            "noise": ("noise",),
        }.items():
            check = checks_by_kind.get(kind)
            if (context_changed and check is None) or (
                check is not None and check["status"] not in {"success", "partial"}
            ):
                for name in names:
                    field = acquired_facts.fields.get(name)
                    if field is not None:
                        field.status = ValueStatus.UNKNOWN
        with self._write():
            self._save_observation(
                acquired_facts, "enrichment-v1", listing_id, kind="enrichment"
            )
            observation_id = self._require_listing(listing_id)["current_observation_id"]
            for check in checks:
                self._conn.execute(
                    "INSERT INTO checks(listing_id,observation_id,kind,input_hash,status,payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        listing_id,
                        observation_id,
                        _text(check["kind"], "check kind"),
                        _text(check["input_hash"], "check input hash"),
                        check["status"],
                        _dump(dict(check["payload"])),
                        _timestamp(check["captured_at"], "check captured_at"),
                    ),
                )
            self._reassess(listing_id, policy)

    def cached_check(
        self, listing_id: int, kind: str, input_hash: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM checks WHERE listing_id=? AND kind=? AND input_hash=? ORDER BY id DESC LIMIT 1",
            (listing_id, kind, input_hash),
        ).fetchone()
        return self._check(row) if row is not None else None

    @staticmethod
    def _check(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = _load(result.pop("payload_json"), f"check {row['id']}")
        return result

    def checks(self, listing_id: int) -> list[dict[str, Any]]:
        return [
            self._check(row)
            for row in self._conn.execute(
                "SELECT * FROM checks WHERE listing_id=? ORDER BY id", (listing_id,)
            )
        ]

    def _save_photos(self, listing_id: int, photos: Sequence[PhotoInput]) -> str:
        self._require_listing(listing_id)
        values = _photo_values(listing_id, photos)
        input_hash = photo_input_hash([PhotoInput(**row) for row in values])
        cursor = self._conn.execute(
            "INSERT INTO photo_sets(listing_id,input_hash,photos_json,created_at) VALUES (?,?,?,?)",
            (listing_id, input_hash, _dump(values), self._now()),
        )
        self._conn.execute(
            "UPDATE listings SET current_photo_set_id=? WHERE id=?",
            (cursor.lastrowid, listing_id),
        )
        return input_hash

    def save_photos(
        self, listing_id: int, photos: Sequence[PhotoInput], policy: Mapping[str, Any]
    ) -> str:
        policy = _policy(policy)
        with self._write():
            input_hash = self._save_photos(listing_id, photos)
            self._reassess(listing_id, policy)
        return input_hash

    def _photo_set(self, listing_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT p.* FROM listings l JOIN photo_sets p ON p.id=l.current_photo_set_id WHERE l.id=?",
            (listing_id,),
        ).fetchone()

    @staticmethod
    def _decode_photos(row: sqlite3.Row) -> list[PhotoInput]:
        values = _load(row["photos_json"], f"photo set {row['id']}", list)
        try:
            photos = [PhotoInput(**value) for value in values]
            if (
                _photo_values(int(row["listing_id"]), photos) != values
                or photo_input_hash(photos) != row["input_hash"]
            ):
                raise ValueError("photo identity differs from its stored hash")
            return photos
        except (TypeError, ValueError) as error:
            raise DatabaseError(f"invalid photo set {row['id']}") from error

    def photos(self, listing_id: int) -> list[PhotoInput]:
        row = self._photo_set(listing_id)
        return self._decode_photos(row) if row is not None else []

    def _vision(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        contract = VisionContract.from_dict(
            _load(result.pop("contract_json"), f"Vision contract {row['id']}")
        )
        if contract.fingerprint() != row["contract_fingerprint"]:
            raise DatabaseError(f"Vision contract {row['id']} fingerprint mismatch")
        result["contract"] = contract
        raw = result.pop("result_json")
        result["result"] = None
        if raw is not None:
            photo_set = self._conn.execute(
                "SELECT * FROM photo_sets WHERE id=?", (row["photo_set_id"],)
            ).fetchone()
            if photo_set is None or photo_set["input_hash"] != row["input_hash"]:
                raise DatabaseError("Vision photo identity mismatch")
            allowed = [
                photo.image_index
                for photo in self._decode_photos(photo_set)
                if photo.status == "indexed"
            ]
            result["result"] = validate_visual_payload(
                _load(raw, f"Vision result {row['id']}"), allowed
            )
            if result["result"]["model_level"] != contract.reasoning_effort:
                raise DatabaseError(
                    "Vision result effort differs from its execution contract"
                )
        if (
            row["status"] in {"pending", "accepted", "rejected"}
            and result["result"] is None
        ):
            raise DatabaseError("completed Vision has no result")
        return result

    def _vision_for(
        self,
        listing_id: int,
        contract: VisionContract,
        input_hash: str | None,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(contract, VisionContract):
            raise ValueError("Vision requires an explicit typed contract")
        photo_set = self._photo_set(listing_id)
        current_hash = photo_set["input_hash"] if photo_set is not None else None
        if input_hash is None:
            input_hash = current_hash
        row = self._conn.execute(
            "SELECT * FROM vision_runs WHERE listing_id=? AND contract_fingerprint=? AND input_hash=? AND (? IS NULL OR status=?) ORDER BY id DESC LIMIT 1",
            (listing_id, contract.fingerprint(), input_hash, status, status),
        ).fetchone()
        return self._vision(row) if row is not None else None

    def current_vision(
        self, listing_id: int, contract: VisionContract, input_hash: str | None = None
    ) -> dict[str, Any] | None:
        return self._vision_for(listing_id, contract, input_hash)

    def accepted_vision(
        self, listing_id: int, contract: VisionContract, input_hash: str | None = None
    ) -> dict[str, Any] | None:
        return self._vision_for(listing_id, contract, input_hash, "accepted")

    def pending_vision(
        self, listing_id: int, contract: VisionContract, input_hash: str | None = None
    ) -> dict[str, Any] | None:
        return self._vision_for(listing_id, contract, input_hash, "pending")

    def latest_vision(self, listing_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM vision_runs WHERE listing_id=? ORDER BY id DESC LIMIT 1",
            (listing_id,),
        ).fetchone()
        return self._vision(row) if row is not None else None

    def begin_vision(
        self, listing_id: int, contract: VisionContract, input_hash: str
    ) -> int:
        if not isinstance(contract, VisionContract):
            raise ValueError("Vision requires an explicit typed contract")
        with self._write():
            self._require_listing(listing_id)
            photo_set = self._photo_set(listing_id)
            if photo_set is None or photo_set["input_hash"] != input_hash:
                raise ValueError(
                    "Vision input must match the current ordered photo set"
                )
            self._decode_photos(photo_set)
            cursor = self._conn.execute(
                "INSERT INTO vision_runs(listing_id,photo_set_id,contract_json,contract_fingerprint,input_hash,status,started_at) VALUES (?,?,?,?,?,'running',?)",
                (
                    listing_id,
                    photo_set["id"],
                    _dump(contract.to_dict()),
                    contract.fingerprint(),
                    input_hash,
                    self._now(),
                ),
            )
            return int(cursor.lastrowid)

    def _require_vision(self, run_id: int, status: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM vision_runs WHERE id=?", (_integer(run_id, "vision run ID"),)
        ).fetchone()
        if row is None or row["status"] != status:
            raise ValueError(f"Vision run {run_id} must be {status}")
        return row

    def complete_vision(
        self,
        run_id: int,
        result: Mapping[str, Any],
        auto_accept: bool,
        policy: Mapping[str, Any],
    ) -> None:
        policy = _policy(policy)
        if type(auto_accept) is not bool:
            raise ValueError("auto_accept must be a boolean")
        with self._write():
            row = self._require_vision(run_id, "running")
            photo_set = self._conn.execute(
                "SELECT * FROM photo_sets WHERE id=?", (row["photo_set_id"],)
            ).fetchone()
            allowed = [
                photo.image_index
                for photo in self._decode_photos(photo_set)
                if photo.status == "indexed"
            ]
            payload = validate_visual_payload(result, allowed)
            contract = VisionContract.from_dict(
                _load(row["contract_json"], f"Vision contract {run_id}")
            )
            if payload["model_level"] != contract.reasoning_effort:
                raise ValueError(
                    "Vision result effort differs from its execution contract"
                )
            current = self._photo_set(int(row["listing_id"]))
            if auto_accept:
                if contract.to_dict() != policy["vision_contract"]:
                    raise ValueError(
                        "cannot accept Vision for a superseded execution contract"
                    )
                if current is None or current["input_hash"] != row["input_hash"]:
                    raise ValueError("cannot accept Vision for a superseded photo set")
                self._decode_photos(current)
            now = self._now()
            self._conn.execute(
                "UPDATE vision_runs SET status=?,result_json=?,finished_at=?,reviewed_at=? WHERE id=?",
                (
                    "accepted" if auto_accept else "pending",
                    _dump(payload),
                    now,
                    now if auto_accept else None,
                    run_id,
                ),
            )
            # Result state and its machine score are committed together.
            self._reassess(int(row["listing_id"]), policy)

    def fail_vision(self, run_id: int, error: str) -> None:
        with self._write():
            self._require_vision(run_id, "running")
            self._conn.execute(
                "UPDATE vision_runs SET status='failed',error=?,finished_at=? WHERE id=?",
                (_text(error, "Vision error"), self._now(), run_id),
            )

    def review_vision(
        self, run_id: int, accept: bool, policy: Mapping[str, Any]
    ) -> None:
        policy = _policy(policy)
        if type(accept) is not bool:
            raise ValueError("accept must be a boolean")
        with self._write():
            row = self._require_vision(run_id, "pending")
            run = self._vision(row)
            current = self._photo_set(int(row["listing_id"]))
            if run["contract"].to_dict() != policy["vision_contract"]:
                raise ValueError(
                    "cannot review Vision for a superseded execution contract"
                )
            if current is None or current["input_hash"] != row["input_hash"]:
                raise ValueError("cannot review Vision for a superseded photo set")
            self._decode_photos(current)
            self._conn.execute(
                "UPDATE vision_runs SET status=?,reviewed_at=? WHERE id=?",
                ("accepted" if accept else "rejected", self._now(), run_id),
            )
            self._reassess(int(row["listing_id"]), policy)

    def _reassess(self, listing_id: int, policy: Mapping[str, Any]) -> None:
        listing = self._require_listing(listing_id)
        observation = self._conn.execute(
            "SELECT * FROM observations WHERE id=?",
            (listing["current_observation_id"],),
        ).fetchone()
        if observation is None:
            raise DatabaseError("listing has no current observation")
        facts = facts_from_dict(
            _load(observation["facts_json"], f"observation {observation['id']}")
        )
        manual = self._conn.execute(
            "SELECT * FROM manual_decisions WHERE listing_id=?", (listing_id,)
        ).fetchone()
        if manual is None:
            raise DatabaseError("listing has no separate manual decision record")
        accepted = None
        if policy["vision_scoring_enabled"] and policy["vision_contract"] is not None:
            accepted = self.accepted_vision(
                listing_id, VisionContract.from_dict(policy["vision_contract"])
            )
        calculated = evaluate_listing(
            facts,
            policy=policy,
            personal_score=manual["personal_score"],
            visual_result=accepted["result"] if accepted is not None else None,
        )
        result = {
            "scores": calculated.scores,
            "assessment": calculated.assessment,
            "auto_score": calculated.auto_score,
            "total_score": calculated.total,
            "personal_score": calculated.personal_score,
            "status": calculated.status,
        }
        raw = _dump(result)
        self._validate_result(result, policy["fingerprint"])
        now = self._now()
        values = (
            listing_id,
            observation["id"],
            listing["current_photo_set_id"],
            accepted["id"] if accepted is not None else None,
            policy["fingerprint"],
            raw,
            now,
        )
        self._conn.execute(
            "INSERT INTO assessments(listing_id,observation_id,photo_set_id,accepted_vision_id,policy_fingerprint,result_json,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(listing_id) DO UPDATE SET observation_id=excluded.observation_id,photo_set_id=excluded.photo_set_id,accepted_vision_id=excluded.accepted_vision_id,policy_fingerprint=excluded.policy_fingerprint,result_json=excluded.result_json,updated_at=excluded.updated_at",
            values,
        )
        self._conn.execute(
            "INSERT INTO assessment_history(listing_id,observation_id,photo_set_id,accepted_vision_id,policy_fingerprint,result_json,created_at) VALUES (?,?,?,?,?,?,?)",
            values,
        )

    @staticmethod
    def _validate_result(result: dict[str, Any], fingerprint: str) -> None:
        if (
            set(result)
            != {
                "scores",
                "assessment",
                "auto_score",
                "total_score",
                "personal_score",
                "status",
            }
            or not isinstance(result["scores"], dict)
            or not isinstance(result["assessment"], dict)
        ):
            raise DatabaseError("invalid stored assessment result")
        for name in ("auto_score", "total_score", "personal_score"):
            _number(result[name], name)
        for name, value in result["scores"].items():
            _number(value, name)
        assessment = result["assessment"]
        eligibility = assessment.get("eligibility")
        if (
            not isinstance(eligibility, dict)
            or eligibility.get("status") not in {"eligible", "needs_review", "rejected"}
            or not isinstance(eligibility.get("checks"), list)
        ):
            raise DatabaseError("assessment has no explicit valid eligibility")
        saved_policy = assessment.get("_policy")
        if (
            not isinstance(saved_policy, dict)
            or _policy(saved_policy)["fingerprint"] != fingerprint
        ):
            raise DatabaseError("assessment policy fingerprint mismatch")
        enabled = {
            name for name, maximum in saved_policy["max_scores"].items() if maximum > 0
        }
        if set(result["scores"]) != enabled or set(assessment) != enabled | {
            "_policy",
            "eligibility",
        }:
            raise DatabaseError("assessment criteria differ from its saved policy")
        for name in enabled:
            score = result["scores"][name]
            detail = assessment[name]
            if (
                not 0 <= score <= saved_policy["max_scores"][name]
                or not isinstance(detail, dict)
                or detail.get("score") != score
                or detail.get("confidence") not in {"confirmed", "partial", "unknown"}
                or not isinstance(detail.get("evidence"), list)
            ):
                raise DatabaseError(f"invalid stored criterion {name}")
            for evidence in detail["evidence"]:
                if (
                    not isinstance(evidence, dict)
                    or set(evidence)
                    != {"source", "detail", "captured_at", "confidence"}
                    or any(not isinstance(evidence[key], str) for key in evidence)
                ):
                    raise DatabaseError(f"invalid stored evidence in {name}")
        automatic = sum(
            value for name, value in result["scores"].items() if name != "personal"
        )
        if (
            not math.isclose(result["auto_score"], automatic, abs_tol=1e-8)
            or not math.isclose(
                result["total_score"],
                automatic + result["personal_score"],
                abs_tol=1e-8,
            )
            or not 0
            <= result["personal_score"]
            <= saved_policy["max_scores"]["personal"]
            or result["status"] != score_bucket(automatic, saved_policy["thresholds"])
        ):
            raise DatabaseError("stored assessment totals or bucket are inconsistent")

    def reassess(self, listing_id: int, policy: Mapping[str, Any]) -> None:
        policy = _policy(policy)
        with self._write():
            self._reassess(listing_id, policy)

    def record_review(
        self,
        listing_id: int,
        *,
        policy: Mapping[str, Any],
        personal_score: float | None = None,
        favorite: bool | None = None,
        disliked: bool | None = None,
    ) -> None:
        """Apply a completed human decision and its score together."""
        policy = _policy(policy)
        if personal_score is not None:
            personal_score = _number(personal_score, "personal score")
            maximum = _number(policy["max_scores"]["personal"], "personal maximum")
            if not 0 <= personal_score <= maximum:
                raise ValueError(f"personal score must be inside [0,{maximum:g}]")
        if (favorite is not None and type(favorite) is not bool) or (
            disliked is not None and type(disliked) is not bool
        ):
            raise ValueError("manual flags must be booleans")
        with self._write():
            self._require_listing(listing_id)
            now = self._now()
            if personal_score is not None:
                self._conn.execute(
                    "UPDATE manual_decisions SET personal_score=?,personal_rated_at=? WHERE listing_id=?",
                    (personal_score, now, listing_id),
                )
            if favorite is not None:
                self._conn.execute(
                    "UPDATE manual_decisions SET favorite=?,favorited_at=? WHERE listing_id=?",
                    (int(favorite), now if favorite else None, listing_id),
                )
            if disliked is not None:
                self._conn.execute(
                    "UPDATE manual_decisions SET disliked=?,disliked_at=? WHERE listing_id=?",
                    (int(disliked), now if disliked else None, listing_id),
                )
            if personal_score is not None:
                self._reassess(listing_id, policy)

    def set_availability(self, listing_id: int, value: str) -> None:
        if value not in {"available", "unavailable", "unknown"}:
            raise ValueError("invalid source availability")
        with self._write():
            self._require_listing(listing_id)
            self._conn.execute(
                "UPDATE listings SET availability=? WHERE id=?", (value, listing_id)
            )

    def listing_ids(self, active_only: bool = True) -> list[int]:
        return [
            int(row[0])
            for row in self._conn.execute(
                "SELECT id FROM listings WHERE (?=0 OR availability!='unavailable') ORDER BY source,source_listing_id,id",
                (int(active_only),),
            )
        ]

    def _listing(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        facts = facts_from_dict(
            _load(result.pop("facts_json"), f"listing {row['id']} facts")
        )
        if (facts.source, facts.source_listing_id, facts.source_url) != (
            row["source"],
            row["source_listing_id"],
            row["source_url"],
        ):
            raise DatabaseError("listing identity differs from its current observation")
        result["facts"] = facts
        assessment = _load(result.pop("result_json"), f"listing {row['id']} assessment")
        self._validate_result(assessment, row["policy_fingerprint"])
        if (
            row["assessment_observation_id"] != row["current_observation_id"]
            or row["assessment_photo_set_id"] != row["current_photo_set_id"]
        ):
            raise DatabaseError("assessment does not refer to current inputs")
        if assessment["personal_score"] != row["personal_score"]:
            raise DatabaseError("assessment differs from separate manual score")
        if row["accepted_vision_id"] is not None:
            vision_row = self._conn.execute(
                "SELECT * FROM vision_runs WHERE id=?", (row["accepted_vision_id"],)
            ).fetchone()
            if vision_row is None:
                raise DatabaseError(
                    "assessment references a missing accepted Vision run"
                )
            vision = self._vision(vision_row)
            photo_set = self._photo_set(row["id"])
            if (
                vision["status"] != "accepted"
                or vision["listing_id"] != row["id"]
                or photo_set is None
                or vision["input_hash"] != photo_set["input_hash"]
                or vision["contract"].to_dict()
                != assessment["assessment"]["_policy"]["vision_contract"]
            ):
                raise DatabaseError(
                    "assessment references incompatible accepted Vision"
                )
        result.update(assessment)
        result["in_search"] = bool(row["in_search"])
        result["favorite"] = bool(row["favorite"])
        result["disliked"] = bool(row["disliked"])
        return result

    def listings(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        with self.read_snapshot():
            return [
                self._listing(row) for row in self._listing_rows(None, include_inactive)
            ]

    def listing(self, listing_id: int) -> dict[str, Any] | None:
        with self.read_snapshot():
            rows = self._listing_rows(_integer(listing_id, "listing_id"), True)
            return self._listing(rows[0]) if rows else None

    def _listing_rows(
        self, listing_id: int | None, include_inactive: bool
    ) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT l.*,o.facts_json,o.captured_at,o.content_sha256,m.personal_score,m.favorite,m.disliked,m.personal_rated_at,m.favorited_at,m.disliked_at,a.result_json,a.policy_fingerprint,a.updated_at,a.observation_id AS assessment_observation_id,a.photo_set_id AS assessment_photo_set_id,a.accepted_vision_id,EXISTS(SELECT 1 FROM search_memberships sm JOIN searches s ON s.url=sm.search_url WHERE sm.source_url=l.source_url AND s.enabled=1) AS in_search FROM listings l LEFT JOIN observations o ON o.id=l.current_observation_id LEFT JOIN manual_decisions m ON m.listing_id=l.id LEFT JOIN assessments a ON a.listing_id=l.id WHERE (? IS NULL OR l.id=?) AND (?=1 OR l.availability!='unavailable') ORDER BY l.source,l.source_listing_id,l.id""",
            (listing_id, listing_id, int(include_inactive)),
        ).fetchall()

    def observation_history(self, listing_id: int) -> list[dict[str, Any]]:
        result = []
        for row in self._conn.execute(
            "SELECT * FROM observations WHERE listing_id=? ORDER BY id", (listing_id,)
        ):
            value = dict(row)
            value["facts"] = facts_from_dict(
                _load(value.pop("facts_json"), f"observation {row['id']}")
            )
            result.append(value)
        return result

    def assessment_history(self, listing_id: int) -> list[dict[str, Any]]:
        result = []
        for row in self._conn.execute(
            "SELECT * FROM assessment_history WHERE listing_id=? ORDER BY id",
            (listing_id,),
        ):
            value = dict(row)
            value["result"] = _load(
                value.pop("result_json"), f"assessment history {row['id']}"
            )
            self._validate_result(value["result"], row["policy_fingerprint"])
            result.append(value)
        return result

    def link_duplicates(
        self,
        left_listing_id: int,
        right_listing_id: int,
        method: str,
        confidence: float,
        evidence: Mapping[str, Any] | Sequence[Any],
    ) -> None:
        left, right = sorted(
            (
                _integer(left_listing_id, "left listing"),
                _integer(right_listing_id, "right listing"),
            )
        )
        confidence = _number(confidence, "duplicate confidence")
        if left == right or not 0 <= confidence <= 1:
            raise ValueError("invalid duplicate link")
        if not isinstance(evidence, (Mapping, list, tuple)):
            raise ValueError("duplicate evidence must be an object or array")
        with self._write():
            self._require_listing(left)
            self._require_listing(right)
            self._conn.execute(
                "INSERT INTO duplicate_links(left_listing_id,right_listing_id,method,confidence,evidence_json,created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(left_listing_id,right_listing_id) DO UPDATE SET method=excluded.method,confidence=excluded.confidence,evidence_json=excluded.evidence_json,created_at=excluded.created_at WHERE duplicate_links.dismissed_at IS NULL",
                (
                    left,
                    right,
                    _text(method, "duplicate method"),
                    confidence,
                    _dump(evidence),
                    self._now(),
                ),
            )

    def duplicate_links(self, listing_id: int) -> list[dict[str, Any]]:
        result = []
        for row in self._conn.execute(
            "SELECT * FROM duplicate_links WHERE (left_listing_id=? OR right_listing_id=?) AND dismissed_at IS NULL ORDER BY left_listing_id,right_listing_id",
            (listing_id, listing_id),
        ):
            value = dict(row)
            value["other_listing_id"] = (
                row["right_listing_id"]
                if row["left_listing_id"] == listing_id
                else row["left_listing_id"]
            )
            raw = value.pop("evidence_json")
            value["evidence"] = _load(raw, "duplicate link evidence", (dict, list))
            result.append(value)
        return result

    def unlink_duplicates(self, left_listing_id: int, right_listing_id: int) -> bool:
        """Dismiss one named edge persistently, retaining other links and decisions."""
        left, right = sorted(
            (
                _integer(left_listing_id, "left listing"),
                _integer(right_listing_id, "right listing"),
            )
        )
        if left == right:
            raise ValueError("duplicate edge must name two distinct listings")
        with self._write():
            self._require_listing(left)
            self._require_listing(right)
            cursor = self._conn.execute(
                "UPDATE duplicate_links SET dismissed_at=? WHERE left_listing_id=? AND right_listing_id=? AND dismissed_at IS NULL",
                (self._now(), left, right),
            )
            return cursor.rowcount == 1

    def start_run(self, kind: str, summary: Mapping[str, Any] | None = None) -> int:
        with self._write():
            cursor = self._conn.execute(
                "INSERT INTO runs(kind,status,summary_json,started_at) VALUES (?,'running',?,?)",
                (_text(kind, "run kind"), _dump(dict(summary or {})), self._now()),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        summary: Mapping[str, Any],
        error: str | None = None,
    ) -> None:
        if status not in {"success", "partial", "failed", "blocked", "cancelled"}:
            raise ValueError("invalid completed run status")
        with self._write():
            row = self._conn.execute(
                "SELECT status FROM runs WHERE id=?", (_integer(run_id, "run ID"),)
            ).fetchone()
            if row is None or row["status"] != "running":
                raise ValueError("only a running operation can finish")
            self._conn.execute(
                "UPDATE runs SET status=?,summary_json=?,error=?,finished_at=? WHERE id=?",
                (status, _dump(dict(summary)), error, self._now(), run_id),
            )

    def runs(self) -> list[dict[str, Any]]:
        result = []
        for row in self._conn.execute("SELECT * FROM runs ORDER BY id"):
            value = dict(row)
            value["summary"] = _load(value.pop("summary_json"), f"run {row['id']}")
            result.append(value)
        return result

    def _archive_record(self, record: Mapping[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO technical_archive(listing_id,source_schema,source_table,source_key,payload_json,archived_at) VALUES (?,?,?,?,?,?)",
            (
                record.get("listing_id"),
                _text(record["source_schema"], "archive schema"),
                _text(record["source_table"], "archive table"),
                _text(record["source_key"], "archive key"),
                _dump(record["payload"]),
                self._now(),
            ),
        )

    def archive_history(self, listing_id: int) -> list[dict[str, Any]]:
        result = []
        for row in self._conn.execute(
            "SELECT * FROM technical_archive WHERE listing_id=? ORDER BY source_schema,source_table,source_key",
            (listing_id,),
        ):
            value = dict(row)
            value["payload"] = _load(
                value.pop("payload_json"), f"technical archive {row['id']}"
            )
            result.append(value)
        return result

    def import_records(
        self,
        records: Sequence[Mapping[str, Any]],
        archive_rows: Sequence[Mapping[str, Any]],
        policy: Mapping[str, Any],
        *,
        imported_at: str | None = None,
    ) -> None:
        """Import prepared offline records into an empty destination in one commit."""
        policy = _policy(policy)
        with self._operation_time(imported_at), self._write():
            if any(
                self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                for table in ("listings", "technical_archive", "runs", "searches")
            ):
                raise DatabaseError(
                    "offline import destination must contain no application records"
                )
            for record in records:
                listing_id = _integer(record["id"], "imported listing ID")
                facts = record["facts"]
                facts_to_dict(facts)
                first_seen = _text(record["first_seen_at"], "first_seen_at")
                last_seen = _text(record["last_seen_at"], "last_seen_at")
                availability = record.get("availability", "unknown")
                if availability not in {"available", "unavailable", "unknown"}:
                    raise ValueError("invalid imported availability")
                self._conn.execute(
                    "INSERT INTO listings(id,source,source_listing_id,source_url,availability,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        listing_id,
                        facts.source,
                        facts.source_listing_id,
                        facts.source_url,
                        availability,
                        first_seen,
                        last_seen,
                    ),
                )
                manual = record.get("manual", {})
                personal = _number(
                    manual.get("personal_score", 0), "imported personal score"
                )
                if not 0 <= personal <= policy["max_scores"]["personal"]:
                    raise ValueError(
                        "imported personal score does not fit the chosen policy"
                    )
                favorite, disliked = (
                    manual.get("favorite", False),
                    manual.get("disliked", False),
                )
                if type(favorite) is not bool or type(disliked) is not bool:
                    raise ValueError("imported manual flags must be boolean")
                self._conn.execute(
                    "INSERT INTO manual_decisions(listing_id,personal_score,favorite,disliked,personal_rated_at,favorited_at,disliked_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        listing_id,
                        personal,
                        int(favorite),
                        int(disliked),
                        manual.get("personal_rated_at"),
                        manual.get("favorited_at"),
                        manual.get("disliked_at"),
                    ),
                )
                observations = record["observations"]
                if not isinstance(observations, list) or not observations:
                    raise ValueError(
                        "import requires explicit nonempty observation history"
                    )
                pointers = []
                for observation in observations:
                    self._save_observation(
                        observation["facts"],
                        observation["parser_version"],
                        listing_id,
                        kind="import",
                        captured_at=observation["captured_at"],
                    )
                    pointers.append(
                        self._require_listing(listing_id)["current_observation_id"]
                    )
                index = record["current_observation_index"]
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(pointers)
                ):
                    raise ValueError("invalid imported current observation index")
                current = observations[index]["facts"]
                if facts_to_dict(current) != facts_to_dict(facts):
                    raise ValueError(
                        "imported current facts differ from selected observation"
                    )
                self._conn.execute(
                    "UPDATE listings SET current_observation_id=?,first_seen_at=?,last_seen_at=?,source_url=? WHERE id=?",
                    (
                        pointers[index],
                        first_seen,
                        last_seen,
                        facts.source_url,
                        listing_id,
                    ),
                )
                self._sync_gallery(listing_id, facts)
                if "photos" in record:
                    self._save_photos(listing_id, record["photos"])
                self._reassess(listing_id, policy)
            for record in archive_rows:
                self._archive_record(record)

    def health(self) -> dict[str, Any]:
        with self.read_snapshot():
            integrity = [row[0] for row in self._conn.execute("PRAGMA integrity_check")]
            foreign_keys = [
                tuple(row) for row in self._conn.execute("PRAGMA foreign_key_check")
            ]
            self._validate_schema()
            return {
                "schema_version": SCHEMA_VERSION,
                "integrity": "ok" if integrity == ["ok"] else "failed",
                "integrity_messages": integrity,
                "foreign_key_errors": foreign_keys,
                "ok": integrity == ["ok"] and not foreign_keys,
            }


__all__ = ["Database", "DatabaseError", "SCHEMA_VERSION"]
