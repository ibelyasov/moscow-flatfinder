"""Bounded local notifications for FlatFinder."""

from __future__ import annotations

import subprocess

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
        timeout=3,
    )


__all__ = ["notify"]
