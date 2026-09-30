"""Generate a deterministic demonstration database entirely from invented data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from flatfinder.config import load_config
from flatfinder.models import Evidence, FieldValue, ListingFacts, ValueStatus

if __package__:
    from .import_v17 import publish_new_database
else:
    from import_v17 import publish_new_database


DEMO_TIME = "2026-01-01T12:00:00+00:00"


def demo_records(count: int) -> list[dict]:
    if not 1 <= count <= 24:
        raise ValueError("demo count must be inside [1,24]")
    records = []
    for index in range(1, count + 1):
        source = "cian" if index % 2 else "yandex"
        values = {
            "title": f"Демо-квартира {index:02d} — полностью вымышленные данные",
            "address": f"Демонстрационная улица, дом {index}, Москва",
            "location_point": {
                "lat": round(55.735 + (index // 6) * 0.004, 6),
                "lon": round(37.585 + (index % 6) * 0.006, 6),
            },
            "price_monthly": 65000 + index * 2500,
            "area_m2": 32 + index * 2,
            "rooms": 1 if index % 3 else 2,
            "floor": 2 + index % 7,
            "total_floors": 12,
            "building_year": 2010 + index % 10,
            "furnished": True,
            "lease_term": "long_term",
            "photos": [],
            "photos_total": 0,
            "photos_observed": 0,
        }
        fields = {
            name: FieldValue(
                value,
                ValueStatus.CONFIRMED,
                [
                    Evidence(
                        "synthetic_demo",
                        "Invented demo value; no real listing",
                        DEMO_TIME,
                    )
                ],
            )
            for name, value in values.items()
        }
        facts = ListingFacts(
            source_listing_id=f"DEMO-{index:03d}",
            source_url=f"https://example.invalid/listing/demo-{index:03d}",
            fields=fields,
            source=source,
        )
        records.append(
            {
                "id": index,
                "facts": facts,
                "observations": [
                    {
                        "facts": facts,
                        "kind": "import",
                        "parser_version": "synthetic-demo-v1",
                        "captured_at": DEMO_TIME,
                    }
                ],
                "current_observation_index": 0,
                "availability": "available",
                "first_seen_at": DEMO_TIME,
                "last_seen_at": DEMO_TIME,
                "manual": {
                    "personal_score": 3 if index == 1 else 0,
                    "favorite": index == 1,
                    "disliked": index == 3,
                    "personal_rated_at": DEMO_TIME if index == 1 else None,
                    "favorited_at": DEMO_TIME if index == 1 else None,
                    "disliked_at": DEMO_TIME if index == 3 else None,
                },
                "photos": [],
            }
        )
    return records


def generate_demo(target: Path, *, count: int = 6) -> dict:
    records = demo_records(count)
    policy = load_config(
        Path(__file__).resolve().parents[1] / "examples" / "config.toml"
    ).policy

    def populate(database):
        database.import_records(records, [], policy, imported_at=DEMO_TIME)

    publish_new_database(target, populate)
    return {
        "target": str(target),
        "listings": count,
        "synthetic": True,
        "fixed_time": DEMO_TIME,
        "integrity": "ok",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--count", type=int, default=6)
    args = parser.parse_args()
    print(json.dumps(generate_demo(args.target, count=args.count), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
