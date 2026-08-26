"""Browser-backed Yandex Maps routes for commute and amenity scoring."""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .browser import detect_blocker
from .scoring import score_commute
from .twogis import (
    CommuteResult,
    FitnessResult,
    ParkResult,
    fitness_score,
    park_score,
    prepare_commute,
    prepare_fitness,
    prepare_park,
)

_MOSCOW = ZoneInfo("Europe/Moscow")
_ROUTE_LOCK = asyncio.Lock()
_last_navigation_at = 0.0
_NO_ROUTE_TEXT = (
    "не удалось построить маршрут",
    "маршрут не найден",
    "нет подходящего маршрута",
)
_HOUR_UNIT_PATTERN = r"(?:ч|час(?:а|ов)?|h(?:ours?|rs?)?)"
_MINUTE_UNIT_PATTERN = r"(?:мин|min(?:utes?|s)?)"
_DURATION_PATTERN = (
    rf"(?:(\d+)\s*{_HOUR_UNIT_PATTERN}"
    rf"(?:\s*(\d+)\s*{_MINUTE_UNIT_PATTERN})?|"
    rf"(\d+)\s*{_MINUTE_UNIT_PATTERN})"
)
_DURATION_RE = re.compile(_DURATION_PATTERN, re.IGNORECASE)


@dataclass(slots=True)
class RouteMeasurement:
    mode: str
    minutes: float
    source_url: str
    captured_at: str

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class YandexMapsRouteError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"Yandex Maps route {reason}")
        self.reason = reason


def _point(value: Mapping[str, Any]) -> str:
    lat, lon = float(value["lat"]), float(value["lon"])
    if (
        not isfinite(lat)
        or not isfinite(lon)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        raise ValueError("route coordinates are out of range")
    return f"{lat:.6f},{lon:.6f}"


def build_route_url(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    mode: str,
    at: datetime | None = None,
) -> str:
    """Build the public Yandex Maps deep link used by the visible browser."""

    rtt = {"transit": "mt", "walking": "pd"}.get(mode)
    if rtt is None:
        raise ValueError("route mode must be transit or walking")
    params: list[tuple[str, str]] = [
        ("mode", "routes"),
        ("rtext", f"{_point(source)}~{_point(target)}"),
        ("rtt", rtt),
        ("ruri", "~"),
    ]
    if at is not None:
        local = (
            at.replace(tzinfo=_MOSCOW) if at.tzinfo is None else at.astimezone(_MOSCOW)
        )
        params.extend(
            (
                ("routes[timeDependent][type]", "departure"),
                (
                    "routes[timeDependent][time]",
                    local.replace(tzinfo=None).isoformat(timespec="seconds"),
                ),
            )
        )
    return f"https://yandex.ru/maps/213/moscow/?{urlencode(params)}"


def parse_duration_minutes(text: str) -> int | None:
    """Parse the first Russian or English route duration from a route card."""

    match = _DURATION_RE.search(str(text or ""))
    if match is None:
        return None
    return int(match.group(1) or 0) * 60 + int(match.group(2) or match.group(3) or 0)


def _config(config: Any, name: str, default: Any) -> Any:
    return (
        config.get(name, default)
        if isinstance(config, Mapping)
        else getattr(config, name, default)
    )


class YandexMapsRouter:
    """One sequential route page with conservative navigation pacing."""

    def __init__(
        self,
        page: Any,
        *,
        min_interval_seconds: float = 30.0,
        jitter_seconds: float = 10.0,
        timeout_seconds: float = 120.0,
        diagnostics_dir: str | Path = "route-diagnostics",
    ) -> None:
        self.page = page
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.jitter_seconds = max(0.0, float(jitter_seconds))
        self.timeout_ms = max(1_000, int(float(timeout_seconds) * 1_000))
        self.diagnostics_dir = Path(diagnostics_dir).expanduser()

    @classmethod
    async def from_context(cls, context: Any, config: Any) -> YandexMapsRouter:
        if context is None or not callable(getattr(context, "new_page", None)):
            raise RuntimeError("browser context cannot open a route tab")
        page = await context.new_page()
        database = Path(
            str(_config(config, "database", "listings.sqlite3"))
        ).expanduser()
        return cls(
            page,
            min_interval_seconds=_config(config, "route_min_interval_seconds", 30),
            jitter_seconds=_config(config, "route_jitter_seconds", 10),
            timeout_seconds=_config(config, "route_timeout_seconds", 120),
            diagnostics_dir=database.parent / "route-diagnostics",
        )

    @classmethod
    async def from_listing_page(
        cls, listing_page: Any, config: Any
    ) -> YandexMapsRouter:
        context = getattr(listing_page, "context", None)
        context = context() if callable(context) else context
        return await cls.from_context(context, config)

    async def close(self) -> None:
        close = getattr(self.page, "close", None)
        if callable(close):
            await close()

    async def _capture_failure(self, reason: str) -> None:
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(_MOSCOW).strftime("%Y%m%d-%H%M%S")
        prefix = self.diagnostics_dir / f"{stamp}-{reason}"
        try:
            await self.page.screenshot(
                path=str(prefix.with_suffix(".png")), full_page=True
            )
        except Exception:
            pass
        try:
            html = await self.page.content()
            prefix.with_suffix(".html").write_text(str(html), encoding="utf-8")
        except Exception:
            pass

    async def route(
        self,
        source: Mapping[str, Any],
        target: Mapping[str, Any],
        mode: str,
        at: datetime | None = None,
    ) -> RouteMeasurement | None:
        """Open one public route page and read the first semantic route card."""

        global _last_navigation_at
        url = build_route_url(source, target, mode, at)
        labels = (
            ("На общественном транспорте", "By public transport", "Public transport")
            if mode == "transit"
            else ("Пешком", "Walking")
        )
        label_pattern = "|".join(re.escape(label) for label in labels)
        card_name = re.compile(
            rf"(?:{label_pattern}).*{_DURATION_PATTERN}", re.IGNORECASE
        )
        async with _ROUTE_LOCK:
            delay = (
                _last_navigation_at
                + self.min_interval_seconds
                + random.uniform(0, self.jitter_seconds)
                - monotonic()
            )
            if delay > 0:
                await asyncio.sleep(delay)
            _last_navigation_at = monotonic()
            try:
                response = await self.page.goto(
                    url, wait_until="domcontentloaded", timeout=self.timeout_ms
                )
                status = getattr(response, "status", None)
                status = status() if callable(status) else status
                if status == 429:
                    await self._capture_failure("http-429")
                    raise YandexMapsRouteError("http_429")
                if status == 403:
                    await self._capture_failure("http-403")
                    raise YandexMapsRouteError("blocked")
                blocker = await detect_blocker(self.page)
                if blocker:
                    await self._capture_failure(blocker)
                    raise YandexMapsRouteError(blocker)
                cards = self.page.get_by_role("listitem", name=card_name)
                await cards.first.wait_for(state="visible", timeout=self.timeout_ms)
                text = await cards.first.inner_text()
                minutes = parse_duration_minutes(text)
                if minutes is None:
                    await self._capture_failure("schema-changed")
                    raise YandexMapsRouteError("schema_changed")
                return RouteMeasurement(
                    mode=mode,
                    minutes=float(minutes),
                    source_url=str(getattr(self.page, "url", url) or url),
                    captured_at=datetime.now(_MOSCOW).isoformat(timespec="seconds"),
                )
            except YandexMapsRouteError:
                raise
            except Exception as error:
                blocker = await detect_blocker(self.page)
                if blocker:
                    await self._capture_failure(blocker)
                    raise YandexMapsRouteError(blocker) from error
                body = ""
                try:
                    body = (
                        await self.page.locator("body").inner_text(timeout=1_000)
                    ).casefold()
                except Exception:
                    pass
                if any(message in body for message in _NO_ROUTE_TEXT):
                    return None
                await self._capture_failure("schema-changed")
                raise YandexMapsRouteError("schema_changed") from error


def _route_call(route: RouteMeasurement | None, mode: str) -> dict[str, Any]:
    return {
        "provider": "yandex_maps",
        "mode": mode,
        "status": "success" if route is not None else "no_route",
        "route": route.to_payload() if route is not None else None,
    }


async def calculate_commute(
    router: YandexMapsRouter,
    address: str,
    destination: str,
    api_key: str,
    *,
    now: datetime | None = None,
    home_point: Mapping[str, Any] | None = None,
    office_point: Mapping[str, Any] | None = None,
) -> CommuteResult:
    result = await asyncio.to_thread(
        prepare_commute,
        address,
        destination,
        api_key,
        now=now,
        home_point=home_point,
        office_point=office_point,
    )
    if result.error:
        return result
    home = {"lat": result.home_lat, "lon": result.home_lon}
    office = {"lat": result.office_lat, "lon": result.office_lon}
    service_day = date.fromisoformat(result.service_date)
    outbound = await router.route(
        home, office, "transit", datetime.combine(service_day, time(9), _MOSCOW)
    )
    result.calls.append(_route_call(outbound, "transit"))
    inbound = await router.route(
        office, home, "transit", datetime.combine(service_day, time(19), _MOSCOW)
    )
    result.calls.append(_route_call(inbound, "transit"))
    if outbound is None or inbound is None:
        result.error = "route missing for one or both directions"
        return result
    result.home_to_work_minutes = outbound.minutes
    result.work_to_home_minutes = inbound.minutes
    result.home_to_work_score = score_commute(outbound.minutes)
    result.work_to_home_score = score_commute(inbound.minutes)
    result.average_minutes = (outbound.minutes + inbound.minutes) / 2
    result.average_score = (result.home_to_work_score + result.work_to_home_score) / 2
    result.gate_status = (
        "failed" if max(outbound.minutes, inbound.minutes) >= 45 else "passed"
    )
    result.commute_score = result.average_score
    result.status = "success"
    return result


async def calculate_park(
    router: YandexMapsRouter,
    address: str,
    api_key: str,
    *,
    home_point: Mapping[str, Any] | None,
) -> ParkResult:
    result = await asyncio.to_thread(
        prepare_park, address, api_key, home_point=home_point
    )
    if result.error:
        return result
    route = await router.route(
        {"lat": result.home_lat, "lon": result.home_lon},
        {"lat": result.place_lat, "lon": result.place_lon},
        "walking",
    )
    result.calls.append(_route_call(route, "walking"))
    if route is None:
        result.error = "walking route missing"
        return result
    result.walking_minutes = route.minutes
    result.park_score = park_score(route.minutes, result.area_hectares)
    result.status = "success"
    return result


async def calculate_fitness(
    router: YandexMapsRouter,
    address: str,
    api_key: str,
    *,
    home_point: Mapping[str, Any] | None,
) -> FitnessResult:
    result = await asyncio.to_thread(
        prepare_fitness, address, api_key, home_point=home_point
    )
    if result.error:
        return result
    route = await router.route(
        {"lat": result.home_lat, "lon": result.home_lon},
        {"lat": result.place_lat, "lon": result.place_lon},
        "walking",
    )
    result.calls.append(_route_call(route, "walking"))
    if route is None:
        result.error = "walking route missing"
        return result
    result.walking_minutes = route.minutes
    result.fitness_score = fitness_score(
        route.minutes, result.rating, result.review_count, result.sauna
    )
    result.status = "success"
    return result


__all__ = [
    "YandexMapsRouteError",
    "YandexMapsRouter",
    "build_route_url",
    "calculate_commute",
    "calculate_fitness",
    "calculate_park",
    "parse_duration_minutes",
]
