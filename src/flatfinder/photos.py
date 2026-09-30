"""Local helpers for deterministic photo URLs and ingestion."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps, ImageStat
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from .models import PhotoInput
from .sources import adapter_for_photo_url

_DHASH_DISTANCE = 6
_MEAN_DISTANCE = 16.0
_MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024


def is_allowed_photo_url(url: str | None) -> bool:
    """Return whether a URL belongs to an explicitly supported listing CDN."""

    if adapter_for_photo_url(url) is None:
        return False
    try:
        parsed = urlsplit(str(url))
        return (
            parsed.scheme.lower() == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
        )
    except ValueError:
        return False


def normalize_photo_url(url: str | None) -> str | None:
    """Use one deterministic identity for supported CDN rendition variants."""

    adapter = adapter_for_photo_url(url)
    return adapter.normalize_photo_url(url) if adapter is not None else url


def photo_input_hash(photos: Sequence[PhotoInput]) -> str:
    """Identify the current ordered gallery by content and stable image index."""

    indices: set[int] = set()
    representatives: set[int] = set()
    identity = []
    for photo in sorted(photos, key=lambda item: item.image_index):
        if (
            isinstance(photo.image_index, bool)
            or not isinstance(photo.image_index, int)
            or photo.image_index < 0
            or photo.image_index in indices
        ):
            raise ValueError("photo image indices must be unique non-negative integers")
        indices.add(photo.image_index)
        if photo.status not in {"indexed", "duplicate", "failed"}:
            raise ValueError("photo status must be indexed, duplicate or failed")
        if photo.status in {"indexed", "duplicate"} and (
            not isinstance(photo.sha256, str)
            or len(photo.sha256) != 64
            or any(character not in "0123456789abcdef" for character in photo.sha256)
        ):
            raise ValueError("usable photo requires its actual SHA-256")
        if photo.status == "duplicate" and (
            isinstance(photo.duplicate_of_index, bool)
            or not isinstance(photo.duplicate_of_index, int)
            or photo.duplicate_of_index not in representatives
        ):
            raise ValueError("duplicate photo must reference a prior image index")
        if photo.status != "duplicate" and photo.duplicate_of_index is not None:
            raise ValueError("only duplicate photos may reference another image")
        if photo.status == "indexed":
            representatives.add(photo.image_index)
        identity.append(
            {
                "image_index": photo.image_index,
                "source_url": photo.source_url,
                "status": photo.status,
                "sha256": photo.sha256,
                "duplicate_of_index": photo.duplicate_of_index,
            }
        )
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _response_body(page: Page, url: str) -> bytes:
    """Read an image through Playwright's request context, never raw HTTP."""

    if not is_allowed_photo_url(url):
        raise ValueError("photo URL must use HTTPS on a supported listing CDN")

    # Do not follow a redirect before checking its destination against the CDN
    # allowlist. A redirected image is an explicit per-photo failure.
    response = await page.request.get(url, timeout=30_000, max_redirects=0)
    try:
        if not 200 <= response.status < 300:
            raise OSError(f"photo request returned HTTP {response.status}")
        if not is_allowed_photo_url(response.url):
            raise OSError("photo response URL is outside the supported CDN allowlist")
        payload = await response.body()
    finally:
        await response.dispose()
    if not isinstance(payload, bytes):
        raise OSError("photo response body is not binary")
    if not payload or len(payload) > _MAX_DOWNLOAD_BYTES:
        raise OSError("photo response has an invalid size")
    return payload


async def ingest_photos(
    page: Page,
    listing_id: int,
    urls: Sequence[str],
    cache_dir: str | Path,
) -> list[PhotoInput]:
    """Download a deterministic index of all canonical photo URLs.

    Returned records include explicit ``indexed``, ``duplicate`` and
    ``failed`` statuses so storage preserves image indices even when a fetch
    fails. Duplicate references use the prior image index in this same set.
    """

    if (
        isinstance(listing_id, bool)
        or not isinstance(listing_id, int)
        or listing_id <= 0
    ):
        raise ValueError("listing_id must be a positive integer")
    if isinstance(urls, (str, bytes)) or any(not isinstance(url, str) for url in urls):
        raise TypeError("photo URLs must be a sequence of strings")
    canonical_urls: dict[str, str] = {}
    for raw_url in urls:
        canonical = normalize_photo_url(raw_url)
        identity = canonical if canonical else raw_url
        if identity not in canonical_urls:
            canonical_urls[identity] = raw_url
    # Dict insertion order preserves the page's canonical URL source order;
    # failed and duplicate records still retain their original image_index.
    ordered = list(canonical_urls.items())
    result: list[PhotoInput] = []
    try:
        target_dir = Path(cache_dir).expanduser().resolve() / str(listing_id)
        target_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError, ValueError) as error:
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
            try:
                # Opening the file makes invalid/non-image responses a local
                # per-photo failure instead of poisoning the whole listing.
                with Image.open(io.BytesIO(payload)) as opened:
                    opened.verify()
                with Image.open(io.BytesIO(payload)) as opened:
                    image_hash = dhash(opened)
                    mean = _mean_rgb(opened)
            except (OSError, ValueError, Image.DecompressionBombError) as error:
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
                seen_sha[sha256] = duplicate_index
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
            filename = f"image_{image_index:04d}_{sha256[:16]}.img"
            temporary = target_dir / f".{filename}.tmp"
            local_path = target_dir / filename
            try:
                temporary.write_bytes(payload)
                temporary.replace(local_path)
            finally:
                temporary.unlink(missing_ok=True)
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
        except (OSError, RuntimeError, ValueError, PlaywrightError) as error:
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

    rgb = _rgb_for(source)
    try:
        return tuple(float(value) for value in ImageStat.Stat(rgb).mean[:3])  # type: ignore[union-attr]
    finally:
        rgb.close()


def dhash(image: Image.Image | str | Path) -> str:
    """Return the 64-bit horizontal difference hash as sixteen hex digits."""

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
    "photo_input_hash",
]
