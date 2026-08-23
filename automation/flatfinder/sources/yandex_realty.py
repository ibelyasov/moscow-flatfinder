"""Read-only, versioned facts adapter for Yandex Realty."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

from ..models import Evidence, FieldValue, ListingFacts, ValueStatus
from .common import (
    ListingOutsideSearch,
    ParserDriftError,
    SearchPageResult,
    SourceAdapter,
    guard_parser_drift,
)

if TYPE_CHECKING:
    from ..models import FullTextRecord

SOURCE = "yandex_realty"
PARSER_VERSION = "yandex-realty-extract-v12"
_YANDEX_MDS_HOSTS = {"avatars.mds.yandex.net", "avatars.mds.yandex.ru"}
_MDS_SIZE = re.compile(
    r"(?:app_)?(?:small|medium|large|xlarge|xxlarge|orig(?:inal)?|preview|thumb(?:nail)?|\d{2,5}x\d{2,5})(?:[_-](?:2x|\d+x\d+))?",
    re.IGNORECASE,
)


def matches_search_url(value: str) -> bool:
    return str(urlsplit(str(value)).hostname or "").lower() == "realty.yandex.ru"


def matches_listing_url(value: str) -> bool:
    return _offer_url(value) is not None


def search_page_url(search_url: str, page_number: int) -> str:
    parts = urlsplit(str(search_url))
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "page"
    ]
    if int(page_number) > 1:
        query.append(("page", str(int(page_number))))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def matches_photo_url(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = str(parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme.lower() in {"http", "https"} and (
        host in _YANDEX_MDS_HOSTS or host.endswith(".mds.yandex.net")
    )


def normalize_photo_url(value: str | None) -> str | None:
    if not isinstance(value, str) or not matches_photo_url(value):
        return value
    parsed = urlsplit(value)
    pieces = parsed.path.split("/")
    for index, piece in enumerate(pieces):
        if _MDS_SIZE.fullmatch(piece):
            pieces[index] = "app_large"
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), "/".join(pieces), "", "")
    )


def is_allowed_photo_url(value: str | None) -> bool:
    return matches_photo_url(value)


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


_FIELDS = (
    "title",
    "address",
    "metro_station",
    "location_point",
    "price_monthly",
    "utilities",
    "commission",
    "deposit",
    "move_in_total",
    "area_m2",
    "rooms",
    "floor",
    "total_floors",
    "building_year",
    "layout",
    "repair",
    "furnished",
    "appliances",
    "route",
    "park",
    "noise",
    "fitness",
    "building",
    "entrance",
    "lease_term",
    "move_in_date",
    "restrictions",
    "photos_total",
    "photos_observed",
    "photos",
)
_INITIAL_STATE_FIELDS = {
    "metro_station",
    "location_point",
    "price_monthly",
    "area_m2",
    "rooms",
    "floor",
    "total_floors",
    "appliances",
    "park",
    "photos_total",
    "photos_observed",
    "photos",
}
_DOM_TITLE_FIELDS = {"title", "address"}

_FACTUAL_FIELDS = {
    "title",
    "address",
    "metro_station",
    "location_point",
    "price_monthly",
    "utilities",
    "commission",
    "deposit",
    "move_in_total",
    "area_m2",
    "rooms",
    "floor",
    "total_floors",
    "building_year",
    "furnished",
    "appliances",
    "lease_term",
    "move_in_date",
    "restrictions",
    "park",
    "photos_total",
    "photos_observed",
    "photos",
}
_CLAIM_FIELDS = {
    "layout",
    "repair",
    "route",
    "noise",
    "fitness",
    "building",
    "entrance",
}


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _key(value: Any) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", str(value).lower())


def _clean(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return _text(value)[:240]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): _clean(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v, depth + 1) for v in list(value)[:200]]
    return _text(value)[:240]


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    match = re.search(
        r"(?<!\d)(\d[\d\s]*(?:[.,]\d+)?)", _text(value).replace("\u00a0", " ")
    )
    if not match:
        return None
    try:
        number = float(match.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    return (
        int(number)
        if math.isfinite(number) and number.is_integer()
        else number
        if math.isfinite(number)
        else None
    )


def _numbers(value: Any) -> list[int | float]:
    return [
        n
        for n in (
            _number(match.group(0))
            for match in re.finditer(r"(?<!\d)\d[\d\s]*(?:[.,]\d+)?", _text(value))
        )
        if n is not None
    ]


def _nearby_park(value: Any) -> dict[str, Any] | None:
    for item in value if isinstance(value, list) else ():
        if not isinstance(item, Mapping) or not (name := _text(item.get("name", ""))):
            continue
        walking = next(
            (
                row
                for row in item.get("timeDistanceList", ())
                if isinstance(row, Mapping) and row.get("transport") == "ON_FOOT"
            ),
            {},
        )
        minutes = _number(walking.get("time")) or (
            (_number(item.get("timeOnFoot")) or 0) / 60
        )
        if minutes <= 0:
            continue
        distance = _number(walking.get("distance")) or _number(
            item.get("distanceOnFoot")
        )
        lat, lon = _number(item.get("latitude")), _number(item.get("longitude"))
        position = max(0.0, min(1.0, (float(minutes) - 10) / 15))
        access = 1 - position * position * (3 - 2 * position)
        return {
            "provider": "yandex_realty",
            "name": name,
            "place_id": _text(item.get("parkId", "")) or None,
            "place_type": _text(item.get("parkType", "")) or None,
            "coordinates": {"lat": lat, "lon": lon},
            "walking_minutes": float(minutes),
            "walking_distance_m": float(distance) if distance is not None else None,
            "score": round(9 * access, 2),
        }
    return None


def _money(value: Any) -> float | None:
    if isinstance(value, Mapping):
        for key in ("amount", "value", "price"):
            if key in value and (number := _number(value[key])) is not None:
                return float(number)
        return None
    match = re.search(
        r"(?<!\d)(\d(?:[\d\s\u00a0]*\d)?(?:[.,]\d+)?)\s*(?:₽|руб(?:л(?:ей|я)?)?\.?|р\.)",
        _text(value),
        re.IGNORECASE,
    )
    if not match:
        return None
    token = re.sub(r"[\s\u00a0]", "", match.group(1))
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", token):
        token = token.replace(".", "")
    else:
        token = token.replace(",", ".")
    try:
        number = float(token)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _percent(value: Any) -> float | None:
    if isinstance(value, Mapping):
        for key in ("percent", "percentage", "rate"):
            if key in value and (number := _number(value[key])) is not None:
                return float(number) if 0 <= float(number) <= 100 else None
        return None
    match = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*%", _text(value))
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    return number if 0 <= number <= 100 else None


def _commission(value: Any) -> dict[str, float | None]:
    text = _text(value)
    key = _key(text)
    if key in {"нет", "0", "0%"} or "безкомисс" in key:
        return {"percent": 0.0, "amount": 0.0}
    return {"percent": _percent(value), "amount": _money(value)}


def _deposit(value: Any) -> dict[str, Any]:
    text = _text(value)
    key = _key(text)
    if key in {"нет", "0"} or "беззалог" in key:
        return {"present": False, "amount": 0.0}
    amount = _money(value)
    month = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*месяц", text, re.IGNORECASE)
    result: dict[str, Any] = {"present": True, "amount": amount}
    if month:
        result["months"] = float(month.group(1).replace(",", "."))
    return result


def _utilities(value: Any) -> dict[str, Any]:
    text = _text(value)
    key = _key(text)
    amount = _money(value)
    if "счетчик" in key or "счётчик" in text.lower():
        mode = "meters_only"
    elif "всяквитанц" in key or "полнаяквитанц" in key or "полностью" in key:
        mode = "full_bill"
    elif "включ" in key:
        mode = "included"
    else:
        mode = "unknown"
    return {"mode": mode, "amount": amount}


def _lease_term(value: Any) -> str:
    text = _text(value)
    key = _key(text)
    months = re.search(r"(?<!\d)(\d+)\s*месяц", text, re.IGNORECASE)
    years = re.search(r"(?<!\d)(\d+)\s*(?:год|лет)", text, re.IGNORECASE)
    if (
        "длительн" in key
        or (months and int(months.group(1)) >= 11)
        or (years and int(years.group(1)) >= 1)
    ):
        return "long_term"
    if "обсужд" in key or "согласован" in key:
        return "needs_agreement"
    if (
        "краткосроч" in key
        or "посуточ" in key
        or (months and int(months.group(1)) < 11)
    ):
        return "unsuitable"
    return text


def _url(value: Any, base_url: str = "") -> str | None:
    if isinstance(value, Mapping):
        for key in ("source_url", "canonicalUrl", "url", "href"):
            if key in value and (found := _url(value[key], base_url)):
                return found
        return None
    if value is None or not (value := _text(value)):
        return None
    value = urljoin(base_url, value)
    return value if re.match(r"https?://", value) else None


def _offer_url(value: Any, base_url: str = "") -> str | None:
    if isinstance(value, Mapping):
        for key in ("source_url", "canonicalUrl", "url", "href"):
            if key in value and (found := _offer_url(value[key], base_url)):
                return found
        return None
    if value is None or not (value := _text(value)):
        return None
    value = urljoin(base_url, value)
    host = urlparse(value).hostname or ""
    valid_host = host == "yandex.ru" or host.endswith(".yandex.ru")
    return value if valid_host and re.search(r"/offer/\d+(?:/|\?|$)", value) else None


def _offer_id(value: Any, base_url: str = "") -> str | None:
    value = _offer_url(value, base_url)
    if match := (re.search(r"/offer/(\d+)(?:/|\?|$)", value) if value else None):
        return match.group(1)
    return None


def _address(value: Any) -> str | None:
    if isinstance(value, Mapping):
        parts = [
            _text(value[k])
            for k in (
                "name",
                "full",
                "text",
                "formatted",
                "formattedAddress",
                "streetAddress",
                "addressLocality",
                "addressRegion",
                "postalCode",
            )
            if value.get(k)
        ]
        return ", ".join(dict.fromkeys(parts)) or None
    return _text(value) or None


def _metro_station(value: Any) -> str | None:
    value = _text(value)
    value = re.sub(r"^(?:метро|м\.)\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+\d+\s*мин\.?(?:\s.*)?$", "", value, flags=re.IGNORECASE)
    return value or None


def _location_point(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        lat = float(value["lat"] if "lat" in value else value["latitude"])
        lon = float(value["lon"] if "lon" in value else value["longitude"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if (
        not math.isfinite(lat)
        or not math.isfinite(lon)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        return None
    return {
        "lat": lat,
        "lon": lon,
        "precision": str(value.get("precision") or "").lower() or None,
    }


def _photo_identity(value: str) -> str:
    value = normalize_photo_url(value) or value
    parsed = urlparse(value)
    path = parsed.path.rstrip("/")
    parts = [part for part in path.split("/") if part]
    for index, part in enumerate(parts):
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27,}", part, re.IGNORECASE):
            path = "/" + "/".join(parts[: index + 1])
            break
    return f"{(parsed.hostname or '').lower()}{path}"


def _photos(value: Any, base_url: str = "") -> list[dict[str, str]]:
    if isinstance(value, Mapping) or not isinstance(value, (list, tuple, set)):
        value = [value]
    result: list[dict[str, str]] = []
    identities: set[str] = set()

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
        if found := _url(item, base_url):
            canonical = normalize_photo_url(found) or found
            if is_allowed_photo_url(canonical):
                identity = _photo_identity(canonical)
                if identity not in identities:
                    identities.add(identity)
                    result.append({"url": canonical, "source_url": found})

    for item in value:
        visit(item)
    blocked = ("favicon", "logo", "icon", "placeholder")
    return [
        item
        for item in result
        if item["url"]
        and not urlparse(item["url"]).path.lower().endswith(".svg")
        and not any(part in urlparse(item["url"]).path.lower() for part in blocked)
    ]


_ALIASES = {
    "title": {"name", "title", "headline"},
    "address": {"address", "streetaddress", "formattedaddress", "locationaddress"},
    "metro_station": {"metro", "metrostation", "subway", "subwaystation"},
    "price_monthly": {
        "price",
        "pricemonthly",
        "monthlyprice",
        "rent",
        "rentprice",
        "rentalprice",
    },
    "utilities": {"utilities", "utility", "utilitycost", "communal"},
    "commission": {"commission", "agencyfee", "fee"},
    "deposit": {"deposit", "securitydeposit", "zalog"},
    "move_in_total": {"moveintotal", "initialpayment", "totalmovein", "entrytotal"},
    "area_m2": {"area", "aream2", "floorsize", "floorarea", "totalarea", "livingarea"},
    "rooms": {"rooms", "roomcount", "numberofrooms", "bedrooms", "roomstotal"},
    "floor": {"floor", "floorlevel", "floornumber"},
    "total_floors": {"totalfloors", "numberoffloors", "buildingfloors", "floorscount"},
    "building_year": {
        "buildingyear",
        "yearbuilt",
        "houseyear",
        "constructionyear",
        "builtdate",
        "годпостройки",
    },
    "layout": {"layout", "planning", "planirovka"},
    "repair": {"repair", "renovation", "finish", "finishing", "otdelka"},
    "furnished": {"furnished", "furniture", "hasfurniture", "mebel"},
    "lease_term": {"lease", "leaseterm", "rentalterm", "term", "срокаренды", "срок"},
    "move_in_date": {
        "moveindate",
        "availablefrom",
        "startdate",
        "датаначала",
        "заезд",
        "началоаренды",
    },
    "restrictions": {
        "restrictions",
        "restriction",
        "rules",
        "ограничения",
        "условия",
        "допустимо",
    },
    "appliances": {"appliances", "equipment", "amenities", "technic"},
    "route": {"route", "commute", "travel"},
    "park": {"park", "parks", "greenarea"},
    "noise": {"noise", "noiserisk", "sound"},
    "fitness": {"fitness", "gym", "sauna", "sport"},
    "building": {"building", "house", "complex"},
    "entrance": {"entrance", "entry", "porch", "podiezd"},
    "photos": {
        "photos",
        "photo",
        "images",
        "image",
        "photourls",
        "imageurls",
        "gallery",
    },
    "photos_total": {"photostotal", "imagecount", "photoscount", "gallerycount"},
    "photos_observed": {"photosobserved", "imagesobserved"},
}
_KEY_FIELDS = {alias: field for field, aliases in _ALIASES.items() for alias in aliases}
_LABELS = (
    ("price_monthly", ("аренд", "цена", "ежемесяч", "в месяц", "за месяц", "rent")),
    ("area_m2", ("площад", "м²", "м2")),
    ("rooms", ("комнат", "bedroom")),
    ("floor", ("этаж", "floor")),
    ("building_year", ("год постройки", "building year", "year built")),
    ("metro_station", ("метро", "metro", "subway")),
    ("address", ("адрес", "address", "улица")),
    ("deposit", ("залог", "deposit")),
    ("commission", ("комисс", "commission")),
    ("utilities", ("коммун", "квитан", "utility")),
    ("move_in_total", ("при въезд", "всего при")),
    ("repair", ("ремонт", "отделк", "renovation")),
    ("furnished", ("мебел", "furnished")),
    ("layout", ("планиров", "layout")),
    ("fitness", ("фитнес", "спортзал", "саун")),
    ("park", ("парк", "green")),
    ("noise", ("шум", "noise")),
    ("photos_total", ("количество фото", "фотографий")),
    ("entrance", ("подъезд", "вход", "entrance")),
    ("lease_term", ("срок аренды", "срок", "lease term", "rental term")),
    (
        "move_in_date",
        ("дата начала", "дата заезда", "заезд", "available from", "start date"),
    ),
    (
        "restrictions",
        ("ограничения", "условия", "можно с", "допустим", "restrictions", "rules"),
    ),
)


def _field(label: Any) -> str | None:
    value = _text(label)
    if _key(value) in _KEY_FIELDS:
        return _KEY_FIELDS[_key(value)]
    lower = value.lower()
    return next(
        (
            field
            for field, needles in _LABELS
            if any(needle in lower for needle in needles)
        ),
        None,
    )


def _value(field: str, value: Any, base_url: str = "") -> Any:
    if field == "address":
        return _address(value)
    if field == "metro_station":
        return _metro_station(value)
    if field == "location_point":
        return _location_point(value)
    if field == "photos":
        return _photos(value, base_url)
    if field in {
        "price_monthly",
        "area_m2",
        "rooms",
        "building_year",
        "photos_total",
        "photos_observed",
    }:
        if isinstance(value, Mapping):
            value = next(
                (
                    value[k]
                    for k in ("value", "amount", "number", "price")
                    if k in value
                ),
                value,
            )
        result = _number(value)
        if field == "building_year":
            year = int(result) if result is not None else None
            return (
                year
                if year is not None
                and 1800 <= year <= datetime.now(timezone.utc).year + 1
                else None
            )
        return (
            int(result)
            if field in {"rooms", "photos_total", "photos_observed"}
            and result is not None
            else result
        )
    if field == "floor":
        numbers = _numbers(value)
        return numbers[0] if numbers else None
    if field == "total_floors":
        numbers = _numbers(value)
        return numbers[-1] if numbers else None
    if field == "furnished":
        lower = _key(value)
        return (
            True
            if lower in {"true", "yes", "да", "есть", "имеется"}
            else False
            if lower in {"false", "no", "нет"}
            else None
        )
    if field == "commission":
        return _commission(value)
    if field == "deposit":
        return _deposit(value)
    if field == "utilities":
        return _utilities(value)
    if field == "lease_term":
        return _lease_term(value)
    return (
        _clean(value)
        if field in {"appliances", "route", "park", "noise", "building"}
        else _text(value) or None
    )


def _tri_state(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = _key(value)
    return (
        True
        if normalized in {"true", "yes", "да", "есть"}
        else False
        if normalized in {"false", "no", "нет"}
        else None
    )


def _improvement_appliances(value: Any) -> dict[str, bool | None] | None:
    if not isinstance(value, Mapping):
        return None
    normalized = {_key(key): item for key, item in value.items()}

    def first(*names: str) -> bool | None:
        for name in names:
            if name in normalized:
                return _tri_state(normalized[name])
        return None

    furniture_room = first("roomfurniture")
    furniture_kitchen = first("kitchenfurniture")
    no_furniture = first("nofurniture")
    if no_furniture is True:
        furniture = False
    elif furniture_room is True or furniture_kitchen is True:
        furniture = True
    elif furniture_room is False and furniture_kitchen is False:
        furniture = False
    else:
        furniture = None
    return {
        "furnished": furniture,
        "ac": first("aircondition", "airconditioning", "airconditioner"),
        "dishwasher": first("dishwasher"),
        "fridge": first("refrigerator", "fridge"),
        "washer": first("washingmachine", "washer"),
    }


def _add(
    out: dict[str, list[tuple[Any, str]]], field: str, value: Any, detail: str
) -> None:
    if field not in _FIELDS:
        return
    if value is None or (field == "photos" and not value):
        return
    # Photo URLs are already normalized and must not pass through the generic
    # depth/list truncation used for arbitrary structured payloads.
    clean = (
        list(value)
        if field == "photos" and isinstance(value, (list, tuple, set))
        else _clean(value)
    )
    out.setdefault(field, []).append((clean, detail[:240]))


def _dom_title_context(
    snapshot: Mapping[str, Any], source_id: str
) -> tuple[str, re.Match[str] | None]:
    document_title = _text(snapshot.get("documentTitle", ""))[:500]
    title_id = re.search(r"\s+—\s*id\s+(\d+)\s*$", document_title, re.IGNORECASE)
    return document_title, title_id


def _dom_pair_candidates(
    snapshot: Mapping[str, Any],
) -> dict[str, list[tuple[Any, str]]]:
    out: dict[str, list[tuple[Any, str]]] = {}
    for pair in snapshot.get("pairs", []):
        if not isinstance(pair, Mapping):
            continue
        label, raw = pair.get("label", ""), pair.get("value", "")
        field = _field(label) or _field(pair.get("key", ""))
        if field:
            _add(
                out,
                field,
                _value(field, raw, str(snapshot.get("url", ""))),
                f"visible label: {_text(label)[:80]}",
            )
            if field == "floor" and len(numbers := _numbers(raw)) > 1:
                _add(
                    out,
                    "total_floors",
                    numbers[-1],
                    f"visible label: {_text(label)[:80]}",
                )
    return out


def _dom_title_candidates(
    snapshot: Mapping[str, Any], source_id: str
) -> dict[str, list[tuple[Any, str]]]:
    out: dict[str, list[tuple[Any, str]]] = {}
    document_title, title_id = _dom_title_context(snapshot, source_id)
    if title_id and str(source_id) and title_id.group(1) == str(source_id):
        before_id = document_title[: title_id.start()]
        street_matches = list(
            re.finditer(
                r"(?:ул\.?|улица|проспект|пр-т|переулок|пер\.?|бульвар|шоссе|набережн\w*|проезд|площадь|аллея|дом|д\.)",
                before_id,
                re.IGNORECASE,
            )
        )
        if street_matches:
            prefix = before_id[: street_matches[-1].start()]
            city_matches = list(
                re.finditer(
                    r"(?:^|[,;|—])\s*((?:г\.\s*)?(?:Москва|Московская область)(?![-–—]))\b",
                    prefix,
                    re.IGNORECASE,
                )
            )
            if city_matches:
                address = _text(before_id[city_matches[-1].start(1) :])
                _add(out, "address", address, "bounded document title")
    if title := _text(snapshot.get("title", "")):
        _add(out, "title", title, "visible page title")
    return out


def _dom(
    snapshot: Mapping[str, Any], source_id: str = ""
) -> dict[str, list[tuple[Any, str]]]:
    pairs = _dom_pair_candidates(snapshot)
    title = _dom_title_candidates(snapshot, source_id)
    return {
        field: list((title if field in _DOM_TITLE_FIELDS else pairs).get(field, ()))
        for field in _FIELDS
    }


async def _evaluate(page: Any, script: str) -> Any:
    try:
        value = page.evaluate(script)
        return await value if hasattr(value, "__await__") else value
    except Exception:
        return None


_INITIAL_STATE_CAPTURE_SCRIPT = r"""(() => {
  window.__flatfinderInitialStateScript = '';
  window.__flatfinderInitialStateUrl = '';
  let initialState;
  Object.defineProperty(window, 'INITIAL_STATE', {
    configurable: true,
    enumerable: true,
    get: () => initialState,
    set: value => {
      initialState = value;
      if (value && typeof value === 'object') {
        try {
          window.__flatfinderInitialStateScript = JSON.stringify(value);
          window.__flatfinderInitialStateUrl = location.href;
        } catch (_) {}
      }
    }
  });
  let observer;
  const capture = node => {
    const script = node?.id === 'initial_state_script'
      ? node
      : node?.querySelector?.('#initial_state_script');
    const text = script?.textContent || '';
    if (!text) return false;
    window.__flatfinderInitialStateScript = text;
    window.__flatfinderInitialStateUrl = location.href;
    observer?.disconnect();
    return true;
  };
  observer = new MutationObserver(records => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (capture(node)) return;
      }
    }
  });
  observer.observe(document, {childList: true, subtree: true});
  document.addEventListener('DOMContentLoaded', () => capture(document), {once: true});
})()"""


async def prepare_page(page: Any) -> None:
    await _await(page.add_init_script(_INITIAL_STATE_CAPTURE_SCRIPT))


async def search_page_loaded(page: Any) -> bool:
    return bool(
        await _evaluate(
            page,
            "() => { const node = document.querySelector('#initial_state_script'); "
            "const text = node ? (node.textContent || '') : ''; "
            "if (text) { window.__flatfinderInitialStateScript = text; "
            "window.__flatfinderInitialStateUrl = location.href; } "
            "return Boolean(window.__flatfinderInitialStateScript) && "
            "window.__flatfinderInitialStateUrl === location.href; }",
        )
    )


def _above_search_price(search_url: str, value: Any) -> bool:
    prices = [
        item
        for key, item in parse_qsl(urlsplit(str(search_url)).query)
        if key in {"priceMax", "maxprice"}
    ]
    try:
        return bool(prices and float(value) > float(prices[-1]))
    except (TypeError, ValueError, OverflowError):
        return False


async def prepare_detail(page: Any, search_url: str) -> None:
    title = getattr(page, "title", None)
    title_text = str(await _await(title())) if callable(title) else ""
    title_price = re.search(
        r"за\s+([\d\s\u00a0]+)\s*₽\s+в месяц", title_text, re.IGNORECASE
    )
    if title_price and _above_search_price(
        search_url, title_price.group(1).replace(" ", "").replace("\u00a0", "")
    ):
        raise ListingOutsideSearch("offer title price is above configured priceMax")
    if not await search_page_loaded(page):
        raise ParserDriftError("current offer initial state is missing")
    await page.locator('[class*="OfferCard__card--"]').first.wait_for(
        state="attached", timeout=10_000
    )


_OFFER_LINKS_SCRIPT = r"""() => {
  const mainIds = new Set();
  let totalPages = null;
  const raw = window.__flatfinderInitialStateUrl === location.href
    ? String(window.__flatfinderInitialStateScript || '')
    : '';
  try {
    const start = raw.indexOf('{'), end = raw.lastIndexOf('}');
    if (start >= 0 && end > start) {
      const state = JSON.parse(raw.slice(start, end + 1));
      const visit = value => {
        if (!value || typeof value !== 'object') return;
        if (Array.isArray(value)) { for (const item of value) visit(item); return; }
        if (value.offerId) mainIds.add(String(value.offerId));
        for (const item of Object.values(value)) visit(item);
      };
      visit(state?.search?.offers?.entities);
      totalPages = state?.search?.offers?.pager?.totalPages ?? null;
    }
  } catch (_) {}
  const links = [...document.querySelectorAll('a[href]')].map(node => {
    const id = (node.href.match(/\/offer\/(\d+)(?:[\/?#]|$)/i) || [,''])[1];
    return {href: node.href, result: mainIds.has(id)};
  }).slice(0, 500);
  return {url: location.href, stateReady: mainIds.size > 0, totalPages, links};
}"""


_DOM_SCRIPT = r"""() => {
  const t = n => String(n?.innerText ?? n?.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 240);
  const pairs = [];
  const visible = n => {
    if (!n || n.closest('[hidden],[aria-hidden="true"]')) return false;
    const s = getComputedStyle(n);
    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  };
  const inOfferHistory = n => {
    for (let node=n; node; node=node.parentElement) {
      const classes=String(node.className?.baseVal ?? node.className ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
      if (classes.includes('offerhistory')) return true;
    }
    return false;
  };
  const currentId = (location.pathname.match(/\/offer\/(\d+)(?:[\/?#]|$)/i) || [,''])[1];
  const marker = n => [n?.getAttribute?.('data-offer-id'),n?.getAttribute?.('data-listing-id'),n?.getAttribute?.('data-id'),n?.getAttribute?.('data-testid'),n?.getAttribute?.('href'),n?.id,n?.className].filter(Boolean).join(' ');
  const currentMarker = value => currentId && (new RegExp(`/offer/${currentId}(?:[/?#]|$)`, 'i').test(value) || new RegExp(`(?:^|[^0-9])${currentId}(?:[^0-9]|$)`).test(value));
  const foreignMarker = value => /\/offer\/\d+(?:[/?#]|$)/i.test(value) && !currentMarker(value);
  const blockedMarker = value => /recommend|similar|cookie|banner|advert|promo|history/i.test(value);
  const primaryOfferMarker = value => /OfferCard__card--/i.test(value);
  const offerRoot = node => {
    for (let current=node; current && current !== document.body; current=current.parentElement) {
      const value = marker(current);
      if (currentMarker(value)) return current;
      if (foreignMarker(value) || blockedMarker(value)) return null;
      if (primaryOfferMarker(value)) return current;
    }
    return null;
  };
  const inCurrentOffer = node => Boolean(offerRoot(node)) && !node.closest('aside,nav,[role="dialog"]');
  for (const n of document.querySelectorAll('dt')) { const v=t(n.nextElementSibling); if(inCurrentOffer(n)&&inCurrentOffer(n.nextElementSibling)&&!inOfferHistory(n)&&!inOfferHistory(n.nextElementSibling)&&visible(n)&&visible(n.nextElementSibling)&&t(n)&&v)pairs.push({label:t(n),value:v}); }
  for (const n of document.querySelectorAll('tr')) { const c=[...n.children].filter(child => visible(child) && inCurrentOffer(child)).map(t).filter(Boolean); if(inCurrentOffer(n)&&!inOfferHistory(n)&&visible(n)&&c.length>1)pairs.push({label:c[0],value:c.slice(1).join(' ')}); }
  const transactionLabel = /^(Залог|Комиссия агенту|Комиссия агентства|Коммунальные услуги|Срок аренды)(?:\s+\d+(?:[.,]\d+)?%)?$/i;
  const transactionPairs = new Set(pairs.map(item => `${item.label}\n${item.value}`));
  for (const n of document.querySelectorAll('div,span,p')) {
    const label=t(n);
    const labelMatch=label.match(transactionLabel);
    if (!labelMatch || !visible(n) || !inCurrentOffer(n) || inOfferHistory(n)) continue;
    const canonicalLabel=labelMatch[1];
    let row=n.parentElement;
    for (let depth=0; row && depth<4; depth++, row=row.parentElement) {
      if (!visible(row) || !inCurrentOffer(row) || inOfferHistory(row)) continue;
      const rowText=t(row);
      const value=rowText.toLowerCase().startsWith(canonicalLabel.toLowerCase()) ? rowText.slice(canonicalLabel.length).trim() : '';
      if (!value || value.length>160) continue;
      const identity=`${canonicalLabel}\n${value}`;
      if (!transactionPairs.has(identity)) {
        transactionPairs.add(identity);
        pairs.push({label:canonicalLabel,value});
      }
      break;
    }
  }
  const buildingYears = new Set();
  for (const n of document.querySelectorAll('div,span,p,li')) {
    if (!visible(n) || !inCurrentOffer(n) || inOfferHistory(n)) continue;
    const value=t(n);
    const match=value.match(/^(?:Дом\s+|Год постройки\s*[:—-]?\s*)((?:18|19|20)\d{2})\s*(?:г\.?|год(?:а)?)?$/i);
    if (match && !buildingYears.has(match[1])) {
      buildingYears.add(match[1]);
      pairs.push({label:'Год постройки',value:match[1]});
    }
  }
  const h1Node=document.querySelector('h1');
  const h1=inCurrentOffer(h1Node)?t(h1Node):'';
  const documentTitle=String(document.title||'').trim().slice(0,500);
  return {url:location.href,title:h1,documentTitle,pairs};
}"""

_FULL_TEXT_SCRIPT = r"""() => {
  const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const visible = n => {
    if (!n || n.closest('[hidden],[aria-hidden="true"]')) return false;
    const s = getComputedStyle(n);
    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  };
  const currentId = (location.pathname.match(/\/offer\/(\d+)(?:[\/?#]|$)/i) || [,''])[1];
  const marker = n => [n?.getAttribute?.('data-offer-id'),n?.getAttribute?.('data-listing-id'),n?.getAttribute?.('data-id'),n?.getAttribute?.('data-testid'),n?.getAttribute?.('href'),n?.id,n?.className].filter(Boolean).join(' ');
  const currentMarker = value => currentId && (new RegExp(`/offer/${currentId}(?:[/?#]|$)`, 'i').test(value) || new RegExp(`(?:^|[^0-9])${currentId}(?:[^0-9]|$)`).test(value));
  const foreignMarker = value => /\/offer\/\d+(?:[/?#]|$)/i.test(value) && !currentMarker(value);
  const blockedMarker = value => /recommend|similar|cookie|banner|advert|promo|history/i.test(value);
  const offerRoot = node => {
    for (let current=node; current && current !== document.body; current=current.parentElement) {
      const value = marker(current);
      if (currentMarker(value)) return current;
      if (foreignMarker(value) || blockedMarker(value)) return null;
    }
    return null;
  };
  const inCurrentOffer = node => Boolean(offerRoot(node)) && !node.closest('aside,nav,[role="dialog"]');
  const selectors = [
    '[itemprop="description"]', '[data-testid*="description" i]',
    '[data-qa*="description" i]', '[class*="description" i]', '[id*="description" i]'
  ];
  const values = [];
  for (const selector of selectors) {
    for (const node of document.querySelectorAll(selector)) {
      if (!visible(node) || !inCurrentOffer(node)) continue;
      const value = clean(node.innerText || node.textContent).slice(0, 20000);
      if (value.length >= 20) values.push(value);
    }
  }
  const text = [...new Set(values)].sort((a,b) => b.length - a.length)[0] || '';
  const sentences = text.split(/(?<=[.!?。！？])\s+/).filter(Boolean).slice(0, 32);
  return {text, source: 'dom-description', quotes: sentences.map((quote, index) => ({quote: quote.slice(0, 240), locator: `description.sentence[${index}]`, source: 'seller_claim'}))};
}"""


def _initial_state_script(source_id: str) -> str:
    encoded_id = json.dumps(str(source_id), ensure_ascii=False)
    return f"""() => {{
  const sourceId = {encoded_id};
  const raw = String(window.__flatfinderInitialStateScript || '');
  if (!raw || !sourceId) return null;
  const start = raw.indexOf('{{'), end = raw.lastIndexOf('}}');
  if (start < 0 || end <= start) return null;
  let state;
  try {{ state = JSON.parse(raw.slice(start, end + 1)); }} catch (_) {{ return null; }}
  const card = state?.offerCard?.card;
  if (!card || String(card.offerId ?? '') !== sourceId) return null;
  const scalar = value => {{
    if (value == null || typeof value !== 'object' || Array.isArray(value)) return value;
    for (const key of ['value','amount','number','price','monthly','rent','meters','text','full','formatted','formattedAddress','url','href']) {{
      if (value[key] != null && typeof value[key] !== 'object') return value[key];
    }}
    return value;
  }};
  const first = (object, keys) => {{
    for (const key of keys) if (object?.[key] != null) return object[key];
    return null;
  }};
  const apartment = card.apartment || {{}};
  const metro = card.location?.metro ?? card.location?.station ?? card.location?.metroList?.[0] ?? card.location?.expectedMetroList?.[0];
  const rawOfferUrl = scalar(first(card, ['offerUrl','url','canonicalUrl','link']));
  const offerText = String(rawOfferUrl || '');
  const offerMatch = offerText.match(/\\/offer\\/(\\d+)(?:[\\/?#]|$)/i);
  const yandexOffer = /^(?:https?:)?\\/\\/(?:[^/]+\\.)?yandex\\.ru\\/offer\\/\\d+(?:[\\/?#]|$)/i.test(offerText);
  if (yandexOffer && (!offerMatch || offerMatch[1] !== sourceId)) return null;
  const offerUrl = yandexOffer && offerMatch?.[1] === sourceId ? rawOfferUrl : null;
  return {{
    offerId: sourceId, offerUrl: offerUrl || null,
    price: scalar(first(card, ['price','priceMonthly','monthlyPrice','rentPrice'])),
    area: scalar(first(card, ['area','areaM2','totalArea'])) ?? scalar(first(apartment, ['area','areaM2','totalArea'])),
    rooms: scalar(first(card, ['rooms','roomsTotal','roomCount'])) ?? scalar(first(apartment, ['rooms','roomsTotal','roomCount'])),
    floor: scalar(first(card, ['floor','floorNumber'])) ?? scalar(first(apartment, ['floor','floorNumber'])),
    floorsOffered: card.floorsOffered,
    totalFloors: scalar(first(card, ['totalFloors','floorsTotal','buildingFloors'])) ?? scalar(first(apartment, ['totalFloors','floorsTotal','buildingFloors'])),
    metroStation: typeof metro === 'string' ? metro : scalar(first(metro, ['name','title','stationName'])),
    fullImages: Array.isArray(card.fullImages) ? card.fullImages : [],
    totalImages: scalar(card.totalImages),
    improvements: card.apartment?.improvements && typeof card.apartment.improvements === 'object' ? card.apartment.improvements : null,
    locationPoint: card.location?.point && typeof card.location.point === 'object' ? card.location.point : null,
    nearbyParks: Array.isArray(card.location?.parks) ? card.location.parks : []
  }};
}}"""


def _initial_card_candidates(
    card: Mapping[str, Any] | None, source_id: str, base_url: str
) -> dict[str, list[tuple[Any, str]]]:
    if not isinstance(card, Mapping) or str(card.get("offerId", "")) != str(source_id):
        return {}
    offer_url = _offer_url(card.get("offerUrl"), base_url)
    if offer_url and _offer_id(offer_url) != str(source_id):
        return {}
    out: dict[str, list[tuple[Any, str]]] = {}
    floor = card.get("floor")
    if floor is None:
        offered = card.get("floorsOffered")
        if isinstance(offered, (list, tuple)) and len(offered) == 1:
            value = offered[0]
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                floor = value
            elif isinstance(value, str) and re.fullmatch(r"\s*[1-9]\d*\s*", value):
                floor = int(value)
    for field, key in (
        ("price_monthly", "price"),
        ("area_m2", "area"),
        ("rooms", "rooms"),
        ("floor", "floor"),
        ("total_floors", "totalFloors"),
        ("metro_station", "metroStation"),
        ("location_point", "locationPoint"),
    ):
        value = floor if key == "floor" else card.get(key)
        if value is not None:
            detail = (
                "initial state: offerCard.card.floorsOffered"
                if key == "floor" and card.get("floor") is None
                else f"initial state: offerCard.card.{key}"
            )
            _add(out, field, _value(field, value, base_url), detail)
    photos = _photos(card.get("fullImages", []), base_url)
    if photos:
        _add(out, "photos", photos, "initial state: offerCard.card.fullImages")
        _add(
            out,
            "photos_observed",
            len(photos),
            "initial state: offerCard.card.fullImages",
        )
    total_images = _number(card.get("totalImages"))
    if total_images is not None:
        _add(
            out,
            "photos_total",
            int(total_images),
            "initial state: offerCard.card.totalImages",
        )
    if (appliances := _improvement_appliances(card.get("improvements"))) is not None:
        _add(
            out,
            "appliances",
            appliances,
            "initial state: offerCard.card.apartment.improvements",
        )
    if park := _nearby_park(card.get("nearbyParks")):
        _add(out, "park", park, "initial state: offerCard.card.location.parks[0]")
    return out


def _full_text_quotes(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    raw_quotes = payload.get("quotes")
    quotes: list[dict[str, str]] = []
    if isinstance(raw_quotes, (list, tuple)):
        for index, item in enumerate(raw_quotes[:32]):
            if isinstance(item, Mapping):
                quote = _text(item.get("quote", item.get("text", "")))[:240]
                if not quote:
                    continue
                locator = _text(item.get("locator", f"description.sentence[{index}]"))[
                    :120
                ]
                source = (
                    _text(item.get("source", "seller_claim"))[:40] or "seller_claim"
                )
            else:
                quote = _text(item)[:240]
                if not quote:
                    continue
                locator, source = f"description.sentence[{index}]", "seller_claim"
            quotes.append({"quote": quote, "locator": locator, "source": source})
    return quotes


async def extract_full_text(page: Any, source_listing_id: str) -> FullTextRecord:
    """Extract the listing description without retaining page-wide DOM data."""

    from ..models import FullTextRecord

    source_id = str(source_listing_id).strip()
    if (
        not re.fullmatch(r"\d+", source_id)
        or _offer_id(getattr(page, "url", "")) != source_id
    ):
        raise ParserDriftError("description identity is missing or inconsistent")
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = await _evaluate(page, _FULL_TEXT_SCRIPT)
    if not isinstance(payload, Mapping) or payload.get("source") != "dom-description":
        raise ParserDriftError("description DOM payload is missing")
    text = _text(payload.get("text", ""))[:20000]
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return FullTextRecord(
        listing_id=int(source_id),
        text=text,
        quotes=_full_text_quotes(payload),
        captured_at=captured_at,
        content_sha256=content_sha256,
    )


async def extract_search_page(page: Any) -> SearchPageResult:
    snapshot = await _evaluate(page, _OFFER_LINKS_SCRIPT)
    if not isinstance(snapshot, Mapping) or snapshot.get("stateReady") is not True:
        raise ParserDriftError("search result initial state is missing")
    result, seen = [], set()
    for item in snapshot.get("links", []):
        if not isinstance(item, Mapping):
            continue
        if not item.get("result"):
            continue
        href = _offer_url(item.get("href"), str(snapshot.get("url", "")))
        if href and (identifier := _offer_id(href)) and identifier not in seen:
            result.append((identifier, href))
            seen.add(identifier)
    raw_total_pages = snapshot.get("totalPages")
    total_pages = (
        raw_total_pages
        if isinstance(raw_total_pages, int)
        and not isinstance(raw_total_pages, bool)
        and raw_total_pages > 0
        else None
    )
    return SearchPageResult(result, total_pages)


async def extract_offer_links(page: Any) -> list[tuple[str, str]]:
    return (await extract_search_page(page)).links


def _first_field_candidate(
    field: str, candidates: Mapping[str, list[tuple[Any, str]]]
) -> tuple[Any, str] | None:
    values = candidates.get(field, [])
    return values[0] if values else None


def _materialize_fields(
    candidates: Mapping[str, list[tuple[Any, str]]], captured_at: str
) -> dict[str, FieldValue]:
    fields: dict[str, FieldValue] = {}
    for field in _FIELDS:
        found = _first_field_candidate(field, candidates)
        if found is None:
            fields[field] = FieldValue(None, ValueStatus.UNKNOWN)
            continue
        value, detail = found
        status, evidence = _fact_evidence(field, value, detail, captured_at)
        fields[field] = FieldValue(value, status, [evidence])
    return fields


def _fact_evidence(
    field: str, value: Any, detail: str, captured_at: str
) -> tuple[ValueStatus, Evidence]:
    source = (
        "page_fact"
        if field in _FACTUAL_FIELDS
        else "seller_claim"
        if field in _CLAIM_FIELDS
        else detail.split(":", 1)[0]
    )
    quote = _text(value)
    if isinstance(value, (Mapping, list, tuple)):
        quote = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    quote = quote[:140]
    locator = _text(detail)[:100]
    bounded = f"locator={locator}; quote={quote}"[:240]
    status = ValueStatus.PARTIAL if source == "seller_claim" else ValueStatus.CONFIRMED
    return status, Evidence(source, bounded, captured_at)


async def extract_listing(
    page: Any, recent_coverages: Sequence[float] = ()
) -> ListingFacts:
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source_url = _offer_url(getattr(page, "url", "")) or ""
    source_id = _offer_id(source_url) or ""
    if not source_id:
        raise ParserDriftError("current offer URL has no listing id")
    initial_card = await _evaluate(page, _initial_state_script(source_id))
    if not isinstance(initial_card, Mapping):
        raise ParserDriftError("current offer initial state is missing")
    snapshot = await _evaluate(page, _DOM_SCRIPT)
    if not isinstance(snapshot, Mapping):
        raise ParserDriftError("current offer DOM snapshot is missing")
    initial_candidates = _initial_card_candidates(initial_card, source_id, source_url)
    dom_candidates = _dom(snapshot, source_id)
    candidates = {
        field: list(
            (
                initial_candidates if field in _INITIAL_STATE_FIELDS else dom_candidates
            ).get(field, ())
        )
        for field in _FIELDS
    }
    fields = _materialize_fields(candidates, captured_at)
    facts = ListingFacts(source_id, source_url, fields, SOURCE)
    guard_parser_drift(facts, recent_coverages)
    return facts


ADAPTER = SourceAdapter(
    source=SOURCE,
    display_name="Яндекс",
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
    prepare_page=prepare_page,
    extract_search_page=extract_search_page,
)


__all__ = [
    "ADAPTER",
    "PARSER_VERSION",
    "SOURCE",
    "extract_full_text",
    "extract_listing",
    "extract_offer_links",
    "extract_search_page",
    "normalize_photo_url",
    "prepare_detail",
    "search_page_url",
]
