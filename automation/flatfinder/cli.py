"""Command-line entrypoint for the production FlatFinder pipeline."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

from .browser import close_context, detect_blocker, find_vault_root, open_context
from .enrich import normalize_facts, persist_enrichment
from .export import export_json
from .models import VISION_RUBRIC_VERSION
from .noise import DEFAULT_SOURCE_URL, apply_noise, build_noise_map, calculate_noise
from .notify import backup_database, notify
from .pipeline import normalize_blocker, run_listing_vision, run_once
from .scoring import (
    normalized_max_scores,
    normalized_scoring_parameters,
    score_bucket,
    score_maxima,
)
from .sources import adapter_for_search_url
from .storage import (
    MAX_SQLITE_ID,
    connect_db,
    latest_commute_check,
    latest_fitness_check,
    latest_fitness_check_at_point,
    latest_office_point,
    merge_run_summary,
    migrate,
    record_commute_check,
    record_fitness_check,
    record_park_check,
    update_personal_score,
)
from .twogis import (
    address_hash,
    apply_commute,
    apply_fitness,
    apply_location_point,
    apply_park,
    geocode_address,
    saved_point,
)
from .yandex_routes import (
    YandexMapsRouteError,
    YandexMapsRouter,
    calculate_commute,
    calculate_fitness,
    calculate_park,
)

_AUTOMATION_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_RUNTIME_DIR = Path.home() / "Library/Application Support/MoscowFlatFinder"
_DEFAULT_CONFIG = _DEFAULT_RUNTIME_DIR / "config.toml"
_ADMIN_APP = _AUTOMATION_DIR / "flatfinder" / "admin.py"
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
_SUPPORTED_HARD_CONSTRAINTS = frozenset(
    {
        "max_monthly_total",
        "min_area_m2",
        "min_floor",
        "max_commute_minutes",
        "min_repair_score",
        "required_equipment",
    }
)


@dataclass(slots=True)
class Config:
    values: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class AnotherRun(RuntimeError):
    pass


def _export(config: Config, conn: Any) -> None:
    export_json(
        conn,
        config.json_export,
        max_scores=getattr(config, "scoring_max_scores", None),
        scoring_parameters=getattr(config, "scoring_parameters", None),
        vision_contract=getattr(config, "vision_contract", None),
    )


def _new_candidate_count(conn: Any, run_id: int) -> int:
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


def _three_failed(conn: Any) -> bool:
    rows = conn.execute(
        "SELECT status FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 4"
    ).fetchall()
    statuses = [str(row[0]) for row in rows]
    return (
        len(statuses) >= 3
        and statuses[:3] == ["failed"] * 3
        and (len(statuses) == 3 or statuses[3] != "failed")
    )


def _notify_safe(event_kind: str, count: int = 1) -> None:
    try:
        notify(event_kind, count)
    except Exception:
        print(f"flatfinder warning: notification {event_kind} failed", file=sys.stderr)


def _notify_result(conn: Any, result: Any) -> None:
    if result.status == "success":
        count = _new_candidate_count(conn, result.run_id)
        if count:
            _notify_safe("new_candidates", count)
    else:
        blocker = normalize_blocker(result.blocked_reason)
        if blocker:
            _notify_safe(blocker)
    if _three_failed(conn):
        _notify_safe("three_failed")


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


def load_config(path: str | Path = _DEFAULT_CONFIG) -> Config:
    config_path = Path(path).expanduser().resolve()
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    values: dict[str, Any] = dict(data)
    capabilities = _table(data, "capabilities")
    geo = _table(data, "geo")
    vision = _table(data, "vision")
    paths = _table(data, "paths")
    scoring = _table(data, "scoring")
    hard_constraints = _table(data, "hard_constraints")

    runtime_raw = data.get("runtime_dir", _DEFAULT_RUNTIME_DIR)
    runtime_dir = Path(str(runtime_raw)).expanduser()
    if not runtime_dir.is_absolute():
        runtime_dir = (config_path.parent / runtime_dir).resolve()
    else:
        runtime_dir = runtime_dir.resolve()
    values["runtime_dir"] = str(runtime_dir)

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
        values[name] = _resolved_path(base, raw)

    values["destination"] = geo.get("destination", data.get("destination", ""))
    direct_key = str(geo.get("twogis_api_key", data.get("twogis_api_key", "")) or "")
    keychain_service = str(geo.get("keychain_service", "MoscowFlatFinder.2GIS") or "")
    keychain_account = str(
        geo.get("keychain_account", os.environ.get("USER", "")) or ""
    )
    values["twogis_api_key"] = direct_key or _keychain_secret(
        keychain_service, keychain_account
    )
    values["twogis_keychain_service"] = keychain_service
    values["twogis_keychain_account"] = keychain_account
    for name, default in (
        ("route_min_interval_seconds", 30),
        ("route_jitter_seconds", 10),
        ("route_timeout_seconds", 120),
    ):
        values[name] = geo.get(name, data.get(name, default))

    legacy_geo = bool(values["destination"] and values["twogis_api_key"])
    values["geo_enabled"] = capabilities.get("geo", legacy_geo)
    values["noise_enabled"] = capabilities.get(
        "noise", bool(data.get("noise_map")) if "noise_map" in data else False
    )
    values["vision_enabled"] = capabilities.get(
        "vision", vision.get("enabled", data.get("vision_enabled", False))
    )
    values["vision_scoring_enabled"] = vision.get(
        "scoring_enabled", data.get("vision_scoring_enabled", False)
    )
    values["vision_auto_validate"] = vision.get(
        "auto_validate", data.get("vision_auto_validate", False)
    )
    values["vision_provider"] = str(vision.get("provider", "codex"))
    values["vision_model"] = str(vision.get("model", "gpt-5.6-luna"))
    values["vision_reasoning_effort"] = str(vision.get("reasoning_effort", "medium"))
    values["vision_codex_bin"] = str(
        vision.get("codex_bin", data.get("vision_codex_bin", "codex"))
    )
    values["vision_claude_bin"] = str(vision.get("claude_bin", "claude"))
    values["vision_timeout_seconds"] = vision.get(
        "timeout_seconds", data.get("vision_timeout_seconds", 900)
    )
    values["vision_contract"] = (
        values["vision_provider"].strip().lower(),
        values["vision_model"],
        values["vision_reasoning_effort"],
        VISION_RUBRIC_VERSION,
    )

    max_points = scoring.get("max_points", {})
    if not isinstance(max_points, dict):
        raise ValueError("[scoring.max_points] must be a TOML table")
    configured_max_scores = normalized_max_scores(max_points)
    values["configured_scoring_max_scores"] = configured_max_scores
    effective_max_scores = dict(configured_max_scores)
    if not values["geo_enabled"]:
        for name in ("commute", "park", "fitness"):
            effective_max_scores[name] = 0.0
    if not values["noise_enabled"]:
        effective_max_scores["noise"] = 0.0
    if not (values["vision_enabled"] and values["vision_scoring_enabled"]):
        for name in ("repair", "visual_layout", "light_view"):
            effective_max_scores[name] = 0.0
    values["scoring_max_scores"] = effective_max_scores
    thresholds = scoring.get("thresholds", {"priority": 80, "good": 70, "reserve": 60})
    if not isinstance(thresholds, dict):
        raise ValueError("[scoring.thresholds] must be a TOML table")
    values["scoring_thresholds"] = dict(thresholds)
    parameters = scoring.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError("[scoring.parameters] must be a TOML table")
    values["scoring_parameters"] = normalized_scoring_parameters(parameters)
    score_maxima(values["scoring_max_scores"])

    unknown_constraints = sorted(set(hard_constraints) - _SUPPORTED_HARD_CONSTRAINTS)
    if unknown_constraints:
        raise ValueError(
            f"unsupported hard constraints: {', '.join(unknown_constraints)}"
        )
    values["hard_constraints"] = dict(hard_constraints)

    for name in (
        "geo_enabled",
        "noise_enabled",
        "vision_enabled",
        "vision_scoring_enabled",
        "vision_auto_validate",
    ):
        if name in values and not isinstance(values[name], bool):
            raise ValueError(f"{name} must be true or false")
    vault_root = find_vault_root(config_path)
    if vault_root is not None:
        values["vault_root"] = str(vault_root)
    values["config_path"] = str(config_path)
    return Config(values)


def _search_configs(config: Config) -> list[Config]:
    """Expand multi-source searches while retaining legacy search_url."""

    raw = config.values.get("searches")
    if raw is None:
        url = str(config.values.get("search_url", "")).strip()
        if not url:
            raise ValueError("search_url or [[searches]] is required")
        raw = [{"url": url}]
    if not isinstance(raw, list) or not raw:
        raise ValueError("searches must be a non-empty TOML array of tables")
    result: list[Config] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("url", "")).strip():
            raise ValueError("every [[searches]] entry requires url")
        url = str(item["url"]).strip()
        adapter_for_search_url(url)
        if url in seen:
            raise ValueError("search URLs must be unique")
        seen.add(url)
        values = {**config.values, "search_url": url}
        result.append(Config(values))
    return result


@contextmanager
def acquire_lock(path: str | Path) -> Iterator[None]:
    lock_path = Path(path).expanduser().resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AnotherRun("another run is active") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def _login(config: Config) -> int:
    context = await open_context(config, headed=True)
    try:
        pages = list(getattr(context, "pages", []))
        opened = []
        for index, search in enumerate(_search_configs(config)):
            page = pages[0] if index == 0 and pages else await context.new_page()
            opened.append(page)
            goto = getattr(page, "goto", None)
            if callable(goto):
                try:
                    await goto(search.search_url, wait_until="domcontentloaded")
                except TypeError:
                    await goto(search.search_url)
        input(
            "Выполните вход на открытых сайтах и нажмите Enter после успешного входа: "
        )
        for page in opened:
            reason = await detect_blocker(page)
            if reason:
                print(f"login not confirmed: {reason}", file=sys.stderr)
                return 2
        print("login confirmed")
        return 0
    finally:
        await close_context(context)


async def _run(config: Config, *, refresh_vision: bool = False) -> int:
    api_key = str(getattr(config, "twogis_api_key", "") or "")
    destination = str(getattr(config, "destination", "") or "").strip()
    if bool(getattr(config, "geo_enabled", False)) and (not api_key or not destination):
        raise ValueError("Geo requires a 2GIS API key and destination")
    if (
        bool(getattr(config, "noise_enabled", False))
        and not str(getattr(config, "noise_map", "") or "").strip()
    ):
        raise ValueError("Noise scoring requires paths.noise_map")
    conn = connect_db(config.database)
    try:
        migrate(conn)
        results = []
        for search in _search_configs(config):
            result = await run_once(
                search, conn, refresh_existing_vision=refresh_vision
            )
            results.append(result)
            print(
                {
                    "source": adapter_for_search_url(search.search_url).source,
                    "status": result.status,
                    "run_id": result.run_id,
                    "cards_found": result.cards_found,
                    "cards_new": result.cards_new,
                    "cards_changed": result.cards_changed,
                    "written_assessments": result.written_assessments,
                    "cards_failed": result.cards_failed,
                    "retries": result.retries,
                    "field_coverage_p50": result.field_coverage_p50,
                    "photos_processed": result.photos_processed,
                    "top_n_checks": result.top_n_checks,
                    "blocked_reason": result.blocked_reason,
                    "enriched_count": result.enriched_count,
                    "enrichment_failed": result.enrichment_failed,
                    "enrichment_errors": result.enrichment_errors,
                    "vision_attempts": result.vision_attempts,
                    "vision_failed": result.vision_failed,
                    "visual_coverage": result.visual_coverage,
                    "manual_review_count": result.manual_review_count,
                }
            )
            _notify_result(conn, result)
        successful = [result for result in results if result.status == "success"]
        if successful:
            _export(config, conn)
            digest = hashlib.sha256(Path(config.json_export).read_bytes()).hexdigest()
            for result in successful:
                merge_run_summary(conn, result.run_id, {"json_export_sha256": digest})
            backup_database(conn, Path(config.database).parent / "backups")
        return (
            2
            if any(result.status == "blocked" for result in results)
            else 1
            if any(result.status == "failed" for result in results)
            else 0
        )
    finally:
        conn.close()


def _personal_score(config: Config, identifier: str, score: str) -> int:
    conn = connect_db(config.database)
    try:
        migrate(conn)
        row = update_personal_score(
            conn,
            identifier,
            float(score),
            max_scores=getattr(config, "scoring_max_scores", None),
        )
        _export(config, conn)
        print(f"personal score updated: {row['personal_score']}")
        return 0
    finally:
        conn.close()


def _refresh_noise_map(config: Config, source: str | None) -> int:
    target = str(getattr(config, "noise_map", "") or "").strip()
    if not target:
        raise ValueError("noise_map is required for refresh-noise-map")
    print(build_noise_map(target, source or DEFAULT_SOURCE_URL))
    return 0


def _doctor(config: Config, *, json_output: bool = False) -> int:
    """Run non-mutating readiness checks for the selected local profile."""

    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    is_macos = platform.system() == "Darwin"
    is_arm = platform.machine() == "arm64"
    add(
        "platform",
        "ok" if is_macos and is_arm else "error",
        "Apple Silicon macOS"
        if is_macos and is_arm
        else f"unsupported: {platform.system()} {platform.machine()}",
    )
    supported_python = (3, 12) <= sys.version_info[:2] < (3, 15)
    add(
        "python",
        "ok" if supported_python else "error",
        platform.python_version(),
    )
    dependencies = ("crawlee", "playwright", "PIL", "streamlit", "osmium", "shapely")
    missing = [name for name in dependencies if importlib.util.find_spec(name) is None]
    add(
        "dependencies",
        "ok" if not missing else "error",
        "installed" if not missing else f"missing: {', '.join(missing)}",
    )
    chromium = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--list"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    chromium_ready = chromium.returncode == 0 and "chromium" in chromium.stdout.lower()
    add(
        "chromium",
        "ok" if chromium_ready else "error",
        "installed" if chromium_ready else "Playwright Chromium is missing",
    )
    try:
        searches = _search_configs(config)
    except (TypeError, ValueError) as error:
        add("sources", "error", str(error))
    else:
        sources = sorted(
            {adapter_for_search_url(item.search_url).source for item in searches}
        )
        add("sources", "ok", ", ".join(sources))

    runtime_dir = Path(config.runtime_dir)
    add(
        "runtime_dir",
        "ok" if runtime_dir.is_dir() else "error",
        "exists" if runtime_dir.is_dir() else "directory is missing",
    )
    profile_dir = Path(config.profile_dir)
    add(
        "browser_profile",
        "ok" if profile_dir.is_dir() else "error",
        "exists" if profile_dir.is_dir() else "directory is missing",
    )
    search_profile = Path(config.search_profile)
    add(
        "search_profile",
        "ok" if search_profile.is_file() else "error",
        "exists" if search_profile.is_file() else "search-profile.md is missing",
    )

    if config.geo_enabled:
        missing_geo = []
        if not str(config.destination or "").strip():
            missing_geo.append("destination")
        if not str(config.twogis_api_key or "").strip():
            missing_geo.append("2GIS key")
        add(
            "geo",
            "ok" if not missing_geo else "error",
            "enabled" if not missing_geo else f"missing: {', '.join(missing_geo)}",
        )
    else:
        add("geo", "ok", "disabled")

    if config.noise_enabled:
        noise_map = Path(config.noise_map)
        add(
            "noise",
            "ok" if noise_map.is_file() else "error",
            "enabled" if noise_map.is_file() else "noise map is missing",
        )
    else:
        add("noise", "ok", "disabled")

    provider = str(config.vision_provider).strip().lower()
    if config.vision_enabled:
        if provider not in {"codex", "claude"}:
            add("vision", "error", f"unsupported provider: {provider}")
        else:
            executable = (
                str(config.vision_codex_bin)
                if provider == "codex"
                else str(config.vision_claude_bin)
            )
            found = Path(executable).is_file() or shutil.which(executable) is not None
            prompt_exists = Path(config.vision_agent_config).is_file()
            cli_ready = False
            if found:
                command = (
                    [executable, "login", "status"]
                    if provider == "codex"
                    else [executable, "--version"]
                )
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                cli_ready = result.returncode == 0 and (
                    provider == "claude"
                    or "ChatGPT" in f"{result.stdout}\n{result.stderr}"
                )
            add(
                "vision",
                "ok" if found and prompt_exists and cli_ready else "error",
                "enabled"
                if found and prompt_exists and cli_ready
                else ", ".join(
                    part
                    for part, missing_part in (
                        ("CLI is missing", not found),
                        ("CLI login is unavailable", found and not cli_ready),
                        ("prompt is missing", not prompt_exists),
                    )
                    if missing_part
                ),
            )
            if config.vision_model != "gpt-5.6-luna":
                add(
                    "vision_calibration",
                    "warning",
                    "model is allowed but not calibrated against Luna",
                )
    else:
        add("vision", "ok", "disabled")

    constraints = config.hard_constraints
    contradictions = []
    if "max_commute_minutes" in constraints and not config.geo_enabled:
        contradictions.append("max_commute_minutes requires Geo")
    if "min_repair_score" in constraints and not (
        config.vision_enabled and config.vision_scoring_enabled
    ):
        contradictions.append("min_repair_score requires enabled Vision scoring")
    try:
        score_bucket(0, config.scoring_thresholds)
    except (TypeError, ValueError) as error:
        contradictions.append(str(error))
    automatic_max, personal_max, total_max = score_maxima(config.scoring_max_scores)
    if float(config.scoring_thresholds.get("reserve", 0)) > automatic_max:
        contradictions.append("reserve threshold exceeds the enabled automatic maximum")
    add(
        "config_consistency",
        "ok" if not contradictions else "error",
        "consistent" if not contradictions else "; ".join(contradictions),
    )
    add(
        "scoring",
        "ok",
        f"automatic_max={automatic_max:g}, personal_max={personal_max:g}, total_max={total_max:g}",
    )

    database = Path(config.database)
    if database.exists():
        try:
            conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                schema = int(conn.execute("PRAGMA user_version").fetchone()[0])
            finally:
                conn.close()
            database_ok = integrity == "ok" and schema in {0, 15, 16}
            add(
                "database",
                "warning"
                if database_ok and schema == 15
                else "ok"
                if database_ok
                else "error",
                f"integrity={integrity}, schema={schema}"
                + ("; migration to 16 is pending" if schema == 15 else ""),
            )
        except sqlite3.Error as error:
            add("database", "error", str(error))
    else:
        add("database", "ok", "not created yet")

    if json_output:
        print(json.dumps({"checks": checks}, ensure_ascii=False, sort_keys=True))
    else:
        for check in checks:
            print(f"{check['status'].upper():7} {check['name']}: {check['detail']}")
    return 1 if any(check["status"] == "error" for check in checks) else 0


def _retry_route_rows(conn: Any, listing_id: int | None = None) -> list[Any]:
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


def _listing_facts_and_address(row: Any) -> tuple[dict[str, Any], str]:
    facts = normalize_facts(
        json.loads(row["facts_json"] or "{}"), source=str(row["source"])
    )
    fields = facts.get("fields", {})
    raw_address = (
        (fields.get("address") or fields.get("location"))
        if isinstance(fields, dict)
        else None
    )
    address = raw_address.get("value") if isinstance(raw_address, dict) else raw_address
    return facts, str(address or "").strip()


async def _retry_route_listing(
    config: Config,
    conn: Any,
    row: Any,
    facts: dict[str, Any],
    address: str,
    router: Any,
    api_key: str,
    destination: str,
    vision_scoring_enabled: bool,
) -> bool | None:
    listing_id = int(row["id"])
    fields = facts.get("fields", {})
    raw_point = fields.get("location_point") if isinstance(fields, dict) else None
    listing_point = saved_point(
        raw_point.get("value") if isinstance(raw_point, dict) else raw_point,
        "home",
    )
    location_changed = False
    if (
        listing_point is None
        or listing_point.get("provider") != "2gis"
        or listing_point.get("precision") != "exact"
        or not listing_point.get("building_id")
    ):
        point = await asyncio.to_thread(
            geocode_address, address, api_key, hint_point=listing_point
        )
        apply_location_point(facts, point)
        listing_point = saved_point(point, "home")
        location_changed = True

    commute_payload = latest_commute_check(
        conn,
        listing_id,
        address_sha256=address_hash(address),
        successful_only=True,
    )
    fitness_payload = latest_fitness_check(
        conn,
        listing_id,
        address_sha256=address_hash(address),
        successful_only=True,
    )
    for name, payload in (
        ("commute", commute_payload),
        ("fitness", fitness_payload),
    ):
        point = saved_point(payload, "home")
        if payload is not None and (
            point is None
            or listing_point is None
            or point["lat"] != listing_point["lat"]
            or point["lon"] != listing_point["lon"]
        ):
            if name == "commute":
                commute_payload = None
            else:
                fitness_payload = None
    if (
        commute_payload is not None
        and fitness_payload is not None
        and not location_changed
    ):
        return None

    if commute_payload is None:
        commute = await calculate_commute(
            router,
            address,
            destination,
            api_key,
            home_point=listing_point,
            office_point=latest_office_point(conn, address_hash(destination)),
        )
        commute_payload = commute.to_payload()
        record_commute_check(conn, listing_id, commute_payload)
    apply_commute(facts, commute_payload)
    if fitness_payload is None:
        if listing_point is not None:
            fitness_payload = latest_fitness_check_at_point(
                conn, listing_point["lat"], listing_point["lon"]
            )
        if fitness_payload is None:
            fitness_payload = (
                await calculate_fitness(
                    router,
                    address,
                    api_key,
                    home_point=listing_point or saved_point(commute_payload, "home"),
                )
            ).to_payload()
        else:
            fitness_payload.pop("id", None)
            fitness_payload.update(
                {"address": address, "address_sha256": address_hash(address)}
            )
        record_fitness_check(conn, listing_id, fitness_payload)
    apply_fitness(facts, fitness_payload)
    persist_enrichment(
        conn,
        listing_id,
        facts,
        vision_scoring_enabled=vision_scoring_enabled,
        max_scores=getattr(config, "scoring_max_scores", None),
        parameters=getattr(config, "scoring_parameters", None),
        thresholds=getattr(config, "scoring_thresholds", None),
        hard_constraints=getattr(config, "hard_constraints", None),
        vision_contract=getattr(config, "vision_contract", None),
    )
    return (
        commute_payload.get("status") == "success"
        and fitness_payload.get("status") == "success"
    )


async def _retry_routes(config: Config, listing_id: int | None = None) -> int:
    api_key = str(getattr(config, "twogis_api_key", "") or "")
    destination = str(getattr(config, "destination", "") or "").strip()
    if not api_key or not destination:
        raise ValueError("twogis_api_key and destination are required for retry-routes")
    conn = connect_db(config.database)
    context = router = None
    retried = succeeded = failed = 0
    try:
        migrate(conn)
        rows = _retry_route_rows(conn, listing_id)
        context = await open_context(
            config, headed=bool(getattr(config, "headed", True))
        )
        router = await YandexMapsRouter.from_context(context, config)
        for row in rows:
            try:
                facts, address = _listing_facts_and_address(row)
                if not address:
                    failed += 1
                    continue
                route_succeeded = await _retry_route_listing(
                    config,
                    conn,
                    row,
                    facts,
                    address,
                    router,
                    api_key,
                    destination,
                    bool(getattr(config, "vision_scoring_enabled", False)),
                )
                if route_succeeded is None:
                    continue
                retried += 1
                if route_succeeded:
                    succeeded += 1
                else:
                    failed += 1
                print(
                    {
                        "listing_id": int(row["id"]),
                        "retried": retried,
                        "succeeded": succeeded,
                        "failed": failed,
                    },
                    flush=True,
                )
            except YandexMapsRouteError:
                raise
            except Exception as error:
                failed += 1
                print(
                    f"flatfinder warning: route retry for listing {row['id']} failed ({error})",
                    file=sys.stderr,
                )
        _export(config, conn)
        print({"retried": retried, "succeeded": succeeded, "failed": failed})
        return 0
    finally:
        if router is not None:
            await router.close()
        await close_context(context)
        conn.close()


def _coordinate_rows(
    conn: Any, listing_id: int | None = None, after_id: int | None = None
) -> list[Any]:
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


def _for_address(payload: dict[str, Any], address: str) -> dict[str, Any]:
    result = json.loads(json.dumps(payload, ensure_ascii=False))
    result.pop("id", None)
    result.update({"address": address, "address_sha256": address_hash(address)})
    return result


async def _refresh_coordinate_listing(
    config: Config,
    conn: Any,
    row: Any,
    facts: dict[str, Any],
    address: str,
    router: Any,
    api_key: str,
    destination: str,
    noise_map: str,
    geocodes: dict[str, dict[str, Any]],
    commutes: dict[tuple[float, float], dict[str, Any]],
    parks: dict[tuple[float, float], dict[str, Any]],
    fitnesses: dict[tuple[float, float], dict[str, Any]],
    office_point: dict[str, Any] | None,
    vision_scoring_enabled: bool,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    Any,
    dict[str, Any] | None,
]:
    listing_id = int(row["id"])
    address_sha256 = address_hash(address)
    fields = facts.get("fields", {})
    raw_point = fields.get("location_point") if isinstance(fields, dict) else None
    hint_point = saved_point(
        raw_point.get("value") if isinstance(raw_point, dict) else raw_point,
        "home",
    )
    point = geocodes.get(address_sha256)
    if point is None:
        point = await asyncio.to_thread(
            geocode_address, address, api_key, hint_point=hint_point
        )
        geocodes[address_sha256] = point
    apply_location_point(facts, point)
    home = saved_point(point, "home")
    if home is None:
        raise ValueError("2GIS returned invalid building coordinates")
    point_key = (home["lat"], home["lon"])

    commute_payload = commutes.get(point_key)
    if commute_payload is None:
        commute_payload = (
            await calculate_commute(
                router,
                address,
                destination,
                api_key,
                home_point=home,
                office_point=office_point,
            )
        ).to_payload()
        commutes[point_key] = commute_payload
        office_point = office_point or saved_point(commute_payload, "office")
    commute_payload = _for_address(commute_payload, address)
    apply_commute(facts, commute_payload)

    park_payload = parks.get(point_key)
    if park_payload is None:
        park_payload = (
            await calculate_park(router, address, api_key, home_point=home)
        ).to_payload()
        parks[point_key] = park_payload
    park_payload = _for_address(park_payload, address)
    apply_park(facts, park_payload)

    fitness_payload = fitnesses.get(point_key)
    if fitness_payload is None:
        fitness_payload = (
            await calculate_fitness(router, address, api_key, home_point=home)
        ).to_payload()
        fitnesses[point_key] = fitness_payload
    fitness_payload = _for_address(fitness_payload, address)
    apply_fitness(facts, fitness_payload)

    noise = await asyncio.to_thread(calculate_noise, address, home, noise_map)
    apply_noise(facts, noise)
    record_commute_check(conn, listing_id, commute_payload)
    record_park_check(conn, listing_id, park_payload)
    record_fitness_check(conn, listing_id, fitness_payload)
    persist_enrichment(
        conn,
        listing_id,
        facts,
        vision_scoring_enabled=vision_scoring_enabled,
        max_scores=getattr(config, "scoring_max_scores", None),
        parameters=getattr(config, "scoring_parameters", None),
        thresholds=getattr(config, "scoring_thresholds", None),
        hard_constraints=getattr(config, "hard_constraints", None),
        vision_contract=getattr(config, "vision_contract", None),
    )
    return home, commute_payload, park_payload, fitness_payload, noise, office_point


async def _refresh_coordinates(
    config: Config,
    listing_id: int | None = None,
    after_id: int | None = None,
) -> int:
    api_key = str(getattr(config, "twogis_api_key", "") or "")
    destination = str(getattr(config, "destination", "") or "").strip()
    noise_map = str(getattr(config, "noise_map", "") or "").strip()
    if not api_key or not destination or not noise_map:
        raise ValueError(
            "twogis_api_key, destination and noise_map are required for refresh-coordinates"
        )
    if listing_id is not None and after_id is not None:
        raise ValueError("--listing-id and --after-id cannot be used together")

    conn = connect_db(config.database)
    context = router = None
    updated = failed = 0
    geocodes: dict[str, dict[str, Any]] = {}
    commutes: dict[tuple[float, float], dict[str, Any]] = {}
    parks: dict[tuple[float, float], dict[str, Any]] = {}
    fitnesses: dict[tuple[float, float], dict[str, Any]] = {}
    try:
        migrate(conn)
        rows = _coordinate_rows(conn, listing_id, after_id)
        backup = backup_database(
            conn, Path(config.database).parent / "backups", keep=10_000
        )
        print({"backup": str(backup), "listings": len(rows)}, flush=True)
        context = await open_context(
            config, headed=bool(getattr(config, "headed", True))
        )
        router = await YandexMapsRouter.from_context(context, config)
        office_point = latest_office_point(conn, address_hash(destination))
        for index, row in enumerate(rows, 1):
            try:
                facts, address = _listing_facts_and_address(row)
                if not address:
                    raise ValueError("address is missing")
                (
                    home,
                    commute_payload,
                    park_payload,
                    fitness_payload,
                    noise,
                    office_point,
                ) = await _refresh_coordinate_listing(
                    config,
                    conn,
                    row,
                    facts,
                    address,
                    router,
                    api_key,
                    destination,
                    noise_map,
                    geocodes,
                    commutes,
                    parks,
                    fitnesses,
                    office_point,
                    bool(getattr(config, "vision_scoring_enabled", False)),
                )
                updated += 1
                print(
                    {
                        "listing_id": int(row["id"]),
                        "progress": f"{index}/{len(rows)}",
                        "coordinates": [home["lat"], home["lon"]],
                        "commute": commute_payload.get("status"),
                        "park": park_payload.get("status"),
                        "fitness": fitness_payload.get("status"),
                        "noise": noise.status,
                    },
                    flush=True,
                )
            except YandexMapsRouteError:
                raise
            except Exception as error:
                failed += 1
                print(
                    f"flatfinder warning: coordinate refresh for listing {row['id']} failed ({error})",
                    file=sys.stderr,
                    flush=True,
                )
        print(
            {"updated": updated, "failed": failed, "geocoded_addresses": len(geocodes)},
            flush=True,
        )
        return 1 if failed else 0
    finally:
        if updated:
            _export(config, conn)
        if router is not None:
            await router.close()
        await close_context(context)
        conn.close()


def _vision(config: Config, identifier: str, force: bool = False) -> int:
    """Run one explicit visual evaluation without downloading a model."""

    conn = connect_db(config.database)
    runtime = None
    load_error: str | None = None
    try:
        migrate(conn)
        try:
            listing_id = int(identifier)
        except (TypeError, ValueError) as error:
            raise ValueError("listing_id must be an integer") from error
        agent_config = getattr(
            config,
            "vision_agent_config",
            str(Path(config.config_path).resolve().parent / "flatfinder-vision.toml"),
        )
        try:
            from .vision import VisionRuntime

            runtime = VisionRuntime.load(
                str(agent_config),
                provider=str(getattr(config, "vision_provider", "codex")),
                model_name=str(getattr(config, "vision_model", "gpt-5.6-luna")),
                reasoning_effort=str(
                    getattr(config, "vision_reasoning_effort", "medium")
                ),
                codex_bin=str(getattr(config, "vision_codex_bin", "codex")),
                claude_bin=str(getattr(config, "vision_claude_bin", "claude")),
                timeout_seconds=int(getattr(config, "vision_timeout_seconds", 900)),
            )
        except Exception as error:
            load_error = str(error)[:1000] or error.__class__.__name__
        result = run_listing_vision(
            conn,
            runtime,
            listing_id,
            force=force,
            auto_validate=bool(getattr(config, "vision_auto_validate", False)),
            vision_scoring_enabled=bool(
                getattr(config, "vision_scoring_enabled", False)
            ),
            max_scores=getattr(config, "scoring_max_scores", None),
            parameters=getattr(config, "scoring_parameters", None),
            thresholds=getattr(config, "scoring_thresholds", None),
            hard_constraints=getattr(config, "hard_constraints", None),
        )
        if load_error and result.error is None:
            result.error = load_error
        _export(config, conn)
        print(
            {
                "listing_id": listing_id,
                "status": result.status,
                "visual_coverage": max(
                    0.0, min(100.0, float(result.visual_coverage) * 100.0)
                ),
                "proposals": len(result.proposals),
                "schema_valid": result.schema_valid,
                "retry_count": result.retry_count,
                "error": result.error,
            }
        )
        return 0 if result.status in {"success", "skipped"} else 1
    finally:
        if runtime is not None:
            runtime.close()
        conn.close()


def _review_port(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("review port must be an integer from 1 to 65535")
    try:
        port = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("review port must be an integer from 1 to 65535") from exc
    if isinstance(value, float) and value != port:
        raise ValueError("review port must be an integer from 1 to 65535")
    if not 1 <= port <= 65535:
        raise ValueError("review port must be an integer from 1 to 65535")
    return port


def _review_listing_id(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("review listing_id must be a positive integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("review listing_id must be a positive integer")
    try:
        listing_id = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("review listing_id must be a positive integer") from exc
    if not isinstance(value, (str, int, float)) and value != listing_id:
        raise ValueError("review listing_id must be a positive integer")
    if listing_id <= 0 or listing_id > MAX_SQLITE_ID:
        raise ValueError("review listing_id must be a positive integer")
    return listing_id


def _review(config: Config, port: int, listing_id: object = None) -> int:
    """Launch the loopback-only Streamlit admin while the caller holds the lock."""

    validated_port = _review_port(port)
    validated_listing_id = _review_listing_id(listing_id)
    env = os.environ.copy()
    env["FLATFINDER_CONFIG"] = str(config.config_path)
    env["FLATFINDER_ADMIN_LOCKED"] = "1"
    if validated_listing_id is None:
        env.pop("FLATFINDER_LISTING_ID", None)
    else:
        env["FLATFINDER_LISTING_ID"] = str(validated_listing_id)
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(_ADMIN_APP),
        "--server.address=127.0.0.1",
        f"--server.port={validated_port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]
    child = subprocess.Popen(command, env=env, start_new_session=True)

    def forward_signal(signum: int, _frame: Any) -> None:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        for signum in previous_handlers:
            signal.signal(signum, forward_signal)
        return int(child.wait())
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flatfinder")
    parser.add_argument(
        "--config", default=str(_DEFAULT_CONFIG), help="path to config.toml"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor_command = commands.add_parser(
        "doctor", help="check local readiness without changing state"
    )
    doctor_command.add_argument("--json", action="store_true", help="emit JSON")
    login_command = commands.add_parser(
        "login", help="open the headed persistent profile for manual login"
    )
    run_command = commands.add_parser("run", help="discover and process listings")
    run_command.add_argument(
        "--refresh-vision",
        action="store_true",
        help="explicitly re-evaluate listings that already have a Vision result",
    )
    score_command = commands.add_parser(
        "personal-score", help="set a manual score from 0 to 10"
    )
    score_command.add_argument("listing_id")
    score_command.add_argument("score")
    vision_command = commands.add_parser(
        "vision", help="evaluate one listing with the configured Vision CLI"
    )
    vision_command.add_argument("--listing-id", required=True)
    vision_command.add_argument(
        "--force", action="store_true", help="ignore the unchanged-content hash"
    )
    review_command = commands.add_parser(
        "review", help="review local visual proposals on loopback"
    )
    review_command.add_argument("--port", type=int, default=8765)
    review_command.add_argument("--listing-id")
    retry_routes_command = commands.add_parser(
        "retry-routes", help="retry incomplete Yandex Maps routes"
    )
    retry_routes_command.add_argument(
        "--listing-id", type=int, help="retry only one internal listing id"
    )
    refresh_coordinates_command = commands.add_parser(
        "refresh-coordinates",
        help="replace listing coordinates with 2GIS and recalculate point-dependent checks",
    )
    refresh_coordinates_command.add_argument(
        "--listing-id", type=int, help="refresh only one internal listing id"
    )
    refresh_coordinates_command.add_argument(
        "--after-id", type=int, help="resume with listings after this internal id"
    )
    noise_command = commands.add_parser(
        "refresh-noise-map", help="build the local OSM road and rail layer"
    )
    noise_command.add_argument(
        "--source", help="local OSM/PBF path or URL (defaults to BBBike Moscow)"
    )
    for command in (
        doctor_command,
        login_command,
        run_command,
        score_command,
        vision_command,
        review_command,
        retry_routes_command,
        refresh_coordinates_command,
        noise_command,
    ):
        command.add_argument(
            "--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            return _doctor(config, json_output=args.json)
        if args.command == "login":
            with acquire_lock(config.lock_path):
                return asyncio.run(_login(config))
        if args.command == "run":
            with acquire_lock(config.lock_path):
                return asyncio.run(_run(config, refresh_vision=args.refresh_vision))
        if args.command == "personal-score":
            with acquire_lock(config.lock_path):
                return _personal_score(config, args.listing_id, args.score)
        if args.command == "vision":
            with acquire_lock(config.lock_path):
                return _vision(config, args.listing_id, args.force)
        if args.command == "retry-routes":
            with acquire_lock(config.lock_path):
                return asyncio.run(_retry_routes(config, args.listing_id))
        if args.command == "refresh-coordinates":
            with acquire_lock(config.lock_path):
                return asyncio.run(
                    _refresh_coordinates(config, args.listing_id, args.after_id)
                )
        if args.command == "refresh-noise-map":
            with acquire_lock(config.lock_path):
                return _refresh_noise_map(config, args.source)
        if args.command == "review":
            with acquire_lock(config.lock_path):
                return _review(config, args.port, args.listing_id)
    except AnotherRun as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        print(f"flatfinder: {error}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["AnotherRun", "Config", "acquire_lock", "load_config", "main"]
