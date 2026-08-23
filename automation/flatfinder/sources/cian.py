"""Whitelisted CIAN adapter for the shared FlatFinder facts model."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..models import Evidence, FieldValue, FullTextRecord, ListingFacts, ValueStatus
from .common import (
    ParserDriftError,
    SearchPageResult,
    SourceAdapter,
    guard_parser_drift,
)

SOURCE = "cian"
PARSER_VERSION = "cian-rent-extract-v1"
_CIAN_IMAGE_HOSTS = {"images.cdn-cian.ru"}


def matches_search_url(value: str) -> bool:
    host = str(urlsplit(str(value)).hostname or "").lower().rstrip(".")
    return host == "cian.ru" or host.endswith(".cian.ru")


def matches_photo_url(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = str(parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme.lower() in {"http", "https"} and host in _CIAN_IMAGE_HOSTS


def normalize_photo_url(value: str | None) -> str | None:
    if not isinstance(value, str) or not matches_photo_url(value):
        return value
    parsed = urlsplit(value)
    path = re.sub(
        r"(?<=\d)-\d+(?=\.(?:jpe?g|png|webp)$)",
        "-1",
        parsed.path,
        flags=re.IGNORECASE,
    )
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def is_allowed_photo_url(value: str | None) -> bool:
    return matches_photo_url(value)


def canonical_offer_url(value: Any, base_url: str = "") -> str | None:
    """Return the stable public CIAN rental URL without search-session data."""

    if value is None:
        return None
    url = urljoin(base_url, str(value).strip())
    parsed = urlsplit(url)
    host = str(parsed.hostname or "").lower().rstrip(".")
    match = re.search(r"/rent/flat/(\d+)(?:/|$)", parsed.path, re.IGNORECASE)
    if host not in {"cian.ru", "www.cian.ru"} or match is None:
        return None
    return f"https://www.cian.ru/rent/flat/{match.group(1)}/"


def offer_id(value: Any, base_url: str = "") -> str | None:
    url = canonical_offer_url(value, base_url)
    match = re.search(r"/rent/flat/(\d+)/$", url or "")
    return match.group(1) if match else None


def matches_listing_url(value: str) -> bool:
    return canonical_offer_url(value) is not None


def search_page_url(search_url: str, page_number: int) -> str:
    """Build CIAN pagination while preserving every configured filter."""

    page_number = max(1, int(page_number))
    parts = urlsplit(str(search_url).strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "p"
    ]
    if page_number > 1:
        query.append(("p", str(page_number)))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


_LINKS_SCRIPT = r"""() => ({
  url: location.href,
  links: [...document.querySelectorAll('[data-name="CardComponent"] a[href*="/rent/flat/"]')]
    .map(node => node.href)
    .filter(Boolean)
    .slice(0, 500)
})"""


_DETAIL_SCRIPT = r"""() => {
  const result = {url: location.href, canonical: document.querySelector('link[rel="canonical"]')?.href || '', offer: null};
  const script = [...document.scripts].find(node => (node.textContent || '').includes('"key":"defaultState"'));
  if (!script) return result;
  const text = script.textContent || '';
  const start = text.indexOf('.concat('), end = text.lastIndexOf(');');
  if (start < 0 || end <= start) return result;
  try {
    const entries = JSON.parse(text.slice(start + 8, end));
    const offer = entries.find(item => item?.key === 'defaultState')?.value?.offerData?.offer;
    if (!offer || typeof offer !== 'object') return result;
    const terms = offer.bargainTerms || {};
    const geo = offer.geo || {};
    result.offer = {
      id: offer.cianId ?? offer.id,
      title: document.querySelector('[data-name="OfferTitleNew"]')?.textContent || '',
      description: typeof offer.description === 'string' ? offer.description : '',
      address: Array.isArray(geo.address) ? geo.address.map(item => ({type:item?.type, fullName:item?.fullName, name:item?.name})) : [],
      coordinates: geo.coordinates && typeof geo.coordinates === 'object' ? {lat:geo.coordinates.lat, lng:geo.coordinates.lng} : null,
      undergrounds: Array.isArray(geo.undergrounds) ? geo.undergrounds.map(item => ({name:item?.name, travelTime:item?.travelTime, travelType:item?.travelType})) : [],
      price: terms.price,
      clientFee: terms.clientFee,
      deposit: terms.deposit,
      prepayMonths: terms.prepayMonths,
      leaseTermType: terms.leaseTermType,
      utilitiesTerms: terms.utilitiesTerms && typeof terms.utilitiesTerms === 'object' ? {
        includedInPrice: terms.utilitiesTerms.includedInPrice,
        flowMetersNotIncludedInPrice: terms.utilitiesTerms.flowMetersNotIncludedInPrice,
        price: terms.utilitiesTerms.price
      } : null,
      totalArea: offer.totalArea,
      roomsCount: offer.roomsCount,
      floorNumber: offer.floorNumber,
      repairType: offer.repairType,
      isApartments: offer.isApartments,
      hasFridge: offer.hasFridge,
      hasDishwasher: offer.hasDishwasher,
      hasConditioner: offer.hasConditioner,
      hasWasher: offer.hasWasher,
      hasFurniture: offer.hasFurniture,
      hasKitchenFurniture: offer.hasKitchenFurniture,
      building: offer.building && typeof offer.building === 'object' ? {
        floorsCount: offer.building.floorsCount,
        buildYear: offer.building.buildYear,
        materialType: offer.building.materialType,
        passengerLiftsCount: offer.building.passengerLiftsCount,
        cargoLiftsCount: offer.building.cargoLiftsCount
      } : null,
      photos: Array.isArray(offer.photos) ? offer.photos.map(item => ({id:item?.id, fullUrl:item?.fullUrl})).slice(0, 100) : []
    };
  } catch (_) {}
  return result;
}"""


async def _evaluate(page: Any, script: str) -> Any:
    try:
        value = page.evaluate(script)
        return await value if hasattr(value, "__await__") else value
    except Exception:
        return None


async def search_page_loaded(page: Any) -> bool:
    raw_url = getattr(page, "url", "")
    value = await raw_url if hasattr(raw_url, "__await__") else raw_url
    return matches_search_url(str(value or ""))


async def prepare_detail(page: Any, search_url: str) -> None:
    del search_url
    waiter = getattr(page, "wait_for_function", None)
    if callable(waiter):
        value = waiter(
            "() => [...document.scripts].some(node => (node.textContent || '').includes('\\\"key\\\":\\\"defaultState\\\"'))",
            timeout=10_000,
        )
        if hasattr(value, "__await__"):
            await value


async def extract_search_page(page: Any) -> SearchPageResult:
    snapshot = await _evaluate(page, _LINKS_SCRIPT)
    if not isinstance(snapshot, Mapping):
        return SearchPageResult([])
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in (
        snapshot.get("links", ()) if isinstance(snapshot.get("links"), list) else ()
    ):
        url = canonical_offer_url(raw, str(snapshot.get("url", "")))
        identifier = offer_id(url)
        if url and identifier and identifier not in seen:
            seen.add(identifier)
            result.append((identifier, url))
    return SearchPageResult(result)


async def extract_offer_links(page: Any) -> list[tuple[str, str]]:
    return (await extract_search_page(page)).links


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return int(number) if number.is_integer() else number


def _field(
    value: Any, detail: str, captured_at: str, *, claim: bool = False
) -> FieldValue:
    if value is None or value == "" or value == []:
        return FieldValue(None, ValueStatus.UNKNOWN)
    source = "seller_claim" if claim else "page_fact"
    quote = (
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        if isinstance(value, (Mapping, list))
        else str(value)
    )
    return FieldValue(
        value,
        ValueStatus.PARTIAL if claim else ValueStatus.CONFIRMED,
        [Evidence(source, f"locator={detail}; quote={quote[:140]}"[:240], captured_at)],
    )


def _address(items: Any) -> str | None:
    if not isinstance(items, list):
        return None
    wanted = {"location", "street", "house"}
    parts = [
        str(item.get("fullName") or item.get("name") or "").strip()
        for item in items
        if isinstance(item, Mapping) and item.get("type") in wanted
    ]
    return ", ".join(dict.fromkeys(part for part in parts if part)) or None


def _utilities(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("includedInPrice") and value.get("flowMetersNotIncludedInPrice"):
        mode = "meters_only"
    elif value.get("includedInPrice"):
        mode = "included"
    else:
        mode = "full_bill" if _number(value.get("price")) else "unknown"
    return {"mode": mode, "amount": _number(value.get("price"))}


def facts_from_payload(
    payload: Mapping[str, Any], recent_coverages: Sequence[float] = ()
) -> ListingFacts:
    """Convert only the whitelisted CIAN payload into shared listing facts."""

    offer = payload.get("offer")
    if not isinstance(offer, Mapping):
        raise ParserDriftError("CIAN defaultState offer is missing")
    source_url = (
        canonical_offer_url(payload.get("canonical") or payload.get("url")) or ""
    )
    source_id = offer_id(source_url) or str(offer.get("id") or "").strip()
    if (
        not source_id
        or not source_url
        or source_id != str(offer.get("id") or "").strip()
    ):
        raise ParserDriftError("CIAN source identity is missing or inconsistent")
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    coordinates = offer.get("coordinates")
    point = None
    if isinstance(coordinates, Mapping):
        lat, lon = _number(coordinates.get("lat")), _number(coordinates.get("lng"))
        if lat is not None and lon is not None:
            point = {
                "lat": lat,
                "lon": lon,
                "precision": "source_offer",
                "provider": SOURCE,
            }
    undergrounds = offer.get("undergrounds")
    metro = (
        next(
            (
                str(item.get("name") or "").strip()
                for item in undergrounds
                if isinstance(item, Mapping) and item.get("name")
            ),
            None,
        )
        if isinstance(undergrounds, list)
        else None
    )
    building = (
        offer.get("building") if isinstance(offer.get("building"), Mapping) else {}
    )
    price = _number(offer.get("price"))
    fee = _number(offer.get("clientFee"))
    deposit = _number(offer.get("deposit"))
    prepay = _number(offer.get("prepayMonths")) or 1
    move_in = (
        (price * prepay if price is not None else 0)
        + (deposit or 0)
        + (price * fee / 100 if price is not None and fee is not None else 0)
    )
    photos: list[dict[str, str]] = []
    for item in (
        offer.get("photos", ()) if isinstance(offer.get("photos"), list) else ()
    ):
        raw = str(item.get("fullUrl") or "") if isinstance(item, Mapping) else ""
        canonical = normalize_photo_url(raw)
        if raw and canonical and is_allowed_photo_url(canonical):
            photos.append({"url": canonical, "source_url": raw})
    furniture_flags = (offer.get("hasFurniture"), offer.get("hasKitchenFurniture"))
    furnished = (
        True
        if True in furniture_flags
        else False
        if furniture_flags == (False, False)
        else None
    )
    appliances = {
        "furnished": furnished,
        "fridge": offer.get("hasFridge"),
        "dishwasher": offer.get("hasDishwasher"),
        "ac": offer.get("hasConditioner"),
        "washer": offer.get("hasWasher"),
    }
    fields = {
        "title": _field(
            str(offer.get("title") or "").strip(), "CIAN offer title", captured_at
        ),
        "address": _field(
            _address(offer.get("address")), "CIAN offer.geo.address", captured_at
        ),
        "metro_station": _field(metro, "CIAN offer.geo.undergrounds[0]", captured_at),
        "location_point": _field(point, "CIAN offer.geo.coordinates", captured_at),
        "price_monthly": _field(price, "CIAN offer.bargainTerms.price", captured_at),
        "utilities": _field(
            _utilities(offer.get("utilitiesTerms")),
            "CIAN offer.bargainTerms.utilitiesTerms",
            captured_at,
        ),
        "commission": _field(
            {"percent": fee, "amount": None} if fee is not None else None,
            "CIAN offer.bargainTerms.clientFee",
            captured_at,
        ),
        "deposit": _field(
            {"present": bool(deposit), "amount": deposit}
            if deposit is not None
            else None,
            "CIAN offer.bargainTerms.deposit",
            captured_at,
        ),
        "move_in_total": _field(
            move_in if move_in else None, "calculated CIAN move-in total", captured_at
        ),
        "area_m2": _field(
            _number(offer.get("totalArea")), "CIAN offer.totalArea", captured_at
        ),
        "rooms": _field(
            _number(offer.get("roomsCount")), "CIAN offer.roomsCount", captured_at
        ),
        "floor": _field(
            _number(offer.get("floorNumber")), "CIAN offer.floorNumber", captured_at
        ),
        "total_floors": _field(
            _number(building.get("floorsCount")),
            "CIAN offer.building.floorsCount",
            captured_at,
        ),
        "building_year": _field(
            _number(building.get("buildYear")),
            "CIAN offer.building.buildYear",
            captured_at,
        ),
        "repair": _field(
            str(offer.get("repairType") or "").strip() or None,
            "CIAN offer.repairType",
            captured_at,
            claim=True,
        ),
        "furnished": _field(furnished, "CIAN offer furniture flags", captured_at),
        "appliances": _field(appliances, "CIAN offer appliance flags", captured_at),
        "building": _field(dict(building) or None, "CIAN offer.building", captured_at),
        "lease_term": _field(
            "long_term"
            if offer.get("leaseTermType") == "longTerm"
            else str(offer.get("leaseTermType") or "") or None,
            "CIAN offer.bargainTerms.leaseTermType",
            captured_at,
        ),
        "restrictions": _field(
            {"apartments": bool(offer.get("isApartments"))}
            if offer.get("isApartments") is not None
            else None,
            "CIAN offer.isApartments",
            captured_at,
        ),
        "photos_total": _field(len(photos), "CIAN offer.photos", captured_at),
        "photos_observed": _field(len(photos), "CIAN offer.photos", captured_at),
        "photos": _field(photos, "CIAN offer.photos", captured_at),
    }
    facts = ListingFacts(source_id, source_url, fields, SOURCE)
    guard_parser_drift(facts, recent_coverages)
    return facts


async def extract_listing(
    page: Any, recent_coverages: Sequence[float] = ()
) -> ListingFacts:
    payload = await _evaluate(page, _DETAIL_SCRIPT)
    if not isinstance(payload, Mapping):
        raise ParserDriftError("CIAN detail payload is missing")
    return facts_from_payload(payload, recent_coverages)


async def extract_full_text(page: Any, source_listing_id: str) -> FullTextRecord:
    payload = await _evaluate(page, _DETAIL_SCRIPT)
    offer = payload.get("offer") if isinstance(payload, Mapping) else None
    if not isinstance(offer, Mapping) or str(offer.get("id") or "") != str(
        source_listing_id
    ):
        raise ParserDriftError("CIAN description identity is missing or inconsistent")
    text = " ".join(str(offer.get("description") or "").split())[:20000]
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    quotes = [
        {
            "quote": part[:240],
            "locator": f"description.sentence[{index}]",
            "source": "seller_claim",
        }
        for index, part in enumerate(re.split(r"(?<=[.!?])\s+", text))
        if part.strip()
    ][:32]
    return FullTextRecord(
        0, text, quotes, captured_at, hashlib.sha256(text.encode("utf-8")).hexdigest()
    )


ADAPTER = SourceAdapter(
    source=SOURCE,
    display_name="CIAN",
    parser_version=PARSER_VERSION,
    matches_search_url=matches_search_url,
    matches_listing_url=matches_listing_url,
    search_page_url=search_page_url,
    search_page_loaded=search_page_loaded,
    prepare_detail=prepare_detail,
    extract_offer_links=extract_offer_links,
    extract_listing=extract_listing,
    extract_full_text=extract_full_text,
    matches_photo_url=matches_photo_url,
    normalize_photo_url=normalize_photo_url,
    extract_search_page=extract_search_page,
)


__all__ = [
    "ADAPTER",
    "PARSER_VERSION",
    "SOURCE",
    "canonical_offer_url",
    "extract_full_text",
    "extract_listing",
    "extract_offer_links",
    "extract_search_page",
    "facts_from_payload",
    "normalize_photo_url",
    "offer_id",
    "prepare_detail",
    "search_page_url",
]
