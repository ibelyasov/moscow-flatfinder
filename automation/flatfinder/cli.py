"""Thin command-line adapter for MoscowFlatFinder application workflows."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .application import (
    analyze_photos,
    collect,
    doctor,
    login,
    reassess,
    record_personal_score,
    refresh_coordinates,
    refresh_noise_map,
    retry_routes,
    review,
)
from .config import (
    DEFAULT_CONFIG,
    Config,
    load_config,
    parse_listing_id,
    photo_cache_dir,
)


class AnotherRun(RuntimeError):
    pass


@contextmanager
def acquire_lock(path: str | Path) -> Iterator[None]:
    lock_path = Path(path).expanduser().resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AnotherRun("another run is active") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flatfinder")
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="path to config.toml"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor_command = commands.add_parser(
        "doctor", help="check local readiness without changing state"
    )
    doctor_command.add_argument("--json", action="store_true", help="emit JSON")
    login_command = commands.add_parser(
        "login", help="open the headed persistent profile for manual login"
    )
    run_command = commands.add_parser("run", help="discover and process listings")
    run_command.add_argument(
        "--refresh-vision",
        action="store_true",
        help="explicitly re-evaluate listings that already have a Vision result",
    )
    score_command = commands.add_parser(
        "personal-score", help="set a manual score within the configured range"
    )
    score_command.add_argument("listing_id")
    score_command.add_argument("score")
    vision_command = commands.add_parser(
        "vision", help="evaluate one listing with the configured Vision CLI"
    )
    vision_command.add_argument("--listing-id", required=True)
    vision_command.add_argument(
        "--force", action="store_true", help="ignore the unchanged-content hash"
    )
    review_command = commands.add_parser(
        "review", help="review local visual proposals on loopback"
    )
    review_command.add_argument("--port", type=int, default=8765)
    review_command.add_argument("--listing-id")
    retry_routes_command = commands.add_parser(
        "retry-routes", help="retry incomplete Yandex Maps routes"
    )
    retry_routes_command.add_argument(
        "--listing-id", type=int, help="retry only one internal listing id"
    )
    refresh_coordinates_command = commands.add_parser(
        "refresh-coordinates",
        help="replace listing coordinates with 2GIS and recalculate point-dependent checks",
    )
    refresh_coordinates_command.add_argument(
        "--listing-id", type=int, help="refresh only one internal listing id"
    )
    refresh_coordinates_command.add_argument(
        "--after-id", type=int, help="resume with listings after this internal id"
    )
    noise_command = commands.add_parser(
        "refresh-noise-map", help="build the local OSM road and rail layer"
    )
    noise_command.add_argument(
        "--source", help="local OSM/PBF path or URL (defaults to BBBike Moscow)"
    )
    reassess_command = commands.add_parser(
        "reassess",
        help="recompute current assessments from saved listing facts",
    )
    reassess_command.add_argument(
        "listing_id",
        nargs="?",
        help="one internal listing id; all active listings if omitted",
    )
    for command in (
        doctor_command,
        login_command,
        run_command,
        score_command,
        vision_command,
        review_command,
        retry_routes_command,
        refresh_coordinates_command,
        noise_command,
        reassess_command,
    ):
        command.add_argument(
            "--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            return doctor(config, json_output=args.json)
        with acquire_lock(config.lock_path):
            if args.command == "login":
                return asyncio.run(login(config))
            if args.command == "run":
                return asyncio.run(collect(config, refresh_vision=args.refresh_vision))
            if args.command == "personal-score":
                return record_personal_score(config, args.listing_id, args.score)
            if args.command == "vision":
                return analyze_photos(config, args.listing_id, args.force)
            if args.command == "retry-routes":
                return asyncio.run(retry_routes(config, args.listing_id))
            if args.command == "refresh-coordinates":
                return asyncio.run(
                    refresh_coordinates(config, args.listing_id, args.after_id)
                )
            if args.command == "refresh-noise-map":
                return refresh_noise_map(config, args.source)
            if args.command == "review":
                return review(config, args.port, args.listing_id)
            if args.command == "reassess":
                return reassess(config, args.listing_id)
    except AnotherRun as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        print(f"flatfinder: {error}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AnotherRun",
    "Config",
    "acquire_lock",
    "load_config",
    "main",
    "parse_listing_id",
    "photo_cache_dir",
]
