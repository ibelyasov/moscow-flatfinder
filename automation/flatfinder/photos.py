"""Local helpers for deterministic photo URLs and ingestion."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping, Sequence
from pathlib import Path

from .models import PhotoInput
from .sources import adapter_for_photo_url

try:
    from PIL import Image, ImageOps, ImageStat
except ImportError:  # pragma: no cover - optional runtime dependency
    Image = ImageOps = ImageStat = None  # type: ignore[assignment]

_DHASH_DISTANCE = 6
_MEAN_DISTANCE = 16.0
_MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024


def _require_pillow() -> None:
    if Image is None or ImageOps is None or ImageStat is None:
        raise RuntimeError("Pillow is required for photo ingestion")


def is_allowed_photo_url(url: str | None) -> bool:
    """Return whether a URL belongs to an explicitly supported listing CDN."""

    return adapter_for_photo_url(url) is not None


def normalize_photo_url(url: str | None) -> str | None:
    """Use one deterministic identity for supported CDN rendition variants."""

    adapter = adapter_for_photo_url(url)
    return adapter.normalize_photo_url(url) if adapter is not None else url


async def _await(value: object) -> object:
    return await value if inspect.isawaitable(value) else value


async def _response_body(page: object, url: str) -> bytes:
    """Read an image through Playwright's request context, never raw HTTP."""

    request = getattr(page, "request", None)
    if request is None:
        context = getattr(page, "context", None)
        request = getattr(context, "request", None) if context is not None else None
    getter = getattr(request, "get", None)
    if not callable(getter):
        getter = getattr(page, "request_get", None)
    if not callable(getter):
        raise RuntimeError("page does not expose a Playwright request context")
    try:
        response = await _await(getter(url, timeout=30_000))
    except TypeError:
        response = await _await(getter(url))
    status = getattr(response, "status", None)
    if callable(status):
        status = await _await(status)
    try:
        if status is not None and not 200 <= int(status) < 300:
            raise OSError(f"photo request returned HTTP {status}")
    except (TypeError, ValueError):
        pass
    body = getattr(response, "body", None)
    body = await _await(body()) if callable(body) else await _await(body)
    if not isinstance(body, (bytes, bytearray)):
        raise OSError("photo response body is not binary")
    payload = bytes(body)
    if not payload or len(payload) > _MAX_DOWNLOAD_BYTES:
        raise OSError("photo response has an invalid size")
    return payload


async def ingest_photos(
    page: object,
    listing_id: int,
    urls: Sequence[str],
    cache_dir: str | Path,
) -> list[PhotoInput]:
    """Download a deterministic index of all canonical photo URLs.

    Returned records include explicit ``indexed``, ``duplicate`` and
    ``failed`` statuses so storage can preserve stable image indices even when
    a fetch fails.  Duplicate references use the prior image index; storage
    resolves that stable reference to its SQLite row id.
    """

    listing_id = int(listing_id)
    if listing_id <= 0:
        raise ValueError("listing_id must be positive")
    canonical_urls: dict[str, str] = {}
    for raw_url in urls or ():
        canonical = normalize_photo_url(raw_url)
        if (
            canonical
            and is_allowed_photo_url(canonical)
            and canonical not in canonical_urls
        ):
            canonical_urls[canonical] = str(raw_url)
    # Dict insertion order preserves the page's canonical URL source order;
    # failed and duplicate records still retain their original image_index.
    ordered = list(canonical_urls.items())
    result: list[PhotoInput] = []
    try:
        target_dir = Path(cache_dir).expanduser().resolve() / str(listing_id)
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as error:
        message = str(error)[:240] or error.__class__.__name__
        return [
            PhotoInput(
                listing_id=listing_id,
                image_index=index,
                source_url=canonical,
                raw_source_url=raw_url,
                status="failed",
                error=message,
            )
            for index, (canonical, raw_url) in enumerate(ordered)
        ]
    seen_sha: dict[str, int] = {}
    seen_visual: list[tuple[int, dict[str, object]]] = []
    for image_index, (canonical, raw_url) in enumerate(ordered):
        try:
            payload = await _response_body(page, canonical)
            sha256 = hashlib.sha256(payload).hexdigest()
            if sha256 in seen_sha:
                result.append(
                    PhotoInput(
                        listing_id=listing_id,
                        image_index=image_index,
                        source_url=canonical,
                        raw_source_url=raw_url,
                        sha256=sha256,
                        status="duplicate",
                        duplicate_of_index=seen_sha[sha256],
                    )
                )
                continue
            filename = f"image_{image_index:04d}_{sha256[:16]}.img"
            temporary = target_dir / f".{filename}.tmp"
            local_path = target_dir / filename
            temporary.write_bytes(payload)
            try:
                # Opening the file makes invalid/non-image responses a local
                # per-photo failure instead of poisoning the whole listing.
                _require_pillow()
                with Image.open(temporary) as opened:
                    opened.verify()
                temporary.replace(local_path)
                image_hash = dhash(local_path)
                mean = _mean_rgb(local_path)
            except Exception as error:
                temporary.unlink(missing_ok=True)
                local_path.unlink(missing_ok=True)
                result.append(
                    PhotoInput(
                        listing_id=listing_id,
                        image_index=image_index,
                        source_url=canonical,
                        raw_source_url=raw_url,
                        sha256=sha256,
                        status="failed",
                        error=str(error)[:240] or error.__class__.__name__,
                    )
                )
                continue
            duplicate_index = next(
                (
                    prior_index
                    for prior_index, prior in seen_visual
                    if _near({"dhash": image_hash, "mean": mean}, prior)
                ),
                None,
            )
            if duplicate_index is not None:
                local_path.unlink(missing_ok=True)
                result.append(
                    PhotoInput(
                        listing_id=listing_id,
                        image_index=image_index,
                        source_url=canonical,
                        raw_source_url=raw_url,
                        sha256=sha256,
                        dhash=image_hash,
                        status="duplicate",
                        duplicate_of_index=duplicate_index,
                    )
                )
                continue
            seen_sha[sha256] = image_index
            seen_visual.append((image_index, {"dhash": image_hash, "mean": mean}))
            result.append(
                PhotoInput(
                    listing_id=listing_id,
                    image_index=image_index,
                    source_url=canonical,
                    raw_source_url=raw_url,
                    local_path=str(local_path),
                    sha256=sha256,
                    dhash=image_hash,
                    status="indexed",
                )
            )
        except Exception as error:
            # One unavailable image must not discard the rest of the gallery.
            result.append(
                PhotoInput(
                    listing_id=listing_id,
                    image_index=image_index,
                    source_url=canonical,
                    raw_source_url=raw_url,
                    status="failed",
                    error=str(error)[:240] or error.__class__.__name__,
                )
            )
            continue
    return result


def _rgb_copy(image: Image.Image) -> Image.Image:
    """Copy, orient and convert an image without closing caller-owned objects."""

    _require_pillow()
    copied = image.copy()
    oriented = ImageOps.exif_transpose(copied)
    try:
        rgb = oriented.convert("RGB")
    finally:
        if oriented is not copied:
            oriented.close()
        copied.close()
    return rgb


def _dhash_rgb(image: Image.Image) -> str:
    _require_pillow()
    gray = ImageOps.grayscale(image)
    small = gray.resize((9, 8), Image.Resampling.LANCZOS)
    try:
        pixels = list(small.getdata())
    finally:
        small.close()
        gray.close()
    value = 0
    for row in range(8):
        start = row * 9
        for column in range(8):
            value = (value << 1) | int(
                pixels[start + column] > pixels[start + column + 1]
            )
    return f"{value:016x}"


def _mean_rgb(source: Image.Image | str | Path) -> tuple[float, float, float]:
    """Return the mean RGB guard used by representative-image dedupe."""

    _require_pillow()
    rgb = _rgb_for(source)
    try:
        return tuple(float(value) for value in ImageStat.Stat(rgb).mean[:3])  # type: ignore[union-attr]
    finally:
        rgb.close()


def dhash(image: Image.Image | str | Path) -> str:
    """Return the 64-bit horizontal difference hash as sixteen hex digits."""

    _require_pillow()
    if isinstance(image, Image.Image):
        rgb = _rgb_copy(image)
        try:
            return _dhash_rgb(rgb)
        finally:
            rgb.close()
    with Image.open(Path(image)) as opened:
        rgb = _rgb_copy(opened)
    try:
        return _dhash_rgb(rgb)
    finally:
        rgb.close()


def _rgb_for(source: Image.Image | str | Path) -> Image.Image:
    _require_pillow()
    if isinstance(source, Image.Image):
        return _rgb_copy(source)
    with Image.open(Path(source)) as opened:
        return _rgb_copy(opened)


def _near(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    try:
        distance = (
            int(str(left["dhash"]), 16) ^ int(str(right["dhash"]), 16)
        ).bit_count()
        means = zip(left["mean"], right["mean"])  # type: ignore[arg-type]
        color_distance = max(abs(float(a) - float(b)) for a, b in means)
    except (TypeError, ValueError):
        return False
    return distance <= _DHASH_DISTANCE and color_distance <= _MEAN_DISTANCE


__all__ = [
    "dhash",
    "ingest_photos",
    "is_allowed_photo_url",
    "normalize_photo_url",
]
