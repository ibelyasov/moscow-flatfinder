"""Local road and railway noise-risk screening from an OpenStreetMap extract."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .models import Evidence, FieldValue, ListingFacts, ValueStatus
from .twogis import address_hash

DEFAULT_SOURCE_URL = "https://download.bbbike.org/osm/bbbike/Moscow/Moscow.osm.pbf"
MAP_VERSION = "osm-transport-v1"
MODEL_VERSION = "osm-transport-night-v2"
ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0"
_ORIGIN_LAT = 55.7558
_ORIGIN_LON = 37.6173
_ROAD_CLASSES = {
    "motorway": (7.5, 400.0),
    "motorway_link": (7.5, 300.0),
    "trunk": (7.5, 330.0),
    "trunk_link": (7.5, 250.0),
    "primary": (7.5, 230.0),
    "primary_link": (7.5, 175.0),
    "secondary": (7.5, 183.0),
    "secondary_link": (7.5, 137.0),
    "tertiary": (7.5, 75.0),
}
_RAIL_CLASSES = {
    "rail": (25.0, 632.0),
    "light_rail": (25.0, 250.0),
    "tram": (25.0, 200.0),
    "monorail": (25.0, 200.0),
}
_SOURCE_CLASSES = _ROAD_CLASSES | _RAIL_CLASSES


@dataclass(slots=True)
class NoiseResult:
    address: str
    address_sha256: str
    captured_at: str
    provider: str = "openstreetmap"
    status: str = "unknown"
    error: str | None = None
    home_lat: float | None = None
    home_lon: float | None = None
    model_version: str = MODEL_VERSION
    source_sha256: str | None = None
    noise_score: float = 0.0
    nearest: list[dict[str, Any]] | None = None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _NoiseMap:
    source_sha256: str
    trees: dict[str, Any]
    names: dict[str, list[str | None]]


def _project(lon: float, lat: float) -> tuple[float, float]:
    return (
        (lon - _ORIGIN_LON) * 111_320.0 * math.cos(math.radians(_ORIGIN_LAT)),
        (lat - _ORIGIN_LAT) * 110_540.0,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_path(source: str, directory: Path) -> Path:
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"}:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"OSM source does not exist: {path}")
        return path
    target = directory / (Path(parsed.path).name or "moscow.osm.pbf")
    request = Request(source, headers={"User-Agent": "FlatFinder/0.1"})
    with urlopen(request, timeout=120) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)
    return target


def build_noise_map(
    output: str | Path, source: str = DEFAULT_SOURCE_URL
) -> dict[str, Any]:
    """Build a compact road/rail JSON layer from a local or remote OSM extract."""

    try:
        import osmium
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency boundary
        raise RuntimeError("osmium is required to build the noise map") from exc

    features: list[dict[str, Any]] = []

    class Handler(osmium.SimpleHandler):
        def way(self, way: Any) -> None:
            source_class = str(way.tags.get("highway") or way.tags.get("railway") or "")
            if source_class not in _SOURCE_CLASSES:
                return
            if str(way.tags.get("tunnel") or "").casefold() in {
                "yes",
                "true",
                "1",
                "building_passage",
            }:
                return
            try:
                coordinates = [
                    [round(float(node.lon), 7), round(float(node.lat), 7)]
                    for node in way.nodes
                ]
            except (RuntimeError, ValueError):
                return
            if len(coordinates) >= 2:
                features.append(
                    {
                        "id": f"way/{way.id}",
                        "class": source_class,
                        "name": str(way.tags.get("name") or "") or None,
                        "coordinates": coordinates,
                    }
                )

    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flatfinder-noise-") as temp_dir:
        source_path = _source_path(source, Path(temp_dir))
        source_hash = _sha256(source_path)
        Handler().apply_file(str(source_path), locations=True, idx="flex_mem")
    if not features:
        raise ValueError("OSM source contains no supported road or railway ways")
    payload = {
        "schema_version": 1,
        "model_version": MAP_VERSION,
        "source": "OpenStreetMap via BBBike"
        if source == DEFAULT_SOURCE_URL
        else source,
        "source_url": source if urlparse(source).scheme in {"http", "https"} else None,
        "source_sha256": source_hash,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attribution": ATTRIBUTION,
        "features": features,
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output_path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        temporary = Path(handle.name)
    temporary.replace(output_path)
    return {
        key: payload[key]
        for key in ("model_version", "source_sha256", "generated_at", "attribution")
    } | {"features": len(features), "path": str(output_path)}


@lru_cache(maxsize=4)
def _load_noise_map(path: str, modified_ns: int) -> _NoiseMap:
    del modified_ns
    from shapely import LineString
    from shapely.strtree import STRtree

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("model_version") != MAP_VERSION or not isinstance(
        payload.get("features"), list
    ):
        raise ValueError("noise map has an unsupported schema or model version")
    geometries: dict[str, list[Any]] = {}
    names: dict[str, list[str | None]] = {}
    for feature in payload["features"]:
        if (
            not isinstance(feature, Mapping)
            or feature.get("class") not in _SOURCE_CLASSES
        ):
            continue
        coordinates = feature.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        source_class = str(feature["class"])
        try:
            line = LineString(
                [_project(float(point[0]), float(point[1])) for point in coordinates]
            )
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
        geometries.setdefault(source_class, []).append(line)
        names.setdefault(source_class, []).append(
            str(feature.get("name") or "") or None
        )
    trees = {
        source_class: STRtree(lines)
        for source_class, lines in geometries.items()
        if lines
    }
    if not trees:
        raise ValueError("noise map contains no valid geometries")
    source_hash = str(payload.get("source_sha256") or "")
    if len(source_hash) != 64:
        raise ValueError("noise map source hash is missing")
    return _NoiseMap(source_hash, trees, names)


def calculate_noise(
    address: str, home_point: Mapping[str, Any] | None, map_path: str | Path
) -> NoiseResult:
    """Return a continuous 0–6 screening score; it is not a dB estimate."""

    result = NoiseResult(
        address=str(address or "").strip(),
        address_sha256=address_hash(address),
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    try:
        if not result.address or not isinstance(home_point, Mapping):
            raise ValueError("exact home coordinates are unavailable")
        lat, lon = float(home_point["lat"]), float(home_point["lon"])
        if (
            not math.isfinite(lat)
            or not math.isfinite(lon)
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
        ):
            raise ValueError("home coordinates are invalid")
        path = Path(map_path).expanduser().resolve()
        noise_map = _load_noise_map(str(path), path.stat().st_mtime_ns)
        from shapely import Point

        point = Point(*_project(lon, lat))
        nearest: list[dict[str, Any]] = []
        for source_class, tree in noise_map.trees.items():
            index = int(tree.nearest(point))
            distance = float(point.distance(tree.geometries[index]))
            zero_distance, radius = _SOURCE_CLASSES[source_class]
            penalty = max(0.0, min(1.0, (radius - distance) / (radius - zero_distance)))
            nearest.append(
                {
                    "class": source_class,
                    "name": noise_map.names[source_class][index],
                    "distance_m": round(distance),
                    "zero_distance_m": zero_distance,
                    "no_penalty_distance_m": radius,
                    "penalty": round(penalty, 4),
                }
            )
        nearest.sort(key=lambda item: float(item["penalty"]), reverse=True)
        result.home_lat, result.home_lon = lat, lon
        result.source_sha256 = noise_map.source_sha256
        result.nearest = nearest[:5]
        result.noise_score = round(
            6.0
            * (1.0 - max((float(item["penalty"]) for item in nearest), default=0.0)),
            1,
        )
        result.status = "success"
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        result.error = str(error)[:240] or error.__class__.__name__
    return result


def apply_noise(
    facts: ListingFacts | dict[str, Any], result: NoiseResult | Mapping[str, Any]
) -> ListingFacts | dict[str, Any]:
    payload = result.to_payload() if isinstance(result, NoiseResult) else dict(result)
    status = (
        ValueStatus.CONFIRMED
        if payload.get("status") == "success"
        else ValueStatus.UNKNOWN
    )
    value = {
        "provider": "openstreetmap",
        "model_version": payload.get("model_version"),
        "source_sha256": payload.get("source_sha256"),
        "score": payload.get("noise_score", 0),
        "nearest": payload.get("nearest") or [],
    }
    summary = {
        key: payload.get(key)
        for key in (
            "status",
            "noise_score",
            "nearest",
            "model_version",
            "source_sha256",
            "error",
        )
    }
    evidence = Evidence(
        "openstreetmap",
        json.dumps(summary, ensure_ascii=False, sort_keys=True),
        str(payload.get("captured_at") or ""),
    )
    if isinstance(facts, ListingFacts):
        facts.fields["noise"] = FieldValue(value, status, [evidence])
        return facts
    fields = facts.setdefault("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("facts.fields must be an object")
    fields["noise"] = {
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
    "ATTRIBUTION",
    "DEFAULT_SOURCE_URL",
    "MAP_VERSION",
    "MODEL_VERSION",
    "NoiseResult",
    "apply_noise",
    "build_noise_map",
    "calculate_noise",
]
