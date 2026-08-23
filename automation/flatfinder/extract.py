"""Compatibility facade for the Yandex Realty source adapter."""

from .sources.common import (
    ParserDriftError,
    REQUIRED_FIELDS,
    collect_photo_urls,
    compute_coverage,
    guard_parser_drift,
)
from .sources.yandex_realty import (
    PARSER_VERSION,
    extract_full_text,
    extract_listing,
    extract_offer_links,
)

__all__ = [
    "PARSER_VERSION",
    "REQUIRED_FIELDS",
    "ParserDriftError",
    "collect_photo_urls",
    "compute_coverage",
    "extract_full_text",
    "extract_listing",
    "extract_offer_links",
    "guard_parser_drift",
]
