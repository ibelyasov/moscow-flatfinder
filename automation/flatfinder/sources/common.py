"""Shared contracts and fail-closed guards for listing source adapters."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

from ..models import FieldValue, FullTextRecord, ListingFacts, ValueStatus

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


class ListingOutsideSearch(RuntimeError):
    """The opened offer no longer matches the configured search."""


@dataclass(frozen=True, slots=True)
class SearchPageResult:
    links: list[tuple[str, str]]
    total_pages: int | None = None


@dataclass(frozen=True, slots=True)
class SourceAdapter:
    source: str
    display_name: str
    parser_version: str
    matches_search_url: Callable[[str], bool]
    matches_listing_url: Callable[[str], bool]
    search_page_url: Callable[[str, int], str]
    search_page_loaded: Callable[[Any], Awaitable[bool]]
    prepare_detail: Callable[[Any, str], Awaitable[None]]
    extract_offer_links: Callable[[Any], Awaitable[list[tuple[str, str]]]]
    extract_listing: Callable[[Any, Sequence[float]], Awaitable[ListingFacts]]
    extract_full_text: Callable[[Any, str], Awaitable[FullTextRecord]]
    matches_photo_url: Callable[[str | None], bool]
    normalize_photo_url: Callable[[str | None], str | None]
    prepare_page: Callable[[Any], Awaitable[None]] | None = None
    extract_search_page: Callable[[Any], Awaitable[SearchPageResult]] | None = None


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


def guard_parser_drift(
    facts: ListingFacts, recent_coverages: Sequence[float] = ()
) -> float:
    coverage = compute_coverage(facts)
    if coverage < 90.0:
        raise ParserDriftError(f"required parser coverage {coverage:.1f} is below 90.0")
    history = []
    for value in recent_coverages or ():
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number) and 0.0 <= number <= 100.0:
            history.append(number)
    if len(history) >= 5 and coverage < median(history[-5:]) - 10.0:
        raise ParserDriftError(
            f"parser coverage {coverage:.1f} fell below recent median by more than 10.0 percentage points"
        )
    return coverage


def collect_photo_urls(facts: ListingFacts) -> list[str]:
    """Return supported photo URLs in deterministic source order."""

    from . import adapter_for_photo_url

    fields = getattr(facts, "fields", {})
    raw = fields.get("photos") if isinstance(fields, Mapping) else None
    value = getattr(raw, "value", raw)
    found: list[str] = []
    seen: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key in (
                "source_url",
                "raw_source_url",
                "url",
                "canonical_url",
                "contentUrl",
                "src",
                "href",
                "image",
            ):
                if key in item:
                    visit(item[key])
            return
        if isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child)
            return
        if not isinstance(item, str):
            return
        adapter = adapter_for_photo_url(item)
        canonical = adapter.normalize_photo_url(item) if adapter is not None else None
        if canonical and canonical not in seen:
            seen.add(canonical)
            found.append(item)

    visit(value)
    return found


__all__ = [
    "REQUIRED_FIELDS",
    "ListingOutsideSearch",
    "ParserDriftError",
    "SearchPageResult",
    "SourceAdapter",
    "collect_photo_urls",
    "compute_coverage",
    "guard_parser_drift",
]
