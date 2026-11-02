"""Track auth session health and detect expired credentials."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from core.storage.models import utcnow


@dataclass
class AuthHealth:
    session_id: str
    last_checked: datetime
    is_valid: bool
    status_code: int | None = None
    error: str | None = None
    cookies_age_hours: float = 0.0


class SessionHealthChecker:
    """Check if recorded auth credentials are still valid."""

    async def check(
        self,
        probe_url: str,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> AuthHealth:
        """Hit a probe URL with stored credentials and check if still authenticated."""
        request_headers = dict(headers or {})
        if cookies:
            request_headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    probe_url,
                    headers=request_headers,
                    follow_redirects=False,
                )
                is_valid = resp.status_code < 400 and resp.status_code not in (301, 302)
                return AuthHealth(
                    session_id="",
                    last_checked=utcnow(),
                    is_valid=is_valid,
                    status_code=resp.status_code,
                )
        except Exception as e:
            return AuthHealth(
                session_id="",
                last_checked=utcnow(),
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
            return (utcnow() - dt).total_seconds() / 3600
        except Exception:
            return 999.0
