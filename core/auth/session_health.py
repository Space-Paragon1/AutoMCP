"""Track auth session health and detect expired credentials."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from core.config import settings


@dataclass
class AuthHealth:
    session_id: str
    last_checked: datetime
    is_valid: bool
    status_code: int | None = None
    error: str | None = None
    cookies_age_hours: float = 0.0


@dataclass
class RefreshStrategy:
    """Describes how to re-authenticate for a session."""
    session_id: str
    login_url: str
    method: str = "POST"
    credential_keys: list[str] = field(default_factory=list)  # vault keys to use
    body_template: dict[str, str] = field(default_factory=dict)  # {"username": "$vault.USER", "password": "$vault.PASS"}
    success_status: int = 200


class SessionHealthChecker:
    """Check if recorded auth credentials are still valid."""

    async def check(self, probe_url: str, cookies: dict[str, str]) -> AuthHealth:
        """Hit a probe URL with stored cookies and check if still authenticated."""
        from core.storage.db import AsyncDatabase
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    probe_url,
                    headers={"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())},
                    follow_redirects=False,
                )
                is_valid = resp.status_code < 400 and resp.status_code not in (301, 302)
                return AuthHealth(
                    session_id="",
                    last_checked=datetime.utcnow(),
                    is_valid=is_valid,
                    status_code=resp.status_code,
                )
        except Exception as e:
            return AuthHealth(
                session_id="",
                last_checked=datetime.utcnow(),
                is_valid=False,
                error=str(e),
            )

    def get_cookies_age_hours(self, session_state: dict) -> float:
        """Return how many hours ago cookies were captured."""
        recorded_at = session_state.get("recorded_at")
        if not recorded_at:
            return 999.0
        try:
            dt = datetime.fromisoformat(recorded_at)
            return (datetime.utcnow() - dt).total_seconds() / 3600
        except Exception:
            return 999.0
