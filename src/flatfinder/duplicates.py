"""Pure cross-source duplicate evidence; offers and human decisions stay independent."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .models import FieldValue, ListingFacts, PhotoInput, ValueStatus

_MAX_HASH_DISTANCE = 6


@dataclass(frozen=True, slots=True)
class PhotoMatch:
    left_image_index: int
    right_image_index: int
    left_dhash: str
    right_dhash: str
    hamming_distance: int


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    building_id: str
    rooms: int
    floor: int
    left_identity: tuple[str, str]
    right_identity: tuple[str, str]
    areas_m2: tuple[float, float]
    gallery_sizes: tuple[int, int]
    shared_photos: tuple[PhotoMatch, ...]
    method: str = field(default="building_photos_v1", init=False)
    confidence: float = field(default=0.95, init=False)

    @property
    def evidence(self) -> dict[str, object]:
        """Return a JSON snapshot without exposing mutable input references."""

        return {
            "building_id": self.building_id,
            "rooms": self.rooms,
            "floor": self.floor,
            "left": {
                "source": self.left_identity[0],
                "source_listing_id": self.left_identity[1],
                "area_m2": self.areas_m2[0],
            },
            "right": {
                "source": self.right_identity[0],
                "source_listing_id": self.right_identity[1],
                "area_m2": self.areas_m2[1],
            },
            "area_difference_m2": abs(self.areas_m2[0] - self.areas_m2[1]),
            "gallery_sizes": list(self.gallery_sizes),
            "photo_matches": len(self.shared_photos),
            "photo_pairs": [
                {
                    "left_image_index": pair.left_image_index,
                    "right_image_index": pair.right_image_index,
                    "left_dhash": pair.left_dhash,
                    "right_dhash": pair.right_dhash,
                    "hamming_distance": pair.hamming_distance,
                }
                for pair in self.shared_photos
            ],
        }


@dataclass(frozen=True, slots=True)
class _ListingKey:
    building_id: str
    rooms: int
    floor: int
    area: float


@dataclass(frozen=True, slots=True)
class _Photo:
    image_index: int
    dhash: str
    bits: int


def _confirmed(facts: ListingFacts, name: str) -> object | None:
    value = facts.fields.get(name)
    return (
        value.value
        if isinstance(value, FieldValue) and value.status == ValueStatus.CONFIRMED
        else None
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _listing_key(facts: ListingFacts) -> _ListingKey | None:
    point = _confirmed(facts, "location_point")
    if not isinstance(point, dict) or point.get("precision") != "exact":
        return None
    building_id = point.get("building_id")
    if not isinstance(building_id, str) or not building_id.strip():
        return None
    rooms = _number(_confirmed(facts, "rooms"))
    floor = _number(_confirmed(facts, "floor"))
    area = _number(_confirmed(facts, "area_m2"))
    if (
        rooms is None
        or floor is None
        or area is None
        or not rooms.is_integer()
        or not floor.is_integer()
        or rooms < 0
        or floor < 1
        or area <= 0
    ):
        return None
    return _ListingKey(building_id, int(rooms), int(floor), area)


def _representatives(photos: Sequence[PhotoInput]) -> tuple[_Photo, ...]:
    if isinstance(photos, (str, bytes)) or not isinstance(photos, Sequence):
        raise TypeError("duplicate matching requires an ordered PhotoInput sequence")
    indices: set[int] = set()
    for photo in photos:
        if not isinstance(photo, PhotoInput):
            raise TypeError("duplicate matching requires PhotoInput records")
        if (
            isinstance(photo.image_index, bool)
            or not isinstance(photo.image_index, int)
            or photo.image_index < 0
            or photo.image_index in indices
        ):
            return ()
        indices.add(photo.image_index)
    result: list[_Photo] = []
    hashes: set[str] = set()
    urls: set[str] = set()
    for photo in sorted(photos, key=lambda item: item.image_index):
        if (
            photo.status != "indexed"
            or photo.duplicate_of_index is not None
            or not isinstance(photo.dhash, str)
            or re.fullmatch(r"[0-9a-fA-F]{16}", photo.dhash) is None
            or not isinstance(photo.source_url, str)
            or not photo.source_url.strip()
            or (
                photo.sha256 is not None
                and (
                    not isinstance(photo.sha256, str)
                    or re.fullmatch(r"[0-9a-fA-F]{64}", photo.sha256) is None
                )
            )
        ):
            continue
        bits = int(photo.dhash, 16)
        if bits in {0, (1 << 64) - 1}:
            continue  # Flat-image hashes do not provide useful matching evidence.
        if (
            photo.source_url in urls
            or (photo.sha256 is not None and photo.sha256.lower() in hashes)
            or any(
                (bits ^ item.bits).bit_count() <= _MAX_HASH_DISTANCE for item in result
            )
        ):
            continue
        result.append(_Photo(photo.image_index, photo.dhash.lower(), bits))
        urls.add(photo.source_url)
        if photo.sha256 is not None:
            hashes.add(photo.sha256.lower())
    return tuple(result)


def _unique_nearest(distances: Sequence[int]) -> int | None:
    minimum = min(distances, default=_MAX_HASH_DISTANCE + 1)
    if minimum > _MAX_HASH_DISTANCE or distances.count(minimum) != 1:
        return None
    return distances.index(minimum)


def match_duplicate(
    facts: ListingFacts,
    photos: Sequence[PhotoInput],
    other_facts: ListingFacts,
    other_photos: Sequence[PhotoInput],
) -> DuplicateMatch | None:
    """Match exact buildings and measurements with two independent photo pairs.

    Source IDs are scoped to their source and may coincide across sources. Only
    confirmed canonical measurements qualify. Near-identical gallery images
    count once; ambiguous nearest photo matches provide no evidence.
    """

    if not isinstance(facts, ListingFacts) or not isinstance(other_facts, ListingFacts):
        raise TypeError("duplicate matching requires ListingFacts")
    if facts.source == other_facts.source:
        return None
    left_key, right_key = _listing_key(facts), _listing_key(other_facts)
    if (
        left_key is None
        or right_key is None
        or left_key.building_id != right_key.building_id
        or left_key.rooms != right_key.rooms
        or left_key.floor != right_key.floor
    ):
        return None
    area_difference = abs(left_key.area - right_key.area)
    if area_difference > 0.5 and not math.isclose(
        area_difference, 0.5, rel_tol=0, abs_tol=1e-9
    ):
        return None
    left, right = _representatives(photos), _representatives(other_photos)
    if len(left) < 2 or len(right) < 2:
        return None
    distances = tuple(
        tuple((first.bits ^ second.bits).bit_count() for second in right)
        for first in left
    )
    nearest_left = tuple(_unique_nearest(row) for row in distances)
    nearest_right = tuple(
        _unique_nearest(tuple(row[index] for row in distances))
        for index in range(len(right))
    )
    pairs = tuple(
        PhotoMatch(
            left[index].image_index,
            right[target].image_index,
            left[index].dhash,
            right[target].dhash,
            distances[index][target],
        )
        for index, target in enumerate(nearest_left)
        if target is not None and nearest_right[target] == index
    )
    if len(pairs) < 2:
        return None
    return DuplicateMatch(
        left_key.building_id,
        left_key.rooms,
        left_key.floor,
        (facts.source, facts.source_listing_id),
        (other_facts.source, other_facts.source_listing_id),
        (left_key.area, right_key.area),
        (len(left), len(right)),
        pairs,
    )


__all__ = ["DuplicateMatch", "PhotoMatch", "match_duplicate"]
