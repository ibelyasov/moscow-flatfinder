"""Small Playwright boundary for FlatFinder's persistent browser profile."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - dependency is installed in production
    async_playwright = None


_PLAYWRIGHT_BY_CONTEXT: dict[int, Any] = {}

_BACKGROUND_BROWSER_SCRIPT = r"""
on run argv
    set previousName to item 1 of argv
    set browserName to "Google Chrome for Testing"
    repeat
        tell application "System Events"
            set frontProcess to first application process whose frontmost is true
            set currentName to name of frontProcess
            if currentName is browserName then
                set visible of frontProcess to false
                if exists application process previousName then
                    set frontmost of application process previousName to true
                end if
            else
                set previousName to currentName
            end if
        end tell
        delay 0.1
    end repeat
end run
"""

_CAPTCHA_URL = ("captcha", "recaptcha", "hcaptcha", "challenge", "verify-human")
_CAPTCHA_TEXT = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "капч",
    "я не робот",
    "не робот",
    "проверка безопасности",
    "security check",
    "verify you are human",
    "подтвердите, что вы человек",
    "подтвердите, что вы не робот",
    "подозрительный трафик",
    "cian_waf_block",
)
_TWO_FACTOR = (
    "2fa",
    "two-factor",
    "two factor",
    "двухфактор",
    "двухэтапн",
    "код подтверждения",
    "код из sms",
    "код из смс",
    "смс-код",
    "sms-code",
    "sms code",
    "sms verification",
    "смс подтверждения",
    "смс-подтверждение",
    "verification code",
    "one-time password",
    "one time code",
    "одноразовый пароль",
    "подтвердите код",
)
_LOGIN_URL = ("/login", "/signin", "/sign-in", "/auth", "passport.yandex", "oauth")
_LOGIN_TEXT = (
    "войдите в аккаунт",
    "войти в аккаунт",
    "авторизуйтесь",
    "авторизация",
    "логин",
    "sign in",
    "log in",
    "login",
    "email or phone",
    "электронная почта или телефон",
)


def _config(config: Any, name: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default)


def find_vault_root(start: str | Path) -> Path | None:
    """Find the nearest vault marker without assuming the current directory."""

    path = Path(start).expanduser().resolve()
    if path.is_file() or path.suffix in {".toml", ".json"}:
        path = path.parent
    for candidate in (path, *path.parents):
        if (candidate / ".vault-config.json").is_file() or (
            candidate / ".obsidian"
        ).is_dir():
            return candidate
    return None


def classify_blocker(url: str = "", text: str = "") -> str | None:
    """Classify visible or exception text using one shared token set."""

    url, text = str(url or "").lower(), str(text or "").lower()
    if any(token in url for token in _CAPTCHA_URL) or any(
        token in text for token in _CAPTCHA_TEXT
    ):
        return "captcha"
    haystack = f"{url}\n{text}"
    if any(token in haystack for token in _TWO_FACTOR):
        return "2fa"
    if any(token in url for token in _LOGIN_URL) or any(
        token in text for token in _LOGIN_TEXT
    ):
        return "login"
    return None


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def prepare_profile_dir(config: Any) -> Path:
    """Validate and prepare the one private profile shared by login and Crawlee."""

    profile = _config(config, "profile_dir")
    if not profile:
        raise ValueError("profile_dir is required for a persistent browser context")
    profile_dir = Path(str(profile)).expanduser().resolve()
    vault_root = _config(config, "vault_root")
    if vault_root is None:
        config_path = _config(config, "config_path")
        vault_root = find_vault_root(config_path or Path.cwd()) or find_vault_root(
            profile_dir
        )
    if vault_root is not None:
        vault_root = Path(str(vault_root)).expanduser().resolve()
        if profile_dir == vault_root or vault_root in profile_dir.parents:
            raise ValueError("profile_dir must be outside the Obsidian vault")
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)
    return profile_dir


def start_browser_background_watcher(enabled: bool) -> subprocess.Popen[Any] | None:
    """Keep FlatFinder's headed Chromium hidden without affecting login."""

    if not enabled or sys.platform != "darwin":
        return None
    frontmost = subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to get name of first application process whose frontmost is true',
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    if not frontmost:
        raise RuntimeError("cannot determine the frontmost macOS application")
    return subprocess.Popen(
        ["osascript", "-e", _BACKGROUND_BROWSER_SCRIPT, frontmost],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_browser_background_watcher(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


async def open_context(config: Any, headed: bool = False) -> Any:
    """Open the one dedicated persistent context used by FlatFinder.

    Playwright owns cookies/local storage inside ``profile_dir``.  We never
    call ``storage_state`` and do not pass a browser channel or executable, so
    the configured directory cannot accidentally become Chrome's default
    profile.
    """

    profile_dir = prepare_profile_dir(config)
    if (
        async_playwright is None
    ):  # pragma: no cover - dependency is installed in production
        raise RuntimeError("Playwright is required for browser commands")

    playwright = await async_playwright().start()
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=not bool(headed),
        )
    except BaseException:
        await playwright.stop()
        raise
    _PLAYWRIGHT_BY_CONTEXT[id(context)] = playwright
    return context


async def close_context(context: Any) -> None:
    """Close a context and its Playwright driver, tolerating fake contexts."""

    if context is None:
        return
    playwright = _PLAYWRIGHT_BY_CONTEXT.pop(id(context), None)
    try:
        close = getattr(context, "close", None)
        if close is not None:
            await _await(close())
    finally:
        if playwright is not None:
            await _await(playwright.stop())


async def _visible_text(page: Any) -> str:
    locator = getattr(page, "locator", None)
    if callable(locator):
        try:
            value = await _await(locator("body").inner_text(timeout=500))
            if isinstance(value, str):
                return value
        except Exception:
            pass
    inner_text = getattr(page, "inner_text", None)
    if callable(inner_text):
        try:
            value = await _await(inner_text("body"))
            if isinstance(value, str):
                return value
        except Exception:
            pass
    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            value = await _await(evaluate("() => document.body?.innerText || ''"))
            if isinstance(value, str):
                return value
        except Exception:
            pass
    return ""


async def detect_blocker(page: Any) -> str | None:
    """Return ``login``, ``2fa`` or ``captcha`` when visibly blocked."""

    raw_url = getattr(page, "url", "")
    raw_url = await _await(raw_url) if callable(raw_url) else raw_url
    url = str(raw_url or "").lower()
    text = (await _visible_text(page)).lower()
    return classify_blocker(url, text)


__all__ = [
    "classify_blocker",
    "close_context",
    "detect_blocker",
    "find_vault_root",
    "open_context",
    "prepare_profile_dir",
    "start_browser_background_watcher",
    "stop_browser_background_watcher",
]
