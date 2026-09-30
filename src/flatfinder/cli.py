"""Command-line adapter for explicit application operations."""

import argparse
import asyncio
import json
import sqlite3
import sys

from . import application
from .config import DEFAULT_CONFIG, load_config
from .locking import AnotherRun


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flatfinder")
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="local config.toml path"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create a new current-schema database")
    commands.add_parser(
        "doctor",
        help="check local files without browser, credentials or provider calls",
    )
    commands.add_parser("login", help="open the dedicated browser for manual login")
    run = commands.add_parser("run", help="discover and collect configured searches")
    run.add_argument(
        "--refresh-vision",
        action="store_true",
        help="explicitly repeat Vision inference",
    )
    for name in ("reassess", "enrich"):
        command = commands.add_parser(
            name,
            help="recompute saved assessments"
            if name == "reassess"
            else "acquire enabled Geo/Noise measurements",
        )
        command.add_argument("listing_id", nargs="?", type=int)
        if name == "enrich":
            command.add_argument(
                "--force", action="store_true", help="ignore cached measurements"
            )
    score = commands.add_parser("personal-score", help="save a personal score")
    score.add_argument("listing_id", type=int)
    score.add_argument("score", type=float)
    vision = commands.add_parser("vision", help="analyze current photos of one listing")
    vision.add_argument("listing_id", type=int)
    vision.add_argument("--force", action="store_true")
    decision = commands.add_parser(
        "vision-review", help="accept or reject a pending Vision run"
    )
    decision.add_argument("run_id", type=int)
    choice = decision.add_mutually_exclusive_group(required=True)
    choice.add_argument("--accept", action="store_true")
    choice.add_argument("--reject", action="store_true")
    review = commands.add_parser("review", help="open the local review interface")
    review.add_argument("--port", type=int, default=8765)
    review.add_argument("--listing-id", type=int)
    noise = commands.add_parser(
        "refresh-noise-map", help="build Noise from a declared complete OSM extract"
    )
    noise.add_argument("--source", help="local OSM/PBF file or URL")
    noise.add_argument(
        "--bounds",
        nargs=4,
        type=float,
        required=True,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
    )
    backup = commands.add_parser(
        "backup", help="create a verified SQLite backup; pruning is explicit"
    )
    backup.add_argument(
        "--keep", type=int, help="prune older backups only after durable publication"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        command = args.command
        if command == "review":
            return application.review(config, args.port, args.listing_id)
        if command == "login":
            return asyncio.run(application.login(config))
        if command == "init":
            result = application.initialize(config)
        elif command == "doctor":
            result = application.doctor(config)
        elif command == "run":
            result = asyncio.run(
                application.collect(config, refresh_vision=args.refresh_vision)
            )
        elif command == "enrich":
            result = asyncio.run(
                application.enrich(config, args.listing_id, force=args.force)
            )
        elif command == "reassess":
            result = application.reassess(config, args.listing_id)
        elif command == "vision":
            result = application.analyze_photos(
                config, args.listing_id, force=args.force
            )
        elif command == "personal-score":
            result = {
                "warnings": application.record_review(
                    config, args.listing_id, personal_score=args.score
                )
            }
        elif command == "vision-review":
            result = {
                "warnings": application.review_vision(config, args.run_id, args.accept)
            }
        elif command == "refresh-noise-map":
            result = application.refresh_noise_map(
                config, args.source, tuple(args.bounds)
            )
        elif command == "backup":
            result = application.backup(config, keep=args.keep)
        else:
            raise AssertionError("unhandled command")
        if isinstance(result, application.OperationResult):
            print(json.dumps(result.to_dict(), ensure_ascii=False))
            return 2 if result.blocked_reason else 1 if result.failed else 0
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("ok") is False or result.get("warnings") else 0
    except AnotherRun as error:
        print(f"flatfinder: {error}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, RuntimeError, ValueError, TypeError) as error:
        print(f"flatfinder: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
