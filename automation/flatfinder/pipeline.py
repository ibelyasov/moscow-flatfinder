"""Single-worker browser pipeline for registered listing sources."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import sqlite3
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from crawlee import ConcurrencySettings, Request, service_locator
from crawlee.browsers import BrowserPool
from crawlee.configuration import Configuration as CrawleeConfiguration
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.errors import ServiceConflictError
from crawlee.events import EventManager
from crawlee.router import Router
from crawlee.storage_clients import FileSystemStorageClient
from crawlee.storages import RequestQueue

from . import queries
from .assessment import evaluate_listing
from .browser import (
    classify_blocker,
    detect_blocker,
    prepare_profile_dir,
    start_browser_background_watcher,
    stop_browser_background_watcher,
)
from .config import Config, photo_cache_dir
from .enrich import (
    apply_enrichment,
    enrich_environment,
    normalize_facts,
    persist_enrichment,
    select_top_candidates,
)
from .models import PhotoInput
from .noise import apply_noise, calculate_noise
from .photos import ingest_photos
from .sources import (
    adapter_for_listing_url,
    adapter_for_search_url,
    adapter_for_source,
)
from .sources.common import (
    ListingOutsideSearch,
    ParserDriftError,
    SourceAdapter,
    collect_photo_urls,
    guard_parser_drift,
)
from .storage import (
    create_run,
    detect_listing_duplicate,
    finish_run,
    latest_commute_check,
    latest_fitness_check,
    latest_fitness_check_at_point,
    latest_office_point,
    latest_park_check,
    latest_park_check_at_point,
    persist_listing,
    reconcile_listing_states,
    record_commute_check,
    record_fitness_check,
    record_park_check,
    upsert_full_text,
    upsert_photo_ingestion,
    vision_manual_review_count,
    visual_score_input_hash,
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
from .vision_workflow import run_listing_vision
from .yandex_routes import (
    YandexMapsRouteError,
    YandexMapsRouter,
    calculate_commute,
    calculate_fitness,
    calculate_park,
)


class HTTPStatusError(RuntimeError):
    """A navigation returned an HTTP error response."""

    def __init__(self, status: int, url: str):
        self.status = int(status)
        super().__init__(f"HTTP {self.status} for {url}")


def _should_run_vision(
    enabled: bool,
    refresh_existing: bool,
    is_new: bool,
    latest_status: str | None,
) -> bool:
    return bool(
        enabled and (refresh_existing or is_new or latest_status in {None, "failed"})
    )


def _above_search_price(search_url: str, fields: Mapping[str, Any]) -> bool:
    prices = [
        value
        for key, value in parse_qsl(urlsplit(str(search_url)).query)
        if key in {"priceMax", "maxprice"}
    ]
    field = fields.get("price_monthly") or fields.get("price")
    value = getattr(field, "value", field)
    try:
        return bool(prices and float(value) > float(prices[-1]))
    except (TypeError, ValueError, OverflowError):
        return False


def _resolve_listing_point(
    address: str, api_key: str, source_point: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Prefer an exact 2GIS building, but retain the source point when the offer omits its number."""

    try:
        return geocode_address(address, api_key, hint_point=source_point)
    except ValueError:
        fallback = saved_point(source_point, "home")
        if fallback is None:
            raise
        return {
            **fallback,
            "point_kind": fallback.get("point_kind") or "source_offer",
            "provider": fallback.get("provider") or "source_offer",
        }


def _normalize_source_url(url: str) -> str:
    """Canonicalize only URL syntax used by the queue identity."""

    parts = urlsplit(str(url).strip())
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, "")
    )


def _request_for_offer(
    source_listing_id: str,
    source_url: str,
    retries: int,
    *,
    source: str | None = None,
    search_url: str | None = None,
    always_enqueue: bool = False,
) -> Request:
    """Build a native Crawlee request keyed by source id plus normalized URL."""

    source_listing_id = str(source_listing_id)
    source_url = str(source_url)
    adapter = adapter_for_listing_url(source_url)
    if source is not None and str(source) != adapter.source:
        raise ValueError("detail request source does not match its URL")
    request_search_url = str(search_url or "").strip()
    if request_search_url and (
        adapter_for_search_url(request_search_url).source != adapter.source
    ):
        raise ValueError("detail request search URL does not match its source")
    request_args: dict[str, Any] = {
        "label": "detail",
        "user_data": {
            "source": adapter.source,
            "source_listing_id": source_listing_id,
            "search_url": request_search_url,
        },
        "max_retries": max(0, int(retries)),
        "always_enqueue": always_enqueue,
    }
    # Crawlee rejects a caller-supplied unique_key together with
    # always_enqueue. Detail requests use the durable custom identity; the
    # helper remains safe if a future caller asks for an always-enqueued URL.
    if not always_enqueue:
        request_args["unique_key"] = (
            f"flatfinder-offer:{source_listing_id}:{_normalize_source_url(source_url)}"
        )
    return Request.from_url(source_url, **request_args)


def _crawlee_storage_dir(config: Config) -> Path:
    database = config.database
    if database and str(database) != ":memory:":
        database_path = Path(str(database)).expanduser().resolve()
    else:
        database_path = (
            Path(__file__).resolve().parents[2] / "data" / "listings.sqlite3"
        ).resolve()
    namespace = hashlib.sha256(str(database_path).encode("utf-8")).hexdigest()[:12]
    return database_path.parent / ".flatfinder-crawlee" / namespace


def _crawlee_configuration(config: Config) -> CrawleeConfiguration:
    desired = CrawleeConfiguration(
        storage_dir=str(_crawlee_storage_dir(config)),
        purge_on_start=False,
    )
    try:
        service_locator.set_configuration(desired)
        return desired
    except ServiceConflictError:
        current = service_locator.get_configuration()
        if Path(str(current.storage_dir)).expanduser().resolve() != Path(
            str(desired.storage_dir)
        ).expanduser().resolve() or bool(current.purge_on_start):
            raise RuntimeError(
                "Crawlee is already configured for a different storage directory"
            ) from None
        return current


def _build_crawler(
    config: Config,
    request_manager: RequestQueue,
    request_handler: Any,
    storage_client: FileSystemStorageClient,
    crawlee_configuration: CrawleeConfiguration,
    event_manager: EventManager,
) -> PlaywrightCrawler:
    """Construct the native persistent Crawlee runtime."""

    profile_dir = prepare_profile_dir(config)
    retries = max(0, int(config.network_retries))
    browser_pool = BrowserPool.with_default_plugin(
        browser_type="chromium",
        user_data_dir=profile_dir,
        headless=not bool(config.headed),
        fingerprint_generator=None,
        use_incognito_pages=False,
        browser_inactive_threshold=timedelta(hours=24),
        retire_browser_after_page_count=10_000,
    )
    return PlaywrightCrawler(
        configuration=crawlee_configuration,
        event_manager=event_manager,
        storage_client=storage_client,
        request_manager=request_manager,
        request_handler=request_handler,
        browser_pool=browser_pool,
        use_session_pool=False,
        retry_on_blocked=False,
        max_session_rotations=0,
        concurrency_settings=ConcurrencySettings(
            min_concurrency=1,
            max_concurrency=1,
            desired_concurrency=1,
        ),
        max_request_retries=retries,
        request_handler_timeout=timedelta(minutes=15),
        goto_options={"wait_until": "domcontentloaded"},
        configure_logging=False,
    )


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _goto(page: Any, url: str) -> None:
    goto = getattr(page, "goto", None)
    if not callable(goto):
        raise TypeError("page does not support navigation")
    try:
        response = await _await(goto(url, wait_until="domcontentloaded"))
    except TypeError:
        response = await _await(goto(url))
    status = getattr(response, "status", None)
    if callable(status):
        status = await _await(status())
    try:
        status = int(status)
    except (TypeError, ValueError, OverflowError):
        status = None
    if status is not None and 400 <= status < 600:
        raise HTTPStatusError(status, url)


class BlockedRun(RuntimeError):
    """A visible login/CAPTCHA/2FA gate stopped the current run."""

    def __init__(self, reason: str):
        self.reason = str(reason)
        super().__init__(self.reason)


@dataclass(slots=True)
class DiscoveryResult:
    """Marketplace discovery counters and normalized listing links."""

    cards_found: int = 0
    cards_new: int = 0
    cards_changed: int = 0
    links: list[tuple[str, str]] = field(default_factory=list)
    complete: bool = False


@dataclass(slots=True)
class QueueResult:
    """Detail-processing counters and non-fatal enrichment diagnostics."""

    status: str = "success"
    blocked_reason: str | None = None
    written_assessments: int = 0
    cards_failed: int = 0
    cards_changed: int = 0
    retries: int = 0
    field_coverages: list[float] = field(default_factory=list)
    photos_processed: int = 0
    enriched_count: int = 0
    enrichment_failed: int = 0
    enrichment_errors: list[str] = field(default_factory=list)
    top_n_checks: int = 0
    vision_attempts: int = 0
    vision_failed: int = 0
    visual_coverage: float = 0.0
    manual_review_count: int = 0

    def record_listing(self, outcome: ListingOutcome, retries: int) -> None:
        self.written_assessments += 1
        self.field_coverages.append(outcome.coverage)
        self.cards_changed += outcome.cards_changed
        self.photos_processed += outcome.photos_processed
        self.enriched_count += outcome.enriched_count
        self.enrichment_errors.extend(outcome.enrichment_errors)
        self.vision_attempts += outcome.vision_attempts
        self.vision_failed += outcome.vision_failed
        if outcome.visual_coverage is not None:
            self.visual_coverage = outcome.visual_coverage
        self.retries += retries


@dataclass(slots=True)
class RunResult:
    """Final persisted outcome returned by one configured source run."""

    run_id: int
    status: str
    blocked_reason: str | None = None
    cards_found: int = 0
    cards_new: int = 0
    cards_changed: int = 0
    cards_failed: int = 0
    retries: int = 0
    written_assessments: int = 0
    field_coverage: float | None = None
    field_coverage_p50: float | None = None
    photos_processed: int = 0
    top_n_checks: int = 0
    enriched_count: int = 0
    enrichment_failed: int = 0
    enrichment_errors: list[str] = field(default_factory=list)
    vision_attempts: int = 0
    vision_failed: int = 0
    visual_coverage: float = 0.0
    manual_review_count: int = 0


def _recent_coverages(conn: Any, parser_version: str) -> list[float]:
    try:
        rows = queries.recent_coverage_rows(conn, parser_version)
    except sqlite3.Error:
        return []
    values: list[float] = []
    for row in rows:
        value = row[0]
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number) and 0 <= number <= 100:
            values.append(number)
    return list(reversed(values))


async def discover(
    config: Config,
    conn: Any,
    page: Any,
    *,
    search_url: str | None = None,
) -> DiscoveryResult:
    """Navigate one validated search URL and return domain discovery facts."""

    reason = await _await(detect_blocker(page))
    if reason:
        raise BlockedRun(reason)
    search_url = search_url or config.search_url
    if not isinstance(search_url, str) or not search_url.strip():
        raise ValueError("search_url is required")
    max_cards = max(0, int(config.max_cards_per_run))
    if max_cards == 0:
        return DiscoveryResult()
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    adapter = adapter_for_search_url(search_url)
    source = adapter.source
    page_number = 1
    complete = False
    while len(links) < max_cards:
        page_url = adapter.search_page_url(search_url, page_number)
        if page_number > 1 or not await adapter.search_page_loaded(page):
            await _goto(page, page_url)
            await adapter.search_page_loaded(page)
        reason = await _await(detect_blocker(page))
        if reason:
            raise BlockedRun(reason)
        search_page = await adapter.extract_search_page(page)
        raw_links = search_page.links
        added = 0
        for item in raw_links:
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            source_id, url = str(item[0]), str(item[1])
            if source_id and url and source_id not in seen:
                seen.add(source_id)
                links.append((source_id, url))
                added += 1
                if len(links) >= max_cards:
                    break
        if (
            search_page.total_pages is not None
            and page_number >= search_page.total_pages
            and len(links) < max_cards
        ):
            complete = True
            break
        if added == 0:
            complete = bool(links)
            break
        page_number += 1

    cards_new = 0
    for source_id, url in links:
        existing_listing = queries.listing_id_by_source(conn, source, source_id)
        if existing_listing is None:
            cards_new += 1
    return DiscoveryResult(len(links), cards_new, 0, links, complete)


def _is_http_4xx(error: BaseException) -> bool:
    status = getattr(error, "status", getattr(error, "status_code", None))
    if status is None and getattr(error, "response", None) is not None:
        status = getattr(error.response, "status", None)
    try:
        if 400 <= int(status) < 500:
            return True
    except (TypeError, ValueError, OverflowError):
        pass
    return bool(
        re.search(r"\b4(?:0\d|1\d|2\d|3\d|4\d|5\d|6\d|7\d|8\d|9\d)\b", str(error))
    )


def _http_status(error: BaseException) -> int | None:
    status = getattr(error, "status", getattr(error, "status_code", None))
    if status is None and getattr(error, "response", None) is not None:
        status = getattr(error.response, "status", None)
    try:
        return int(status)
    except (TypeError, ValueError, OverflowError):
        return None


def _is_http_5xx(error: BaseException) -> bool:
    status = _http_status(error)
    return bool(status is not None and 500 <= status < 600) or bool(
        re.search(r"\b5\d{2}\b", str(error))
    )


def _is_retryable_error(error: BaseException) -> bool:
    """Allow Crawlee retries only for parser/network/transient server failures."""

    if isinstance(error, ParserDriftError):
        return True
    status = _http_status(error)
    if status == 429 or _is_http_5xx(error):
        return True
    if isinstance(
        error, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)
    ):
        return True
    return bool(
        re.search(
            r"network|timeout|timed out|temporar|connection|\b429\b",
            str(error),
            re.IGNORECASE,
        )
    )


def _exception_blocker(error: BaseException) -> str | None:
    return classify_blocker(text=str(error))


def normalize_blocker(reason: Any) -> str | None:
    """Reduce a visible/error reason to the notification-safe blocker set."""

    value = str(reason or "")
    blocker = classify_blocker(text=value)
    if blocker:
        return blocker
    return (
        "parser_drift"
        if re.search(r"parser[\s_-]?drift", value, re.IGNORECASE)
        else None
    )


def _listing_source(url: str) -> str:
    return adapter_for_search_url(url).source


def _parser_version(config: Config) -> str:
    return adapter_for_search_url(str(config.search_url)).parser_version


def _previous_listing(conn: Any, facts: Any) -> tuple[Any, float, dict[str, Any]]:
    source_id = str(getattr(facts, "source_listing_id", ""))
    source = adapter_for_source(str(getattr(facts, "source", ""))).source
    row = queries.previous_listing_assessment(conn, source, source_id)
    if row is None:
        return None, 0.0, {}
    try:
        personal = float(row[2] or 0)
    except (TypeError, ValueError, OverflowError):
        personal = 0.0
    try:
        assessment = json.loads(row[3] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        assessment = {}
    return row, personal, assessment if isinstance(assessment, dict) else {}


def _coverage_p50(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def _summary_payload(
    *,
    cards_found: int,
    cards_new: int,
    cards_changed: int,
    cards_failed: int,
    retries: int,
    blocker: Any,
    field_coverages: list[float],
    photos_processed: int,
    top_n_checks: int,
    vision_attempts: int = 0,
    vision_failed: int = 0,
    visual_coverage: float = 0.0,
    manual_review_count: int = 0,
    parser_version: str | None = None,
) -> dict[str, Any]:
    if not str(parser_version or "").strip():
        raise ValueError("parser_version is required for run summary")
    return {
        "parser_version": str(parser_version),
        "cards_found": int(cards_found),
        "cards_new": int(cards_new),
        "cards_changed": int(cards_changed),
        "cards_failed": int(cards_failed),
        "retries": int(retries),
        "blocker": normalize_blocker(blocker),
        "field_coverage_p50": _coverage_p50(field_coverages),
        "photos_processed": int(photos_processed),
        "top_n_checks": int(top_n_checks),
        "vision_attempts": int(vision_attempts),
        "vision_failed": int(vision_failed),
        "visual_coverage": float(visual_coverage),
        "manual_review_count": int(manual_review_count),
    }


def _enrichment_candidates(conn: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Read base assessments and their latest normalized facts for top-N checks."""
    rows = queries.enrichment_candidate_rows(conn)
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in rows:
        try:
            raw_facts = json.loads(
                row["facts_json"] if hasattr(row, "keys") else row[7]
            )
            source_id = str(
                row["source_listing_id"] if hasattr(row, "keys") else row[1]
            )
            source_url = str(row["source_url"] if hasattr(row, "keys") else row[2])
            source = str(row["source"] if hasattr(row, "keys") else row[8])
            facts = normalize_facts(raw_facts, source_id, source_url, source=source)
        except (TypeError, ValueError, IndexError, json.JSONDecodeError) as error:
            listing_id = row["listing_id"] if hasattr(row, "keys") else row[0]
            errors.append(f"listing {listing_id}: malformed facts ({error})")
            continue
        if not isinstance(facts, dict) or not facts.get("fields"):
            listing_id = row["listing_id"] if hasattr(row, "keys") else row[0]
            errors.append(f"listing {listing_id}: malformed facts (missing fields)")
            continue
        fields = facts.get("fields", {})
        address = fields.get("address") if isinstance(fields, dict) else None
        if isinstance(address, dict):
            address = address.get("value")
        listing_id = int(row["listing_id"] if hasattr(row, "keys") else row[0])
        try:
            published = facts.get("published_at")
            if not published and isinstance(fields, dict):
                published_field = fields.get("published_at")
                published = (
                    published_field.get("value")
                    if isinstance(published_field, dict)
                    else published_field
                )
            candidate = {
                "listing_id": listing_id,
                "source_listing_id": str(
                    row["source_listing_id"] if hasattr(row, "keys") else row[1]
                ),
                "source_url": str(
                    row["source_url"] if hasattr(row, "keys") else row[2]
                ),
                "published_at": str(published or ""),
                "auto_score": float(
                    row["auto_score"] if hasattr(row, "keys") else row[4]
                ),
                "completeness": float(
                    row["completeness"] if hasattr(row, "keys") else row[5]
                ),
                "priority": str(
                    row["status"] if hasattr(row, "keys") else row[6]
                ).lower(),
                "address": address or "",
                "facts": facts,
            }
        except (TypeError, ValueError, OverflowError) as error:
            errors.append(f"listing {listing_id}: malformed assessment ({error})")
            continue
        candidates.append(candidate)
    return candidates, errors


async def _enrich_top_candidates(
    config: Config, conn: Any, page: Any
) -> tuple[int, int, list[str], int]:
    try:
        limit = max(0, int(config.top_n))
    except (TypeError, ValueError):
        limit = 10
    vision_scoring_enabled = bool(config.vision_scoring_enabled)
    candidates, errors = _enrichment_candidates(conn)
    selected = select_top_candidates(candidates, limit=limit)
    enriched = 0
    failed = len(errors)
    for candidate in selected:
        try:
            environment = await _await(enrich_environment(page, candidate))
            facts = apply_enrichment(candidate["facts"], environment=environment)
            persist_enrichment(
                conn,
                candidate["listing_id"],
                facts,
                vision_scoring_enabled=vision_scoring_enabled,
                max_scores=config.scoring_max_scores,
                parameters=config.scoring_parameters,
                thresholds=config.scoring_thresholds,
                hard_constraints=config.hard_constraints,
                vision_contract=config.vision_contract,
            )
            enriched += 1
        # Network adapters and enrichment providers expose third-party errors;
        # one bad candidate must be reported without aborting the remaining set.
        except Exception as error:  # noqa: BLE001
            failed += 1
            errors.append(
                f"listing {candidate['listing_id']}: enrichment failed ({error})"
            )
    return enriched, failed, errors, len(selected)


@dataclass(slots=True)
class _PreparedListing:
    source: str
    parser_version: str
    facts: Any
    previous: Any
    personal_score: float
    previous_assessment: dict[str, Any]
    address: str
    source_point: dict[str, Any] | None


@dataclass(slots=True)
class _ListingEnrichment:
    new_commute_payload: dict[str, Any] | None
    new_park_payload: dict[str, Any] | None
    new_fitness_payload: dict[str, Any] | None


@dataclass(slots=True)
class _ListingRouteState:
    facts: Any
    previous: Any
    address: str
    source_point: dict[str, Any] | None
    listing_point: dict[str, Any] | None = None
    commute_payload: dict[str, Any] | None = None
    park_payload: dict[str, Any] | None = None
    fitness_payload: dict[str, Any] | None = None
    new_commute_payload: dict[str, Any] | None = None
    new_park_payload: dict[str, Any] | None = None
    new_fitness_payload: dict[str, Any] | None = None
    router: YandexMapsRouter | None = None


@dataclass(slots=True)
class _ScoredListing:
    scores: dict[str, float]
    assessment: dict[str, Any]
    auto_score: float
    total: float
    coverage: float
    status: str


async def _navigate_and_extract_listing(
    adapter: SourceAdapter, search_url: str, page: Any, recent: list[float]
) -> tuple[str, str, Any]:
    """Navigate the offer page and extract facts before search filters."""

    reason = await _await(detect_blocker(page))
    if reason:
        raise BlockedRun(reason)
    await adapter.prepare_detail(page, search_url)
    reason = await _await(detect_blocker(page))
    if reason:
        raise BlockedRun(reason)
    facts = await adapter.extract_listing(page, recent)
    reason = await _await(detect_blocker(page))
    if reason:
        raise BlockedRun(reason)
    return adapter.source, adapter.parser_version, facts


def _filter_listing(
    config: Config,
    conn: Any,
    search_url: str,
    source: str,
    parser_version: str,
    facts: Any,
) -> _PreparedListing:
    """Apply configured search filters and retain prior scoring context."""

    previous, personal_score, previous_assessment = _previous_listing(conn, facts)
    fact_fields = getattr(facts, "fields", {})
    if _above_search_price(search_url, fact_fields):
        raise ListingOutsideSearch("offer price is above configured priceMax")
    address_field = fact_fields.get("address") or fact_fields.get("location")
    raw_address = getattr(address_field, "value", address_field)
    address = str(raw_address or "").strip()
    source_point_field = fact_fields.get("location_point")
    source_point = saved_point(
        getattr(source_point_field, "value", source_point_field), "home"
    )
    return _PreparedListing(
        source=source,
        parser_version=parser_version,
        facts=facts,
        previous=previous,
        personal_score=personal_score,
        previous_assessment=previous_assessment,
        address=address,
        source_point=source_point,
    )


async def _prepare_listing(
    config: Config,
    conn: Any,
    adapter: SourceAdapter,
    search_url: str,
    page: Any,
    recent: list[float],
) -> _PreparedListing:
    """Navigate, parse, and reject offers outside the configured search."""

    source, parser_version, facts = await _navigate_and_extract_listing(
        adapter, search_url, page, recent
    )
    return _filter_listing(config, conn, search_url, source, parser_version, facts)


async def _enrich_commute(
    config: Config,
    conn: Any,
    page: Any,
    state: _ListingRouteState,
) -> None:
    """Reuse or calculate commute while retaining the resolved home point."""

    if state.previous is not None and state.address:
        state.commute_payload = latest_commute_check(
            conn, int(state.previous[0]), address_sha256=address_hash(state.address)
        )
        state.listing_point = saved_point(state.commute_payload, "home")
        if (
            state.listing_point is None
            or state.listing_point.get("precision") != "exact"
            or not state.listing_point.get("building_id")
        ):
            state.commute_payload = state.listing_point = None
    if state.commute_payload is not None:
        apply_location_point(
            state.facts,
            {
                **state.listing_point,
                "address": state.address,
                "captured_at": state.commute_payload.get("captured_at"),
            },
        )
        apply_commute(state.facts, state.commute_payload)
        return
    state.router = state.router or await YandexMapsRouter.from_listing_page(
        page, config
    )
    destination = str(config.destination or "")
    state.listing_point = await asyncio.to_thread(
        _resolve_listing_point,
        state.address,
        str(config.twogis_api_key or ""),
        state.source_point,
    )
    commute = await calculate_commute(
        state.router,
        state.address,
        destination,
        str(config.twogis_api_key or ""),
        home_point=state.listing_point,
        office_point=latest_office_point(conn, address_hash(destination)),
    )
    commute_point = saved_point(commute.to_payload(), "home")
    state.listing_point = commute_point or state.listing_point
    if state.listing_point.get("building_id"):
        apply_location_point(
            state.facts,
            {
                **state.listing_point,
                "address": state.address,
                "captured_at": commute.captured_at,
            },
        )
    apply_commute(state.facts, commute)
    state.new_commute_payload = commute.to_payload()


def _load_park_and_fitness_cache(conn: Any, state: _ListingRouteState) -> None:
    """Load both environment caches before either one can trigger a route."""

    if state.previous is not None and state.address:
        state.park_payload = latest_park_check(
            conn, int(state.previous[0]), address_sha256=address_hash(state.address)
        )
        state.fitness_payload = latest_fitness_check(
            conn, int(state.previous[0]), address_sha256=address_hash(state.address)
        )
    for name, payload in (
        ("park", state.park_payload),
        ("fitness", state.fitness_payload),
    ):
        point = saved_point(payload, "home")
        if payload is not None and (
            point is None
            or point["lat"] != state.listing_point["lat"]
            or point["lon"] != state.listing_point["lon"]
        ):
            if name == "park":
                state.park_payload = None
            else:
                state.fitness_payload = None


async def _enrich_park(
    config: Config, conn: Any, page: Any, state: _ListingRouteState
) -> None:
    """Reuse or calculate the nearest park for the resolved home point."""

    if state.park_payload is None:
        state.park_payload = latest_park_check_at_point(
            conn, state.listing_point["lat"], state.listing_point["lon"]
        )
        if state.park_payload is not None:
            state.park_payload.pop("id", None)
            state.park_payload.update(
                {
                    "address": state.address,
                    "address_sha256": address_hash(state.address),
                }
            )
            state.new_park_payload = state.park_payload
    if state.park_payload is not None:
        apply_park(state.facts, state.park_payload)
        return
    state.router = state.router or await YandexMapsRouter.from_listing_page(
        page, config
    )
    park = await calculate_park(
        state.router,
        state.address,
        str(config.twogis_api_key or ""),
        home_point=state.listing_point,
    )
    apply_park(state.facts, park)
    state.new_park_payload = park.to_payload()


async def _enrich_fitness(
    config: Config, conn: Any, page: Any, state: _ListingRouteState
) -> None:
    """Reuse or calculate fitness amenities for the resolved home point."""

    if state.fitness_payload is None and state.listing_point is not None:
        state.fitness_payload = latest_fitness_check_at_point(
            conn, state.listing_point["lat"], state.listing_point["lon"]
        )
        if state.fitness_payload is not None:
            state.fitness_payload.pop("id", None)
            state.fitness_payload.update(
                {
                    "address": state.address,
                    "address_sha256": address_hash(state.address),
                }
            )
            state.new_fitness_payload = state.fitness_payload
    if state.fitness_payload is not None:
        apply_fitness(state.facts, state.fitness_payload)
        return
    state.router = state.router or await YandexMapsRouter.from_listing_page(
        page, config
    )
    fitness = await calculate_fitness(
        state.router,
        state.address,
        str(config.twogis_api_key or ""),
        home_point=state.listing_point
        or saved_point(state.new_commute_payload or state.commute_payload, "home"),
    )
    apply_fitness(state.facts, fitness)
    state.new_fitness_payload = fitness.to_payload()


async def _apply_listing_noise(config: Config, state: _ListingRouteState) -> None:
    """Apply optional local noise data after the route browser is closed."""

    if not bool(config.noise_enabled):
        return
    noise_map = str(config.noise_map or "").strip()
    if noise_map:
        noise = await asyncio.to_thread(
            calculate_noise,
            state.address,
            state.listing_point
            or saved_point(state.new_commute_payload or state.commute_payload, "home"),
            noise_map,
        )
        apply_noise(state.facts, noise)


async def _enrich_listing_routes(
    config: Config, conn: Any, page: Any, prepared: _PreparedListing
) -> _ListingEnrichment:
    """Reuse or calculate commute, park, fitness, and noise enrichment."""

    state = _ListingRouteState(
        facts=prepared.facts,
        previous=prepared.previous,
        address=prepared.address,
        source_point=prepared.source_point,
    )
    if not bool(config.geo_enabled):
        state.listing_point = state.source_point
        await _apply_listing_noise(config, state)
        return _ListingEnrichment(None, None, None)
    try:
        await _enrich_commute(config, conn, page, state)
        _load_park_and_fitness_cache(conn, state)
        await _enrich_park(config, conn, page, state)
        await _enrich_fitness(config, conn, page, state)
    except YandexMapsRouteError as error:
        raise BlockedRun(error.reason) from error
    finally:
        if state.router is not None:
            await state.router.close()
    await _apply_listing_noise(config, state)
    return _ListingEnrichment(
        new_commute_payload=state.new_commute_payload,
        new_park_payload=state.new_park_payload,
        new_fitness_payload=state.new_fitness_payload,
    )


def _score_listing(
    config: Config, conn: Any, recent: list[float], prepared: _PreparedListing
) -> _ScoredListing:
    """Evaluate through the same domain operation used by every reassessment."""

    evaluation = evaluate_listing(
        prepared.facts,
        prepared.previous_assessment,
        prepared.personal_score,
        max_scores=config.scoring_max_scores,
        parameters=config.scoring_parameters,
        thresholds=config.scoring_thresholds,
        hard_constraints=config.hard_constraints,
        visual_hash=visual_score_input_hash(
            conn, int(prepared.previous[0]), config.vision_contract
        )
        if prepared.previous is not None
        else None,
        vision_scoring_enabled=bool(config.vision_scoring_enabled),
        vision_contract=config.vision_contract,
    )
    coverage = guard_parser_drift(prepared.facts, recent)
    return _ScoredListing(
        evaluation.scores,
        evaluation.assessment,
        evaluation.auto_score,
        evaluation.total,
        float(coverage),
        evaluation.status,
    )


async def _persist_scored_listing(
    config: Config,
    conn: Any,
    page: Any,
    prepared: _PreparedListing,
    scored: _ScoredListing,
) -> int:
    """Check the page once more, then atomically persist the listing snapshot."""

    reason = await _await(detect_blocker(page))
    if reason:
        raise BlockedRun(reason)
    listing_id = persist_listing(
        conn,
        prepared.facts,
        scored.scores,
        scored.total,
        scored.coverage,
        scored.assessment,
        prepared.parser_version,
        personal_score=prepared.personal_score,
        status=scored.status,
        max_scores=config.scoring_max_scores,
    )

    return listing_id


@dataclass(slots=True)
class _ListingProgress:
    cards_changed: int = 0
    photos_processed: int = 0
    enriched_count: int = 0
    enrichment_errors: list[str] = field(default_factory=list)
    vision_attempts: int = 0
    vision_failed: int = 0
    visual_coverage: float | None = None


def _record_commute_history(
    conn: Any, result: _ListingProgress, listing_id: int, payload: Mapping[str, Any]
) -> None:
    """Record commute history and preserve its queue counters/error text."""

    try:
        record_commute_check(conn, listing_id, payload)
    except (sqlite3.Error, OverflowError, RuntimeError, TypeError, ValueError) as error:
        result.enrichment_errors.append(
            f"listing {listing_id}: commute history failed ({error})"
        )
    else:
        if payload.get("status") == "success":
            result.enriched_count += 1
        else:
            result.enrichment_errors.append(
                f"listing {listing_id}: commute unknown ({payload.get('error') or 'Yandex Maps returned no complete route'})"
            )


def _record_park_history(
    conn: Any, result: _ListingProgress, listing_id: int, payload: Mapping[str, Any]
) -> None:
    """Record park history without making enrichment persistence fatal."""

    try:
        record_park_check(conn, listing_id, payload)
    except (sqlite3.Error, OverflowError, RuntimeError, TypeError, ValueError) as error:
        result.enrichment_errors.append(
            f"listing {listing_id}: park history failed ({error})"
        )


def _record_fitness_history(
    conn: Any, result: _ListingProgress, listing_id: int, payload: Mapping[str, Any]
) -> None:
    """Record fitness history without making enrichment persistence fatal."""

    try:
        record_fitness_check(conn, listing_id, payload)
    except (sqlite3.Error, OverflowError, RuntimeError, TypeError, ValueError) as error:
        result.enrichment_errors.append(
            f"listing {listing_id}: fitness history failed ({error})"
        )


def _record_enrichment_history(
    conn: Any, result: _ListingProgress, listing_id: int, enrichment: _ListingEnrichment
) -> None:
    """Persist newly calculated route/environment checks in their old order."""

    if enrichment.new_commute_payload is not None:
        _record_commute_history(
            conn, result, listing_id, enrichment.new_commute_payload
        )
    if enrichment.new_park_payload is not None:
        _record_park_history(conn, result, listing_id, enrichment.new_park_payload)
    if enrichment.new_fitness_payload is not None:
        _record_fitness_history(
            conn, result, listing_id, enrichment.new_fitness_payload
        )


async def _score_and_persist_listing(
    config: Config,
    conn: Any,
    page: Any,
    recent: list[float],
    result: _ListingProgress,
    prepared: _PreparedListing,
    enrichment: _ListingEnrichment,
) -> tuple[int, float]:
    """Score, persist, and record the listing's calculated enrichment."""

    scored = _score_listing(config, conn, recent, prepared)
    listing_id = await _persist_scored_listing(config, conn, page, prepared, scored)
    _record_enrichment_history(conn, result, listing_id, enrichment)
    current = queries.listing_content_hash(conn, listing_id)
    if (
        prepared.previous is not None
        and current is not None
        and current[0] != prepared.previous[1]
    ):
        result.cards_changed += 1
    return listing_id, scored.coverage


async def _persist_listing_artifacts(
    config: Config,
    conn: Any,
    page: Any,
    prepared: _PreparedListing,
    listing_id: int,
    result: _ListingProgress,
) -> None:
    """Persist full text and photos after the listing snapshot is durable."""

    facts = prepared.facts
    try:
        full_text = await adapter_for_source(prepared.source).extract_full_text(
            page, str(getattr(facts, "source_listing_id", ""))
        )
        if full_text.listing_id != listing_id:
            full_text = replace(full_text, listing_id=listing_id)
        upsert_full_text(conn, full_text)
    # Source adapters combine Playwright and marketplace-specific parsers;
    # full-text failure is non-fatal after the listing snapshot is durable.
    except Exception as error:  # noqa: BLE001
        message = str(error)[:240] or error.__class__.__name__
        result.enrichment_errors.append(
            f"listing {listing_id}: full-text persistence failed ({message})"
        )
    photo_urls = collect_photo_urls(facts)
    photos: list[PhotoInput] = []
    try:
        photos = await ingest_photos(
            page, listing_id, photo_urls, photo_cache_dir(config)
        )
    # Photo ingestion crosses Playwright, HTTP, Pillow, and filesystem APIs;
    # retain failed photo rows instead of losing the durable listing.
    except Exception as error:  # noqa: BLE001
        message = str(error)[:240] or error.__class__.__name__
        fallback_urls = list(dict.fromkeys(str(url) for url in photo_urls if url))
        photos = [
            PhotoInput(
                listing_id=listing_id,
                image_index=index,
                source_url=url,
                raw_source_url=url,
                status="failed",
                error=message,
            )
            for index, url in enumerate(fallback_urls)
        ]
    try:
        upsert_photo_ingestion(conn, photos, listing_id=listing_id, replace=True)
        result.photos_processed += len(photos)
        detect_listing_duplicate(conn, listing_id)
    except (sqlite3.Error, OverflowError, RuntimeError, TypeError, ValueError) as error:
        message = str(error)[:240] or error.__class__.__name__
        result.enrichment_errors.append(
            f"listing {listing_id}: photo persistence or duplicate detection failed ({message})"
        )


async def _run_listing_vision_if_needed(
    config: Config,
    conn: Any,
    result: _ListingProgress,
    listing_id: int,
    previous: Any,
    vision_runtime: Any,
    vision_enabled: bool,
    refresh_existing_vision: bool,
) -> None:
    """Run optional Vision off the event loop and update queue counters."""

    latest_vision = queries.latest_vision_status(conn, listing_id)
    should_run_vision = _should_run_vision(
        vision_enabled,
        refresh_existing_vision,
        previous is None,
        latest_vision[0] if latest_vision is not None else None,
    )
    if not should_run_vision:
        return
    try:
        vision_result = await asyncio.to_thread(
            run_listing_vision,
            conn,
            vision_runtime,
            listing_id,
            auto_validate=bool(config.vision_auto_validate),
            vision_scoring_enabled=bool(config.vision_scoring_enabled),
            max_scores=config.scoring_max_scores,
            parameters=config.scoring_parameters,
            thresholds=config.scoring_thresholds,
            hard_constraints=config.hard_constraints,
        )
        if vision_result.status != "skipped":
            result.vision_attempts += 1
        if vision_result.status == "failed":
            result.vision_failed += 1
        result.visual_coverage = float(vision_result.visual_coverage) * 100.0
    # Optional model providers can fail with provider-specific exception types;
    # preserve the deterministic listing and expose the Vision failure.
    except Exception as error:  # noqa: BLE001
        result.vision_failed += 1
        result.enrichment_errors.append(
            f"listing {listing_id}: vision failed ({error})"
        )


@dataclass(frozen=True, slots=True)
class ListingClaim:
    adapter: SourceAdapter
    search_url: str
    recent_coverages: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ListingOutcome:
    listing_id: int
    coverage: float
    cards_changed: int
    photos_processed: int
    enriched_count: int
    enrichment_errors: tuple[str, ...]
    vision_attempts: int
    vision_failed: int
    visual_coverage: float | None


@dataclass(slots=True)
class ListingProcessor:
    """Process a claimed card; Crawlee owns queue lifecycle and retries."""

    config: Config
    conn: Any
    vision_runtime: Any = None
    vision_enabled: bool = False
    refresh_existing_vision: bool = False

    async def process(self, claim: ListingClaim, page: Any) -> ListingOutcome:
        progress = _ListingProgress()
        recent = list(claim.recent_coverages)
        prepared = await _prepare_listing(
            self.config, self.conn, claim.adapter, claim.search_url, page, recent
        )
        enrichment = await _enrich_listing_routes(
            self.config, self.conn, page, prepared
        )
        listing_id, coverage = await _score_and_persist_listing(
            self.config, self.conn, page, recent, progress, prepared, enrichment
        )
        await _persist_listing_artifacts(
            self.config, self.conn, page, prepared, listing_id, progress
        )
        await _run_listing_vision_if_needed(
            self.config,
            self.conn,
            progress,
            listing_id,
            prepared.previous,
            self.vision_runtime,
            self.vision_enabled,
            self.refresh_existing_vision,
        )
        return ListingOutcome(
            listing_id,
            coverage,
            progress.cards_changed,
            progress.photos_processed,
            progress.enriched_count,
            tuple(progress.enrichment_errors),
            progress.vision_attempts,
            progress.vision_failed,
            progress.visual_coverage,
        )


async def _run_crawlee(
    config: Config,
    conn: Any,
    run_id: int,
    *,
    vision_runtime: Any = None,
    vision_enabled: bool = False,
    refresh_existing_vision: bool = False,
) -> tuple[DiscoveryResult, QueueResult]:
    """Run one discovery/details phase, then a separate native finalize phase."""

    result = QueueResult()
    processor = ListingProcessor(
        config, conn, vision_runtime, vision_enabled, refresh_existing_vision
    )
    discovery_result = DiscoveryResult()
    search_url = config.search_url
    if not isinstance(search_url, str) or not search_url.strip():
        raise ValueError("search_url is required")
    adapter = adapter_for_search_url(search_url)
    recent_by_source = {adapter.source: _recent_coverages(conn, adapter.parser_version)}
    crawlee_configuration = _crawlee_configuration(config)
    event_manager = service_locator.get_event_manager()
    storage_client = FileSystemStorageClient()
    request_manager = await RequestQueue.open(
        name="flatfinder-listings",
        configuration=crawlee_configuration,
        storage_client=storage_client,
    )
    crawler: PlaywrightCrawler | None = None

    router = Router[PlaywrightCrawlingContext]()

    async def stop_blocked(reason: str, request: Request) -> None:
        result.status, result.blocked_reason = "blocked", str(reason)
        request.no_retry = True
        if crawler is not None:
            crawler.stop(reason=f"FlatFinder blocker: {reason}")

    @router.handler("discovery")
    async def discovery_handler(context: PlaywrightCrawlingContext) -> None:
        nonlocal discovery_result
        try:
            request_search_url = str(context.request.url)
            request_adapter = adapter_for_search_url(request_search_url)
            if request_adapter.source != adapter.source:
                context.request.no_retry = True
                return
            declared_source = str(
                (getattr(context.request, "user_data", None) or {}).get("source", "")
            ).strip()
            if declared_source and declared_source != request_adapter.source:
                raise ParserDriftError(
                    "discovery request source metadata does not match its URL"
                )
            reason = await _await(detect_blocker(context.page))
            if reason:
                await stop_blocked(reason, context.request)
                return
            discovery_result = await discover(
                config, conn, context.page, search_url=request_search_url
            )
            if discovery_result.complete:
                reactivated, unpublished = reconcile_listing_states(
                    conn,
                    request_adapter.source,
                    [source_id for source_id, _ in discovery_result.links],
                )
                discovery_result.cards_changed += reactivated + unpublished
            result.retries += int(context.request.retry_count)
            requests = [
                _request_for_offer(
                    source_id,
                    url,
                    int(config.network_retries),
                    source=request_adapter.source,
                    search_url=request_search_url,
                    always_enqueue=True,
                )
                for source_id, url in queries.processable_listing_links(
                    conn,
                    request_adapter.source,
                    discovery_result.links,
                )
            ]
            if requests:
                await context.add_requests(
                    requests, wait_for_all_requests_to_be_added=True
                )
        except BlockedRun as error:
            await stop_blocked(error.reason, context.request)

    @router.handler("detail")
    async def detail_handler(context: PlaywrightCrawlingContext) -> None:
        user_data = getattr(context.request, "user_data", None) or {}
        source_id = str(user_data.get("source_listing_id", ""))
        request_url = str(context.request.url)
        try:
            request_adapter = adapter_for_listing_url(request_url)
            if request_adapter.source != adapter.source:
                context.request.no_retry = True
                return
            declared_source = str(user_data.get("source", "")).strip()
            if declared_source and declared_source != request_adapter.source:
                raise ParserDriftError(
                    "detail request source metadata does not match its URL"
                )
            request_search_url = str(user_data.get("search_url", "")).strip()
            if request_search_url:
                if (
                    adapter_for_search_url(request_search_url).source
                    != request_adapter.source
                ):
                    raise ParserDriftError(
                        "detail request search URL does not match its source"
                    )
            else:
                request_search_url = request_url
            if source_id and not queries.processable_listing_links(
                conn,
                request_adapter.source,
                [(source_id, request_url)],
            ):
                context.request.no_retry = True
                return
            recent = recent_by_source.setdefault(
                request_adapter.source,
                _recent_coverages(conn, request_adapter.parser_version),
            )
            outcome = await processor.process(
                ListingClaim(request_adapter, request_search_url, tuple(recent)),
                context.page,
            )
            result.record_listing(outcome, int(context.request.retry_count))
            recent_by_source[request_adapter.source] = (recent + [outcome.coverage])[
                -5:
            ]

        except ListingOutsideSearch:
            context.request.no_retry = True
            if source_id:
                queries.mark_listing_inactive(conn, request_adapter.source, source_id)
        except BlockedRun as error:
            await stop_blocked(error.reason, context.request)
        except Exception as error:
            if _is_http_4xx(error) and _http_status(error) != 429:
                context.request.no_retry = True
                result.cards_failed += 1
                result.status = "failed"
                return
            raise

    @router.handler("finalize")
    async def finalize_handler(context: PlaywrightCrawlingContext) -> None:
        # A process killed between phases can leave an older finalize request
        # pending in the durable queue. Consume that request without running
        # enrichment; only this run's second phase may finalize its domain data.
        request_run_id = (getattr(context.request, "user_data", None) or {}).get(
            "run_id"
        )
        if request_run_id != run_id:
            return
        if result.status != "success" or result.cards_failed:
            return
        try:
            enriched, failed, errors, checks = await _enrich_top_candidates(
                config, conn, context.page
            )
            result.enriched_count += enriched
            result.enrichment_failed = failed
            result.enrichment_errors.extend(errors)
            result.top_n_checks = checks
            if failed:
                result.status = "failed"
                first_error = errors[0] if errors else "enrichment failure"
                result.blocked_reason = f"enrichment_failed: {failed}; {first_error}"
        except BlockedRun as error:
            await stop_blocked(error.reason, context.request)
        # Final enrichment is a fail-closed boundary around multiple providers;
        # every unexpected provider error must finish the run as failed.
        except Exception as error:  # noqa: BLE001
            result.status = "failed"
            result.enrichment_failed = 1
            result.blocked_reason = f"enrichment_failed: {error}"

    async def error_handler(context: Any, error: Exception) -> None:
        if isinstance(error, BlockedRun):
            request_run_id = (getattr(context.request, "user_data", None) or {}).get(
                "run_id"
            )
            if context.request.label == "finalize" and request_run_id != run_id:
                context.request.no_retry = True
                return
            await stop_blocked(error.reason, context.request)
            return
        if not _is_retryable_error(error):
            context.request.no_retry = True

    async def failed_request_handler(context: Any, error: Exception) -> None:
        result.retries += int(context.request.retry_count)
        if context.request.label == "discovery":
            blocker = (
                error.reason
                if isinstance(error, BlockedRun)
                else _exception_blocker(error)
            )
            result.status = "blocked" if blocker else "failed"
            result.blocked_reason = blocker or str(error)
            if blocker and crawler is not None:
                crawler.stop(
                    reason=f"FlatFinder discovery failed: {result.blocked_reason}"
                )
        elif context.request.label == "detail":
            user_data = getattr(context.request, "user_data", None) or {}
            source_id = str(user_data.get("source_listing_id", "")).strip()
            if _http_status(error) == 404 and source_id:
                try:
                    request_adapter = adapter_for_listing_url(str(context.request.url))
                except ValueError:
                    pass
                else:
                    declared_source = str(user_data.get("source", "")).strip()
                    if declared_source and declared_source != request_adapter.source:
                        result.cards_failed += 1
                        result.status = "failed"
                        return
                    request_search_url = str(user_data.get("search_url", "")).strip()
                    if request_search_url:
                        try:
                            search_adapter = adapter_for_search_url(request_search_url)
                        except ValueError:
                            result.cards_failed += 1
                            result.status = "failed"
                            return
                        if search_adapter.source != request_adapter.source:
                            result.cards_failed += 1
                            result.status = "failed"
                            return
                    result.cards_changed += queries.mark_listing_inactive(
                        conn, request_adapter.source, source_id
                    )
                    return
            result.cards_failed += 1
            result.status = "failed"
            if isinstance(error, BlockedRun):
                await stop_blocked(error.reason, context.request)
        elif context.request.label == "finalize":
            request_run_id = (getattr(context.request, "user_data", None) or {}).get(
                "run_id"
            )
            if request_run_id != run_id:
                return
            result.status = "failed"
            result.blocked_reason = str(error)

    crawler = _build_crawler(
        config,
        request_manager,
        router,
        storage_client,
        crawlee_configuration,
        event_manager,
    )
    if adapter.prepare_page is not None:

        async def prepare_source_page(context: Any) -> None:
            await adapter.prepare_page(context.page)

        crawler.pre_navigation_hook(prepare_source_page)
    crawler.error_handler(error_handler)
    crawler.failed_request_handler(failed_request_handler)
    background_watcher = start_browser_background_watcher(bool(config.headed))
    try:
        await request_manager.add_request(
            Request.from_url(
                search_url,
                label="discovery",
                user_data={"run_id": run_id, "source": adapter.source},
                always_enqueue=True,
                max_retries=max(0, int(config.network_retries)),
            )
        )
        await crawler.run(purge_request_queue=False)
        if result.status == "success" and not await request_manager.is_finished():
            result.status = "failed"
            result.blocked_reason = "interrupted"
        if result.status == "success" and not result.cards_failed:
            await request_manager.add_request(
                Request.from_url(
                    search_url,
                    label="finalize",
                    user_data={"run_id": run_id, "source": adapter.source},
                    always_enqueue=True,
                    max_retries=0,
                )
            )
            await crawler.run(purge_request_queue=False)
            if result.status == "success" and not await request_manager.is_finished():
                result.status = "failed"
                result.blocked_reason = "interrupted"
        if result.status == "success" and result.cards_failed:
            result.status = "failed"
    finally:
        stop_browser_background_watcher(background_watcher)
    result.manual_review_count = vision_manual_review_count(
        conn, vision_contract=config.vision_contract
    )
    return discovery_result, result


async def run_once(
    config: Config, conn: Any, *, refresh_existing_vision: bool = False
) -> RunResult:
    """Run native discovery/details and the follow-up finalize phase."""

    parser_version = _parser_version(config)
    run_id = create_run(conn, parser_version)
    vision_runtime = None
    vision_enabled = bool(config.vision_enabled)
    cards_found = cards_new = cards_changed = 0
    queue = QueueResult()
    status = "success"
    blocked_reason: str | None = None
    try:
        if vision_enabled:
            agent_config = config.vision_agent_config
            try:
                from .vision import VisionRuntime

                vision_runtime = VisionRuntime.load(
                    str(agent_config),
                    provider=str(config.vision_provider),
                    model_name=str(config.vision_model),
                    reasoning_effort=str(config.vision_reasoning_effort),
                    codex_bin=str(config.vision_codex_bin),
                    claude_bin=str(config.vision_claude_bin),
                    timeout_seconds=int(config.vision_timeout_seconds),
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                vision_runtime = None
        discovery, queue = await _run_crawlee(
            config,
            conn,
            run_id,
            vision_runtime=vision_runtime,
            vision_enabled=vision_enabled,
            refresh_existing_vision=refresh_existing_vision,
        )
        cards_found, cards_new, cards_changed = (
            discovery.cards_found,
            discovery.cards_new,
            discovery.cards_changed,
        )
        status = queue.status
        blocked_reason = queue.blocked_reason
        cards_changed += queue.cards_changed
        if status != "blocked" and queue.cards_failed:
            status = "failed"
    except BlockedRun as error:
        blocked_reason, status = error.reason, "blocked"
    except ParserDriftError as error:
        blocked_reason, status = f"parser_drift: {error}", "failed"
    # This is the source-run boundary: unexpected crawler/provider failures
    # must be persisted as failed instead of escaping before finish_run().
    except Exception as error:  # noqa: BLE001
        blocked_reason, status = str(error), "failed"
    finally:
        try:
            finish_run(
                conn,
                run_id,
                status,
                blocked_reason,
                cards_found=cards_found,
                cards_new=cards_new,
                cards_failed=queue.cards_failed,
                field_coverage=(sum(queue.field_coverages) / len(queue.field_coverages))
                if queue.field_coverages
                else None,
                summary=_summary_payload(
                    cards_found=cards_found,
                    cards_new=cards_new,
                    cards_changed=cards_changed,
                    cards_failed=queue.cards_failed,
                    retries=queue.retries,
                    blocker=blocked_reason,
                    field_coverages=queue.field_coverages,
                    photos_processed=queue.photos_processed,
                    top_n_checks=queue.top_n_checks,
                    vision_attempts=queue.vision_attempts,
                    vision_failed=queue.vision_failed,
                    visual_coverage=queue.visual_coverage,
                    manual_review_count=vision_manual_review_count(
                        conn, vision_contract=config.vision_contract
                    ),
                    parser_version=parser_version,
                ),
            )
        finally:
            if vision_runtime is not None:
                with suppress(OSError, RuntimeError, TypeError, ValueError):
                    vision_runtime.close()
    return RunResult(
        run_id=run_id,
        status=status,
        blocked_reason=blocked_reason,
        cards_found=cards_found,
        cards_new=cards_new,
        cards_changed=cards_changed,
        cards_failed=queue.cards_failed,
        retries=queue.retries,
        written_assessments=queue.written_assessments,
        field_coverage=(sum(queue.field_coverages) / len(queue.field_coverages))
        if queue.field_coverages
        else None,
        field_coverage_p50=_coverage_p50(queue.field_coverages),
        photos_processed=queue.photos_processed,
        top_n_checks=queue.top_n_checks,
        enriched_count=queue.enriched_count,
        enrichment_failed=queue.enrichment_failed,
        enrichment_errors=queue.enrichment_errors,
        vision_attempts=queue.vision_attempts,
        vision_failed=queue.vision_failed,
        visual_coverage=queue.visual_coverage,
        manual_review_count=vision_manual_review_count(
            conn, vision_contract=config.vision_contract
        ),
    )


__all__ = [
    "BlockedRun",
    "DiscoveryResult",
    "HTTPStatusError",
    "ListingClaim",
    "ListingOutcome",
    "ListingProcessor",
    "QueueResult",
    "RunResult",
    "discover",
    "normalize_blocker",
    "run_once",
]
