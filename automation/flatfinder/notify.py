"""Local notifications and durable SQLite backups for FlatFinder."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_APPLE_SCRIPT = """on run argv
    if (count of argv) is not 2 then error "invalid notification arguments"
    display notification (item 2 of argv) with title (item 1 of argv)
end run"""

_MESSAGES = {
    "new_candidates": (
        "FlatFinder: новые варианты",
        lambda count: f"Найдено новых подходящих объявлений: {count}.",
    ),
    "captcha": (
        "FlatFinder: нужна проверка",
        lambda count: "Обнаружена CAPTCHA. Проверьте браузерный профиль.",
    ),
    "login": (
        "FlatFinder: нужен вход",
        lambda count: "Сессия источника объявлений требует повторного входа.",
    ),
    "2fa": (
        "FlatFinder: нужна двухфакторная проверка",
        lambda count: "Подтвердите вход в браузерном профиле.",
    ),
    "parser_drift": (
        "FlatFinder: изменилась страница",
        lambda count: "Структура страницы изменилась. Запуск остановлен до проверки.",
    ),
    "three_failed": (
        "FlatFinder: три запуска неудачны",
        lambda count: "Три последовательных запуска завершились ошибкой.",
    ),
}


def notify(event_kind: str, count: int = 1) -> None:
    """Show one predefined macOS notification without accepting page text."""

    if not isinstance(event_kind, str) or event_kind not in _MESSAGES:
        raise ValueError(f"unsupported notification kind: {event_kind!r}")
    if type(count) is not int or count < 0:
        raise ValueError("notification count must be a non-negative integer")
    title, make_body = _MESSAGES[event_kind]
    body = make_body(count)
    subprocess.run(
        ["/usr/bin/osascript", "-e", _APPLE_SCRIPT, "--", title, body],
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )


def _backup_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("flatfinder-*.sqlite3"), key=lambda path: path.name)


def _prune_backups(directory: Path, keep: int) -> None:
    for path in _backup_files(directory)[:-keep]:
        path.unlink()


def backup_database(
    conn: sqlite3.Connection,
    destination: str | Path,
    keep: int = 7,
) -> Path:
    """Create, verify, atomically publish, and retain a SQLite backup."""

    if type(keep) is not int or keep < 1:
        raise ValueError("keep must be a positive integer")
    directory = Path(destination).expanduser()
    if directory.exists() and not directory.is_dir():
        raise NotADirectoryError(directory)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"flatfinder-{timestamp}.sqlite3"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=directory,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)

        backup_conn = sqlite3.connect(temporary_path)
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()

        with temporary_path.open("rb") as handle:
            os.fsync(handle.fileno())
        check_conn = sqlite3.connect(temporary_path)
        try:
            result = check_conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            check_conn.close()
        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError(f"backup integrity check failed: {result!r}")

        os.replace(temporary_path, target)
        temporary_path = None
        _prune_backups(directory, keep)
        return target
    except BaseException:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        raise


__all__ = ["backup_database", "notify"]
