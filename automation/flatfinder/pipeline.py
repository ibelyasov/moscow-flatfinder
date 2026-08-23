"""Single-worker browser pipeline for registered listing sources."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
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

from .browser import (
    classify_blocker,
    detect_blocker,
    prepare_profile_dir,
    start_browser_background_watcher,
    stop_browser_background_watcher,
)
from .enrich import (
    apply_enrichment,
    enrich_environment,
    normalize_facts,
    persist_enrichment,
    recompute_assessment,
    select_top_candidates,
)
from .models import FullTextRecord, PhotoInput, ResultStatus, ReviewStatus
from .noise import apply_noise, calculate_noise
from .photos import ingest_photos
from .scoring import (
    criterion_input_hashes,
    evaluate_hard_constraints,
    reuse_unchanged_criteria,
    score_bucket,
    score_listing,
    score_maxima,
    score_total,
    visual_input_hash,
)
from .sources import (
    adapter_for_listing_url,
    adapter_for_search_url,
    adapter_for_source,
)
from .sources.common import (
    ListingOutsideSearch,
    ParserDriftError,
    SearchPageResult,
    SourceAdapter,
    collect_photo_urls,
    guard_parser_drift,
)
from .storage import (
    create_run,
    create_vision_run,
    detect_listing_duplicate,
    finish_run,
    finish_vision_run,
    insert_vision_proposals,
    latest_commute_check,
    latest_fitness_check,
    latest_fitness_check_at_point,
    latest_office_point,
    latest_park_check,
    latest_park_check_at_point,
    mark_vision_content,
    persist_listing,
    reconcile_listing_states,
    record_commute_check,
    record_fitness_check,
    record_park_check,
    review_proposal,
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


def _config(config: Any, name: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        return config.get(name, default)
    return getattr(config, name, default)


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


def _crawlee_storage_dir(config: Any) -> Path:
    database = _config(config, "database")
    if database and str(database) != ":memory:":
        database_path = Path(str(database)).expanduser().resolve()
    else:
        database_path = (
            Path(__file__).resolve().parents[2] / "data" / "listings.sqlite3"
        ).resolve()
    namespace = hashlib.sha256(str(database_path).encode("utf-8")).hexdigest()[:12]
    return database_path.parent / ".flatfinder-crawlee" / namespace


def _crawlee_configuration(config: Any) -> CrawleeConfiguration:
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
    config: Any,
    request_manager: RequestQueue,
    request_handler: Any,
    storage_client: FileSystemStorageClient,
    crawlee_configuration: CrawleeConfiguration,
    event_manager: EventManager,
) -> PlaywrightCrawler:
    """Construct the native persistent Crawlee runtime."""

    profile_dir = prepare_profile_dir(config)
    retries = max(0, int(_config(config, "network_retries", 2)))
    browser_pool = BrowserPool.with_default_plugin(
        browser_type="chromium",
        user_data_dir=profile_dir,
        headless=not bool(_config(config, "headed", False)),
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
        rows = conn.execute(
            "SELECT field_coverage FROM runs WHERE parser_version = ? AND field_coverage IS NOT NULL ORDER BY id DESC LIMIT 5",
            (str(parser_version),),
        ).fetchall()
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
    config: Any,
    conn: Any,
    page: Any,
    *,
    search_url: str | None = None,
) -> DiscoveryResult:
    """Navigate one validated search URL and return domain discovery facts."""

    reason = await _await(detect_blocker(page))
    if reason:
        raise BlockedRun(reason)
    search_url = search_url or _config(config, "search_url")
    if not isinstance(search_url, str) or not search_url.strip():
        raise ValueError("search_url is required")
    max_cards = max(0, int(_config(config, "max_cards_per_run", 100)))
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
        search_page = (
            await adapter.extract_search_page(page)
            if adapter.extract_search_page is not None
            else SearchPageResult(await adapter.extract_offer_links(page))
        )
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
        existing_listing = conn.execute(
            "SELECT id FROM listings WHERE source = ? AND source_listing_id = ? LIMIT 1",
            (source, source_id),
        ).fetchone()
        if existing_listing is None:
            cards_new += 1
    return DiscoveryResult(len(links), cards_new, 0, links, complete)


def _status(value: Any) -> str:
    return str(getattr(value, "value", value))


def _assessment(facts: Any, scores: Mapping[str, float]) -> dict[str, Any]:
    fields = getattr(facts, "fields", {})
    field_map = {
        "noise": ("noise",),
        "park": ("park",),
        "equipment": ("appliances", "equipment"),
        "repair": ("repair",),
        "price": ("price_monthly", "price", "commission", "utilities"),
        "commute": ("route", "route_minutes"),
        "area": ("area_m2",),
        "visual_layout": ("layout",),
        "floor": ("floor", "total_floors"),
        "light_view": ("light_view",),
        "building": ("building_year",),
        "personal": (),
        "fitness": ("fitness",),
    }
    result: dict[str, Any] = {}
    for criterion, score in scores.items():
        evidence: list[str] = []
        states: list[str] = []
        for name in field_map.get(criterion, ()):
            value = fields.get(name) if isinstance(fields, Mapping) else None
            if value is None:
                continue
            states.append(_status(getattr(value, "status", "unknown")))
            for item in getattr(value, "evidence", ()) or ():
                detail = getattr(item, "detail", item)
                if str(detail) not in evidence:
                    evidence.append(str(detail))
        confidence = (
            "confirmed"
            if states and all(state == "confirmed" for state in states)
            else "partial"
            if evidence
            else "unknown"
        )
        if criterion == "equipment" and isinstance(fields, Mapping):
            equipment = fields.get("appliances")
            equipment = getattr(equipment, "value", equipment)
            required = ("furnished", "ac", "dishwasher", "fridge", "washer")
            if isinstance(equipment, Mapping):
                complete = all(
                    isinstance(
                        equipment.get("bed")
                        if name == "furnished" and equipment.get(name) is None
                        else equipment.get(name),
                        bool,
                    )
                    for name in required
                )
                confidence = (
                    "confirmed" if complete else "partial" if evidence else "unknown"
                )
        result[criterion] = {
            "score": float(score),
            "evidence": evidence or ["No confirmed source evidence."],
            "confidence": confidence,
        }
    return result


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


def _photo_cache_dir(config: Any) -> Path:
    project_root = Path(__file__).resolve().parents[2]
    configured = _config(config, "photo_cache_dir")
    if configured:
        path = Path(str(configured)).expanduser()
        return (path if path.is_absolute() else project_root / path).resolve()
    return (project_root / "data" / "photos").resolve()


def _listing_source(url: str) -> str:
    return adapter_for_search_url(url).source


def _parser_version(config: Any) -> str:
    return adapter_for_search_url(str(_config(config, "search_url", ""))).parser_version


def _processable_links(
    conn: Any,
    source: str,
    links: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Keep hidden or inactive listings out of detail work."""

    skipped = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT l.source_listing_id
            FROM listings AS l
            LEFT JOIN assessments AS a ON a.listing_id = l.id
            WHERE l.source = ? AND (a.disliked_at IS NOT NULL OR l.state != 'active')
            """,
            (source,),
        ).fetchall()
    }
    return [(source_id, url) for source_id, url in links if source_id not in skipped]


def _previous_listing(conn: Any, facts: Any) -> tuple[Any, float, dict[str, Any]]:
    source_id = str(getattr(facts, "source_listing_id", ""))
    source = adapter_for_source(str(getattr(facts, "source", ""))).source
    row = conn.execute(
        """
        SELECT l.id, l.content_sha256, a.personal_score, a.assessment_json
        FROM listings AS l
        LEFT JOIN assessments AS a ON a.listing_id = l.id
        WHERE l.source = ? AND l.source_listing_id = ?
        """,
        (source, source_id),
    ).fetchone()
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


def vision_content_hash(
    facts: Mapping[str, Any],
    full_text: FullTextRecord | Mapping[str, Any],
    photos: Sequence[PhotoInput],
) -> str:
    """Hash only photo identities; fact changes do not invalidate Vision."""

    del facts, full_text
    return visual_input_hash(photos)


def _photo_rows(conn: Any, listing_id: int) -> list[PhotoInput]:
    rows = conn.execute(
        """
        SELECT id, image_index, source_url, local_path, sha256, dhash,
               duplicate_of, status, error, raw_source_url
        FROM photo_ingestion
        WHERE listing_id = ?
        ORDER BY image_index
        """,
        (int(listing_id),),
    ).fetchall()
    return [
        PhotoInput(
            listing_id=int(listing_id),
            image_index=int(row["image_index"] if hasattr(row, "keys") else row[1]),
            source_url=str(row["source_url"] if hasattr(row, "keys") else row[2]),
            local_path=(row["local_path"] if hasattr(row, "keys") else row[3]),
            sha256=(row["sha256"] if hasattr(row, "keys") else row[4]),
            dhash=(row["dhash"] if hasattr(row, "keys") else row[5]),
            duplicate_of=(
                int(row["duplicate_of"])
                if hasattr(row, "keys") and row["duplicate_of"] is not None
                else int(row[6])
                if not hasattr(row, "keys") and row[6] is not None
                else None
            ),
            status=str(row["status"] if hasattr(row, "keys") else row[7]),
            error=(row["error"] if hasattr(row, "keys") else row[8]),
            raw_source_url=(row["raw_source_url"] if hasattr(row, "keys") else row[9]),
        )
        for row in rows
    ]


def run_listing_vision(
    conn: Any,
    runtime: Any,
    listing_id: int,
    *,
    force: bool = False,
    auto_validate: bool = False,
    vision_scoring_enabled: bool = False,
    max_scores: Mapping[str, float] | None = None,
    parameters: Mapping[str, float] | None = None,
    thresholds: Mapping[str, float] | None = None,
    hard_constraints: Mapping[str, Any] | None = None,
) -> Any:
    """Evaluate one listing and optionally apply its validated visual assessment."""

    from .vision import DEFAULT_PROMPT_VERSION, MODEL_NAME, VisionRunResult, run_passes

    listing_id = int(listing_id)
    listing = conn.execute(
        "SELECT vision_content_hash, state FROM listings WHERE id = ?", (listing_id,)
    ).fetchone()
    if listing is None:
        raise ValueError(f"listing {listing_id} does not exist")
    state = listing["state"] if hasattr(listing, "keys") else listing[1]
    if state != "active":
        return VisionRunResult(status="skipped", error="listing is not published")
    snapshot = conn.execute(
        """
        SELECT facts_json FROM listing_snapshots
        WHERE listing_id = ? ORDER BY id DESC LIMIT 1
        """,
        (listing_id,),
    ).fetchone()
    if snapshot is None:
        raise ValueError(f"listing {listing_id} has no facts snapshot")
    try:
        facts = json.loads(
            snapshot["facts_json"] if hasattr(snapshot, "keys") else snapshot[0]
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"listing {listing_id} has invalid facts snapshot") from error
    if not isinstance(facts, Mapping):
        # Stored-payload validation intentionally uses the project's ValueError API.
        raise ValueError(  # noqa: TRY004
            f"listing {listing_id} facts snapshot is not an object"
        )
    text_row = conn.execute(
        "SELECT text, quotes_json, content_sha256, captured_at FROM full_text WHERE listing_id = ?",
        (listing_id,),
    ).fetchone()
    if text_row is None:
        full_text = FullTextRecord(
            listing_id=listing_id,
            text="",
            quotes=[],
            captured_at="",
            content_sha256=hashlib.sha256(b"").hexdigest(),
        )
    else:
        try:
            quotes = json.loads(
                text_row["quotes_json"] if hasattr(text_row, "keys") else text_row[1]
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            quotes = []
        full_text = FullTextRecord(
            listing_id=listing_id,
            text=str(text_row["text"] if hasattr(text_row, "keys") else text_row[0]),
            quotes=quotes if isinstance(quotes, list) else [],
            captured_at=str(
                text_row["captured_at"] if hasattr(text_row, "keys") else text_row[3]
            ),
            content_sha256=str(
                text_row["content_sha256"] if hasattr(text_row, "keys") else text_row[2]
            ),
        )
    photos = _photo_rows(conn, listing_id)
    content_hash = vision_content_hash(facts, full_text, photos)
    model_name = str(getattr(runtime, "model_name", MODEL_NAME) or MODEL_NAME)
    model_version = str(getattr(runtime, "model_version", MODEL_NAME) or MODEL_NAME)
    provider = str(getattr(runtime, "provider", "codex") or "codex")
    reasoning_effort = str(getattr(runtime, "reasoning_effort", "medium") or "medium")
    prompt_version = str(
        getattr(runtime, "prompt_version", DEFAULT_PROMPT_VERSION)
        or DEFAULT_PROMPT_VERSION
    )
    latest = conn.execute(
        """
        SELECT content_hash, status, schema_valid, provider, model_name, model_version,
               reasoning_effort, prompt_version, visual_coverage
        FROM vision_runs
        WHERE listing_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (listing_id,),
    ).fetchone()
    prior_hash = (
        listing["vision_content_hash"] if hasattr(listing, "keys") else listing[0]
    )
    latest_values = (
        (
            latest["content_hash"],
            latest["status"],
            latest["schema_valid"],
            latest["provider"],
            latest["model_name"],
            latest["model_version"],
            latest["reasoning_effort"],
            latest["prompt_version"],
            latest["visual_coverage"],
        )
        if latest is not None and hasattr(latest, "keys")
        else tuple(latest)
        if latest is not None
        else ()
    )
    if (
        not force
        and prior_hash == content_hash
        and latest_values[:8]
        == (
            content_hash,
            "success",
            1,
            provider,
            model_name,
            model_version,
            reasoning_effort,
            prompt_version,
        )
    ):
        coverage = float(latest_values[8]) / 100.0
        if vision_scoring_enabled:
            recompute_assessment(
                conn,
                listing_id,
                vision_scoring_enabled=True,
                max_scores=max_scores,
                parameters=parameters,
                thresholds=thresholds,
                hard_constraints=hard_constraints,
                vision_contract=(
                    provider,
                    model_name,
                    reasoning_effort,
                    prompt_version,
                ),
            )
        return VisionRunResult(
            status="skipped", visual_coverage=max(0.0, min(1.0, coverage))
        )

    # Move the current content hash before inference so proposals from an old
    # snapshot stop being scoreable/manual immediately, even when this run fails.
    contract_changed = bool(
        latest_values
        and latest_values[3:8]
        != (provider, model_name, model_version, reasoning_effort, prompt_version)
    )
    if prior_hash != content_hash or contract_changed:
        mark_vision_content(conn, listing_id, content_hash, 0.0)
    run_id = create_vision_run(
        conn,
        listing_id,
        model_name,
        model_version,
        prompt_version,
        provider=provider,
        reasoning_effort=reasoning_effort,
        content_hash=content_hash,
    )
    if runtime is None:
        error = "Luna photo-scoring runtime is unavailable; manual review required"
        finish_vision_run(conn, run_id, "failed", schema_valid=False, error=error)
        return VisionRunResult(
            status="failed", schema_valid=False, error=error, visual_coverage=0.0
        )
    try:
        result = run_passes(
            runtime,
            listing_id,
            photos,
            full_text,
            facts,
            model_version=model_version,
            prompt_version=prompt_version,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        message = str(error)[:1000] or error.__class__.__name__
        finish_vision_run(conn, run_id, "failed", schema_valid=False, error=message)
        return VisionRunResult(
            status="failed", schema_valid=False, error=message, visual_coverage=0.0
        )
    proposals = [
        replace(proposal, vision_run_id=run_id) for proposal in result.proposals
    ]
    coverage = max(0.0, min(1.0, float(result.visual_coverage)))
    status = "success" if result.status == "success" else "failed"
    apply_scores = bool(
        auto_validate
        and vision_scoring_enabled
        and status == "success"
        and result.schema_valid
    )
    try:
        proposal_ids: list[int] = []
        if proposals:
            proposal_ids = insert_vision_proposals(conn, proposals)
        finish_vision_run(
            conn,
            run_id,
            status,
            schema_valid=bool(result.schema_valid),
            retry_count=int(result.retry_count),
            visual_coverage=coverage * 100.0,
            error=result.error,
        )
        if status == "success":
            if apply_scores:
                for proposal, proposal_id in zip(proposals, proposal_ids, strict=True):
                    if proposal.result_status == ResultStatus.CATEGORY:
                        review_proposal(
                            conn,
                            proposal_id,
                            ReviewStatus.VALIDATED,
                            vision_contract=(
                                provider,
                                model_name,
                                reasoning_effort,
                                prompt_version,
                            ),
                        )
                result.proposals = [
                    replace(proposal, review_status=ReviewStatus.VALIDATED)
                    if proposal.result_status == ResultStatus.CATEGORY
                    else proposal
                    for proposal in proposals
                ]
            mark_vision_content(conn, listing_id, content_hash, coverage * 100.0)
            if apply_scores:
                recompute_assessment(
                    conn,
                    listing_id,
                    vision_scoring_enabled=True,
                    max_scores=max_scores,
                    parameters=parameters,
                    thresholds=thresholds,
                    hard_constraints=hard_constraints,
                    vision_contract=(
                        provider,
                        model_name,
                        reasoning_effort,
                        prompt_version,
                    ),
                )
    except (sqlite3.Error, OverflowError, RuntimeError, TypeError, ValueError) as error:
        message = str(error)[:1000] or error.__class__.__name__
        try:
            finish_vision_run(conn, run_id, "failed", schema_valid=False, error=message)
        except (
            sqlite3.Error,
            OverflowError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as persistence_error:
            message = (
                f"{message}; failure status persistence failed ({persistence_error})"
            )
        return VisionRunResult(
            status="failed",
            proposals=[],
            schema_valid=False,
            error=message,
            visual_coverage=coverage,
        )
    return result


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
    rows = conn.execute(
        """
        SELECT l.id AS listing_id, l.source_listing_id, l.source_url, l.first_seen_at,
               a.auto_score, a.completeness, a.status,
               (SELECT s.facts_json FROM listing_snapshots AS s
                WHERE s.listing_id = l.id
                  AND s.content_sha256 = l.content_sha256
                ORDER BY s.id DESC LIMIT 1) AS facts_json,
               l.source AS source
        FROM assessments AS a JOIN listings AS l ON l.id = a.listing_id
        LEFT JOIN listing_duplicates AS d ON d.listing_id = l.id
        LEFT JOIN listings AS canonical ON canonical.id = d.canonical_listing_id
        WHERE l.state = 'active' AND a.disliked_at IS NULL
          AND (d.listing_id IS NULL OR canonical.state != 'active')
        """
    ).fetchall()
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
    config: Any, conn: Any, page: Any
) -> tuple[int, int, list[str], int]:
    try:
        limit = max(0, int(_config(config, "top_n", 10)))
    except (TypeError, ValueError):
        limit = 10
    vision_scoring_enabled = bool(_config(config, "vision_scoring_enabled", False))
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
                max_scores=_config(config, "scoring_max_scores"),
                parameters=_config(config, "scoring_parameters"),
                thresholds=_config(config, "scoring_thresholds"),
                hard_constraints=_config(config, "hard_constraints"),
                vision_contract=_config(config, "vision_contract"),
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
    config: Any,
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
    config: Any,
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
    config: Any,
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
    destination = str(_config(config, "destination", "") or "")
    state.listing_point = await asyncio.to_thread(
        _resolve_listing_point,
        state.address,
        str(_config(config, "twogis_api_key", "") or ""),
        state.source_point,
    )
    commute = await calculate_commute(
        state.router,
        state.address,
        destination,
        str(_config(config, "twogis_api_key", "") or ""),
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
    config: Any, conn: Any, page: Any, state: _ListingRouteState
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
        str(_config(config, "twogis_api_key", "") or ""),
        home_point=state.listing_point,
    )
    apply_park(state.facts, park)
    state.new_park_payload = park.to_payload()


async def _enrich_fitness(
    config: Any, conn: Any, page: Any, state: _ListingRouteState
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
        str(_config(config, "twogis_api_key", "") or ""),
        home_point=state.listing_point
        or saved_point(state.new_commute_payload or state.commute_payload, "home"),
    )
    apply_fitness(state.facts, fitness)
    state.new_fitness_payload = fitness.to_payload()


async def _apply_listing_noise(config: Any, state: _ListingRouteState) -> None:
    """Apply optional local noise data after the route browser is closed."""

    if not bool(_config(config, "noise_enabled", False)):
        return
    noise_map = str(_config(config, "noise_map", "") or "").strip()
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
    config: Any, conn: Any, page: Any, prepared: _PreparedListing
) -> _ListingEnrichment:
    """Reuse or calculate commute, park, fitness, and noise enrichment."""

    state = _ListingRouteState(
        facts=prepared.facts,
        previous=prepared.previous,
        address=prepared.address,
        source_point=prepared.source_point,
    )
    if not bool(_config(config, "geo_enabled", False)):
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
    config: Any, conn: Any, recent: list[float], prepared: _PreparedListing
) -> _ScoredListing:
    """Calculate scores and parser coverage before the persistence gate."""

    facts = prepared.facts
    previous = prepared.previous
    personal_score = prepared.personal_score
    previous_assessment = prepared.previous_assessment
    max_scores = _config(config, "scoring_max_scores")
    parameters = _config(config, "scoring_parameters")
    scores = score_listing(facts, {}, max_scores=max_scores, parameters=parameters)
    assessment = _assessment(facts, scores)
    assessment["eligibility"] = evaluate_hard_constraints(
        facts, _config(config, "hard_constraints"), parameters
    )
    input_hashes = criterion_input_hashes(
        facts,
        visual_hash=visual_score_input_hash(
            conn, int(previous[0]), _config(config, "vision_contract")
        )
        if previous is not None
        else None,
        max_scores=max_scores,
        parameters=parameters,
    )
    scores, assessment = reuse_unchanged_criteria(
        scores,
        assessment,
        previous_assessment,
        input_hashes,
        max_scores=max_scores,
    )
    if previous is not None:
        previous_personal = previous_assessment.get("personal")
        if isinstance(previous_personal, Mapping):
            personal_detail = json.loads(
                json.dumps(previous_personal, ensure_ascii=False)
            )
            personal_detail["score"] = personal_score
            assessment["personal"] = personal_detail
        elif personal_score:
            assessment["personal"]["score"] = personal_score
    auto_score = sum(value for name, value in scores.items() if name != "personal")
    automatic_max, _personal_max, _total_max = score_maxima(max_scores)
    total = score_total(list(scores.values()), automatic_max) + personal_score
    coverage = guard_parser_drift(facts, recent)
    return _ScoredListing(scores, assessment, auto_score, total, float(coverage))


async def _persist_scored_listing(
    config: Any,
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
        status=score_bucket(scored.auto_score, _config(config, "scoring_thresholds")),
        max_scores=_config(config, "scoring_max_scores"),
    )

    return listing_id


def _record_commute_history(
    conn: Any, result: QueueResult, listing_id: int, payload: Mapping[str, Any]
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
    conn: Any, result: QueueResult, listing_id: int, payload: Mapping[str, Any]
) -> None:
    """Record park history without making enrichment persistence fatal."""

    try:
        record_park_check(conn, listing_id, payload)
    except (sqlite3.Error, OverflowError, RuntimeError, TypeError, ValueError) as error:
        result.enrichment_errors.append(
            f"listing {listing_id}: park history failed ({error})"
        )


def _record_fitness_history(
    conn: Any, result: QueueResult, listing_id: int, payload: Mapping[str, Any]
) -> None:
    """Record fitness history without making enrichment persistence fatal."""

    try:
        record_fitness_check(conn, listing_id, payload)
    except (sqlite3.Error, OverflowError, RuntimeError, TypeError, ValueError) as error:
        result.enrichment_errors.append(
            f"listing {listing_id}: fitness history failed ({error})"
        )


def _record_enrichment_history(
    conn: Any, result: QueueResult, listing_id: int, enrichment: _ListingEnrichment
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
    config: Any,
    conn: Any,
    page: Any,
    recent: list[float],
    result: QueueResult,
    prepared: _PreparedListing,
    enrichment: _ListingEnrichment,
) -> tuple[int, float]:
    """Score, persist, and record the listing's calculated enrichment."""

    scored = _score_listing(config, conn, recent, prepared)
    listing_id = await _persist_scored_listing(config, conn, page, prepared, scored)
    _record_enrichment_history(conn, result, listing_id, enrichment)
    current = conn.execute(
        "SELECT content_sha256 FROM listings WHERE id = ?", (listing_id,)
    ).fetchone()
    if (
        prepared.previous is not None
        and current is not None
        and current[0] != prepared.previous[1]
    ):
        result.cards_changed += 1
    return listing_id, scored.coverage


async def _persist_listing_artifacts(
    config: Any,
    conn: Any,
    page: Any,
    prepared: _PreparedListing,
    listing_id: int,
    result: QueueResult,
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
            page, listing_id, photo_urls, _photo_cache_dir(config)
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
    config: Any,
    conn: Any,
    result: QueueResult,
    listing_id: int,
    previous: Any,
    vision_runtime: Any,
    vision_enabled: bool,
    refresh_existing_vision: bool,
) -> None:
    """Run optional Vision off the event loop and update queue counters."""

    latest_vision = conn.execute(
        "SELECT status FROM vision_runs WHERE listing_id = ? ORDER BY id DESC LIMIT 1",
        (listing_id,),
    ).fetchone()
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
            auto_validate=bool(_config(config, "vision_auto_validate", False)),
            vision_scoring_enabled=bool(
                _config(config, "vision_scoring_enabled", False)
            ),
            max_scores=_config(config, "scoring_max_scores"),
            parameters=_config(config, "scoring_parameters"),
            thresholds=_config(config, "scoring_thresholds"),
            hard_constraints=_config(config, "hard_constraints"),
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


async def _process_listing(
    config: Any,
    conn: Any,
    adapter: SourceAdapter,
    search_url: str,
    page: Any,
    recent: list[float],
    result: QueueResult,
    vision_runtime: Any,
    vision_enabled: bool,
    refresh_existing_vision: bool,
) -> float:
    """Run one claimed listing; Crawlee owns retries around this operation."""
    prepared = await _prepare_listing(config, conn, adapter, search_url, page, recent)
    enrichment = await _enrich_listing_routes(config, conn, page, prepared)
    listing_id, coverage = await _score_and_persist_listing(
        config, conn, page, recent, result, prepared, enrichment
    )
    await _persist_listing_artifacts(config, conn, page, prepared, listing_id, result)
    await _run_listing_vision_if_needed(
        config,
        conn,
        result,
        listing_id,
        prepared.previous,
        vision_runtime,
        vision_enabled,
        refresh_existing_vision,
    )
    return coverage


async def _run_crawlee(
    config: Any,
    conn: Any,
    run_id: int,
    *,
    vision_runtime: Any = None,
    vision_enabled: bool = False,
    refresh_existing_vision: bool = False,
) -> tuple[DiscoveryResult, QueueResult]:
    """Run one discovery/details phase, then a separate native finalize phase."""

    result = QueueResult()
    discovery_result = DiscoveryResult()
    search_url = _config(config, "search_url")
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
                    int(_config(config, "network_retries", 2)),
                    source=request_adapter.source,
                    search_url=request_search_url,
                    always_enqueue=True,
                )
                for source_id, url in _processable_links(
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
            if source_id and not _processable_links(
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
            coverage = await _process_listing(
                config,
                conn,
                request_adapter,
                request_search_url,
                context.page,
                recent,
                result,
                vision_runtime,
                vision_enabled,
                refresh_existing_vision,
            )
            result.written_assessments += 1
            result.field_coverages.append(coverage)
            recent_by_source[request_adapter.source] = (recent + [coverage])[-5:]
            result.retries += int(context.request.retry_count)
        except ListingOutsideSearch:
            context.request.no_retry = True
            if source_id:
                conn.execute(
                    "UPDATE listings SET state = 'inactive' WHERE source = ? AND source_listing_id = ?",
                    (request_adapter.source, source_id),
                )
                conn.commit()
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
                    cursor = conn.execute(
                        "UPDATE listings SET state = 'inactive' "
                        "WHERE source = ? AND source_listing_id = ? AND state != 'inactive'",
                        (request_adapter.source, source_id),
                    )
                    conn.commit()
                    result.cards_changed += max(0, int(cursor.rowcount))
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
    background_watcher = start_browser_background_watcher(
        bool(_config(config, "headed", False))
    )
    try:
        await request_manager.add_request(
            Request.from_url(
                search_url,
                label="discovery",
                user_data={"run_id": run_id, "source": adapter.source},
                always_enqueue=True,
                max_retries=max(0, int(_config(config, "network_retries", 2))),
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
        conn, vision_contract=_config(config, "vision_contract")
    )
    return discovery_result, result


async def run_once(
    config: Any, conn: Any, *, refresh_existing_vision: bool = False
) -> RunResult:
    """Run native discovery/details and the follow-up finalize phase."""

    parser_version = _parser_version(config)
    run_id = create_run(conn, parser_version)
    vision_runtime = None
    vision_enabled = bool(_config(config, "vision_enabled", False))
    cards_found = cards_new = cards_changed = 0
    queue = QueueResult()
    status = "success"
    blocked_reason: str | None = None
    try:
        if vision_enabled:
            config_path = Path(
                str(_config(config, "config_path", "automation/config.toml"))
            ).resolve()
            agent_config = _config(
                config,
                "vision_agent_config",
                str(config_path.parent / "flatfinder-vision.toml"),
            )
            try:
                from .vision import VisionRuntime

                vision_runtime = VisionRuntime.load(
                    str(agent_config),
                    provider=str(_config(config, "vision_provider", "codex")),
                    model_name=str(_config(config, "vision_model", "gpt-5.6-luna")),
                    reasoning_effort=str(
                        _config(config, "vision_reasoning_effort", "medium")
                    ),
                    codex_bin=str(_config(config, "vision_codex_bin", "codex")),
                    claude_bin=str(_config(config, "vision_claude_bin", "claude")),
                    timeout_seconds=int(_config(config, "vision_timeout_seconds", 900)),
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
                        conn, vision_contract=_config(config, "vision_contract")
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
            conn, vision_contract=_config(config, "vision_contract")
        ),
    )


__all__ = [
    "BlockedRun",
    "DiscoveryResult",
    "HTTPStatusError",
    "QueueResult",
    "RunResult",
    "discover",
    "normalize_blocker",
    "run_listing_vision",
    "run_once",
    "vision_content_hash",
]
