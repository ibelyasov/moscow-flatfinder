"""Whitelisted CIAN adapter for the shared FlatFinder facts model."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from playwright.async_api import Page

from ..models import Evidence, FieldValue, ListingFacts, ValueStatus
from .common import (
    ListingUnavailable,
    ParserDriftError,
    SearchPageResult,
    SourceAdapter,
    guard_parser_drift,
)

SOURCE = "cian"
PARSER_VERSION = "cian-rent-extract-v2"
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
    return urlunsplit(("https", str(parsed.hostname).lower(), path, "", ""))


def is_allowed_photo_url(value: str | None) -> bool:
    return matches_photo_url(value) and urlsplit(str(value)).scheme.lower() == "https"


def canonical_offer_url(value: Any, base_url: str = "") -> str | None:
    """Return the stable public CIAN rental URL without search-session data."""

    if value is None:
        return None
    url = urljoin(base_url, str(value).strip())
    parsed = urlsplit(url)
    host = str(parsed.hostname or "").lower().rstrip(".")
    match = re.fullmatch(r"/rent/flat/(\d+)/?", parsed.path, re.IGNORECASE)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 80, 443}
        or host not in {"cian.ru", "www.cian.ru"}
        or match is None
    ):
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

    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
    ):
        raise ValueError("page_number must be a positive integer")
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


_SEARCH_STATE_SCRIPT = r"""() => {
  const cards = [...document.querySelectorAll('[data-name="CardComponent"]')];
  const links = cards.flatMap(card => [...card.querySelectorAll('a[href*="/rent/flat/"]')])
    .map(node => node.href).filter(Boolean).slice(0, 500);
  const pagers = [...document.querySelectorAll('[data-name*="Pagination"], [class*="pagination" i]')];
  const current = Number(new URL(location.href).searchParams.get('p') || 1);
  const pages = pagers.flatMap(pager => [...pager.querySelectorAll('a[href]')])
    .map(node => Number(new URL(node.href).searchParams.get('p') || 1))
    .filter(value => Number.isInteger(value) && value > 0);
  const totalPages = pages.length ? Math.max(current, ...pages) : null;
  const nextControls = pagers.flatMap(pager => [...pager.querySelectorAll(
    '[aria-label*="след" i], [title*="след" i], [aria-label*="next" i], [title*="next" i]'
  )]);
  const nextDisabled = nextControls.length > 0 && nextControls.every(node =>
    node.hasAttribute('disabled') || node.getAttribute('aria-disabled') === 'true');
  const text = document.querySelector('main')?.innerText || document.body?.innerText || '';
  const empty = cards.length === 0 && /по вашему запросу (?:ничего|объявлений) не найдено|ничего не найдено|нет объявлений/i.test(text);
  return {url: location.href, links, ready: links.length > 0 || empty, empty,
    complete: nextDisabled && totalPages !== null && current >= totalPages};
}"""


_DETAIL_SCRIPT = r"""() => {
  const result = {url: location.href, canonical: document.querySelector('link[rel="canonical"]')?.href || '', offer: null,
    unavailable: /объявление снято с публикации|объявление удалено|объявление больше неактуально/i.test(document.body?.innerText || '')};
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
      flatType: offer.flatType,
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


async def wait_search(page: Page, timeout_ms: float) -> None:
    await page.wait_for_function(
        f"() => ({_SEARCH_STATE_SCRIPT})().ready", timeout=timeout_ms
    )


async def wait_detail(page: Page, timeout_ms: float) -> None:
    await page.wait_for_function(
        f"() => Boolean(({_DETAIL_SCRIPT})().offer) || "
        "/объявление снято с публикации|объявление удалено|объявление больше неактуально/i.test(document.body?.innerText || '')",
        timeout=timeout_ms,
    )
    payload = await page.evaluate(_DETAIL_SCRIPT)
    if isinstance(payload, Mapping) and isinstance(payload.get("offer"), Mapping):
        return
    if isinstance(payload, Mapping) and payload.get("unavailable") is True:
        raise ListingUnavailable("CIAN explicitly reports an unavailable offer")
    raise ParserDriftError("CIAN ready offer payload disappeared")


async def extract_search_page(page: Page) -> SearchPageResult:
    snapshot = await page.evaluate(_SEARCH_STATE_SCRIPT)
    if not isinstance(snapshot, Mapping) or snapshot.get("ready") is not True:
        raise ParserDriftError(
            "CIAN search result cards or explicit empty result are missing"
        )
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
    empty = snapshot.get("empty") is True
    if not result and not empty:
        raise ParserDriftError("CIAN result cards have no supported rental links")
    return SearchPageResult(
        tuple(result), complete=snapshot.get("complete") is True, empty=empty
    )


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return (
        (int(number) if number.is_integer() else number)
        if math.isfinite(number)
        else None
    )


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


def facts_from_payload(payload: Mapping[str, Any]) -> ListingFacts:
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
    captured_at = datetime.now(UTC).isoformat(timespec="seconds")
    coordinates = offer.get("coordinates")
    point = None
    if isinstance(coordinates, Mapping):
        lat, lon = _number(coordinates.get("lat")), _number(coordinates.get("lng"))
        if (
            lat is not None
            and lon is not None
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        ):
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
    prepay = _number(offer.get("prepayMonths"))
    move_in = (
        price * prepay + deposit + price * fee / 100
        if price is not None
        and prepay is not None
        and deposit is not None
        and fee is not None
        and price > 0
        and prepay > 0
        and deposit >= 0
        and 0 <= fee <= 100
        else None
    )
    photos: list[dict[str, str]] = []
    seen_photos: set[str] = set()
    for item in (
        offer.get("photos", ()) if isinstance(offer.get("photos"), list) else ()
    ):
        raw = str(item.get("fullUrl") or "") if isinstance(item, Mapping) else ""
        canonical = normalize_photo_url(raw)
        if (
            raw
            and canonical
            and is_allowed_photo_url(canonical)
            and canonical not in seen_photos
        ):
            seen_photos.add(canonical)
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
            move_in, "calculated from explicit CIAN payment terms", captured_at
        ),
        "area_m2": _field(
            _number(offer.get("totalArea")), "CIAN offer.totalArea", captured_at
        ),
        "rooms": _field(
            0
            if offer.get("flatType") == "studio"
            else _number(offer.get("roomsCount")),
            "CIAN offer.roomsCount/flatType",
            captured_at,
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
    guard_parser_drift(facts)
    return facts


async def extract_listing(page: Page) -> ListingFacts:
    payload = await page.evaluate(_DETAIL_SCRIPT)
    if not isinstance(payload, Mapping):
        raise ParserDriftError("CIAN detail payload is missing")
    return facts_from_payload(payload)


ADAPTER = SourceAdapter(
    source=SOURCE,
    display_name="CIAN",
    parser_version=PARSER_VERSION,
    matches_search_url=matches_search_url,
    matches_listing_url=matches_listing_url,
    search_page_url=search_page_url,
    canonical_listing_url=canonical_offer_url,
    listing_id=offer_id,
    wait_search=wait_search,
    wait_detail=wait_detail,
    extract_search_page=extract_search_page,
    extract_listing=extract_listing,
    matches_photo_url=matches_photo_url,
    normalize_photo_url=normalize_photo_url,
)


__all__ = [
    "ADAPTER",
    "PARSER_VERSION",
    "SOURCE",
    "canonical_offer_url",
    "extract_listing",
    "extract_search_page",
    "facts_from_payload",
    "normalize_photo_url",
    "offer_id",
    "search_page_url",
    "wait_detail",
    "wait_search",
]
