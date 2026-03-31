from __future__ import annotations

import json
import re
from dataclasses import dataclass

from playwright.async_api import BrowserContext


@dataclass
class StorageSnapshot:
    local_storage: dict[str, str]
    session_storage: dict[str, str]
    origin: str


JWT_PATTERN = re.compile(r'^[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+$')
API_KEY_PATTERN = re.compile(r'^[A-Za-z0-9_\-]{20,}$')


class StorageTokenExtractor:
    async def extract(self, context: BrowserContext, origin: str) -> StorageSnapshot:
        """Extract localStorage and sessionStorage from the browser context."""
        page = await context.new_page()
        try:
            await page.goto(origin)
            local = await page.evaluate(
                "() => Object.fromEntries(Object.entries(localStorage))"
            )
            session = await page.evaluate(
                "() => Object.fromEntries(Object.entries(sessionStorage))"
            )
            return StorageSnapshot(
                local_storage=local or {},
                session_storage=session or {},
                origin=origin,
            )
        finally:
            await page.close()

    def detect_auth_tokens(self, snapshot: StorageSnapshot) -> list[str]:
        """Return keys whose values look like JWTs or API keys."""
        auth_keys: list[str] = []
        all_storage = {**snapshot.local_storage, **snapshot.session_storage}
        for key, value in all_storage.items():
            if JWT_PATTERN.match(value) or (
                API_KEY_PATTERN.match(value) and len(value) >= 32
            ):
                auth_keys.append(key)
        return auth_keys
