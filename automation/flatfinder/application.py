"""Command-line entrypoint for the production FlatFinder pipeline."""

from __future__ import annotations

import asyncio
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
from dataclasses import replace
from pathlib import Path
from typing import Any

from .browser import close_context, detect_blocker, open_context
from .config import ADMIN_APP, Config, parse_listing_id, resolve_credentials
from .enrich import normalize_facts, persist_enrichment, recompute_assessment
from .export import export_json
from .noise import DEFAULT_SOURCE_URL, apply_noise, build_noise_map, calculate_noise
from .notify import backup_database, notify
from .queries import (
    coordinate_rows,
    database_health,
    has_new_three_failed_run_streak,
    new_candidate_count,
    reassessment_listing_ids,
    retry_route_rows,
)
from .scoring import score_bucket, score_maxima
from .scoring_policy import normalize_policy
from .sources import adapter_for_search_url
from .storage import (
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
    set_listing_disliked,
    set_listing_favorited,
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


def _export(config: Config, conn: Any) -> None:
    policy = normalize_policy(
        max_scores=config.scoring_max_scores,
        parameters=config.scoring_parameters,
        thresholds=config.scoring_thresholds,
        hard_constraints=config.hard_constraints,
        vision_scoring_enabled=config.vision_scoring_enabled,
        vision_contract=config.vision_contract,
    )
    export_json(
        conn,
        config.json_export,
        policy=policy,
        max_scores=config.scoring_max_scores,
        scoring_parameters=config.scoring_parameters,
        vision_contract=config.vision_contract,
    )


def _notify_safe(event_kind: str, count: int = 1) -> None:
    try:
        notify(event_kind, count)
    except Exception:
        print(f"flatfinder warning: notification {event_kind} failed", file=sys.stderr)


def _notify_result(conn: Any, result: Any) -> None:
    if result.status == "success":
        count = new_candidate_count(conn, result.run_id)
        if count:
            _notify_safe("new_candidates", count)
    else:
        from .pipeline import normalize_blocker

        blocker = normalize_blocker(result.blocked_reason)
        if blocker:
            _notify_safe(blocker)
    if has_new_three_failed_run_streak(conn):
        _notify_safe("three_failed")


def _search_configs(config: Config) -> list[Config]:
    """Expand configured searches into one typed config per source."""

    urls = config.searches or ((config.search_url,) if config.search_url else ())
    if not urls:
        raise ValueError("search_url or [[searches]] is required")
    result: list[Config] = []
    seen: set[str] = set()
    for url in urls:
        url = str(url).strip()
        adapter_for_search_url(url)
        if url in seen:
            raise ValueError("search URLs must be unique")
        seen.add(url)
        result.append(replace(config, search_url=url))
    return result


async def login(config: Config) -> int:
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


async def collect(config: Config, *, refresh_vision: bool = False) -> int:
    from .pipeline import run_once

    if config.geo_enabled:
        config = resolve_credentials(config)
    api_key = str(config.twogis_api_key or "")
    destination = str(config.destination or "").strip()
    if bool(config.geo_enabled) and (not api_key or not destination):
        raise ValueError("Geo requires a 2GIS API key and destination")
    if bool(config.noise_enabled) and not str(config.noise_map or "").strip():
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


def record_personal_score(config: Config, identifier: str, score: str) -> int:
    value = float(score)
    warnings = record_review(config, identifier, personal_score=value)
    for warning in warnings:
        print(f"flatfinder warning: {warning}", file=sys.stderr)
    print(f"personal score updated: {value}")
    return 0


def record_review(
    config: Config,
    listing_id: int | str,
    *,
    personal_score: float | None = None,
    disliked: bool | None = None,
    favorited: bool | None = None,
) -> list[str]:
    """Persist review decisions and report a generated-export failure as a warning."""

    supplied = sum(value is not None for value in (personal_score, disliked, favorited))
    if supplied != 1:
        raise ValueError("exactly one review decision is required")
    conn = connect_db(config.database)
    try:
        migrate(conn)
        if personal_score is not None:
            update_personal_score(
                conn,
                listing_id,
                personal_score,
                max_scores=config.scoring_max_scores,
            )
        if disliked is not None:
            set_listing_disliked(conn, listing_id, disliked)
        if favorited is not None:
            set_listing_favorited(conn, listing_id, favorited)
        try:
            _export(config, conn)
        except Exception as exc:
            detail = str(exc)[:500] or exc.__class__.__name__
            return [f"JSON-экспорт: {detail}"]
        return []
    finally:
        conn.close()


def reassess(config: Config, identifier: object = None) -> int:
    """Recompute assessments from saved facts without provider or Vision calls."""

    listing_id = parse_listing_id(identifier)
    conn = connect_db(config.database)
    try:
        migrate(conn)
        listing_ids = reassessment_listing_ids(conn, listing_id)
        succeeded = 0
        errors: list[dict[str, Any]] = []
        for current_id in listing_ids:
            try:
                recompute_assessment(
                    conn,
                    current_id,
                    vision_scoring_enabled=config.vision_scoring_enabled,
                    max_scores=config.scoring_max_scores,
                    parameters=config.scoring_parameters,
                    thresholds=config.scoring_thresholds,
                    hard_constraints=config.hard_constraints,
                    vision_contract=config.vision_contract,
                )
                succeeded += 1
            except Exception as exc:
                errors.append(
                    {
                        "listing_id": current_id,
                        "error": str(exc)[:500] or exc.__class__.__name__,
                    }
                )
        _export(config, conn)
        print(
            {
                "requested": len(listing_ids),
                "reassessed": succeeded,
                "failed": len(errors),
                "errors": errors,
                "listing_id": listing_id,
            }
        )
        return 1 if errors else 0
    finally:
        conn.close()


def refresh_noise_map(config: Config, source: str | None) -> int:
    target = str(config.noise_map or "").strip()
    if not target:
        raise ValueError("noise_map is required for refresh-noise-map")
    print(build_noise_map(target, source or DEFAULT_SOURCE_URL))
    return 0


def doctor(config: Config, *, json_output: bool = False) -> int:
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
        if (
            not str(config.twogis_api_key or "").strip()
            and not str(config.twogis_keychain_service or "").strip()
        ):
            missing_geo.append("2GIS key")
        add(
            "geo",
            "ok" if not missing_geo else "error",
            "enabled; credential resolution deferred"
            if not missing_geo
            else f"missing: {', '.join(missing_geo)}",
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
                integrity, schema = database_health(conn)
            finally:
                conn.close()
            database_ok = integrity == "ok" and schema in {0, 15, 16, 17}
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
        max_scores=config.scoring_max_scores,
        parameters=config.scoring_parameters,
        thresholds=config.scoring_thresholds,
        hard_constraints=config.hard_constraints,
        vision_contract=config.vision_contract,
    )
    return (
        commute_payload.get("status") == "success"
        and fitness_payload.get("status") == "success"
    )


async def retry_routes(config: Config, listing_id: int | None = None) -> int:
    config = resolve_credentials(config)
    api_key = str(config.twogis_api_key or "")
    destination = str(config.destination or "").strip()
    if not api_key or not destination:
        raise ValueError("twogis_api_key and destination are required for retry-routes")
    conn = connect_db(config.database)
    context = router = None
    retried = succeeded = failed = 0
    try:
        migrate(conn)
        rows = retry_route_rows(conn, listing_id)
        context = await open_context(config, headed=bool(config.headed))
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
                    bool(config.vision_scoring_enabled),
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
        await _close_geo_session(conn, router, context)


async def _close_geo_session(conn: Any, router: Any, context: Any) -> None:
    """Release every acquired resource even when another cleanup fails."""

    try:
        if router is not None:
            await router.close()
    finally:
        try:
            await close_context(context)
        finally:
            conn.close()


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
        max_scores=config.scoring_max_scores,
        parameters=config.scoring_parameters,
        thresholds=config.scoring_thresholds,
        hard_constraints=config.hard_constraints,
        vision_contract=config.vision_contract,
    )
    return home, commute_payload, park_payload, fitness_payload, noise, office_point


async def refresh_coordinates(
    config: Config,
    listing_id: int | None = None,
    after_id: int | None = None,
) -> int:
    config = resolve_credentials(config)
    api_key = str(config.twogis_api_key or "")
    destination = str(config.destination or "").strip()
    noise_map = str(config.noise_map or "").strip()
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
        rows = coordinate_rows(conn, listing_id, after_id)
        backup = backup_database(
            conn, Path(config.database).parent / "backups", keep=10_000
        )
        print({"backup": str(backup), "listings": len(rows)}, flush=True)
        context = await open_context(config, headed=bool(config.headed))
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
                    bool(config.vision_scoring_enabled),
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
        try:
            if updated:
                _export(config, conn)
        finally:
            await _close_geo_session(conn, router, context)


def analyze_photos(config: Config, identifier: str, force: bool = False) -> int:
    """Run one explicit visual evaluation without downloading a model."""

    from .vision_workflow import run_listing_vision

    conn = connect_db(config.database)
    runtime = None
    load_error: str | None = None
    try:
        migrate(conn)
        try:
            listing_id = int(identifier)
        except (TypeError, ValueError) as error:
            raise ValueError("listing_id must be an integer") from error
        agent_config = config.vision_agent_config
        try:
            from .vision import VisionRuntime

            runtime = VisionRuntime.load(
                str(agent_config),
                provider=str(config.vision_provider),
                model_name=str(config.vision_model),
                reasoning_effort=str(config.vision_reasoning_effort),
                codex_bin=str(config.vision_codex_bin),
                claude_bin=str(config.vision_claude_bin),
                timeout_seconds=int(config.vision_timeout_seconds),
            )
        except Exception as error:
            load_error = str(error)[:1000] or error.__class__.__name__
        result = run_listing_vision(
            conn,
            runtime,
            listing_id,
            force=force,
            auto_validate=bool(config.vision_auto_validate),
            vision_scoring_enabled=bool(config.vision_scoring_enabled),
            max_scores=config.scoring_max_scores,
            parameters=config.scoring_parameters,
            thresholds=config.scoring_thresholds,
            hard_constraints=config.hard_constraints,
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
        try:
            if runtime is not None:
                runtime.close()
        finally:
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


def review(config: Config, port: int, listing_id: object = None) -> int:
    """Launch the loopback-only Streamlit admin while the caller holds the lock."""

    validated_port = _review_port(port)
    validated_listing_id = parse_listing_id(listing_id)
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
        str(ADMIN_APP),
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
