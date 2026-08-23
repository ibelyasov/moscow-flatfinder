"""Native Streamlit review dashboard for FlatFinder."""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

try:
    import streamlit as st
except ModuleNotFoundError:  # compile/import checks can run before dependencies install
    st = None  # type: ignore[assignment]

if __package__ in {None, ""}:  # streamlit run automation/flatfinder/admin.py
    root = str(Path(__file__).resolve().parents[1])
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)

try:
    from flatfinder.export import dashboard_payload, export_json
    from flatfinder.models import VISION_SCHEMA_VERSION
    from flatfinder.photos import is_allowed_photo_url
    from flatfinder.pipeline import _photo_cache_dir
    from flatfinder.sources import display_name as source_display_name
    from flatfinder.storage import (
        connect_db,
        migrate,
        set_listing_disliked,
        update_personal_score,
    )
except ModuleNotFoundError:  # pragma: no cover - package execution fallback
    from .export import dashboard_payload, export_json
    from .photos import is_allowed_photo_url
    from .pipeline import _photo_cache_dir
    from .sources import display_name as source_display_name
    from .storage import (
        connect_db,
        migrate,
        set_listing_disliked,
        update_personal_score,
    )


_DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.toml"
_METRO_DATA = Path(__file__).with_name("moscow_metro.json")
_STATUS = {
    "all": "Все статусы",
    "priority": "Приоритет",
    "good": "Хороший вариант",
    "reserve": "Запасной вариант",
    "skip": "Пропустить",
}
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")
_MAP_LAYER_ID = "apartments"
_MAP_COLOR_STOPS = (
    (220, 38, 38),
    (249, 115, 22),
    (234, 179, 8),
    (132, 204, 22),
    (22, 163, 74),
    (6, 182, 212),
    (37, 99, 235),
    (79, 70, 229),
    (147, 51, 234),
)
_MAP_STYLE = "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json"
_CRITERION_LABELS = {
    "noise": "Тишина",
    "park": "Парк и прогулки",
    "equipment": "Оснащение и мебель",
    "repair": "Ремонт",
    "price": "Полная стоимость",
    "commute": "Дорога",
    "area": "Площадь",
    "visual_layout": "Планировка по фото",
    "floor": "Этаж",
    "light_view": "Свет и вид",
    "building": "Год дома",
    "fitness": "Зал с сауной",
}
_SCORE_HELP = {
    "noise": "0–6 по ночной модели транспортного риска: достаточно одного близкого источника. Радиусы — 183 м для автодороги и 632 м для тяжёлой ЖД с OSM-поправками по классу; это screening, не расчёт дБ.",
    "park": "Ближайший парк берётся из данных об окружении объявления. До 10 минут пешком сохраняется максимум; затем балл плавно снижается до 0 к 25 минутам.",
    "equipment": "Кровать, кондиционер, посудомойка, холодильник и стиральная машина — по 3 за подтверждённое наличие, без бонуса за полный комплект.",
    "repair": "Фотооценка ремонта по выбранной Vision-модели; неполные фото расширяют диапазон.",
    "price": "Аренда + коммуналка + 1/12 комиссии. До 90 тыс. — 16; затем smoothstep плавно снижает оценку до 0 на 115 тыс.",
    "commute": "До 25 мин — 9; 30 — 7; 35 — 5; 40 — 2; 45 и больше или неизвестно — 0. Между точками — интерполяция.",
    "area": "Площадь оценивается отдельно от визуальной планировки.",
    "visual_layout": "Vision оценивает удобство и свободную циркуляцию по фото.",
    "floor": "Промежуточный этаж — 2; последний — 1; первый или неизвестный — 0.",
    "light_view": "Vision оценивает свет и вид по фотографиям; без подходящих фото — 0.",
    "building": "Только год постройки: 2020+ — 2; 2010-е — 1,5; 2000-е — 1; 1980–1999 — 0,5; раньше — 0; неизвестно — 1.",
    "personal": "Ваша оценка от 0 до 10.",
    "fitness": "Один поиск 2ГИС в радиусе 2 км. Качество по рейтингу и числу отзывов: обычный зал — до 2, хороший без сауны — до 4, хороший с явно указанной сауной — до 6. После 10 минут балл плавно снижается до 0 к 25 минутам.",
}
_GALLERY_CSS = """
<style>
.st-key-flatfinder-photo-strip[data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap;
    overflow-x: auto;
    padding-bottom: .5rem;
    scroll-snap-type: x proximity;
}
.st-key-flatfinder-photo-strip[data-testid="stHorizontalBlock"] > div {
    flex: 0 0 190px;
    scroll-snap-align: start;
}
.st-key-flatfinder-photo-strip [data-testid="stImage"] img {
    width: 190px;
    height: 130px;
    object-fit: cover;
    border-radius: .5rem;
}
.st-key-flatfinder-photo-viewer [data-testid="stImage"] {
    display: flex;
    justify-content: center;
}
.st-key-flatfinder-photo-viewer [data-testid="stImage"] img {
    width: auto !important;
    height: auto !important;
    max-width: 100% !important;
    max-height: calc(100dvh - 280px) !important;
    object-fit: contain !important;
}
[class*="st-key-flatfinder-map-"] {
    height: calc(100dvh - 390px) !important;
    min-height: calc(100dvh - 390px) !important;
}
[class*="st-key-flatfinder-map-"] > [data-testid="stFullScreenFrame"],
[class*="st-key-flatfinder-map-"] [data-testid="stDeckGlJsonChart"] {
    height: 100% !important;
}
.flatfinder-map-legend-slot {
    position: relative;
    height: 0;
    z-index: 5;
    pointer-events: none;
}
.flatfinder-map-legend {
    position: absolute;
    top: 12px;
    left: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 10px;
    color: #111827;
    background: rgba(255, 255, 255, .9);
    border: 1px solid rgba(17, 24, 39, .15);
    border-radius: 8px;
    box-shadow: 0 2px 8px rgba(17, 24, 39, .12);
    font-size: 12px;
    font-weight: 600;
    flex-wrap: wrap;
    max-width: min(760px, calc(100vw - 96px));
}
.flatfinder-map-legend-scale {
    width: 150px;
    height: 8px;
    border-radius: 999px;
}
</style>
"""


def _cfg(config: Any, name: str, default: Any = None) -> Any:
    return (
        config.get(name, default)
        if isinstance(config, Mapping)
        else getattr(config, name, default)
    )


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    return (
        list(value)
        if isinstance(value, (list, tuple))
        else ([] if value is None else [value])
    )


def _text(value: Any, default: str = "—") -> str:
    if value in (None, ""):
        return default
    if isinstance(value, Mapping):
        for key in ("detail", "text", "value", "name", "category"):
            if key in value and value[key] not in (None, ""):
                return _text(value[key], default)
        return default
    return str(value)


def _source_label(value: Any) -> str:
    return source_display_name(value if isinstance(value, str) else "")


def _texts(value: Any) -> list[str]:
    return [_text(item) for item in _items(value) if item not in (None, "")]


def _num(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _score(value: Any, default: str = "нет") -> str:
    result = _num(value)
    return f"{result:g}" if result is not None else default


def _minutes(value: Any) -> str:
    result = _num(value)
    return f"{result:.0f} мин" if result is not None else "—"


def _area(value: Any) -> str:
    result = _num(value)
    return f"{result:g} м²" if result is not None else "—"


def _money(value: Any) -> str:
    result = _num(value)
    if result is None:
        return _text(value, "не указана")
    rounded = math.floor(result / 1_000 + 0.5) * 1_000
    return f"{rounded:,} ₽".replace(",", " ")


def _repair_details(
    item: Mapping[str, Any], assessment: Mapping[str, Any]
) -> Mapping[str, Any]:
    details = _map(_map(assessment.get("repair")).get("details"))
    if details.get("status") in {"scoreable", "unknown"}:
        return details
    for proposal in reversed(_items(item.get("vision_proposals"))):
        proposal = _map(proposal)
        value = _map(proposal.get("value"))
        if (
            proposal.get("is_current")
            and proposal.get("criterion") == "owner_visual_assessment"
            and value.get("schema_version") == VISION_SCHEMA_VERSION
        ):
            return _map(value.get("repair"))
    return {}


def _date(value: Any) -> str:
    if not value:
        return "Дата не указана"
    parsed = _datetime(value)
    return parsed.strftime("%d.%m.%Y %H:%M") if parsed else str(value)


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(_MOSCOW_TZ)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    return (
        value.strip()
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc
        else None
    )


def _safe_local_image(value: Any, root: Path) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        candidate = Path(value).expanduser().resolve()
        cache_root = root.expanduser().resolve()
    except OSError:
        return None
    if (
        cache_root == candidate
        or cache_root not in candidate.parents
        or not candidate.is_file()
    ):
        return None
    return str(candidate)


def _safe_image_source(value: Any, config: Any) -> str | None:
    if is_allowed_photo_url(value):
        return str(value)
    return _safe_local_image(value, _photo_cache_dir(config))


def _photo_sources(config: Any, item: Mapping[str, Any]) -> list[str]:
    photos = [
        source
        for raw in _items(item.get("photo_urls"))
        if (source := _safe_image_source(raw, config))
    ]
    contact_sheet = _safe_image_source(item.get("contact_sheet"), config)
    return list(dict.fromkeys(photos or ([contact_sheet] if contact_sheet else [])))


def _photo_index(value: Any, count: int) -> int | None:
    try:
        index = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return index if str(index) == str(value) and 0 <= index < count else None


def _status(value: Any) -> str:
    value = str(value or "reserve").lower()
    return value if value in _STATUS and value != "all" else "reserve"


def _field_value(item: Mapping[str, Any], name: str, *aliases: str) -> Any:
    """Read a field from the canonical wrapped values without trusting its shape."""

    names = (name, *aliases)
    sources = (_map(item.get("field_values")), _map(item.get("facts")), item)
    for source in sources:
        for key in names:
            value = source.get(key)
            if value not in (None, ""):
                if (
                    isinstance(value, Mapping)
                    and "value" in value
                    and value.get("value") in (None, "")
                ):
                    continue
                return value
    return None


def _field_text(
    item: Mapping[str, Any], name: str, *aliases: str, default: str = "—"
) -> str:
    text = _text(_field_value(item, name, *aliases), default)
    number = _num(text)
    return _score(number) if number is not None else text


def _listing_heading(item: Mapping[str, Any]) -> str:
    address = _field_text(item, "address", "location", default="")
    address = address.removeprefix("Москва, ")
    metro = _field_text(item, "metro_station", "metro", default="")
    return " · ".join(
        part for part in (f"м. {metro}" if metro else "", address) if part
    ) or _title(item)


def _title(item: Mapping[str, Any]) -> str:
    facts = _map(item.get("facts"))
    return _text(
        item.get("title") or facts.get("title") or item.get("address"),
        f"Объявление {item.get('listing_id', '')}",
    )


def _filtered(
    payload: Mapping[str, Any],
    *,
    min_total: float | None,
    max_total: float | None,
    min_area: float | None,
    max_area: float | None,
    min_commute: float | None,
    max_commute: float | None,
    new_only: bool,
    show_basic: bool,
    show_hidden: bool,
    show_inactive: bool,
    inactive_since: datetime | None,
) -> list[Mapping[str, Any]]:
    def included(item: Mapping[str, Any]) -> bool:
        hidden = bool(item.get("disliked_at"))
        inactive = str(item.get("state") or "active") == "inactive"
        inactive_at = _datetime(item.get("inactive_at")) if inactive else None
        inactive_allowed = not inactive or (
            show_inactive
            and (
                inactive_since is None
                or (inactive_at is not None and inactive_at >= inactive_since)
            )
        )
        total = _num(item.get("estimated_monthly_total"))
        area = _num(item.get("area_m2"))
        commute = _num(item.get("average_commute_minutes"))
        return (
            (show_hidden or not hidden)
            and inactive_allowed
            and (show_basic or hidden or inactive)
            and (not new_only or bool(item.get("is_new")))
            and (min_total is None or (total is not None and total >= min_total))
            and (max_total is None or (total is not None and total <= max_total))
            and (min_area is None or (area is not None and area >= min_area))
            and (max_area is None or (area is not None and area <= max_area))
            and (
                min_commute is None or (commute is not None and commute >= min_commute)
            )
            and (
                max_commute is None or (commute is not None and commute <= max_commute)
            )
        )

    rows = [
        item
        for item in _items(payload.get("listings"))
        if isinstance(item, Mapping) and included(item)
    ]
    score = lambda item, name: _num(item.get(name), 0) or 0
    rows.sort(
        key=lambda item: (
            -score(item, "total_score"),
            -score(item, "auto_score"),
            str(item.get("listing_id", "")),
        )
    )
    return rows


def _map_coordinates(item: Mapping[str, Any]) -> tuple[float, float] | None:
    for point, lat_name, lon_name in (
        (_map(_field_value(item, "location_point")), "lat", "lon"),
        (_map(item.get("commute")), "home_lat", "home_lon"),
        (_map(item.get("fitness")), "home_lat", "home_lon"),
    ):
        lat, lon = _num(point.get(lat_name)), _num(point.get(lon_name))
        if (
            lat is not None
            and lon is not None
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        ):
            return lat, lon
    return None


def _map_color(value: Any) -> list[int]:
    score = min(100.0, max(0.0, _num(value, 0) or 0))
    position = score / 100 * (len(_MAP_COLOR_STOPS) - 1)
    left = min(int(position), len(_MAP_COLOR_STOPS) - 2)
    fraction = position - left
    color = [
        round(start + (end - start) * fraction)
        for start, end in zip(_MAP_COLOR_STOPS[left], _MAP_COLOR_STOPS[left + 1])
    ]
    return [*color, 220]


def _map_rows(listings: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in listings:
        point = _map_coordinates(item)
        if point is None:
            continue
        score = max(0.0, _num(item.get("total_score"), 0) or 0)
        rows.append(
            {
                "listing_id": int(item["listing_id"]),
                "lat": point[0],
                "lon": point[1],
                "address": _listing_heading(item),
                "price": _money(item.get("estimated_monthly_total")),
                "area": _area(item.get("area_m2")),
                "commute": _minutes(item.get("average_commute_minutes")),
                "score": round(score, 1),
            }
        )
    if rows:
        low, high = min(row["score"] for row in rows), max(row["score"] for row in rows)
        for row in rows:
            row["color"] = _map_color(
                50 if low == high else (row["score"] - low) / (high - low) * 100
            )
        groups: dict[tuple[float, float], list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault((row["lat"], row["lon"]), []).append(row)
        for group in groups.values():
            for index, row in enumerate(group):
                angle = 2 * math.pi * index / len(group)
                radius = 18 if len(group) > 1 else 0
                row["pixel_offset"] = [
                    round(radius * math.cos(angle)),
                    round(radius * math.sin(angle)),
                ]
    return rows


def _metro_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw_line in _items(payload.get("lines")):
        line = _map(raw_line)
        hex_color = str(line.get("hex_color") or "0078D4").removeprefix("#")
        try:
            color = (
                [*bytes.fromhex(hex_color), 235]
                if len(hex_color) == 6
                else [0, 120, 212, 235]
            )
        except ValueError:
            color = [0, 120, 212, 235]
        for raw_station in _items(line.get("stations")):
            station = _map(raw_station)
            lat, lon = _num(station.get("lat")), _num(station.get("lng"))
            if (
                lat is None
                or lon is None
                or not (-90 <= lat <= 90 and -180 <= lon <= 180)
            ):
                continue
            rows.append(
                {
                    "station_id": str(station.get("id") or ""),
                    "name": _text(station.get("name"), "Метро"),
                    "line": _text(line.get("name"), ""),
                    "lat": lat,
                    "lon": lon,
                    "color": color,
                }
            )
    return rows


def _metro_display_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["name"], []).append(row)
    markers, labels = [], []
    for name, group in groups.items():
        lat = sum(row["lat"] for row in group) / len(group)
        lon = sum(row["lon"] for row in group) / len(group)
        lines = list({row["line"]: row for row in group}.values())
        for index, row in enumerate(lines):
            markers.append(
                {
                    **row,
                    "lat": lat,
                    "lon": lon,
                    "pixel_offset": [round((index - (len(lines) - 1) / 2) * 16), 0],
                    "symbol": "М",
                }
            )
        suffix = "линии" if 2 <= len(lines) <= 4 else "линий"
        labels.append(
            {
                "name": name if len(lines) == 1 else f"{name} · {len(lines)} {suffix}",
                "lat": lat,
                "lon": lon,
            }
        )
    return markers, labels


def _map_legend(rows: list[Mapping[str, Any]]) -> str:
    low, high = min(row["score"] for row in rows), max(row["score"] for row in rows)
    gradient = ", ".join(
        f"rgb({red}, {green}, {blue})" for red, green, blue in _MAP_COLOR_STOPS
    )
    return (
        '<div class="flatfinder-map-legend-slot"><div class="flatfinder-map-legend" '
        'title="Относительная оценка среди показанных квартир">'
        f'<span>{low:g}</span><span class="flatfinder-map-legend-scale" style="background:linear-gradient(90deg,{gradient})"></span>'
        f"<span>{high:g}</span></div></div>"
    )


def _map_key(rows: list[Mapping[str, Any]]) -> str:
    return "flatfinder-map-" + "-".join(
        str(row["listing_id"])
        for row in sorted(rows, key=lambda row: int(row["listing_id"]))
    )


def _office_coordinates(
    listings: list[Mapping[str, Any]],
) -> tuple[float, float] | None:
    points = {
        (lat, lon)
        for item in listings
        for commute in [_map(item.get("commute"))]
        if (lat := _num(commute.get("office_lat"))) is not None
        and (lon := _num(commute.get("office_lon"))) is not None
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    }
    return next(iter(points)) if len(points) == 1 else None


def _selected_map_listing_id(event: Any) -> int | None:
    selection = (
        event.get("selection")
        if isinstance(event, Mapping)
        else getattr(event, "selection", None)
    )
    objects = _map(_map(selection).get("objects"))
    selected = _items(objects.get(_MAP_LAYER_ID))
    listing_id = _num(_map(selected[0]).get("listing_id")) if selected else None
    return (
        int(listing_id) if listing_id is not None and listing_id.is_integer() else None
    )


def _render_map(listings: list[Mapping[str, Any]]) -> None:
    import pydeck as pdk

    rows = _map_rows(listings)
    if not rows:
        st.info("У выбранных квартир пока нет координат.")
        return
    points = {(row["lon"], row["lat"]) for row in rows}
    office = _office_coordinates(listings)
    if office is not None:
        points.add((office[1], office[0]))
    if len(points) == 1:
        lon, lat = next(iter(points))
        view = pdk.ViewState(longitude=lon, latitude=lat, zoom=13, pitch=0, bearing=0)
    else:
        view = pdk.data_utils.compute_view([list(point) for point in points])
        view.zoom = min(14, (_num(view.zoom, 11) or 11) + 2)
        view.pitch = 0
        view.bearing = 0
    st.html(_map_legend(rows))
    try:
        metro = _metro_rows(json.loads(_METRO_DATA.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        metro = []
    metro_markers, metro_labels = _metro_display_rows(metro)
    layers = [
        pdk.Layer(
            "TextLayer",
            data=metro_markers,
            id="metro-symbols",
            get_position="[lon, lat]",
            get_pixel_offset="pixel_offset",
            get_text="symbol",
            get_color="color",
            get_size=60,
            size_units="'meters'",
            size_min_pixels=12,
            size_max_pixels=14,
            get_text_anchor="'middle'",
            get_alignment_baseline="'center'",
            get_content_box=[-20, -20, 40, 40],
            content_cutoff_pixels=[4, 4],
            character_set="'auto'",
            font_family="'Arial, sans-serif'",
            font_weight="'bold'",
            font_settings={"sdf": True},
            outline_width=0.12,
            outline_color=[255, 255, 255, 245],
            pickable=False,
        ),
        pdk.Layer(
            "TextLayer",
            data=metro_labels,
            id="metro-labels",
            get_position="[lon, lat]",
            get_text="name",
            get_color=[31, 41, 55, 230],
            get_size=45,
            size_units="'meters'",
            size_min_pixels=13,
            size_max_pixels=14,
            get_pixel_offset=[0, -13],
            get_text_anchor="'middle'",
            get_alignment_baseline="'bottom'",
            get_content_box=[-300, -70, 600, 140],
            content_cutoff_pixels=[60, 14],
            character_set="'auto'",
            font_family="'Arial, sans-serif'",
            font_settings={"sdf": True},
            outline_width=0.18,
            outline_color=[255, 255, 255, 245],
            pickable=False,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            data=rows,
            id=_MAP_LAYER_ID,
            get_position="[lon, lat]",
            get_pixel_offset="pixel_offset",
            get_fill_color="color",
            get_radius=90,
            radius_min_pixels=7,
            radius_max_pixels=18,
            pickable=True,
            auto_highlight=True,
            highlight_color=[255, 255, 255, 240],
            stroked=True,
            get_line_color=[255, 255, 255, 180],
            line_width_min_pixels=1,
        ),
    ]
    if office is not None:
        office_data = [{"lat": office[0], "lon": office[1], "label": "Офис"}]
        layers.extend(
            (
                pdk.Layer(
                    "ScatterplotLayer",
                    data=office_data,
                    id="office-halo",
                    get_position="[lon, lat]",
                    get_fill_color=[99, 102, 241, 55],
                    get_radius=270,
                    radius_min_pixels=14,
                    radius_max_pixels=28,
                    pickable=False,
                ),
                pdk.Layer(
                    "ScatterplotLayer",
                    data=office_data,
                    id="office",
                    get_position="[lon, lat]",
                    get_fill_color=[79, 70, 229, 255],
                    get_radius=120,
                    radius_min_pixels=9,
                    radius_max_pixels=17,
                    pickable=False,
                    stroked=True,
                    get_line_color=[255, 255, 255, 255],
                    line_width_min_pixels=3,
                ),
                pdk.Layer(
                    "TextLayer",
                    data=office_data,
                    id="office-label",
                    get_position="[lon, lat]",
                    get_text="label",
                    get_color=[49, 46, 129, 255],
                    get_size=15,
                    get_pixel_offset=[0, -31],
                    get_text_anchor="'middle'",
                    get_alignment_baseline="'bottom'",
                    background=True,
                    get_background_color=[255, 255, 255, 245],
                    background_padding=[8, 5],
                    background_border_radius=6,
                    character_set="'auto'",
                    font_family="'Arial, sans-serif'",
                    font_weight="'bold'",
                ),
            )
        )
    event = st.pydeck_chart(
        pdk.Deck(
            map_style=_MAP_STYLE,
            initial_view_state=view,
            layers=layers,
            tooltip={
                "text": "{address}\n{price} · {area}\nДо офиса: {commute}\nОценка: {score}"
            },
        ),
        width="stretch",
        height=650,
        on_select="rerun",
        selection_mode="single-object",
        key=_map_key(rows),
    )
    selected_id = _selected_map_listing_id(event)
    if selected_id is not None:
        st.query_params["listing_id"] = selected_id
        st.rerun()


def _rows(listings: list[Mapping[str, Any]], base_url: str) -> list[dict[str, Any]]:
    return [
        {
            "ID": int(item["listing_id"]),
            "Объявление": _listing_heading(item),
            "Открыть": f"{base_url.rstrip('/')}/?listing_id={int(item['listing_id'])}",
            "Площадь": _area(item.get("area_m2")),
            "Оригинал": _safe_http_url(item.get("source_url")),
            "Полная стоимость": _money(item.get("estimated_monthly_total")),
            "Итог": _num(item.get("total_score")),
            "Допуск": {
                "eligible": "Подходит",
                "needs_review": "Нужно проверить",
                "rejected": "Не подходит",
            }.get(str(item.get("eligibility_status")), "—"),
            "Среднее время": _minutes(item.get("average_commute_minutes")),
            "Дата": _datetime(item.get("captured_at")),
        }
        for item in listings
    ]


def _read_payload(
    database: str,
    listing_id: int | None = None,
    max_scores_json: str = "{}",
    scoring_parameters_json: str = "{}",
    vision_contract_json: str = "[]",
) -> dict[str, Any]:
    conn = connect_db(database)
    try:
        migrate(conn)
        max_scores = json.loads(max_scores_json)
        scoring_parameters = json.loads(scoring_parameters_json)
        raw_contract = json.loads(vision_contract_json)
        vision_contract = tuple(raw_contract) if len(raw_contract) == 4 else None
        return dashboard_payload(
            conn,
            listing_id=listing_id,
            include_inactive=True,
            max_scores=max_scores,
            scoring_parameters=scoring_parameters,
            vision_contract=vision_contract,
        )
    finally:
        conn.close()


_cached_payload = (
    st.cache_data(ttl=30, show_spinner=False)(_read_payload)
    if st is not None
    else _read_payload
)


def _load_config(path: Path) -> Any:
    try:
        from flatfinder.cli import load_config
    except ModuleNotFoundError:  # pragma: no cover
        from .cli import load_config
    return load_config(path)


def _write_personal_score(config: Any, listing_id: int, score: float) -> list[str]:
    conn = connect_db(_cfg(config, "database"))
    try:
        migrate(conn)
        update_personal_score(
            conn,
            listing_id,
            score,
            max_scores=_cfg(config, "scoring_max_scores", {}),
        )
        try:
            export_json(
                conn,
                _cfg(config, "json_export"),
                max_scores=_cfg(config, "scoring_max_scores", {}),
                scoring_parameters=_cfg(config, "scoring_parameters", {}),
                vision_contract=_cfg(config, "vision_contract"),
            )
        except Exception as exc:
            return [f"JSON-экспорт: {str(exc)[:500] or exc.__class__.__name__}"]
        return []
    finally:
        conn.close()


def _write_disliked(config: Any, listing_id: int, disliked: bool) -> list[str]:
    conn = connect_db(_cfg(config, "database"))
    try:
        migrate(conn)
        set_listing_disliked(conn, listing_id, disliked)
        try:
            export_json(
                conn,
                _cfg(config, "json_export"),
                max_scores=_cfg(config, "scoring_max_scores", {}),
                scoring_parameters=_cfg(config, "scoring_parameters", {}),
                vision_contract=_cfg(config, "vision_contract"),
            )
        except Exception as exc:
            return [f"JSON-экспорт: {str(exc)[:500] or exc.__class__.__name__}"]
        return []
    finally:
        conn.close()


def _render_listing_summary(
    item: Mapping[str, Any],
    total_max: int,
    park: Mapping[str, Any],
    fitness: Mapping[str, Any],
    criteria: Mapping[str, Any],
) -> None:
    with st.container(border=True, key="flatfinder-listing-summary"):
        heading, actions = st.columns([6, 4], vertical_alignment="center")
        with heading:
            st.subheader(_listing_heading(item))
            st.caption(
                f"ID объявления {item.get('listing_id')} · Обновлено {_date(item.get('last_seen_at'))}"
            )
        with actions.container(
            horizontal=True, horizontal_alignment="right", vertical_alignment="center"
        ):
            if item.get("state") == "inactive":
                st.badge("Пропала из фильтра", color="red")
            else:
                status = _status(item.get("status"))
                st.badge(
                    _STATUS.get(status, status),
                    color={
                        "priority": "green",
                        "good": "blue",
                        "reserve": "gray",
                        "skip": "red",
                    }.get(status, "gray"),
                )
            eligibility = str(item.get("eligibility_status", "eligible"))
            st.badge(
                {
                    "eligible": "Ограничения пройдены",
                    "needs_review": "Нужно проверить ограничения",
                    "rejected": "Нарушено обязательное условие",
                }.get(eligibility, eligibility),
                color={
                    "eligible": "green",
                    "needs_review": "orange",
                    "rejected": "red",
                }.get(eligibility, "gray"),
            )
            source_offers = _items(item.get("source_offers")) or [item]
            valid_sources = [
                (
                    _text(_map(offer).get("source"), "Источник"),
                    _safe_http_url(_map(offer).get("source_url")),
                )
                for offer in source_offers
                if _safe_http_url(_map(offer).get("source_url"))
            ]
            for source, source_url in valid_sources:
                label = _source_label(source)
                st.link_button(label, source_url, width=110)
            if not valid_sources and item.get("source_url"):
                st.caption("Источник недоступен")

        st.divider()
        listing_area = _field_text(item, "area_m2", "area", default="—")
        summary_cols = st.columns(4)
        for col, label, value in zip(
            summary_cols,
            ("Итог", "Полная стоимость", "Площадь", "Среднее время"),
            (
                f"{_score(item.get('total_score'), '0')}/{total_max}",
                _money(item.get("estimated_monthly_total")),
                f"{listing_area} м²" if listing_area != "—" else listing_area,
                _minutes(item.get("average_commute_minutes")),
            ),
        ):
            col.metric(label, value)

        if park.get("status") == "success" or fitness.get("status") == "success":
            st.markdown("**Рядом с домом**")
            park_column, fitness_column = st.columns(2)
            if park.get("status") == "success":
                with park_column.container(border=True):
                    with st.container(
                        horizontal=True,
                        horizontal_alignment="distribute",
                        vertical_alignment="center",
                    ):
                        st.markdown("**Парк и прогулки**")
                        st.badge(
                            f"{_score(_map(_map(item.get('assessment')).get('park')).get('score'), '0')}/{_score(_map(criteria.get('park')).get('max'), '0')}",
                            color="blue",
                        )
                    name = _text(park.get("place_name"), "Прогулочная зона")
                    minutes = _minutes(park.get("walking_minutes"))
                    st.caption(f"{name} · {minutes}")
            if fitness.get("status") == "success":
                with fitness_column.container(border=True):
                    with st.container(
                        horizontal=True,
                        horizontal_alignment="distribute",
                        vertical_alignment="center",
                    ):
                        st.markdown("**Фитнес**")
                        st.badge(
                            f"{_score(_map(_map(item.get('assessment')).get('fitness')).get('score'), '0')}/{_score(_map(criteria.get('fitness')).get('max'), '0')}",
                            color="green",
                        )
                    name = _text(fitness.get("place_name"), "Фитнес-клуб")
                    minutes = _minutes(fitness.get("walking_minutes"))
                    rating = _num(fitness.get("rating"))
                    reviews = fitness.get("review_count")
                    sauna = " · сауна" if fitness.get("sauna") else ""
                    rating_text = (
                        f" · рейтинг {rating:.1f} ({reviews})"
                        if rating is not None and reviews is not None
                        else ""
                    )
                    st.caption(f"{name} · {minutes}{rating_text}{sauna}")


def _render_photo_gallery(config: Any, item: Mapping[str, Any]) -> None:
    photos = _photo_sources(config, item)
    with st.container(border=True, key="flatfinder-photo-gallery"):
        with st.container(
            horizontal=True,
            horizontal_alignment="distribute",
            vertical_alignment="center",
        ):
            st.subheader("Фотографии")
            st.badge(f"{len(photos)} фото", color="gray")
        if photos:
            with st.container(
                key="flatfinder-photo-strip", horizontal=True, gap="small"
            ):
                for index, source in enumerate(photos):
                    with st.container(width=190):
                        st.image(
                            source,
                            caption=str(index + 1),
                            width=190,
                            link=f"?listing_id={int(item['listing_id'])}&photo={index}",
                        )
            st.html(
                """
                <script>
                (() => {
                    const page = window.parent;
                    if (page.flatfinderPhotoLinks) {
                        page.document.removeEventListener("click", page.flatfinderPhotoLinks, true);
                    }
                    page.flatfinderPhotoLinks = (event) => {
                        const link = event.target.closest?.('.st-key-flatfinder-photo-strip a[href*="photo="]');
                        if (!link) return;
                        event.preventDefault();
                        event.stopPropagation();
                        page.location.assign(link.href);
                    };
                    page.document.addEventListener("click", page.flatfinderPhotoLinks, true);
                })();
                </script>
                """,
                unsafe_allow_javascript=True,
            )
        else:
            st.info("Фото не зафиксированы.")


def _render_decision_controls(
    item: Mapping[str, Any], total_max: int, personal_max: float
) -> tuple[float, bool, bool, bool]:
    current_score = min(
        personal_max, max(0.0, _num(item.get("personal_score"), 0) or 0.0)
    )
    with st.container(border=True, key="flatfinder-decision-card"):
        choice, preview = st.columns([4, 1.2], gap="large", vertical_alignment="center")
        with choice:
            st.subheader("Моё решение")
            if personal_max > 0:
                st.caption("Добавьте личные баллы к автоматической оценке квартиры.")
                personal_score = st.slider(
                    "Насколько хочется здесь жить?",
                    min_value=0.0,
                    max_value=personal_max,
                    value=current_score,
                    step=0.5,
                    key=f"personal-score-{item['listing_id']}",
                    label_visibility="collapsed",
                    width="stretch",
                )
            else:
                personal_score = 0.0
                st.caption("Личный критерий отключён в конфиге.")
        with preview:
            auto_score = _num(item.get("auto_score"), 0) or 0
            st.metric(
                "Итог после оценки",
                f"{_score(auto_score + personal_score, '0')}/{total_max}",
                delta=f"+{personal_score}" if personal_score else None,
                border=True,
            )

        save_column, dislike_column = st.columns([3, 1])
        with save_column:
            save_score = st.button(
                "Сохранить решение",
                key=f"save-score-{item['listing_id']}",
                type="primary",
                width="stretch",
            )
        disliked = bool(item.get("disliked_at"))
        with dislike_column:
            dislike_clicked = st.button(
                "Вернуть в список" if disliked else "Не подходит",
                key=f"dislike-{item['listing_id']}",
                width="stretch",
            )
    return personal_score, save_score, dislike_clicked, disliked


def _render_score_details(
    item: Mapping[str, Any],
    assessment: Mapping[str, Any],
    auto_criteria: list[tuple[str, Mapping[str, Any]]],
) -> None:
    st.subheader("Детализация оценки")
    criteria_by_key = dict(auto_criteria)
    score_groups = (
        (
            "Квартира",
            (
                "equipment",
                "repair",
                "area",
                "visual_layout",
                "floor",
                "light_view",
                "building",
            ),
        ),
        ("Окружение", ("noise", "park", "fitness")),
        ("Цена и логистика", ("price", "commute")),
    )
    for column, (group_label, keys) in zip(st.columns(3), score_groups, strict=True):
        with column.container(border=True):
            st.markdown(f"**{group_label}**")
            for key in keys:
                criterion = _map(criteria_by_key.get(key))
                detail = _map(assessment.get(key))
                label = (
                    _text(criterion.get("label"), "")
                    or _CRITERION_LABELS.get(key)
                    or key.replace("_", " ").strip().capitalize()
                )
                score_value = _num(detail.get("score"), 0) or 0
                max_value = _num(criterion.get("max"), 0) or 0
                confidence = str(detail.get("confidence", "unknown"))
                unknown = confidence in {"unknown", "partial", "absent"}
                missing_label = (
                    "неполные данные" if confidence == "partial" else "нет данных"
                )
                ratio = (
                    score_value / max_value if score_value > 0 and max_value > 0 else 0
                )
                color = (
                    "gray"
                    if ratio == 0
                    else "orange"
                    if ratio < 0.5
                    else "blue"
                    if ratio < 0.8
                    else "green"
                )
                st.badge(
                    f"{label} — {missing_label if unknown else _score(score_value, '0') + '/' + _score(max_value, '0')}",
                    color=color,
                    help=_SCORE_HELP.get(key),
                    width="stretch",
                )

    repair = _repair_details(item, assessment)
    if repair:
        scoreable = repair.get("status") == "scoreable"
        score_range = _items(repair.get("interval"))
        title = (
            f"Ремонт — {_score(repair.get('score'))}/16"
            if scoreable
            else f"Ремонт — нет данных · диапазон {_score(score_range[0] if score_range else None)}–{_score(score_range[1] if len(score_range) > 1 else None)}"
        )
        with st.expander(title, expanded=not scoreable):
            if not scoreable:
                st.warning(
                    "Репрезентативных фото интерьера недостаточно; результат не считается как 0/16."
                )
            st.markdown(_text(repair.get("summary"), "Нет пояснения"))
            worst = _text(repair.get("worst_zone"), "")
            if worst:
                st.caption(f"Худшая видимая зона: {worst}")
            unknowns = _texts(repair.get("unknowns"))
            if unknowns:
                st.caption("Неизвестно: " + "; ".join(unknowns))
            indices = [
                index
                for index in _items(repair.get("evidence_indices"))
                if isinstance(index, int) and not isinstance(index, bool)
            ]
            if indices:
                st.markdown(
                    "Опорные фото: "
                    + ", ".join(
                        f"[фото {index + 1}](?listing_id={int(item.get('listing_id'))}&photo={index})"
                        for index in indices
                    )
                )


def _render_listing(
    config: Any, payload: Mapping[str, Any], item: Mapping[str, Any]
) -> None:
    assessment = _map(item.get("assessment"))
    rubric = _map(payload.get("rubric"))
    total_max = int(_num(rubric.get("total_max"), 100) or 100)
    criteria = _map(rubric.get("criteria")) or rubric
    auto_criteria = [
        (key, _map(criterion))
        for key, criterion in criteria.items()
        if key
        not in {
            "version",
            "automatic_max",
            "personal_max",
            "total_max",
            "criteria",
            "personal",
        }
    ]
    park = _map(item.get("park"))
    fitness = _map(item.get("fitness"))
    _render_listing_summary(item, total_max, park, fitness, criteria)
    _render_photo_gallery(config, item)
    personal_score, save_score, dislike_clicked, disliked = _render_decision_controls(
        item, total_max, float(_num(rubric.get("personal_max"), 0) or 0)
    )
    if save_score:
        try:
            errors = _write_personal_score(
                config, int(item["listing_id"]), float(personal_score)
            )
        except Exception as exc:
            st.error(str(exc)[:500] or exc.__class__.__name__)
        else:
            st.cache_data.clear()
            text = f"Личная оценка {personal_score} сохранена."
            st.session_state["_flatfinder_notice"] = (
                text + (" " + "; ".join(errors) if errors else ""),
                bool(errors),
            )
            st.rerun()

    if dislike_clicked:
        try:
            errors = _write_disliked(config, int(item["listing_id"]), not disliked)
        except Exception as exc:
            st.error(str(exc)[:500] or exc.__class__.__name__)
        else:
            st.cache_data.clear()
            text = "Квартира возвращена в список." if disliked else "Квартира скрыта."
            st.session_state["_flatfinder_notice"] = (
                text + (" " + "; ".join(errors) if errors else ""),
                bool(errors),
            )
            st.rerun()

    _render_score_details(item, assessment, auto_criteria)


def _dismiss_photo() -> None:
    if st is not None and "photo" in st.query_params:
        del st.query_params["photo"]


if st is not None:

    @st.dialog("Фото квартиры", width="large", on_dismiss=_dismiss_photo)
    def _photo_dialog(config: Any, item: Mapping[str, Any], index: int) -> None:
        photos = _photo_sources(config, item)
        if not photos or not 0 <= index < len(photos):
            st.warning("Фото не найдено.")
            return
        with st.container(key="flatfinder-photo-viewer"):
            st.image(
                photos[index],
                caption=f"Фото {index + 1} из {len(photos)}",
                width="stretch",
            )
        previous, counter, following = st.columns(
            [2, 3, 2], vertical_alignment="center"
        )
        if previous.button("← Предыдущее", disabled=index == 0, width="stretch"):
            st.query_params["photo"] = index - 1
            st.rerun()
        counter.markdown(
            f"<div style='text-align:center'>{index + 1} из {len(photos)}</div>",
            unsafe_allow_html=True,
        )
        if following.button(
            "Следующее →", disabled=index == len(photos) - 1, width="stretch"
        ):
            st.query_params["photo"] = index + 1
            st.rerun()
        if st.button("Вернуться к деталям", width="stretch"):
            del st.query_params["photo"]
            st.rerun()
        st.caption("Листать фото: клавиши ← и →")
        st.html(
            f"""
            <script>
            (() => {{
                if (window.flatfinderPhotoKeys) {{
                    document.removeEventListener("keydown", window.flatfinderPhotoKeys);
                }}
                window.flatfinderPhotoKeys = (event) => {{
                    if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)) return;
                    const url = new URL(window.location.href);
                    if (url.searchParams.get("photo") !== "{index}") return;
                    let next = null;
                    if (event.key === "ArrowLeft" && {index} > 0) next = {index - 1};
                    if (event.key === "ArrowRight" && {index} < {len(photos) - 1}) next = {index + 1};
                    if (next === null) return;
                    event.preventDefault();
                    url.searchParams.set("photo", String(next));
                    window.location.assign(url);
                }};
                document.addEventListener("keydown", window.flatfinderPhotoKeys);
            }})();
            </script>
            """,
            unsafe_allow_javascript=True,
        )

else:  # pragma: no cover

    def _photo_dialog(config: Any, item: Mapping[str, Any], index: int) -> None:
        raise RuntimeError("Streamlit is required")


def main() -> None:
    if st is None:  # pragma: no cover
        raise RuntimeError("Streamlit is required; install project dependencies first")
    st.set_page_config(page_title="FlatFinder", page_icon="🏠", layout="wide")
    st.html(_GALLERY_CSS)
    if os.environ.get("FLATFINDER_ADMIN_LOCKED") != "1":
        st.error(
            "Откройте интерфейс через `flatfinder review`; прямой запуск Streamlit отключён."
        )
        st.stop()
    notice = st.session_state.pop("_flatfinder_notice", None)
    if notice:
        (st.warning if notice[1] else st.success)(notice[0])
    config_path = Path(
        os.environ.get("FLATFINDER_CONFIG", str(_DEFAULT_CONFIG))
    ).expanduser()
    try:
        from flatfinder.cli import _review_listing_id as parse_listing_id

        listing_id = parse_listing_id(os.environ.get("FLATFINDER_LISTING_ID"))
        detail_listing_id = parse_listing_id(st.query_params.get("listing_id"))
        requested_id = listing_id if listing_id is not None else detail_listing_id
        config = _load_config(config_path)
        payload = _cached_payload(
            str(_cfg(config, "database")),
            requested_id,
            json.dumps(_cfg(config, "scoring_max_scores", {}), sort_keys=True),
            json.dumps(_cfg(config, "scoring_parameters", {}), sort_keys=True),
            json.dumps(_cfg(config, "vision_contract", ())),
        )
    except Exception as exc:
        st.error(
            f"Не удалось загрузить FlatFinder: {str(exc)[:500] or exc.__class__.__name__}"
        )
        st.stop()
    listings = [
        item for item in _items(payload.get("listings")) if isinstance(item, Mapping)
    ]
    if not listings:
        st.title("Квартиры к просмотру")
        st.error(
            f"Объявление с ID {requested_id} не найдено."
        ) if requested_id is not None else st.info("В базе пока нет объявлений.")
        return
    if requested_id is not None:
        item = listings[0]
        if (
            listing_id is None
            and detail_listing_id is not None
            and st.button("← Назад к результатам", type="tertiary")
        ):
            for name in ("listing_id", "photo"):
                if name in st.query_params:
                    del st.query_params[name]
            st.rerun()
        selected_photo = _photo_index(
            st.query_params.get("photo"), len(_photo_sources(config, item))
        )
        if selected_photo is not None:
            _photo_dialog(config, item, selected_photo)
        else:
            _render_listing(config, payload, item)
        return
    st.sidebar.header("Фильтры")
    st.sidebar.caption("Сумма, ₽")
    total_from, total_to = st.sidebar.columns(2)
    min_total = total_from.number_input(
        "От", min_value=0, value=None, step=5_000, placeholder="Любая", key="min-total"
    )
    max_total = total_to.number_input(
        "До", min_value=0, value=None, step=5_000, placeholder="Любая", key="max-total"
    )
    st.sidebar.caption("Площадь, м²")
    area_from, area_to = st.sidebar.columns(2)
    min_area = area_from.number_input(
        "От", min_value=0.0, value=None, step=1.0, placeholder="Любая", key="min-area"
    )
    max_area = area_to.number_input(
        "До", min_value=0.0, value=None, step=1.0, placeholder="Любая", key="max-area"
    )
    st.sidebar.caption("Время в дороге, мин")
    commute_from, commute_to = st.sidebar.columns(2)
    min_commute = commute_from.number_input(
        "От",
        min_value=0.0,
        value=None,
        step=1.0,
        placeholder="Любое",
        key="min-commute",
    )
    max_commute = commute_to.number_input(
        "До",
        min_value=0.0,
        value=None,
        step=1.0,
        placeholder="Любое",
        key="max-commute",
    )
    new_only = st.sidebar.checkbox("Только новые")
    show_hidden = st.sidebar.checkbox("Показывать скрытые")
    inactive_scope = st.sidebar.selectbox(
        "Пропавшие из фильтра",
        ("Не показывать", "За последние 24 часа", "Все"),
    )
    show_inactive = inactive_scope != "Не показывать"
    inactive_since = (
        datetime.now(timezone.utc) - timedelta(days=1)
        if inactive_scope == "За последние 24 часа"
        else None
    )
    show_basic = st.sidebar.checkbox(
        "Показывать базовые",
        value=True,
        help="Активные объявления, которые не были скрыты.",
    )
    filtered = _filtered(
        payload,
        min_total=min_total,
        max_total=max_total,
        min_area=min_area,
        max_area=max_area,
        min_commute=min_commute,
        max_commute=max_commute,
        new_only=new_only,
        show_basic=show_basic,
        show_hidden=show_hidden,
        show_inactive=show_inactive,
        inactive_since=inactive_since,
    )
    visible = filtered
    st.title("Квартиры к просмотру")
    st.caption(f"Выпуск: {_date(payload.get('updated_at'))}")
    cols = st.columns(4)
    for col, label, value in zip(
        cols,
        ("Показано", "Приоритет", "Новые", "Источников"),
        (
            len(filtered),
            sum(_status(i.get("status")) == "priority" for i in visible),
            sum(bool(i.get("is_new")) for i in visible),
            len({i.get("source") for i in visible if i.get("source")}),
        ),
    ):
        col.metric(label, value)
    if not filtered:
        st.warning("Фильтр ничего не нашёл.")
        return
    base_url = str(st.context.url)
    view = st.segmented_control(
        "Представление",
        ("Список", "Карта"),
        default=st.session_state.get("_flatfinder_view", "Список"),
        key="flatfinder-view",
        label_visibility="collapsed",
    )
    st.session_state["_flatfinder_view"] = view
    if view == "Карта":
        _render_map(filtered)
        return
    table = _rows(filtered, base_url)
    table_config = {
        "ID": None,
        "Объявление": st.column_config.TextColumn(width="large"),
        "Открыть": st.column_config.LinkColumn(display_text="Открыть ↗", width="small"),
        "Площадь": st.column_config.TextColumn(width="small"),
        "Оригинал": st.column_config.LinkColumn(display_text="Открыть", width="small"),
        "Полная стоимость": st.column_config.TextColumn(width="small"),
        "Итог": st.column_config.NumberColumn(width="small"),
        "Допуск": st.column_config.TextColumn(width="medium"),
        "Среднее время": st.column_config.TextColumn(width="small"),
        "Дата": st.column_config.DatetimeColumn(
            format="DD.MM.YYYY HH:mm", width="medium"
        ),
    }
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        height="content",
        column_config=table_config,
    )
    st.caption("«Открыть» показывает карточку объявления в новой вкладке.")


if __name__ == "__main__":
    main()


__all__ = ["main"]
