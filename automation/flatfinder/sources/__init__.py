"""Registry of supported listing source adapters."""

from __future__ import annotations

from urllib.parse import urlsplit

from . import cian, yandex_realty
from .common import SourceAdapter

ADAPTERS = (yandex_realty.ADAPTER, cian.ADAPTER)
_BY_SOURCE = {adapter.source: adapter for adapter in ADAPTERS}
if len(_BY_SOURCE) != len(ADAPTERS):
    raise RuntimeError("listing source adapter names must be unique")


def adapter_for_search_url(url: str) -> SourceAdapter:
    value = str(url).strip()
    if urlsplit(value).scheme.lower() != "https":
        raise ValueError("search URL must use HTTPS on a supported listing source")
    matches = [adapter for adapter in ADAPTERS if adapter.matches_search_url(value)]
    if len(matches) != 1:
        raise ValueError("search URL must point to a supported listing source")
    return matches[0]


def adapter_for_source(source: str) -> SourceAdapter:
    try:
        return _BY_SOURCE[str(source).strip()]
    except KeyError as error:
        raise ValueError(f"unsupported listing source: {source!r}") from error


def adapter_for_listing_url(url: str) -> SourceAdapter:
    value = str(url).strip()
    if urlsplit(value).scheme.lower() != "https":
        raise ValueError("listing URL must use HTTPS on a supported listing source")
    matches = [adapter for adapter in ADAPTERS if adapter.matches_listing_url(value)]
    if len(matches) != 1:
        raise ValueError("listing URL must point to a supported listing source")
    return matches[0]


def adapter_for_photo_url(url: str | None) -> SourceAdapter | None:
    matches = [adapter for adapter in ADAPTERS if adapter.matches_photo_url(url)]
    return matches[0] if len(matches) == 1 else None


def display_name(source: str) -> str:
    raw = str(source).strip()
    if not raw or raw.casefold() in {"unknown", "none", "null"}:
        return "Источник"
    try:
        return adapter_for_source(raw).display_name
    except ValueError:
        return raw.replace("_", " ").replace("-", " ").strip().capitalize()


__all__ = [
    "ADAPTERS",
    "SourceAdapter",
    "adapter_for_listing_url",
    "adapter_for_photo_url",
    "adapter_for_search_url",
    "adapter_for_source",
    "display_name",
]
