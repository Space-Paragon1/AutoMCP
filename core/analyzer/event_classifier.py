from __future__ import annotations

from urllib.parse import urlparse

from core.config import settings
from core.storage.models import CapturedRequest

# Blocked URL patterns (analytics, tracking noise)
BLOCKED_PATTERNS = [
    "/analytics",
    "/telemetry",
    "/metrics",
    "/ping",
    "/beacon",
    "/collect",
    "/track",
    "/__utm",
    "/ga.",
    "/gtm.",
    "/fbq",
]


class EventClassifier:
    def __init__(self, blocked_domains: list[str] | None = None):
        self.blocked_domains = blocked_domains or settings.blocked_domains

    def classify(self, requests: list[CapturedRequest]) -> list[CapturedRequest]:
        """Return only requests that are useful MCP tool candidates."""
        return [r for r in requests if not self._should_reject(r)]

    def get_rejection_reason(self, request: CapturedRequest) -> list[str]:
        reasons: list[str] = []
        if request.response_status >= 500:
            reasons.append(f"Server error status {request.response_status}")
        if request.method == "OPTIONS":
            reasons.append("Preflight OPTIONS request")
        if self._is_blocked_domain(request.url):
            reasons.append("Blocked domain (analytics/CDN)")
        if self._is_blocked_pattern(request.url):
            reasons.append("Analytics/telemetry URL pattern")
        if request.response_status in (301, 302, 303, 307, 308):
            reasons.append("Redirect response")
        return reasons

    def _should_reject(self, r: CapturedRequest) -> bool:
        return bool(self.get_rejection_reason(r))

    def _is_blocked_domain(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(domain in host for domain in self.blocked_domains)

    def _is_blocked_pattern(self, url: str) -> bool:
        url_lower = url.lower()
        return any(pattern in url_lower for pattern in BLOCKED_PATTERNS)
