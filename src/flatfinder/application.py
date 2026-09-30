"""Application operations with explicit resources and incremental persistence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .browser import browser_context, detect_blocker
from .collection import CollectionBlocked, ListingUnavailable, discover, extract
from .config import Config, parse_listing_id, resolve_geo_credentials
from .database import Database
from .duplicates import match_duplicate
from .export import export_json
from .geo import MODEL_VERSION as GEO_MODEL_VERSION
from .geo import enrich_listing
from .locking import acquire_lock
from .noise import DEFAULT_SOURCE_URL, build_noise_map
from .noise import MODEL_VERSION as NOISE_MODEL_VERSION
from .notify import notify
from .photos import ingest_photos, photo_input_hash
from .scoring_policy import normalize_policy
from .sources import adapter_for_search_url, adapter_for_source
from .sources.common import collect_photo_urls
from .vision import VisionRuntime, build_contract, load_prompt
from .vision_contract import VisionContract
from .vision_workflow import run_listing_vision
from .yandex_routes import YandexMapsRouter


@dataclass(frozen=True, slots=True)
class OperationResult:
    operation: str
    requested: int
    completed: int
    errors: tuple[dict[str, Any], ...] = ()
    blocked_reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> int:
        return len(self.errors)

    @property
    def succeeded(self) -> int:
        return self.completed

    @property
    def status(self) -> str:
        return (
            "blocked" if self.blocked_reason else "failed" if self.errors else "success"
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "status": self.status}


def current_policy(config: Config) -> dict[str, Any]:
    """Identify the effective rubric without probing credentials or provider CLIs."""
    base = config.policy
    contract = None
    if config.vision is not None:
        contract = build_contract(
            config.vision, load_prompt(config.paths.vision_prompt)
        )
    measurement = {"geo": None, "noise": None}
    if config.geo is not None:
        settings = config.geo
        measurement["geo"] = {
            "destination": settings.destination,
            "departure_weekday": settings.departure_weekday,
            "to_work_time": settings.to_work_time,
            "to_home_time": settings.to_home_time,
            "model_version": GEO_MODEL_VERSION,
        }
    if config.noise_enabled:
        map_hash = None
        if config.paths.noise_map.is_file():
            with config.paths.noise_map.open("rb") as handle:
                map_hash = hashlib.file_digest(handle, "sha256").hexdigest()
        measurement["noise"] = {
            "map_sha256": map_hash,
            "model_version": NOISE_MODEL_VERSION,
        }
    context = (
        hashlib.sha256(json.dumps(measurement, sort_keys=True).encode()).hexdigest()
        if config.geo is not None or config.noise_enabled
        else None
    )
    return normalize_policy(
        measurement_context=context,
        max_scores=base["max_scores"],
        parameters=base["parameters"],
        thresholds=base["thresholds"],
        hard_constraints=base["hard_constraints"],
        vision_scoring_enabled=base["vision_scoring_enabled"],
        vision_contract=contract,
    )


def _identifier(value: object) -> int:
    identifier = parse_listing_id(value)
    if identifier is None:
        raise ValueError("listing ID must be a positive integer")
    return identifier


def _error(
    config: Config, stage: str, error: Exception, identifier: int | None = None
) -> dict[str, Any]:
    message = str(error) or type(error).__name__
    private = list(config.searches)
    if config.geo is not None:
        private += [config.geo.destination, config.geo.twogis_api_key or ""]
    for value in sorted(filter(None, private), key=len, reverse=True):
        message = message.replace(value, "[private]")
    return {
        "stage": stage,
        "listing_id": identifier,
        "type": type(error).__name__,
        "message": message[:1000],
    }


def initialize(config: Config) -> dict[str, Any]:
    with (
        acquire_lock(config.paths.lock),
        Database.initialize(config.paths.database) as database,
    ):
        return database.health()


def _export_warnings(
    config: Config, database: Database, policy: dict[str, Any]
) -> list[str]:
    try:
        export_json(database, config.paths.export, policy)
    except (OSError, ValueError, RuntimeError) as error:
        return [
            "Данные сохранены в SQLite; JSON не обновлён: "
            + _error(config, "export", error)["message"]
        ]
    return []


def _finish(
    config: Config,
    database: Database,
    run_id: int,
    operation: str,
    requested: int,
    completed: int,
    errors: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    blocked_reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> OperationResult:
    for warning in _export_warnings(config, database, policy):
        errors.append(
            {
                "stage": "export",
                "listing_id": None,
                "type": "ExportError",
                "message": warning,
            }
        )
    result = OperationResult(
        operation, requested, completed, tuple(errors), blocked_reason, details or {}
    )
    database.finish_run(run_id, result.status, result.to_dict())
    return result


def record_review(
    config: Config,
    listing_id: int,
    *,
    personal_score: float | None = None,
    disliked: bool | None = None,
    favorited: bool | None = None,
) -> list[str]:
    policy = current_policy(config)
    with acquire_lock(config.paths.lock), Database(config.paths.database) as database:
        database.record_review(
            _identifier(listing_id),
            policy=policy,
            personal_score=personal_score,
            disliked=disliked,
            favorite=favorited,
        )
        return _export_warnings(config, database, policy)


def review_vision(config: Config, run_id: int, accept: bool) -> list[str]:
    policy = current_policy(config)
    with acquire_lock(config.paths.lock), Database(config.paths.database) as database:
        database.review_vision(_identifier(run_id), accept, policy)
        return _export_warnings(config, database, policy)


def reassess(config: Config, listing_id: int | None = None) -> OperationResult:
    policy = current_policy(config)
    with acquire_lock(config.paths.lock), Database(config.paths.database) as database:
        ids = (
            [_identifier(listing_id)]
            if listing_id is not None
            else database.listing_ids()
        )
        run_id = database.start_run("reassess")
        errors: list[dict[str, Any]] = []
        completed = 0
        try:
            for identifier in ids:
                try:
                    database.reassess(identifier, policy)
                    completed += 1
                except Exception as error:
                    errors.append(_error(config, "assessment", error, identifier))
            return _finish(
                config,
                database,
                run_id,
                "reassess",
                len(ids),
                completed,
                errors,
                policy,
            )
        except BaseException:
            database.finish_run(run_id, "cancelled", {"completed": completed})
            raise


def _vision_runtime(config: Config) -> VisionRuntime:
    if config.vision is None:
        raise ValueError("Vision capability is disabled")
    prompt = load_prompt(config.paths.vision_prompt)
    return VisionRuntime.load(
        config.vision, prompt, build_contract(config.vision, prompt)
    )


def _analyze(
    database: Database,
    config: Config,
    identifier: int,
    policy: dict[str, Any],
    runtime: VisionRuntime | None,
    runtime_error: Exception | None,
    *,
    force: bool = False,
):
    if runtime_error is not None:
        contract = VisionContract.from_dict(policy["vision_contract"])
        run_id = database.begin_vision(
            identifier, contract, photo_input_hash(database.photos(identifier))
        )
        database.fail_vision(
            run_id, _error(config, "vision", runtime_error, identifier)["message"]
        )
        raise runtime_error
    if runtime is None or config.vision is None:
        raise ValueError("Vision capability is disabled")
    result = run_listing_vision(
        database,
        identifier,
        runtime=runtime,
        policy=policy,
        auto_accept=config.vision.auto_accept,
        force=force,
    )
    if result.error:
        raise RuntimeError(result.error)
    return result


def analyze_photos(
    config: Config, listing_id: int, *, force: bool = False
) -> OperationResult:
    identifier = _identifier(listing_id)
    policy = current_policy(config)
    if config.vision is None:
        raise ValueError("Vision capability is disabled")
    with acquire_lock(config.paths.lock), Database(config.paths.database) as database:
        if database.listing(identifier) is None:
            raise ValueError("listing does not exist")
        run_id = database.start_run("vision")
        errors: list[dict[str, Any]] = []
        details: dict[str, Any] = {}
        completed = 0
        try:
            try:
                runtime = _vision_runtime(config)
            except Exception as error:
                runtime, runtime_error = None, error
            else:
                runtime_error = None
            try:
                result = _analyze(
                    database,
                    config,
                    identifier,
                    policy,
                    runtime,
                    runtime_error,
                    force=force,
                )
                details = {
                    "vision_run_id": result.run_id,
                    "vision_status": result.status,
                }
                completed = 1
            except Exception as error:
                errors.append(_error(config, "vision", error, identifier))
            return _finish(
                config,
                database,
                run_id,
                "vision",
                1,
                completed,
                errors,
                policy,
                details=details,
            )
        except BaseException:
            database.finish_run(run_id, "cancelled", {})
            raise


async def _router(
    context: Any, config: Config, settings: Any
) -> YandexMapsRouter | None:
    if settings is None:
        return None
    return YandexMapsRouter(
        await context.new_page(),
        min_interval_seconds=settings.min_interval_seconds,
        jitter_seconds=settings.jitter_seconds,
        timeout_seconds=settings.timeout_seconds,
        diagnostics_dir=config.runtime_dir / "diagnostics",
    )


async def _enrich(
    database: Database,
    config: Config,
    identifier: int,
    policy: dict[str, Any],
    settings: Any,
    router: YandexMapsRouter | None,
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    item = database.listing(identifier)
    if item is None:
        raise ValueError("listing does not exist")
    result = await enrich_listing(
        item["facts"],
        settings=settings,
        noise_enabled=config.noise_enabled,
        noise_map=config.paths.noise_map,
        router=router,
        api_key=settings.twogis_api_key if settings is not None else None,
        cached_check=None
        if force
        else lambda kind, identity: database.cached_check(identifier, kind, identity),
    )
    database.save_enrichment(identifier, result.facts, result.checks, policy)
    return result.checks


def _link_duplicates(database: Database, identifier: int) -> None:
    current = database.listing(identifier)
    if current is None:
        raise ValueError("listing does not exist")
    photos = database.photos(identifier)
    for other in database.listings():
        if other["source"] == current["source"]:
            continue
        match = match_duplicate(
            current["facts"], photos, other["facts"], database.photos(other["id"])
        )
        if match is not None:
            database.link_duplicates(
                identifier, other["id"], match.method, match.confidence, match.evidence
            )


def _notify_result(
    database: Database, result: OperationResult, new_ids: set[int]
) -> None:
    events: list[tuple[str, int]] = []
    if result.blocked_reason in {"captcha", "login", "2fa"}:
        events.append((result.blocked_reason, 1))
    if result.status == "success":
        count = sum(
            item["status"] in {"priority", "good"}
            and item["assessment"]["eligibility"]["status"] == "eligible"
            and not item["disliked"]
            for item in database.listings()
            if item["id"] in new_ids
        )
        if count:
            events.append(("new_candidates", count))
    recent = [
        run["status"]
        for run in reversed(database.runs())
        if run["kind"] == "collection" and run["status"] != "running"
    ][:4]
    if (
        len(recent) >= 3
        and recent[:3] == ["failed"] * 3
        and (len(recent) == 3 or recent[3] != "failed")
    ):
        events.append(("three_failed", 1))
    for event, count in events:
        try:
            notify(event, count)
        except (OSError, subprocess.SubprocessError):
            pass


async def enrich(
    config: Config, listing_id: int | None = None, *, force: bool = False
) -> OperationResult:
    if config.geo is None and not config.noise_enabled:
        raise ValueError("Geo and Noise capabilities are disabled")
    policy = current_policy(config)
    settings = resolve_geo_credentials(config.geo) if config.geo is not None else None
    with acquire_lock(config.paths.lock), Database(config.paths.database) as database:
        ids = (
            [_identifier(listing_id)]
            if listing_id is not None
            else database.listing_ids()
        )
        run_id = database.start_run("enrich")
        errors: list[dict[str, Any]] = []
        completed = 0
        blocked_reason = None

        async def process(router: YandexMapsRouter | None) -> None:
            nonlocal completed, blocked_reason
            for identifier in ids:
                try:
                    checks = await _enrich(
                        database,
                        config,
                        identifier,
                        policy,
                        settings,
                        router,
                        force=force,
                    )
                    _link_duplicates(database, identifier)
                    failed = [
                        check
                        for check in checks
                        if check["status"] in {"failed", "blocked"}
                    ]
                    blocked_reason = next(
                        (
                            check["payload"].get("blocked_reason")
                            for check in failed
                            if check["status"] == "blocked"
                        ),
                        None,
                    )
                    if failed:
                        raise RuntimeError(
                            "enrichment checks failed: "
                            + ", ".join(check["kind"] for check in failed)
                        )
                    completed += 1
                except Exception as error:
                    errors.append(_error(config, "enrichment", error, identifier))
                    if blocked_reason:
                        break

        try:
            if settings is None:
                await process(None)
            else:
                async with browser_context(
                    config.paths.browser_profile, config.collection.headed
                ) as context:
                    await process(await _router(context, config, settings))
            return _finish(
                config,
                database,
                run_id,
                "enrich",
                len(ids),
                completed,
                errors,
                policy,
                blocked_reason=blocked_reason,
            )
        except Exception as error:
            errors.append(_error(config, "enrichment", error))
            return _finish(
                config,
                database,
                run_id,
                "enrich",
                len(ids),
                completed,
                errors,
                policy,
                blocked_reason=blocked_reason,
            )
        except BaseException:
            database.finish_run(run_id, "cancelled", {"completed": completed})
            raise


async def collect(config: Config, *, refresh_vision: bool = False) -> OperationResult:
    if not config.searches:
        raise ValueError("configure at least one search")
    for url in config.searches:
        adapter_for_search_url(url)
    policy = current_policy(config)
    settings = resolve_geo_credentials(config.geo) if config.geo is not None else None
    runtime = runtime_error = None
    if config.vision is not None:
        try:
            runtime = _vision_runtime(config)
        except Exception as error:
            runtime_error = error
    with acquire_lock(config.paths.lock):
        database = (
            Database(config.paths.database)
            if config.paths.database.exists()
            else Database.initialize(config.paths.database)
        )
        with database:
            database.register_searches(config.searches)
            existing_ids = set(database.listing_ids(active_only=False))
            new_ids: set[int] = set()
            run_id = database.start_run("collection")
            errors: list[dict[str, Any]] = []
            completed = requested = 0
            blocked_reason = None
            blocked_sources: set[str] = set()
            geo_blocked_reason = None
            offers: dict[tuple[str, str], str] = {}
            try:
                async with browser_context(
                    config.paths.browser_profile, config.collection.headed
                ) as context:
                    page = await context.new_page()
                    router = await _router(context, config, settings)
                    for search in config.searches:
                        discovered = await discover(
                            page,
                            search,
                            config.collection.max_listings,
                            config.collection.timeout_seconds,
                            config.collection.retries,
                        )
                        database.record_search(
                            search,
                            [url for _, url in discovered.links],
                            discovered.complete,
                        )
                        for source_id, url in discovered.links:
                            offers[(discovered.source, source_id)] = url
                        if discovered.error:
                            errors.append(
                                _error(
                                    config, "discovery", RuntimeError(discovered.error)
                                )
                            )
                            if discovered.error.startswith("blocked:"):
                                blocked_reason = discovered.error.split(":", 1)[1]
                                blocked_sources.add(discovered.source)
                    selected = list(offers.items())[: config.collection.max_listings]
                    requested = len(selected)
                    for (source, source_id), url in selected:
                        if source in blocked_sources:
                            continue
                        identifier = None
                        try:
                            facts = await extract(
                                page,
                                url,
                                source_id,
                                config.collection.timeout_seconds,
                                config.collection.retries,
                            )
                            identifier = database.save_observation(
                                facts, adapter_for_source(source).parser_version, policy
                            )
                            completed += 1
                            if identifier not in existing_ids:
                                new_ids.add(identifier)
                        except CollectionBlocked as error:
                            blocked_reason = error.reason
                            blocked_sources.add(source)
                            errors.append(_error(config, "collection", error))
                            continue
                        except ListingUnavailable:
                            for item in database.listings(include_inactive=True):
                                if (
                                    item["source"] == source
                                    and item["source_listing_id"] == source_id
                                ):
                                    database.set_availability(item["id"], "unavailable")
                                    break
                            continue
                        except Exception as error:
                            errors.append(_error(config, "collection", error))
                            continue
                        try:
                            photos = await ingest_photos(
                                page,
                                identifier,
                                collect_photo_urls(facts),
                                config.paths.photos,
                            )
                            database.save_photos(identifier, photos, policy)
                            if any(photo.status == "failed" for photo in photos):
                                raise RuntimeError(
                                    "one or more listing photos could not be downloaded"
                                )
                        except Exception as error:
                            errors.append(_error(config, "photos", error, identifier))
                        if (
                            settings is not None and not geo_blocked_reason
                        ) or config.noise_enabled:
                            try:
                                checks = await _enrich(
                                    database,
                                    config,
                                    identifier,
                                    policy,
                                    settings if not geo_blocked_reason else None,
                                    router if not geo_blocked_reason else None,
                                )
                                blockers = [
                                    check
                                    for check in checks
                                    if check["status"] == "blocked"
                                ]
                                if blockers:
                                    geo_blocked_reason = blockers[0]["payload"].get(
                                        "blocked_reason"
                                    )
                                    blocked_reason = geo_blocked_reason
                                if any(
                                    check["status"] in {"failed", "blocked"}
                                    for check in checks
                                ):
                                    raise RuntimeError(
                                        "one or more enrichment checks failed"
                                    )
                            except Exception as error:
                                errors.append(
                                    _error(config, "enrichment", error, identifier)
                                )
                        try:
                            _link_duplicates(database, identifier)
                        except Exception as error:
                            errors.append(
                                _error(config, "duplicates", error, identifier)
                            )
                        if config.vision is not None:
                            try:
                                _analyze(
                                    database,
                                    config,
                                    identifier,
                                    policy,
                                    runtime,
                                    runtime_error,
                                    force=refresh_vision,
                                )
                            except Exception as error:
                                errors.append(
                                    _error(config, "vision", error, identifier)
                                )
                result = _finish(
                    config,
                    database,
                    run_id,
                    "collection",
                    requested,
                    completed,
                    errors,
                    policy,
                    blocked_reason=blocked_reason,
                    details={
                        "discovered": len(offers),
                        "processed_limit": config.collection.max_listings,
                    },
                )
            except Exception as error:
                errors.append(_error(config, "collection", error))
                result = _finish(
                    config,
                    database,
                    run_id,
                    "collection",
                    requested,
                    completed,
                    errors,
                    policy,
                    blocked_reason=blocked_reason,
                )
            except BaseException:
                database.finish_run(run_id, "cancelled", {"completed": completed})
                raise
            _notify_result(database, result, new_ids)
            return result


async def login(config: Config) -> int:
    if not config.searches:
        raise ValueError("configure at least one search")
    with acquire_lock(config.paths.lock):
        async with browser_context(config.paths.browser_profile, True) as context:
            pages = []
            for url in config.searches:
                adapter_for_search_url(url)
                page = await context.new_page()
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=config.collection.timeout_seconds * 1000,
                )
                pages.append(page)
            input("Выполните вход в открытом браузере и нажмите Enter: ")
            return 2 if any([await detect_blocker(page) for page in pages]) else 0


def refresh_noise_map(
    config: Config, source: str | None, bounds: tuple[float, float, float, float]
) -> dict[str, Any]:
    if not config.noise_enabled:
        raise ValueError("Noise capability is disabled")
    with acquire_lock(config.paths.lock):
        result = build_noise_map(
            config.paths.noise_map, source or DEFAULT_SOURCE_URL, coverage_bounds=bounds
        )
    return result


def doctor(config: Config) -> dict[str, Any]:
    """Local checks only: no browser, inference, credentials or schema writes."""
    checks: dict[str, Any] = {}
    try:
        policy = current_policy(config)
        checks["policy"] = {"ok": True, "fingerprint": policy["fingerprint"]}
    except (OSError, ValueError) as error:
        checks["policy"] = {
            "ok": False,
            "error": _error(config, "policy", error)["message"],
        }
    checks["searches"] = {"ok": bool(config.searches), "count": len(config.searches)}
    for url in config.searches:
        try:
            adapter_for_search_url(url)
        except ValueError:
            checks["searches"]["ok"] = False
    if config.paths.database.exists():
        try:
            with Database(config.paths.database, readonly=True) as database:
                checks["database"] = database.health()
        except (OSError, ValueError) as error:
            checks["database"] = {
                "ok": False,
                "error": _error(config, "database", error)["message"],
            }
    else:
        checks["database"] = {
            "ok": False,
            "error": "database missing; run init or offline import",
        }
    if config.vision is not None:
        checks["vision_binary"] = {
            "ok": shutil.which(config.vision.binary) is not None,
            "authentication": "not_checked",
        }
    if config.noise_enabled:
        checks["noise_map"] = {"ok": config.paths.noise_map.is_file()}
    return {"ok": all(check["ok"] for check in checks.values()), "checks": checks}


def review(config: Config, port: int = 8765, listing_id: int | None = None) -> int:
    if not 1024 <= port <= 65535:
        raise ValueError("review port must be within 1024..65535")
    with Database(config.paths.database, readonly=True):
        pass
    environment = {**os.environ, "FLATFINDER_CONFIG": str(config.config_path)}
    if listing_id is not None:
        environment["FLATFINDER_LISTING_ID"] = str(_identifier(listing_id))
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(Path(__file__).with_name("ui.py")),
            "--server.address",
            "127.0.0.1",
            "--server.port",
            str(port),
        ],
        env=environment,
    )


def backup(config: Config, *, keep: int | None = None) -> dict[str, Any]:
    from .backups import backup_database

    with acquire_lock(config.paths.lock):
        with Database(config.paths.database, readonly=True) as database:
            if not database.health()["ok"]:
                raise ValueError("source database integrity check failed")
        path = backup_database(
            config.paths.database, config.runtime_dir / "backups", keep=keep
        )
    return {"backup": str(path), "keep": keep}


def unlink_duplicate(config: Config, left: int, right: int) -> list[str]:
    policy = current_policy(config)
    with acquire_lock(config.paths.lock), Database(config.paths.database) as database:
        database.unlink_duplicates(_identifier(left), _identifier(right))
        return _export_warnings(config, database, policy)
