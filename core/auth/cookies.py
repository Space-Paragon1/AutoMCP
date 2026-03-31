from __future__ import annotations

import json

from playwright.async_api import BrowserContext

from core.storage.models import CapturedRequest


class CookieStrategy:
    """Handles cookie-based authentication extraction and replay."""

    async def extract(self, context: BrowserContext) -> dict[str, str]:
        """Extract all cookies from the Playwright browser context."""
        cookies = await context.cookies()
        return {c["name"]: c["value"] for c in cookies}

    def inject(self, headers: dict[str, str], cookies: dict[str, str]) -> dict[str, str]:
        """Inject cookies as Cookie header."""
        if not cookies:
            return headers
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return {**headers, "Cookie": cookie_str}

    def serialize(self, cookies: dict[str, str]) -> str:
        return json.dumps(cookies)

    def deserialize(self, raw: str) -> dict[str, str]:
        return json.loads(raw)

    def detect_auth_cookies(self, cookies: dict[str, str]) -> list[str]:
        """Heuristically identify cookies likely to be auth-related."""
        auth_indicators = ["session", "token", "auth", "jwt", "sid", "user", "login", "csrf"]
        return [
            name for name in cookies
            if any(indicator in name.lower() for indicator in auth_indicators)
        ]
