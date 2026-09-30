"""Shared contracts and fail-closed guards for listing source adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page

from ..models import FieldValue, ListingFacts, ValueStatus

REQUIRED_FIELDS = (
    "source_listing_id",
    "source_url",
    "price_monthly",
    "area_m2",
    "rooms",
    "floor",
    "address",
    "location_point",
    "photos",
)


class ParserDriftError(RuntimeError):
    """Coverage is unsafe for a write to storage."""


class ListingUnavailable(RuntimeError):
    """The source explicitly reports that the requested offer is unavailable."""


@dataclass(frozen=True, slots=True)
class SearchPageResult:
    links: tuple[tuple[str, str], ...]
    total_pages: int | None = None
    complete: bool = False
    empty: bool = False


@dataclass(frozen=True, slots=True)
class SourceAdapter:
    source: str
    display_name: str
    parser_version: str
    matches_search_url: Callable[[str], bool]
    matches_listing_url: Callable[[str], bool]
    search_page_url: Callable[[str, int], str]
    canonical_listing_url: Callable[[str], str | None]
    listing_id: Callable[[str], str | None]
    wait_search: Callable[[Page, float], Awaitable[None]]
    wait_detail: Callable[[Page, float], Awaitable[None]]
    extract_search_page: Callable[[Page], Awaitable[SearchPageResult]]
    extract_listing: Callable[[Page], Awaitable[ListingFacts]]
    matches_photo_url: Callable[[str | None], bool]
    normalize_photo_url: Callable[[str | None], str | None]
    prepare_page: (
        Callable[[Page], Awaitable[AbstractAsyncContextManager[Any]]] | None
    ) = None


def compute_coverage(facts: ListingFacts) -> float:
    """Return required-field coverage as a percentage in the range 0..100."""

    def known(value: FieldValue | None) -> bool:
        if (
            not isinstance(value, FieldValue)
            or value.status in {ValueStatus.UNKNOWN, ValueStatus.ABSENT}
            or value.value is None
        ):
            return False
        return (
            bool(value.value)
            if isinstance(value.value, (str, list, tuple, dict, set))
            else True
        )

    fields = facts.fields if isinstance(facts.fields, Mapping) else {}
    covered = bool(facts.source_listing_id.strip()) + bool(facts.source_url.strip())
    return (
        100.0
        * (covered + sum(known(fields.get(name)) for name in REQUIRED_FIELDS[2:]))
        / len(REQUIRED_FIELDS)
    )


def guard_parser_drift(facts: ListingFacts) -> float:
    coverage = compute_coverage(facts)
    if coverage < 90.0:
        raise ParserDriftError(f"required parser coverage {coverage:.1f} is below 90.0")
    return coverage


def collect_photo_urls(facts: ListingFacts) -> list[str]:
    """Return canonical photo URLs from the current ordered facts shape."""

    if not isinstance(facts, ListingFacts):
        raise TypeError("photo collection requires ListingFacts")
    field = facts.fields.get("photos")
    if field is None:
        return []
    if not isinstance(field, FieldValue):
        raise TypeError("photos field must be FieldValue")
    entries = field.value
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise TypeError("photos field must contain an ordered list")
    found: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            url = entry
        elif isinstance(entry, dict) and set(entry) == {"url", "source_url"}:
            url = entry["url"]
            if (
                not isinstance(entry["source_url"], str)
                or not entry["source_url"].strip()
            ):
                raise ValueError("raw photo URL must be non-empty text")
        else:
            raise ValueError("photo entries require canonical url and source_url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("canonical photo URL must be non-empty text")
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found


__all__ = [
    "REQUIRED_FIELDS",
    "ListingUnavailable",
    "ParserDriftError",
    "SearchPageResult",
    "SourceAdapter",
    "collect_photo_urls",
    "compute_coverage",
    "guard_parser_drift",
]
