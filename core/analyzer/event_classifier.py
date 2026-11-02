from __future__ import annotations

from urllib.parse import urlparse

from core.config import settings
from core.storage.models import CapturedRequest

# Path segments that mark analytics/tracking endpoints. Matched against whole
# segments, so "/collect" is blocked but "/collections" is not.
BLOCKED_SEGMENTS = {
    "analytics",
    "telemetry",
    "metrics",
    "ping",
    "beacon",
    "collect",
    "track",
    "tracking",
    "__utm.gif",
    "fbq",
    "rum",
}
# Segment prefixes for tracker scripts/pixels (e.g. /ga.js, /gtm.js)
BLOCKED_SEGMENT_PREFIXES = ("ga.", "gtm.", "__utm")

# Playwright resource types that are never API calls
NON_API_RESOURCE_TYPES = {
    "stylesheet", "image", "media", "font", "script", "texttrack",
    "manifest", "websocket", "eventsource", "ping",
}


class EventClassifier:
    def __init__(
        self,
        blocked_domains: list[str] | None = None,
        include_pages: bool = False,
    ):
        self.blocked_domains = [d.lower() for d in (blocked_domains or settings.blocked_domains)]
        # Keep full HTML page loads (useful for server-rendered sites without a JSON API)
        self.include_pages = include_pages

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
            reasons.append("Blocked domain (analytics/tracking)")
        if self._is_blocked_pattern(request.url):
            reasons.append("Analytics/telemetry URL pattern")
        if request.response_status in (301, 302, 303, 307, 308):
            reasons.append("Redirect response")
        rtype = request.resource_type
        if rtype in NON_API_RESOURCE_TYPES:
            reasons.append(f"Not an API call (resource type: {rtype})")
        elif rtype == "document" and request.method == "GET" and not self.include_pages:
            reasons.append("HTML page load (use --include-pages to keep)")
        return reasons

    def _should_reject(self, r: CapturedRequest) -> bool:
        return bool(self.get_rejection_reason(r))

    def _is_blocked_domain(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self.blocked_domains)

    def _is_blocked_pattern(self, url: str) -> bool:
        segments = [s.lower() for s in urlparse(url).path.split("/") if s]
        return any(
            seg in BLOCKED_SEGMENTS or seg.startswith(BLOCKED_SEGMENT_PREFIXES)
            for seg in segments
        )
