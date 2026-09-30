"""Strict current TOML configuration; credentials resolve only on Geo operations."""

from __future__ import annotations

import platform
import re
import subprocess
import tomllib
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from typing import Any

from .scoring import MAX_SCORES
from .scoring_policy import normalize_policy

DEFAULT_RUNTIME_DIR = Path.home() / "Library/Application Support/MoscowFlatFinder"
DEFAULT_CONFIG = DEFAULT_RUNTIME_DIR / "config.toml"
_PATH_DEFAULTS = {
    "database": "data/listings.sqlite3",
    "export": "exports/listings.json",
    "noise_map": "data/moscow-transport-noise.json",
    "browser_profile": "browser-profile",
    "photos": "photos",
    "vision_prompt": "vision-prompt.toml",
    "search_profile": "search-profile.md",
    "lock": "flatfinder.lock",
}


@dataclass(frozen=True, slots=True)
class Paths:
    database: Path
    export: Path
    noise_map: Path
    browser_profile: Path
    photos: Path
    vision_prompt: Path
    search_profile: Path
    lock: Path


@dataclass(frozen=True, slots=True)
class CollectionSettings:
    max_listings: int
    retries: int
    headed: bool
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class GeoSettings:
    destination: str
    twogis_api_key: str
    keychain_service: str
    keychain_account: str
    min_interval_seconds: float
    jitter_seconds: float
    timeout_seconds: float
    departure_weekday: int
    to_work_time: str
    to_home_time: str


@dataclass(frozen=True, slots=True)
class VisionSettings:
    provider: str
    model: str
    reasoning_effort: str
    binary: str
    timeout_seconds: float
    scoring_enabled: bool
    auto_accept: bool


@dataclass(frozen=True, slots=True)
class Config:
    config_path: Path
    runtime_dir: Path
    paths: Paths
    searches: tuple[str, ...]
    collection: CollectionSettings
    geo: GeoSettings | None
    noise_enabled: bool
    vision: VisionSettings | None
    policy: dict[str, Any]


def _keys(data: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"unknown {name} keys: {', '.join(unknown)}")


def _table(data: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a TOML table")
    _keys(value, allowed, name)
    return value


def _string(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{name} must be {'a' if empty else 'a non-empty'} string")
    return value.strip()


def _bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false")
    return value


def _integer(value: Any, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'> 0' if positive else '>= 0'}")
    return result


def _path(base: Path, value: Any, name: str) -> Path:
    target = Path(_string(value, name)).expanduser()
    return (target if target.is_absolute() else base / target).resolve()


def _time(value: Any, name: str) -> str:
    result = _string(value, name)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", result):
        raise ValueError(f"{name} must have HH:MM format")
    return result


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    """Read and validate current TOML without resolving credentials or running CLIs."""
    config_path = Path(path).expanduser().resolve()
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    _keys(
        data,
        {
            "runtime_dir",
            "paths",
            "searches",
            "collection",
            "capabilities",
            "geo",
            "vision",
            "scoring",
            "hard_constraints",
        },
        "root",
    )
    runtime_dir = _path(
        config_path.parent,
        data.get("runtime_dir", str(DEFAULT_RUNTIME_DIR)),
        "runtime_dir",
    )
    if any(
        (parent / ".git").exists() for parent in (runtime_dir, *runtime_dir.parents)
    ):
        raise ValueError("runtime_dir must be outside the project checkout")
    paths_data = _table(data, "paths", set(_PATH_DEFAULTS))
    resolved_paths = {
        name: _path(runtime_dir, paths_data.get(name, default), f"paths.{name}")
        for name, default in _PATH_DEFAULTS.items()
    }
    for name, target in resolved_paths.items():
        if target == runtime_dir or not target.is_relative_to(runtime_dir):
            raise ValueError(f"paths.{name} must be inside runtime_dir")
    if len(set(resolved_paths.values())) != len(resolved_paths):
        raise ValueError("configured paths must be distinct")

    raw_searches = data.get("searches", [])
    if not isinstance(raw_searches, list):
        raise ValueError("searches must be an array of url tables")
    searches = []
    for entry in raw_searches:
        if not isinstance(entry, dict):
            raise ValueError("searches must be an array of url tables")
        _keys(entry, {"url"}, "searches")
        searches.append(_string(entry.get("url"), "searches.url"))
    if len(set(searches)) != len(searches):
        raise ValueError("search URLs must be unique")

    collection_data = _table(
        data, "collection", {"max_listings", "retries", "headed", "timeout_seconds"}
    )
    collection = CollectionSettings(
        _integer(collection_data.get("max_listings", 100), "collection.max_listings"),
        _integer(collection_data.get("retries", 2), "collection.retries", minimum=0),
        _bool(collection_data.get("headed", True), "collection.headed"),
        _number(
            collection_data.get("timeout_seconds", 120),
            "collection.timeout_seconds",
            positive=True,
        ),
    )
    capabilities = _table(data, "capabilities", {"geo", "noise", "vision"})
    enabled = {
        name: _bool(capabilities.get(name, False), f"capabilities.{name}")
        for name in ("geo", "noise", "vision")
    }
    geo_data = _table(data, "geo", set(GeoSettings.__dataclass_fields__))
    weekday = _integer(
        geo_data.get("departure_weekday", 1), "geo.departure_weekday", minimum=0
    )
    if weekday > 6:
        raise ValueError("geo.departure_weekday must be between 0 and 6")
    geo = GeoSettings(
        _string(geo_data.get("destination", ""), "geo.destination", empty=True),
        _string(geo_data.get("twogis_api_key", ""), "geo.twogis_api_key", empty=True),
        _string(
            geo_data.get("keychain_service", ""), "geo.keychain_service", empty=True
        ),
        _string(
            geo_data.get("keychain_account", ""), "geo.keychain_account", empty=True
        ),
        _number(geo_data.get("min_interval_seconds", 30), "geo.min_interval_seconds"),
        _number(geo_data.get("jitter_seconds", 10), "geo.jitter_seconds"),
        _number(
            geo_data.get("timeout_seconds", 120), "geo.timeout_seconds", positive=True
        ),
        weekday,
        _time(geo_data.get("to_work_time", "09:00"), "geo.to_work_time"),
        _time(geo_data.get("to_home_time", "19:00"), "geo.to_home_time"),
    )
    if enabled["geo"] and (
        not geo.destination
        or not (geo.twogis_api_key or (geo.keychain_service and geo.keychain_account))
    ):
        raise ValueError(
            "enabled Geo requires destination and an inline key "
            "or explicit Keychain service/account"
        )

    vision_data = _table(data, "vision", set(VisionSettings.__dataclass_fields__))
    provider = _string(vision_data.get("provider", "codex"), "vision.provider")
    if provider not in {"codex", "claude"}:
        raise ValueError("vision.provider must be codex or claude")
    effort = _string(
        vision_data.get("reasoning_effort", "medium"), "vision.reasoning_effort"
    )
    efforts = (
        {"minimal", "low", "medium", "high", "xhigh"}
        if provider == "codex"
        else {"low", "medium", "high", "xhigh", "max"}
    )
    if effort not in efforts:
        raise ValueError(f"unsupported vision.reasoning_effort for {provider}")
    vision = VisionSettings(
        provider,
        _string(
            vision_data.get("model", ""), "vision.model", empty=not enabled["vision"]
        ),
        effort,
        _string(vision_data.get("binary", provider), "vision.binary"),
        _number(
            vision_data.get("timeout_seconds", 900),
            "vision.timeout_seconds",
            positive=True,
        ),
        _bool(vision_data.get("scoring_enabled", False), "vision.scoring_enabled"),
        _bool(vision_data.get("auto_accept", False), "vision.auto_accept"),
    )
    if not enabled["vision"] and (vision.scoring_enabled or vision.auto_accept):
        raise ValueError(
            "Vision scoring/automatic acceptance requires capabilities.vision"
        )
    scoring = _table(data, "scoring", {"max_points", "parameters", "thresholds"})
    hard_constraints = data.get("hard_constraints", {})
    if not isinstance(hard_constraints, dict):
        raise ValueError("[hard_constraints] must be a TOML table")
    scoring_tables = {}
    for name in ("max_points", "parameters", "thresholds"):
        value = scoring.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"[scoring.{name}] must be a TOML table")
        scoring_tables[name] = value
    if "max_commute_minutes" in hard_constraints and not enabled["geo"]:
        raise ValueError("hard_constraints.max_commute_minutes requires Geo")
    if "min_repair_score" in hard_constraints and not (
        enabled["vision"] and vision.scoring_enabled
    ):
        raise ValueError("hard_constraints.min_repair_score requires Vision scoring")
    _keys(scoring_tables["max_points"], set(MAX_SCORES), "scoring.max_points")
    maxima = dict(MAX_SCORES)
    maxima.update(
        {
            name: _number(value, f"scoring.max_points.{name}")
            for name, value in scoring_tables["max_points"].items()
        }
    )
    if not enabled["geo"]:
        for name in ("commute", "park", "fitness"):
            maxima[name] = 0.0
    if not enabled["noise"]:
        maxima["noise"] = 0.0
    policy = normalize_policy(
        max_scores=maxima,
        parameters=scoring_tables["parameters"],
        thresholds=scoring_tables["thresholds"],
        hard_constraints=hard_constraints,
        vision_scoring_enabled=enabled["vision"] and vision.scoring_enabled,
        vision_contract=None,
    )
    return Config(
        config_path,
        runtime_dir,
        Paths(**resolved_paths),
        tuple(searches),
        collection,
        geo if enabled["geo"] else None,
        enabled["noise"],
        vision if enabled["vision"] else None,
        policy,
    )


def resolve_geo_credentials(settings: GeoSettings) -> GeoSettings:
    """Resolve the explicitly configured Keychain item for a requested Geo operation."""
    if settings.twogis_api_key:
        return settings
    if platform.system() != "Darwin" or not (
        settings.keychain_service and settings.keychain_account
    ):
        raise ValueError("Geo credentials are unavailable")
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-s",
            settings.keychain_service,
            "-a",
            settings.keychain_account,
            "-w",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError("the configured Geo Keychain credential is unavailable")
    return replace(settings, twogis_api_key=result.stdout.strip())


def parse_listing_id(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError("review listing_id must be a positive integer")
    try:
        listing_id = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("review listing_id must be a positive integer") from exc
    if not isinstance(value, (str, int, float)) and value != listing_id:
        raise ValueError("review listing_id must be a positive integer")
    if listing_id <= 0 or listing_id > 2**63 - 1:
        raise ValueError("review listing_id must be a positive integer")
    return listing_id


__all__ = [
    "DEFAULT_CONFIG",
    "CollectionSettings",
    "Config",
    "GeoSettings",
    "Paths",
    "VisionSettings",
    "load_config",
    "parse_listing_id",
    "resolve_geo_credentials",
]
