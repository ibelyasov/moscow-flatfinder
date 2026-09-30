"""One explicit, provider-only per-listing enrichment boundary."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import ListingFacts, ValueStatus
from .noise import MODEL_VERSION as NOISE_MODEL_VERSION
from .noise import _sha256, apply_noise, calculate_noise
from .twogis import (
    _default_fetch,
    apply_commute,
    apply_fitness,
    apply_location_point,
    apply_park,
    geocode_address,
    saved_point,
    service_date,
)
from .yandex_routes import (
    YandexMapsRouteError,
    YandexMapsRouter,
    calculate_commute,
    calculate_fitness,
    calculate_park,
)

if TYPE_CHECKING:
    from .config import GeoSettings

MODEL_VERSION = "geo-measurements-v2-single-candidate"


@dataclass(slots=True)
class EnrichmentResult:
    facts: ListingFacts
    checks: list[dict[str, Any]]


def _identity(value: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


async def enrich_listing(
    facts: ListingFacts,
    *,
    settings: GeoSettings | None,
    noise_enabled: bool,
    noise_map: Path | None = None,
    router: YandexMapsRouter | None = None,
    api_key: str | None = None,
    cached_check: Callable[[str, str], Mapping[str, Any] | None] | None = None,
    fetch: Callable[..., Any] = _default_fetch,
    now: datetime | None = None,
) -> EnrichmentResult:
    """Acquire measurements without SQL; caller persists the completed result.

    A cache is bound to one listing by the caller. Disabled capabilities neither
    query it nor access credentials, provider pages, or the noise map.
    """
    result = EnrichmentResult(copy.deepcopy(facts), [])
    if settings is None and not noise_enabled:
        return result
    address_field = facts.fields.get("address")
    address = str(address_field.value or "").strip() if address_field else ""
    point_field = facts.fields.get("location_point")
    raw_point = (
        point_field.value
        if point_field and isinstance(point_field.value, Mapping)
        else None
    )
    base = {
        "address": address,
        "source_point": raw_point,
        "source_point_status": str(point_field.status) if point_field else None,
        "model_version": MODEL_VERSION,
    }
    if settings is not None:
        if router is None or not api_key:
            raise ValueError(
                "enabled Geo requires an explicit router and resolved API key"
            )
        base |= {
            "destination": settings.destination,
            "departure_weekday": settings.departure_weekday,
            "to_work_time": settings.to_work_time,
            "to_home_time": settings.to_home_time,
            "service_date": service_date(settings.departure_weekday, now).isoformat(),
        }

    def cached(kind: str, identity: str) -> Mapping[str, Any] | None:
        check = cached_check(kind, identity) if cached_check else None
        if check is None:
            return None
        if (
            not isinstance(check, Mapping)
            or check.get("kind") != kind
            or check.get("input_hash") != identity
            or not isinstance(check.get("payload"), Mapping)
        ):
            raise ValueError("invalid cached enrichment check")
        # Failed/unknown checks must not prevent a later explicit retry.
        return (
            check["payload"] if check.get("status") in {"success", "partial"} else None
        )

    def record(kind: str, identity: str, payload: Mapping[str, Any]) -> None:
        try:
            captured_at = datetime.fromisoformat(str(payload.get("captured_at") or ""))
            if captured_at.tzinfo is None:
                raise ValueError("capture timestamp has no timezone")
        except ValueError:
            captured_at = datetime.now(timezone.utc)
        result.checks.append(
            {
                "kind": kind,
                "input_hash": identity,
                "status": payload.get("status", "unknown"),
                "payload": dict(payload),
                "captured_at": captured_at.astimezone(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }
        )

    home = saved_point(raw_point, "home")
    if settings is not None:
        identity = _identity(base | {"kind": "geocode"})
        payload = cached("geocode", identity)
        if payload is None:
            calls: list[dict[str, Any]] = []
            try:
                point = await asyncio.to_thread(
                    geocode_address,
                    address,
                    api_key,
                    hint_point=raw_point,
                    timeout=settings.timeout_seconds,
                    fetch=fetch,
                    calls=calls,
                )
                payload = {
                    **point,
                    "status": "success"
                    if point.get("precision") == "exact"
                    else "partial",
                    "calls": calls,
                }
            except ValueError as error:
                payload = {"status": "unknown", "error": str(error), "calls": calls}
        record("geocode", identity, payload)
        if payload.get("status") in {"success", "partial"}:
            apply_location_point(result.facts, payload)
            home = saved_point(payload, "home")
        else:
            # Source coordinates remain source coordinates, never exact geocoding.
            home = saved_point(raw_point, "home")
            if home:
                home["precision"] = "source_pin"
        measured_base = base | {"home_point": home}
        operations = (
            ("commute", calculate_commute, apply_commute),
            ("park", calculate_park, apply_park),
            ("fitness", calculate_fitness, apply_fitness),
        )
        blocked_reason: str | None = None
        for kind, calculate, apply in operations:
            identity = _identity(measured_base | {"kind": kind})
            if blocked_reason is not None:
                payload = {
                    "status": "blocked",
                    "error": "not attempted after provider block",
                    "blocked_reason": blocked_reason,
                    "attempted": False,
                }
            else:
                payload = cached(kind, identity)
            if payload is None:
                if home is None:
                    payload = {
                        "status": "unknown",
                        "error": "home coordinates are unavailable",
                    }
                else:
                    kwargs = {
                        "home_point": home,
                        "fetch": fetch,
                        "timeout": settings.timeout_seconds,
                    }
                    try:
                        if kind == "commute":
                            kwargs |= {
                                "now": now,
                                "departure_weekday": settings.departure_weekday,
                                "to_work_time": settings.to_work_time,
                                "to_home_time": settings.to_home_time,
                            }
                            measurement = await calculate(
                                router, address, settings.destination, api_key, **kwargs
                            )
                        else:
                            measurement = await calculate(
                                router, address, api_key, **kwargs
                            )
                        payload = measurement.to_payload()
                    except YandexMapsRouteError as error:
                        if error.reason in {
                            "captcha",
                            "login",
                            "2fa",
                            "blocked",
                            "http_429",
                        }:
                            blocked_reason = error.reason
                            payload = {
                                "status": "blocked",
                                "error": str(error),
                                "blocked_reason": blocked_reason,
                                "attempted": True,
                            }
                        else:
                            payload = {"status": "failed", "error": str(error)}
                    except (OSError, TimeoutError, ValueError, RuntimeError) as error:
                        # Exception text from an HTTP client may contain credentials.
                        payload = {"status": "failed", "error": type(error).__name__}
            apply(result.facts, payload)
            field = result.facts.fields["route" if kind == "commute" else kind]
            if (
                field.status == ValueStatus.CONFIRMED
                and home
                and home.get("precision") != "exact"
            ):
                field.status = ValueStatus.PARTIAL
            if kind == "commute":
                result.facts.fields["route_minutes"].status = field.status
            record(kind, identity, payload)
    if noise_enabled:
        if noise_map is None:
            raise ValueError("enabled Noise requires an explicit map path")
        path = Path(noise_map).expanduser().resolve()
        try:
            map_hash = _sha256(path) if path.is_file() else None
        except OSError:
            map_hash = None
        identity = _identity(
            base
            | {
                "kind": "noise",
                "home_point": home,
                "noise_model": NOISE_MODEL_VERSION,
                "map_sha256": map_hash,
            }
        )
        payload = cached("noise", identity)
        if payload is None:
            payload = (
                await asyncio.to_thread(calculate_noise, address, home, path)
            ).to_payload()
        payload = {**payload, "home_precision": home.get("precision") if home else None}
        apply_noise(result.facts, payload)
        if (
            home
            and home.get("precision") != "exact"
            and result.facts.fields["noise"].status == ValueStatus.CONFIRMED
        ):
            result.facts.fields["noise"].status = ValueStatus.PARTIAL
        record("noise", identity, payload)
    return result
