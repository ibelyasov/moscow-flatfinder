"""Sequential discovery and extraction; the application owns persistence."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from urllib.parse import SplitResult, parse_qsl, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, TimeoutError

from .browser import classify_blocker, detect_blocker
from .models import ListingFacts
from .sources import adapter_for_listing_url, adapter_for_search_url
from .sources.common import (
    ListingUnavailable,
    ParserDriftError,
    SearchPageResult,
    SourceAdapter,
)


class CollectionError(RuntimeError):
    """Acquisition failed without a safe complete observation."""


class CollectionBlocked(CollectionError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"blocked:{reason}")


class HTTPStatusError(CollectionError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"source returned HTTP {status}")


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    source: str
    links: tuple[tuple[str, str], ...] = ()
    complete: bool = False
    error: str | None = None


def _settings(timeout_seconds: float, retries: int) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a positive finite number")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("retries must be a non-negative integer")
    return float(timeout_seconds) * 1000


def _retryable(error: Exception) -> bool:
    if isinstance(error, HTTPStatusError):
        return error.status in {408, 425, 429} or 500 <= error.status < 600
    if isinstance(error, TimeoutError):
        return True
    return isinstance(error, PlaywrightError) and bool(
        re.search(
            r"net::|network|connection reset|connection refused",
            str(error),
            re.IGNORECASE,
        )
    )


async def _retry[T](operation: Callable[[], Awaitable[T]], retries: int) -> T:
    for attempt in range(retries + 1):
        try:
            return await operation()
        except Exception as error:
            blocker = classify_blocker(text=str(error))
            if blocker and not isinstance(error, CollectionBlocked):
                raise CollectionBlocked(blocker) from error
            if attempt == retries or not _retryable(error):
                raise
            await asyncio.sleep(min(2**attempt, 5))
    raise AssertionError("unreachable retry state")


async def _navigate(page: Page, url: str, timeout_ms: float) -> int | None:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    reason = await detect_blocker(page)
    if reason:
        raise CollectionBlocked(reason)
    status = response.status if response is not None else None
    if status is not None and status >= 400:
        raise HTTPStatusError(status)
    return status


def _same_search_page(actual_url: str, expected_url: str) -> bool:
    actual, expected = urlsplit(actual_url), urlsplit(expected_url)

    # CIAN's public apex and www form address the same canonical search.
    def identity(parts: SplitResult) -> tuple:
        host = (parts.hostname or "").lower()
        if host == "cian.ru":
            host = "www.cian.ru"
        return (
            parts.scheme.lower(),
            host,
            parts.port,
            parts.path.rstrip("/"),
            sorted(parse_qsl(parts.query, keep_blank_values=True)),
        )

    return identity(actual) == identity(expected)


async def _search_page(
    page: Page, adapter: SourceAdapter, url: str, timeout_ms: float
) -> SearchPageResult:
    await _navigate(page, url, timeout_ms)
    if not _same_search_page(page.url, url):
        raise ParserDriftError("search redirected to a different filter or page")
    try:
        await adapter.wait_search(page, timeout_ms)
    except Exception:
        reason = await detect_blocker(page)
        if reason:
            raise CollectionBlocked(reason)
        raise
    reason = await detect_blocker(page)
    if reason:
        raise CollectionBlocked(reason)
    return await adapter.extract_search_page(page)


async def discover(
    page: Page,
    search_url: str,
    limit: int,
    timeout_seconds: float,
    retries: int,
) -> DiscoveryResult:
    """Read one search in order, keeping partial links when discovery fails."""

    timeout_ms = _settings(timeout_seconds, retries)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    adapter = adapter_for_search_url(search_url)
    if limit == 0:
        return DiscoveryResult(adapter.source)
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    total_pages: int | None = None
    page_number = 1
    try:
        async with AsyncExitStack() as scripts:
            if adapter.prepare_page is not None:
                await scripts.enter_async_context(await adapter.prepare_page(page))
            while len(links) < limit:
                page_url = adapter.search_page_url(search_url, page_number)
                result = await _retry(
                    lambda page_url=page_url: _search_page(
                        page, adapter, page_url, timeout_ms
                    ),
                    retries,
                )
                if result.total_pages is not None:
                    if (
                        isinstance(result.total_pages, bool)
                        or not isinstance(result.total_pages, int)
                        or result.total_pages < page_number
                        or (
                            total_pages is not None
                            and result.total_pages != total_pages
                        )
                    ):
                        raise ParserDriftError(
                            "search pagination is invalid or changed"
                        )
                    total_pages = result.total_pages
                if not result.links:
                    if result.empty and (
                        total_pages is None or (page_number == 1 and total_pages == 1)
                    ):
                        return DiscoveryResult(adapter.source, tuple(links), True)
                    raise ParserDriftError(
                        "empty search page lacks exhaustion evidence"
                    )
                if result.empty:
                    raise ParserDriftError(
                        "search page contradicts its empty-result evidence"
                    )
                if (
                    result.complete
                    and total_pages is not None
                    and page_number < total_pages
                ):
                    raise ParserDriftError(
                        "search exhaustion contradicts its pagination"
                    )
                added = 0
                for identifier, url in result.links:
                    canonical = adapter.canonical_listing_url(url)
                    if not canonical or adapter.listing_id(canonical) != identifier:
                        raise ParserDriftError(
                            "search emitted an invalid listing identity"
                        )
                    if identifier not in seen:
                        seen.add(identifier)
                        links.append((identifier, canonical))
                        added += 1
                        if len(links) >= limit:
                            # A cap cannot establish absence of other matching offers.
                            return DiscoveryResult(adapter.source, tuple(links))
                if added == 0:
                    raise ParserDriftError(
                        "search pagination repeated already seen offers"
                    )
                if result.complete or (
                    total_pages is not None and page_number == total_pages
                ):
                    return DiscoveryResult(adapter.source, tuple(links), True)
                page_number += 1
    except (CollectionError, ParserDriftError, PlaywrightError, ValueError) as error:
        return DiscoveryResult(adapter.source, tuple(links), False, str(error)[:500])
    return DiscoveryResult(adapter.source, tuple(links))


async def extract(
    page: Page,
    source_url: str,
    expected_id: str,
    timeout_seconds: float,
    retries: int,
) -> ListingFacts:
    """Open the requested card and require source, URL and ID to agree."""

    timeout_ms = _settings(timeout_seconds, retries)
    adapter = adapter_for_listing_url(source_url)
    canonical = adapter.canonical_listing_url(source_url)
    if (
        not isinstance(expected_id, str)
        or not canonical
        or adapter.listing_id(canonical) != expected_id
    ):
        raise ValueError("expected_id must match the requested listing URL")

    async def read() -> ListingFacts:
        try:
            await _navigate(page, canonical, timeout_ms)
        except HTTPStatusError as error:
            if (
                error.status in {404, 410}
                and adapter.canonical_listing_url(page.url) == canonical
            ):
                raise ListingUnavailable(
                    f"source returned HTTP {error.status} for the requested offer"
                ) from error
            raise
        if adapter.canonical_listing_url(page.url) != canonical:
            raise ParserDriftError("detail redirected to a different source or offer")
        try:
            await adapter.wait_detail(page, timeout_ms)
        except Exception:
            reason = await detect_blocker(page)
            if reason:
                raise CollectionBlocked(reason)
            raise
        reason = await detect_blocker(page)
        if reason:
            raise CollectionBlocked(reason)
        facts = await adapter.extract_listing(page)
        if (
            facts.source != adapter.source
            or facts.source_listing_id != expected_id
            or adapter.canonical_listing_url(facts.source_url) != canonical
        ):
            raise ParserDriftError(
                "extracted listing identity differs from the request"
            )
        return facts

    try:
        async with AsyncExitStack() as scripts:
            if adapter.prepare_page is not None:
                await scripts.enter_async_context(await adapter.prepare_page(page))
            return await _retry(read, retries)
    except (CollectionError, ListingUnavailable):
        raise
    except Exception as error:
        raise CollectionError(str(error)[:500]) from error


__all__ = [
    "CollectionBlocked",
    "CollectionError",
    "DiscoveryResult",
    "HTTPStatusError",
    "ListingUnavailable",
    "discover",
    "extract",
]
