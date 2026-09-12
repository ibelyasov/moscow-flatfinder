"""Validated application configuration and credential resolution."""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from typing import Any

import tomllib

from .scoring_policy import normalize_policy
from .vision_contract import DEFAULT_VISION_CONTRACT

AUTOMATION_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_DIR = Path.home() / "Library/Application Support/MoscowFlatFinder"
DEFAULT_CONFIG = DEFAULT_RUNTIME_DIR / "config.toml"
ADMIN_APP = AUTOMATION_DIR / "flatfinder" / "admin.py"

_PATH_DEFAULTS = {
    "database": "data/listings.sqlite3",
    "json_export": "exports/listings.json",
    "noise_map": "data/moscow-transport-noise.json",
    "profile_dir": "browser-profile",
    "photo_cache_dir": "photos",
    "vision_agent_config": "vision-prompt.toml",
    "search_profile": "search-profile.md",
    "lock_path": "flatfinder.lock",
}
_MAX_SQLITE_ID = 2**63 - 1


@dataclass(frozen=True, slots=True)
class Config:
    """Complete validated config used by all application entrypoints."""

    config_path: str
    runtime_dir: str
    database: str
    json_export: str
    noise_map: str
    profile_dir: str
    photo_cache_dir: str
    vision_agent_config: str
    search_profile: str
    lock_path: str
    searches: tuple[str, ...]
    search_url: str | None
    destination: str
    twogis_api_key: str
    twogis_keychain_service: str
    twogis_keychain_account: str
    route_min_interval_seconds: float
    route_jitter_seconds: float
    route_timeout_seconds: float
    geo_enabled: bool
    noise_enabled: bool
    vision_enabled: bool
    vision_scoring_enabled: bool
    vision_auto_validate: bool
    vision_provider: str
    vision_model: str
    vision_reasoning_effort: str
    vision_codex_bin: str
    vision_claude_bin: str
    vision_timeout_seconds: int
    vision_contract: tuple[str, str, str, str]
    configured_scoring_max_scores: dict[str, float]
    scoring_max_scores: dict[str, float]
    scoring_thresholds: dict[str, Any]
    scoring_parameters: dict[str, float]
    hard_constraints: dict[str, Any]
    top_n: int
    max_cards_per_run: int
    network_retries: int
    headed: bool
    vault_root: str | None


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a TOML table")
    return value


def _resolved_path(base: Path, raw: Any) -> str:
    target = Path(str(raw)).expanduser()
    return str(
        (base / target).resolve() if not target.is_absolute() else target.resolve()
    )


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if isinstance(value, float) and value != result:
        raise ValueError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


def _seconds(value: Any, name: str, *, allow_zero: bool = True) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not isfinite(result) or result < 0 or (not allow_zero and result == 0):
        operator = ">= 0" if allow_zero else "> 0"
        raise ValueError(f"{name} must be finite and {operator}")
    return result


def find_vault_root(start: str | Path) -> Path | None:
    start = Path(start).expanduser().resolve()
    path = (
        start.parent if start.is_file() or start.suffix in {".toml", ".json"} else start
    )
    for candidate in (path, *path.parents):
        if (candidate / ".vault-config.json").is_file() or (
            candidate / ".obsidian"
        ).is_dir():
            return candidate
    return None


def _search_urls(data: dict[str, Any]) -> tuple[str, ...]:
    raw = data.get("searches")
    if raw is None:
        legacy = str(data.get("search_url", "")).strip()
        return (legacy,) if legacy else ()
    if not isinstance(raw, list) or not raw:
        raise ValueError("searches must be a non-empty TOML array of tables")
    result = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("url", "")).strip():
            raise ValueError("every [[searches]] entry requires url")
        result.append(str(item["url"]).strip())
    if len(set(result)) != len(result):
        raise ValueError("search URLs must be unique")
    return tuple(result)


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    """Load TOML without contacting Keychain or another credential provider."""

    config_path = Path(path).expanduser().resolve()
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    capabilities = _table(data, "capabilities")
    geo = _table(data, "geo")
    vision = _table(data, "vision")
    paths = _table(data, "paths")
    scoring = _table(data, "scoring")
    hard_constraints = _table(data, "hard_constraints")

    runtime_dir = Path(str(data.get("runtime_dir", DEFAULT_RUNTIME_DIR))).expanduser()
    runtime_dir = (
        (config_path.parent / runtime_dir).resolve()
        if not runtime_dir.is_absolute()
        else runtime_dir.resolve()
    )
    resolved_paths: dict[str, str] = {}
    for name, default in _PATH_DEFAULTS.items():
        raw = paths.get(name, data.get(name, default))
        if (
            name == "vision_agent_config"
            and not paths
            and name not in data
            and (config_path.parent / "flatfinder-vision.toml").is_file()
        ):
            raw = config_path.parent / "flatfinder-vision.toml"
        base = config_path.parent if name in data and name not in paths else runtime_dir
        resolved_paths[name] = _resolved_path(base, raw)

    destination = str(geo.get("destination", data.get("destination", "")) or "")
    direct_key = str(geo.get("twogis_api_key", data.get("twogis_api_key", "")) or "")
    keychain_service = str(geo.get("keychain_service", "MoscowFlatFinder.2GIS") or "")
    keychain_account = str(
        geo.get("keychain_account", os.environ.get("USER", "")) or ""
    )
    # A pure loader cannot inspect whether the default Keychain item exists.
    # Deferred Keychain credentials therefore require an explicit capability;
    # legacy automatic enablement remains available for an inline key.
    legacy_geo = bool(
        destination and (direct_key or ("keychain_service" in geo and keychain_service))
    )
    geo_enabled = _bool(capabilities.get("geo", legacy_geo), "geo_enabled")
    noise_enabled = _bool(
        capabilities.get(
            "noise", bool(data.get("noise_map")) if "noise_map" in data else False
        ),
        "noise_enabled",
    )
    vision_enabled = _bool(
        capabilities.get(
            "vision", vision.get("enabled", data.get("vision_enabled", False))
        ),
        "vision_enabled",
    )
    vision_scoring_enabled = _bool(
        vision.get("scoring_enabled", data.get("vision_scoring_enabled", False)),
        "vision_scoring_enabled",
    )
    vision_auto_validate = _bool(
        vision.get("auto_validate", data.get("vision_auto_validate", False)),
        "vision_auto_validate",
    )
    headed = _bool(data.get("headed", True), "headed")
    provider = str(vision.get("provider", DEFAULT_VISION_CONTRACT.provider))
    model = str(vision.get("model", DEFAULT_VISION_CONTRACT.model_name))
    reasoning = str(
        vision.get("reasoning_effort", DEFAULT_VISION_CONTRACT.reasoning_effort)
    )
    vision_contract = (
        provider.strip().lower(),
        model,
        reasoning,
        DEFAULT_VISION_CONTRACT.prompt_version,
    )

    max_points = scoring.get("max_points", {})
    thresholds = scoring.get("thresholds", {"priority": 80, "good": 70, "reserve": 60})
    parameters = scoring.get("parameters", {})
    if not isinstance(max_points, dict):
        raise ValueError("[scoring.max_points] must be a TOML table")
    if not isinstance(thresholds, dict):
        raise ValueError("[scoring.thresholds] must be a TOML table")
    if not isinstance(parameters, dict):
        raise ValueError("[scoring.parameters] must be a TOML table")
    configured_policy = normalize_policy(
        max_scores=max_points,
        parameters=parameters if isinstance(parameters, dict) else None,
        thresholds=thresholds if isinstance(thresholds, dict) else None,
        hard_constraints=hard_constraints,
        vision_scoring_enabled=True,
        vision_contract=vision_contract,
    )
    configured_max_scores = dict(configured_policy["max_scores"])
    effective_max_scores = dict(configured_max_scores)
    if not geo_enabled:
        for name in ("commute", "park", "fitness"):
            effective_max_scores[name] = 0.0
    if not noise_enabled:
        effective_max_scores["noise"] = 0.0
    if not (vision_enabled and vision_scoring_enabled):
        for name in ("repair", "visual_layout", "light_view"):
            effective_max_scores[name] = 0.0
    policy = normalize_policy(
        max_scores=effective_max_scores,
        parameters=parameters,
        thresholds=thresholds,
        hard_constraints=hard_constraints,
        vision_scoring_enabled=vision_enabled and vision_scoring_enabled,
        vision_contract=vision_contract,
    )

    searches = _search_urls(data)
    vault = find_vault_root(config_path)
    explicit: dict[str, Any] = {
        "config_path": str(config_path),
        "runtime_dir": str(runtime_dir),
        **resolved_paths,
        "searches": searches,
        "search_url": str(data.get("search_url", "")).strip() or None,
        "destination": destination,
        "twogis_api_key": direct_key,
        "twogis_keychain_service": keychain_service,
        "twogis_keychain_account": keychain_account,
        "route_min_interval_seconds": _seconds(
            geo.get(
                "route_min_interval_seconds", data.get("route_min_interval_seconds", 30)
            ),
            "route_min_interval_seconds",
        ),
        "route_jitter_seconds": _seconds(
            geo.get("route_jitter_seconds", data.get("route_jitter_seconds", 10)),
            "route_jitter_seconds",
        ),
        "route_timeout_seconds": _seconds(
            geo.get("route_timeout_seconds", data.get("route_timeout_seconds", 120)),
            "route_timeout_seconds",
            allow_zero=False,
        ),
        "geo_enabled": geo_enabled,
        "noise_enabled": noise_enabled,
        "vision_enabled": vision_enabled,
        "vision_scoring_enabled": vision_scoring_enabled,
        "vision_auto_validate": vision_auto_validate,
        "vision_provider": provider,
        "vision_model": model,
        "vision_reasoning_effort": reasoning,
        "vision_codex_bin": str(
            vision.get("codex_bin", data.get("vision_codex_bin", "codex"))
        ),
        "vision_claude_bin": str(vision.get("claude_bin", "claude")),
        "vision_timeout_seconds": _positive_int(
            vision.get("timeout_seconds", data.get("vision_timeout_seconds", 900)),
            "vision_timeout_seconds",
        ),
        "vision_contract": vision_contract,
        "configured_scoring_max_scores": configured_max_scores,
        "scoring_max_scores": dict(policy["max_scores"]),
        "scoring_thresholds": dict(policy["thresholds"]),
        "scoring_parameters": dict(policy["parameters"]),
        "hard_constraints": dict(policy["hard_constraints"]),
        "top_n": _positive_int(data.get("top_n", 10), "top_n"),
        "max_cards_per_run": _positive_int(
            data.get("max_cards_per_run", 100), "max_cards_per_run"
        ),
        "network_retries": _positive_int(
            data.get("network_retries", 2), "network_retries", allow_zero=True
        ),
        "headed": headed,
        "vault_root": str(vault) if vault is not None else None,
    }
    return Config(**explicit)


def _keychain_secret(service: str, account: str) -> str:
    if platform.system() != "Darwin" or not service:
        return ""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve_credentials(config: Config) -> Config:
    """Return a config with its optional Geo credential resolved on demand."""

    if config.twogis_api_key:
        return config
    key = _keychain_secret(
        config.twogis_keychain_service, config.twogis_keychain_account
    )
    return replace(config, twogis_api_key=key)


def photo_cache_dir(config: Config | Any) -> Path:
    configured = getattr(config, "photo_cache_dir", None)
    if configured:
        path = Path(str(configured)).expanduser()
        return (
            path if path.is_absolute() else Path(__file__).resolve().parents[2] / path
        ).resolve()
    return (Path(__file__).resolve().parents[2] / "data" / "photos").resolve()


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
    if listing_id <= 0 or listing_id > _MAX_SQLITE_ID:
        raise ValueError("review listing_id must be a positive integer")
    return listing_id


__all__ = [
    "Config",
    "DEFAULT_CONFIG",
    "find_vault_root",
    "load_config",
    "parse_listing_id",
    "photo_cache_dir",
    "resolve_credentials",
]
