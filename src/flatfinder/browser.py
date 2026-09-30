"""Explicit ownership of the dedicated async Playwright browser context."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import BrowserContext, Page, TimeoutError, async_playwright

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
    "sign in",
    "log in",
    "email or phone",
    "электронная почта или телефон",
)


def classify_blocker(url: str = "", text: str = "") -> str | None:
    """Classify a CAPTCHA, login or two-factor gate without bypassing it."""

    url, text = url.lower(), text.lower()
    if any(token in url for token in _CAPTCHA_URL) or any(
        token in text for token in _CAPTCHA_TEXT
    ):
        return "captcha"
    if any(token in f"{url}\n{text}" for token in _TWO_FACTOR):
        return "2fa"
    if any(token in url for token in _LOGIN_URL) or any(
        token in text for token in _LOGIN_TEXT
    ):
        return "login"
    return None


async def detect_blocker(page: Page) -> str | None:
    """Inspect the current URL and visible body through the async Page API."""

    reason = classify_blocker(page.url)
    if reason:
        return reason
    try:
        text = await page.locator("body").inner_text(timeout=500)
    except TimeoutError:
        text = ""
    return classify_blocker(page.url, text)


@asynccontextmanager
async def browser_context(
    profile_dir: Path, headed: bool
) -> AsyncIterator[BrowserContext]:
    """Open and close one persistent context and its Playwright driver."""

    if not isinstance(profile_dir, Path):
        raise TypeError("browser profile must be a Path")
    if not isinstance(headed, bool):
        raise TypeError("headed must be a bool")
    profile_dir = profile_dir.expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir, headless=not headed
        )
        try:
            yield context
        finally:
            await context.close()


__all__ = ["browser_context", "classify_blocker", "detect_blocker"]
