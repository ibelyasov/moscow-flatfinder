"""2GIS geocoding and place search for FlatFinder route enrichment."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .models import Evidence, FieldValue, ListingFacts, ValueStatus

_MOSCOW = ZoneInfo("Europe/Moscow")
_GEOCODER_URL = "https://catalog.api.2gis.com/3.0/items/geocode"
_PLACES_URL = "https://catalog.api.2gis.com/3.0/items"
_API_LIMITS = {
    "geocode": ("geocoder", 600),
    "park_search": ("geocoder", 600),
    "fitness_search": ("places", 600),
}
_API_LOCKS = {service: Lock() for service, _ in _API_LIMITS.values()}
_api_last_call = {service: 0.0 for service, _ in _API_LIMITS.values()}
_POINT_RE = re.compile(
    r"POINT\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", re.IGNORECASE
)


class TwoGisAPIError(RuntimeError):
    def __init__(self, status: int, response: Any) -> None:
        super().__init__(f"2GIS HTTP {status}")
        self.status = int(status)
        self.response = response


@dataclass(slots=True)
class CommuteResult:
    address: str
    destination: str
    address_sha256: str
    captured_at: str
    service_date: str
    provider: str = "yandex_maps"
    status: str = "unknown"
    error: str | None = None
    home_lat: float | None = None
    home_lon: float | None = None
    point_kind: str | None = None
    building_id: str | None = None
    entrance_id: str | None = None
    geocode_precision: str | None = None
    office_lat: float | None = None
    office_lon: float | None = None
    home_to_work_minutes: float | None = None
    work_to_home_minutes: float | None = None
    home_to_work_score: float | None = None
    work_to_home_score: float | None = None
    average_minutes: float | None = None
    average_score: float | None = None
    commute_score: float = 0.0
    gate_status: str = "unknown"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CommuteResult:
        names = cls.__dataclass_fields__
        return cls(**{name: payload[name] for name in names if name in payload})


@dataclass(slots=True)
class ParkResult:
    address: str
    address_sha256: str
    captured_at: str
    provider: str = "2gis"
    route_provider: str = "yandex_maps"
    status: str = "unknown"
    error: str | None = None
    home_lat: float | None = None
    home_lon: float | None = None
    place_id: str | None = None
    place_name: str | None = None
    place_type: str | None = None
    place_lat: float | None = None
    place_lon: float | None = None
    area_hectares: float | None = None
    quality: float | None = None
    walking_minutes: float | None = None
    walking_distance_m: float | None = None
    park_score: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FitnessResult:
    address: str
    address_sha256: str
    captured_at: str
    provider: str = "2gis"
    route_provider: str = "yandex_maps"
    status: str = "unknown"
    error: str | None = None
    home_lat: float | None = None
    home_lon: float | None = None
    place_id: str | None = None
    place_name: str | None = None
    place_lat: float | None = None
    place_lon: float | None = None
    rating: float | None = None
    review_count: int | None = None
    sauna: bool = False
    quality: float = 0.0
    walking_minutes: float | None = None
    walking_distance_m: float | None = None
    fitness_score: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def address_hash(address: str) -> str:
    normalized = " ".join(str(address or "").casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def next_tuesday(now: datetime | None = None) -> date:
    current = (
        now.astimezone(_MOSCOW)
        if now is not None and now.tzinfo
        else (now.replace(tzinfo=_MOSCOW) if now is not None else datetime.now(_MOSCOW))
    )
    days = (1 - current.weekday()) % 7 or 7
    return current.date() + timedelta(days=days)


def _json_body(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return {"text": text[:2000]}


def _wait_for_api_slot(endpoint: str) -> None:
    service, requests_per_minute = _API_LIMITS[endpoint]
    with _API_LOCKS[service]:
        delay = _api_last_call[service] + 60 / requests_per_minute + 0.1 - monotonic()
        if delay > 0:
            sleep(delay)
        _api_last_call[service] = monotonic()


def _default_fetch(
    endpoint: str, request: dict[str, Any], api_key: str, timeout: float
) -> Any:
    if endpoint == "geocode":
        fields = "items.point,items.full_address_name,items.search_attributes.dgis_address_details"
        if request.get("with_entrances", True):
            fields += ",items.context,items.links.database_entrances"
        params = {"type": "building", "page_size": 5, "fields": fields, "key": api_key}
        if "q" in request:
            params["q"] = request["q"]
        else:
            params.update({"lat": request["lat"], "lon": request["lon"], "radius": 30})
        http_request = Request(
            f"{_GEOCODER_URL}?{urlencode(params)}",
            headers={"Accept": "application/json"},
        )
    elif endpoint == "park_search":
        point = f"{request['lon']},{request['lat']}"
        params = {
            "q": "парк",
            "type": "adm_div.place",
            "point": point,
            "location": point,
            "radius": 2000,
            "sort": "distance",
            "page_size": 10,
            "fields": "items.point,items.full_address_name,items.geometry.centroid,items.geometry.selection,items.rubrics",
            "key": api_key,
        }
        http_request = Request(
            f"{_GEOCODER_URL}?{urlencode(params)}",
            headers={"Accept": "application/json"},
        )
    elif endpoint == "fitness_search":
        point = f"{request['lon']},{request['lat']}"
        params = {
            "q": "фитнес-клуб",
            "point": point,
            "location": point,
            "radius": 2000,
            "sort": "distance",
            "page_size": 10,
            "fields": "items.point,items.full_address_name,items.reviews,items.attribute_groups,items.rubrics",
            "key": api_key,
        }
        http_request = Request(
            f"{_PLACES_URL}?{urlencode(params)}", headers={"Accept": "application/json"}
        )
    else:
        raise ValueError(f"unsupported 2GIS endpoint: {endpoint}")
    _wait_for_api_slot(endpoint)
    try:
        with urlopen(http_request, timeout=timeout) as response:
            return _json_body(response.read())
    except HTTPError as error:
        raise TwoGisAPIError(error.code, _json_body(error.read())) from None


def _safe_error(error: BaseException) -> str:
    if isinstance(error, TwoGisAPIError):
        return f"2GIS HTTP {error.status}"
    return error.__class__.__name__


def _redact_secret(value: Any, secret: str) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "<redacted>")
    if isinstance(value, Mapping):
        return {
            _redact_secret(key, secret): _redact_secret(item, secret)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    return value


def _call(
    endpoint: str,
    request: dict[str, Any],
    api_key: str,
    timeout: float,
    fetch: Callable[[str, dict[str, Any], str, float], Any],
    calls: list[dict[str, Any]],
) -> Any:
    item: dict[str, Any] = {"endpoint": endpoint, "attempt": 1, "request": request}
    try:
        item["response"] = _redact_secret(
            fetch(endpoint, request, api_key, timeout), api_key
        )
        calls.append(item)
        return item["response"]
    except Exception as error:
        item["error"] = _safe_error(error)
        if isinstance(error, TwoGisAPIError):
            item["response"] = _redact_secret(error.response, api_key)
        calls.append(item)
    return None


def _entrance_point(item: Mapping[str, Any]) -> tuple[float, float, str | None] | None:
    links = item.get("links")
    entrances = links.get("database_entrances") if isinstance(links, Mapping) else None
    if not isinstance(entrances, list):
        return None
    context = item.get("context")
    entrance_id = (
        str(context.get("entrance_id"))
        if isinstance(context, Mapping) and context.get("entrance_id")
        else None
    )
    candidates = [
        entry
        for entry in entrances
        if isinstance(entry, Mapping)
        and (entrance_id is None or str(entry.get("id")) == entrance_id)
    ]
    if entrance_id is None and len(candidates) != 1:
        return None
    if len(candidates) != 1:
        return None
    geometry = candidates[0].get("geometry")
    points = geometry.get("points") if isinstance(geometry, Mapping) else None
    match = (
        _POINT_RE.fullmatch(str(points[0]))
        if isinstance(points, list) and points
        else None
    )
    if match is None:
        return None
    lat, lon = float(match.group(2)), float(match.group(1))
    if (
        not math.isfinite(lat)
        or not math.isfinite(lon)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        return None
    return lat, lon, str(candidates[0].get("id") or "") or None


def _geocode(response: Any) -> dict[str, Any]:
    result = response.get("result") if isinstance(response, Mapping) else None
    items = result.get("items") if isinstance(result, Mapping) else None
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, Mapping) or item.get("type") != "building":
            continue
        search = item.get("search_attributes")
        details = (
            search.get("dgis_address_details") if isinstance(search, Mapping) else None
        )
        precision = (
            str(details.get("precision")) if isinstance(details, Mapping) else ""
        )
        if precision != "exact":
            continue
        point = item.get("point")
        if not isinstance(point, Mapping):
            continue
        try:
            lat, lon = float(point["lat"]), float(point["lon"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if (
            not math.isfinite(lat)
            or not math.isfinite(lon)
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
        ):
            continue
        entrance = _entrance_point(item)
        if entrance is not None:
            lat, lon, entrance_id = entrance
            point_kind = "entrance"
        else:
            entrance_id = None
            point_kind = "building"
        return {
            "lat": lat,
            "lon": lon,
            "point_kind": point_kind,
            "building_id": str(item.get("id") or "") or None,
            "entrance_id": entrance_id,
            "precision": precision,
            "address": str(
                item.get("full_name")
                or item.get("full_address_name")
                or item.get("address_name")
                or ""
            ),
        }
    raise ValueError("exact building not found")


def _reverse_geocode(response: Any) -> dict[str, Any]:
    result = response.get("result") if isinstance(response, Mapping) else None
    items = result.get("items") if isinstance(result, Mapping) else None
    buildings = (
        [
            item
            for item in items
            if isinstance(item, Mapping) and item.get("type") == "building"
        ]
        if isinstance(items, list)
        else []
    )
    if len(buildings) != 1:
        raise ValueError("unique nearby 2GIS building not found")
    building = {
        **buildings[0],
        "search_attributes": {"dgis_address_details": {"precision": "exact"}},
    }
    return _geocode({"result": {"items": [building]}})


def geocode_address(
    address: str,
    api_key: str,
    *,
    hint_point: Mapping[str, Any] | None = None,
    timeout: float = 45.0,
    fetch: Callable[[str, dict[str, Any], str, float], Any] = _default_fetch,
    calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve one exact 2GIS building point without storing the API key."""

    address = str(address or "").strip()
    if not address or not api_key:
        raise ValueError("address and twogis_api_key are required")
    history = calls if calls is not None else []
    response = _call(
        "geocode",
        {"q": address, "with_entrances": True},
        api_key,
        timeout,
        fetch,
        history,
    )
    if response is None:
        response = _call(
            "geocode",
            {"q": address, "with_entrances": False},
            api_key,
            timeout,
            fetch,
            history,
        )
    if response is None:
        raise ValueError("2GIS address geocoding failed")
    try:
        point = _geocode(response)
    except ValueError:
        hint = saved_point(hint_point, "home")
        if hint is None:
            raise
        response = _call(
            "geocode",
            {"lat": hint["lat"], "lon": hint["lon"], "with_entrances": True},
            api_key,
            timeout,
            fetch,
            history,
        )
        if response is None:
            raise ValueError("2GIS reverse geocoding failed")
        point = _reverse_geocode(response)
        point["match_method"] = "reverse_near_source_point"
    return {
        **point,
        "provider": "2gis",
        "query_address": address,
        "address_sha256": address_hash(address),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def saved_point(
    payload: Mapping[str, Any] | None, prefix: str
) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping) or prefix not in {"home", "office"}:
        return None
    try:
        lat = float(payload["lat"] if "lat" in payload else payload[f"{prefix}_lat"])
        lon = float(payload["lon"] if "lon" in payload else payload[f"{prefix}_lon"])
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
        "point_kind": payload.get("point_kind") if prefix == "home" else "building",
        "building_id": payload.get("building_id") if prefix == "home" else None,
        "entrance_id": payload.get("entrance_id") if prefix == "home" else None,
        "precision": (payload.get("geocode_precision") or payload.get("precision"))
        if prefix == "home"
        else "exact",
        "provider": payload.get("provider") if prefix == "home" else None,
    }


def apply_location_point(
    facts: ListingFacts | dict[str, Any], point: Mapping[str, Any]
) -> ListingFacts | dict[str, Any]:
    """Replace the source listing point with one exact 2GIS building point."""

    normalized = saved_point(point, "home")
    if (
        normalized is None
        or normalized.get("precision") != "exact"
        or not normalized.get("building_id")
    ):
        raise ValueError("exact 2GIS building coordinates are required")
    value = {
        **normalized,
        "provider": "2gis",
        "address": str(point.get("address") or point.get("query_address") or ""),
    }
    detail = json.dumps(
        {
            key: value.get(key)
            for key in (
                "lat",
                "lon",
                "point_kind",
                "building_id",
                "entrance_id",
                "precision",
                "address",
            )
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    captured_at = str(
        point.get("captured_at")
        or datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    evidence = Evidence("2gis_geocoder", detail, captured_at)
    if isinstance(facts, ListingFacts):
        facts.fields["location_point"] = FieldValue(
            value, ValueStatus.CONFIRMED, [evidence]
        )
        return facts
    fields = facts.setdefault("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("facts.fields must be an object")
    fields["location_point"] = {
        "value": value,
        "status": ValueStatus.CONFIRMED.value,
        "evidence": [
            {
                "source": evidence.source,
                "detail": evidence.detail,
                "captured_at": evidence.captured_at,
                "confidence": ValueStatus.CONFIRMED.value,
            }
        ],
    }
    return facts


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _smoothstep(value: float) -> float:
    value = _clamp(value)
    return value * value * (3 - 2 * value)


def park_quality(area_hectares: float | None) -> float:
    if area_hectares is None or not math.isfinite(area_hectares):
        return 0.5
    lower, upper = 0.3, 5.0
    if area_hectares <= lower:
        return 0.3
    if area_hectares >= upper:
        return 1.0
    position = math.log(area_hectares / lower) / math.log(upper / lower)
    return 0.3 + 0.7 * _smoothstep(position)


def park_score(minutes: float | None, area_hectares: float | None) -> float:
    if minutes is None or not math.isfinite(minutes) or minutes < 0:
        return 0.0
    position = _clamp((minutes - 10) / 15)
    access = 1 - _smoothstep(position)
    return round(9 * park_quality(area_hectares) * access, 2)


def fitness_quality(rating: float | None, review_count: int | None) -> float:
    if (
        rating is None
        or review_count is None
        or not math.isfinite(rating)
        or review_count < 0
    ):
        return 0.0
    return _smoothstep((rating - 4.0) / 0.5) * _smoothstep(review_count / 20)


def fitness_score(
    minutes: float | None,
    rating: float | None,
    review_count: int | None,
    sauna: bool,
) -> float:
    if minutes is None or not math.isfinite(minutes) or minutes < 0:
        return 0.0
    quality = fitness_quality(rating, review_count)
    venue_score = 2 + 2 * quality * (1 + int(bool(sauna)))
    access = 1 - _smoothstep((minutes - 10) / 15)
    return round(venue_score * access, 2)


def _coordinate_pairs(wkt: Any) -> list[tuple[float, float]]:
    if not isinstance(wkt, str):
        return []
    return [
        (float(lon), float(lat))
        for lon, lat in re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", wkt)
    ]


def _geometry_area_hectares(wkt: Any) -> float | None:
    points = _coordinate_pairs(wkt)
    if len(points) < 3:
        return None
    mean_lat = math.radians(sum(lat for _, lat in points) / len(points))
    projected = [
        (lon * 111_320 * math.cos(mean_lat), lat * 110_540) for lon, lat in points
    ]
    area = (
        abs(
            sum(
                left[0] * right[1] - right[0] * left[1]
                for left, right in zip(projected, projected[1:] + projected[:1])
            )
        )
        / 2
        / 10_000
    )
    return round(area, 3) if math.isfinite(area) and area > 0 else None


def _point(value: Any) -> tuple[float, float] | None:
    if isinstance(value, Mapping):
        try:
            lat, lon = float(value["lat"]), float(value["lon"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        return (
            (lat, lon)
            if math.isfinite(lat)
            and math.isfinite(lon)
            and -90 <= lat <= 90
            and -180 <= lon <= 180
            else None
        )
    pairs = _coordinate_pairs(value)
    return (pairs[0][1], pairs[0][0]) if pairs else None


def _distance_m(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    lat1, lat2 = math.radians(float(left["lat"])), math.radians(float(right["lat"]))
    dlat = lat2 - lat1
    dlon = math.radians(float(right["lon"]) - float(left["lon"]))
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(value)))


def _park_candidates(response: Any, home: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = response.get("result") if isinstance(response, Mapping) else None
    items = result.get("items") if isinstance(result, Mapping) else None
    candidates: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, Mapping) or str(item.get("type") or "") in {
            "branch",
            "building",
        }:
            continue
        geometry = (
            item.get("geometry") if isinstance(item.get("geometry"), Mapping) else {}
        )
        point = _point(item.get("point")) or _point(geometry.get("centroid"))
        if point is None:
            continue
        area = _geometry_area_hectares(geometry.get("selection"))
        candidate = {
            "id": str(item.get("id") or "") or None,
            "name": str(item.get("name") or item.get("full_name") or "Парк"),
            "type": str(item.get("subtype") or item.get("type") or "park"),
            "lat": point[0],
            "lon": point[1],
            "area_hectares": area,
        }
        estimated_minutes = _distance_m(home, candidate) * 1.25 / 80
        candidate["estimated_score"] = park_score(estimated_minutes, area)
        candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda item: (-float(item["estimated_score"]), _distance_m(home, item)),
    )


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _review_stats(reviews: Mapping[str, Any]) -> tuple[float | None, int | None]:
    fallback: tuple[float | None, int | None] = (None, None)
    for rating_name, count_name in (
        ("general_rating", "general_review_count"),
        ("rating", "review_count"),
        ("org_rating", "org_review_count"),
    ):
        rating = _optional_number(reviews.get(rating_name))
        count = _optional_number(reviews.get(count_name))
        pair = (rating, int(count) if count is not None else None)
        if fallback == (None, None) and pair != (None, None):
            fallback = pair
        if rating is not None and count is not None and count > 0:
            return pair
    return fallback


def _attribute_has_sauna(value: Any) -> bool:
    if isinstance(value, list):
        return any(_attribute_has_sauna(item) for item in value)
    if not isinstance(value, Mapping):
        return False
    label = " ".join(
        str(value.get(name) or "")
        for name in ("name", "caption", "label", "text", "tag")
    ).casefold()
    if "саун" in label or "sauna" in label:
        raw = value.get("value", value.get("is_enabled", True))
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().casefold() not in {
            "",
            "0",
            "false",
            "нет",
            "отсутствует",
            "не предусмотрена",
        }
    return any(_attribute_has_sauna(item) for item in value.values())


def _fitness_candidates(response: Any, home: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = response.get("result") if isinstance(response, Mapping) else None
    items = result.get("items") if isinstance(result, Mapping) else None
    candidates: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, Mapping) or item.get("type") != "branch":
            continue
        point = _point(item.get("point"))
        if point is None:
            continue
        reviews = (
            item.get("reviews") if isinstance(item.get("reviews"), Mapping) else {}
        )
        rating, review_count = _review_stats(reviews)
        candidate = {
            "id": str(item.get("id") or "") or None,
            "name": str(item.get("name") or item.get("full_name") or "Фитнес-клуб"),
            "lat": point[0],
            "lon": point[1],
            "rating": rating,
            "review_count": review_count,
            "sauna": _attribute_has_sauna(item.get("attribute_groups")),
        }
        estimated_minutes = _distance_m(home, candidate) * 1.25 / 80
        candidate["estimated_score"] = fitness_score(
            estimated_minutes,
            candidate["rating"],
            candidate["review_count"],
            candidate["sauna"],
        )
        candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda item: (-float(item["estimated_score"]), _distance_m(home, item)),
    )


def prepare_park(
    address: str,
    api_key: str,
    *,
    home_point: Mapping[str, Any] | None,
    timeout: float = 45.0,
    fetch: Callable[[str, dict[str, Any], str, float], Any] = _default_fetch,
) -> ParkResult:
    address = str(address or "").strip()
    result = ParkResult(
        address,
        address_hash(address),
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    home = saved_point(home_point, "home")
    if not address or not api_key or home is None:
        result.error = "address, twogis_api_key and saved home coordinates are required"
        return result
    result.home_lat, result.home_lon = home["lat"], home["lon"]
    places = _call(
        "park_search",
        {"lat": home["lat"], "lon": home["lon"]},
        api_key,
        timeout,
        fetch,
        result.calls,
    )
    candidates = _park_candidates(places, home)
    if not candidates:
        result.error = "park not found within 2 km"
        return result
    place = candidates[0]
    result.place_id, result.place_name, result.place_type = (
        place["id"],
        place["name"],
        place["type"],
    )
    result.place_lat, result.place_lon = place["lat"], place["lon"]
    result.area_hectares = place["area_hectares"]
    result.quality = park_quality(result.area_hectares)
    return result


def prepare_fitness(
    address: str,
    api_key: str,
    *,
    home_point: Mapping[str, Any] | None,
    timeout: float = 45.0,
    fetch: Callable[[str, dict[str, Any], str, float], Any] = _default_fetch,
) -> FitnessResult:
    address = str(address or "").strip()
    result = FitnessResult(
        address,
        address_hash(address),
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    home = saved_point(home_point, "home")
    if not address or not api_key or home is None:
        result.error = "address, twogis_api_key and saved home coordinates are required"
        return result
    result.home_lat, result.home_lon = home["lat"], home["lon"]
    places = _call(
        "fitness_search",
        {"lat": home["lat"], "lon": home["lon"]},
        api_key,
        timeout,
        fetch,
        result.calls,
    )
    candidates = _fitness_candidates(places, home)
    if not candidates:
        result.error = "fitness club not found within 2 km"
        return result
    place = candidates[0]
    result.place_id, result.place_name = place["id"], place["name"]
    result.place_lat, result.place_lon = place["lat"], place["lon"]
    result.rating, result.review_count, result.sauna = (
        place["rating"],
        place["review_count"],
        place["sauna"],
    )
    result.quality = fitness_quality(result.rating, result.review_count)
    return result


def prepare_commute(
    address: str,
    destination: str,
    api_key: str,
    *,
    timeout: float = 45.0,
    now: datetime | None = None,
    home_point: Mapping[str, Any] | None = None,
    office_point: Mapping[str, Any] | None = None,
    fetch: Callable[[str, dict[str, Any], str, float], Any] = _default_fetch,
) -> CommuteResult:
    address = str(address or "").strip()
    destination = str(destination or "").strip()
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    service_day = next_tuesday(now)
    result = CommuteResult(
        address,
        destination,
        address_hash(address),
        captured_at,
        service_day.isoformat(),
    )
    if not address or not destination:
        result.error = "address and destination are required"
        return result

    home = saved_point(home_point, "home")
    if home is None:
        try:
            home = geocode_address(
                address, api_key, timeout=timeout, fetch=fetch, calls=result.calls
            )
        except ValueError as error:
            result.error = str(error)
            return result
    result.home_lat, result.home_lon = home["lat"], home["lon"]
    result.point_kind = str(home["point_kind"])
    result.building_id = home["building_id"]
    result.entrance_id = home["entrance_id"]
    result.geocode_precision = str(home["precision"])

    office = saved_point(office_point, "office")
    if office is None:
        try:
            office = geocode_address(
                destination, api_key, timeout=timeout, fetch=fetch, calls=result.calls
            )
        except ValueError as error:
            result.error = str(error)
            return result
    result.office_lat, result.office_lon = office["lat"], office["lon"]
    return result


def _route_value(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "provider": "yandex_maps",
        "service_date": payload.get("service_date"),
        "home_to_work": {
            "minutes": payload.get("home_to_work_minutes"),
            "score": payload.get("home_to_work_score"),
        },
        "work_to_home": {
            "minutes": payload.get("work_to_home_minutes"),
            "score": payload.get("work_to_home_score"),
        },
        "average_minutes": payload.get("average_minutes"),
        "average_score": payload.get("average_score"),
        "commute_score": payload.get("commute_score", 0),
        "gate_status": payload.get("gate_status", "unknown"),
        "coordinates": {
            "lat": payload.get("home_lat"),
            "lon": payload.get("home_lon"),
            "point_kind": payload.get("point_kind"),
            "building_id": payload.get("building_id"),
            "entrance_id": payload.get("entrance_id"),
            "precision": payload.get("geocode_precision"),
        },
    }


def apply_commute(
    facts: ListingFacts | dict[str, Any], result: CommuteResult | Mapping[str, Any]
) -> ListingFacts | dict[str, Any]:
    payload = result.to_payload() if isinstance(result, CommuteResult) else dict(result)
    status = (
        ValueStatus.CONFIRMED
        if payload.get("status") == "success"
        else ValueStatus.UNKNOWN
    )
    summary = {
        key: payload.get(key)
        for key in (
            "status",
            "service_date",
            "home_to_work_minutes",
            "work_to_home_minutes",
            "average_minutes",
            "average_score",
            "commute_score",
            "gate_status",
            "error",
        )
    }
    evidence = Evidence(
        "yandex_maps_browser",
        json.dumps(summary, ensure_ascii=False, sort_keys=True),
        str(payload.get("captured_at") or ""),
    )
    value = _route_value(payload)
    if isinstance(facts, ListingFacts):
        facts.fields["route"] = FieldValue(value, status, [evidence])
        facts.fields["route_minutes"] = FieldValue(
            payload.get("average_minutes"), status, [evidence]
        )
        return facts
    fields = facts.setdefault("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("facts.fields must be an object")
    evidence_payload = [
        {
            "source": evidence.source,
            "detail": evidence.detail,
            "captured_at": evidence.captured_at,
            "confidence": status.value,
        }
    ]
    fields["route"] = {
        "value": value,
        "status": status.value,
        "evidence": evidence_payload,
    }
    fields["route_minutes"] = {
        "value": payload.get("average_minutes"),
        "status": status.value,
        "evidence": evidence_payload,
    }
    return facts


def apply_park(
    facts: ListingFacts | dict[str, Any], result: ParkResult | Mapping[str, Any]
) -> ListingFacts | dict[str, Any]:
    payload = result.to_payload() if isinstance(result, ParkResult) else dict(result)
    status = (
        ValueStatus.CONFIRMED
        if payload.get("status") == "success"
        else ValueStatus.UNKNOWN
    )
    value = {
        "provider": "2gis",
        "route_provider": payload.get("route_provider", "yandex_maps"),
        "name": payload.get("place_name"),
        "place_id": payload.get("place_id"),
        "place_type": payload.get("place_type"),
        "coordinates": {
            "lat": payload.get("place_lat"),
            "lon": payload.get("place_lon"),
        },
        "area_hectares": payload.get("area_hectares"),
        "quality": payload.get("quality"),
        "walking_minutes": payload.get("walking_minutes"),
        "walking_distance_m": payload.get("walking_distance_m"),
        "score": payload.get("park_score", 0),
    }
    summary = {
        key: payload.get(key)
        for key in (
            "status",
            "place_name",
            "place_type",
            "area_hectares",
            "quality",
            "walking_minutes",
            "walking_distance_m",
            "park_score",
            "error",
        )
    }
    evidence = Evidence(
        "2gis_api+yandex_maps_browser",
        json.dumps(summary, ensure_ascii=False, sort_keys=True),
        str(payload.get("captured_at") or ""),
    )
    if isinstance(facts, ListingFacts):
        facts.fields["park"] = FieldValue(value, status, [evidence])
        return facts
    fields = facts.setdefault("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("facts.fields must be an object")
    fields["park"] = {
        "value": value,
        "status": status.value,
        "evidence": [
            {
                "source": evidence.source,
                "detail": evidence.detail,
                "captured_at": evidence.captured_at,
                "confidence": status.value,
            }
        ],
    }
    return facts


def apply_fitness(
    facts: ListingFacts | dict[str, Any], result: FitnessResult | Mapping[str, Any]
) -> ListingFacts | dict[str, Any]:
    payload = result.to_payload() if isinstance(result, FitnessResult) else dict(result)
    status = (
        ValueStatus.CONFIRMED
        if payload.get("status") == "success"
        else ValueStatus.UNKNOWN
    )
    value = {
        "provider": "2gis",
        "route_provider": payload.get("route_provider", "yandex_maps"),
        "name": payload.get("place_name"),
        "place_id": payload.get("place_id"),
        "coordinates": {
            "lat": payload.get("place_lat"),
            "lon": payload.get("place_lon"),
        },
        "rating": payload.get("rating"),
        "review_count": payload.get("review_count"),
        "sauna": bool(payload.get("sauna", False)),
        "quality": payload.get("quality"),
        "walking_minutes": payload.get("walking_minutes"),
        "walking_distance_m": payload.get("walking_distance_m"),
        "score": payload.get("fitness_score", 0),
    }
    summary = {
        key: payload.get(key)
        for key in (
            "status",
            "place_name",
            "rating",
            "review_count",
            "sauna",
            "quality",
            "walking_minutes",
            "walking_distance_m",
            "fitness_score",
            "error",
        )
    }
    evidence = Evidence(
        "2gis_api+yandex_maps_browser",
        json.dumps(summary, ensure_ascii=False, sort_keys=True),
        str(payload.get("captured_at") or ""),
    )
    if isinstance(facts, ListingFacts):
        facts.fields["fitness"] = FieldValue(value, status, [evidence])
        return facts
    fields = facts.setdefault("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("facts.fields must be an object")
    fields["fitness"] = {
        "value": value,
        "status": status.value,
        "evidence": [
            {
                "source": evidence.source,
                "detail": evidence.detail,
                "captured_at": evidence.captured_at,
                "confidence": status.value,
            }
        ],
    }
    return facts


__all__ = [
    "CommuteResult",
    "FitnessResult",
    "ParkResult",
    "address_hash",
    "apply_commute",
    "apply_fitness",
    "apply_location_point",
    "apply_park",
    "fitness_quality",
    "fitness_score",
    "geocode_address",
    "next_tuesday",
    "park_quality",
    "park_score",
    "prepare_commute",
    "prepare_fitness",
    "prepare_park",
    "saved_point",
]
