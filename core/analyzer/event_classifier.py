from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from core.auth.headers import AUTH_HEADERS
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
    # Cloudflare's own endpoints (bot checks, beacons, email obfuscation)
    "cdn-cgi",
    # Next.js build assets and page data; the URLs change on every deploy
    "_next",
    # Page data files from static site generators (Gatsby, react-static)
    "page-data.json",
    "app-data.json",
    "routeinfo.json",
}
# Segment prefixes for tracker scripts/pixels (e.g. /ga.js, /gtm.js)
BLOCKED_SEGMENT_PREFIXES = ("ga.", "gtm.", "__utm")

# Logging in is the user's job, not a tool's: these steps (and 2FA polling)
# never become tools.
AUTH_FLOW_SEGMENTS = {
    "login", "logout", "signin", "signout", "sign_in", "sign_out", "sign-in", "sign-out",
    "signup", "sign_up", "sign-up", "two-factor", "two_factor", "2fa", "mfa", "u2f",
    "webauthn", "otp", "password_reset", "forgot_password", "reset_password",
}
# Endpoints that hand out tokens (".../token", "/cmc-api-token", "/csrf_token").
# As tools they would give the AI live credentials.
_CREDENTIAL_SEGMENT = re.compile(r"(^|[-_.])(token|tokens|csrf)$", re.I)
# A password field in a JSON, URL-encoded or multipart body: a login, sign-up
# or password change, which must never become a tool
_PASSWORD_FIELD = re.compile(
    r'"[^"]*pass(?:word|wd)[^"]*"\s*:'
    r"|(?:^|&)[^=&]*pass(?:word|wd)[^=&]*="
    r'|name="[^"]*pass(?:word|wd)[^"]*"',
    re.I,
)
# Build files with a content hash in the name (opt-in.specrule-0a0e9d38bd18.json)
_HASHED_FILE = re.compile(r"[.-][0-9a-f]{8,}\.(?:json|js|mjs|css|map|wasm)$", re.I)

# Playwright resource types that are never API calls
NON_API_RESOURCE_TYPES = {
    "stylesheet", "image", "media", "font", "script", "texttrack",
    "manifest", "websocket", "eventsource", "ping",
}

# Second-level labels that act as public suffixes under country TLDs (example.co.uk)
_COUNTRY_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go"}
# Hosting platforms where every subdomain belongs to a different owner
_SHARED_HOSTING = (
    "github.io", "vercel.app", "netlify.app", "pages.dev", "workers.dev", "web.app",
    "firebaseapp.com", "herokuapp.com", "onrender.com", "fly.dev", "azurewebsites.net",
    "appspot.com", "cloudfront.net", "amazonaws.com", "supabase.co", "glitch.me",
    "replit.app", "ngrok.io", "ngrok-free.app", "blogspot.com",
)
# Embedded widgets that send their own authenticated calls (payments, chat,
# captchas). Their calls are dropped unless they are the site being recorded.
WIDGET_SITES = {
    "stripe.com", "stripe.network", "paypal.com", "braintreegateway.com",
    "zendesk.com", "zdassets.com", "hubspot.com", "hsforms.com", "drift.com",
    "crisp.chat", "tawk.to", "recaptcha.net", "hcaptcha.com", "gstatic.com",
}


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def site_of(host: str) -> str:
    """The registrable domain of a host: api.example.co.uk -> example.co.uk."""
    host = host.lower().rstrip(".")
    if not host or "." not in host or _is_ip(host):
        return host
    labels = host.split(".")
    for suffix in _SHARED_HOSTING:
        if host == suffix or host.endswith("." + suffix):
            return ".".join(labels[-(suffix.count(".") + 2):])
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _COUNTRY_SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _header(request: CapturedRequest, name: str) -> str:
    return next((v for k, v in request.request_headers.items() if k.lower() == name), "")


class EventClassifier:
    def __init__(
        self,
        blocked_domains: list[str] | None = None,
        include_pages: bool = False,
        site_url: str | None = None,
        allowed_hosts: list[str] | None = None,
    ):
        self.blocked_domains = [d.lower() for d in (blocked_domains or settings.blocked_domains)]
        # Keep full HTML page loads (useful for server-rendered sites without a JSON API)
        self.include_pages = include_pages
        # With a site URL, calls to other sites (fonts, widgets, ads) are dropped
        self.site_url = site_url
        self.allowed_hosts = [h.lower() for h in (allowed_hosts or [])]
        self._first_party: set[str] = set()

    def classify(self, requests: list[CapturedRequest]) -> list[CapturedRequest]:
        """Return only requests that are useful MCP tool candidates."""
        self._first_party = self.first_party_sites(requests)
        return [r for r in requests if not self._should_reject(r)]

    def first_party_sites(self, requests: list[CapturedRequest]) -> set[str]:
        """The recorded site, plus any site the user visited as a full page (not an iframe)."""
        if not self.site_url:
            return set()
        sites = {site_of(_host(self.site_url))}
        for r in requests:
            if r.resource_type == "document" and _header(r, "sec-fetch-dest") == "document":
                sites.add(site_of(_host(r.url)))
        return sites

    def get_rejection_reason(self, request: CapturedRequest) -> list[str]:
        reasons: list[str] = []
        if request.response_status >= 500:
            reasons.append(f"Server error status {request.response_status}")
        if request.method == "OPTIONS":
            reasons.append("Preflight OPTIONS request")
        if self._is_blocked_domain(request.url):
            reasons.append("Blocked domain (analytics/tracking)")
        if self._is_blocked_pattern(request.url):
            reasons.append("Tracking or site-internal URL pattern")
        if self._is_auth_flow(request.url):
            reasons.append("Login step or token endpoint (never a tool)")
        if request.response_status in (404, 405, 410):
            reasons.append(f"Failed request (HTTP {request.response_status})")
        # A redirected GET is just a page move, but a form POST that redirects
        # afterwards (to the thing it created) is a real action
        if request.response_status in (301, 302, 303, 307, 308) and request.method in ("GET", "HEAD"):
            reasons.append("Redirect response")
        if _PASSWORD_FIELD.search(request.request_body or ""):
            reasons.append("Sends a password (login or account change, never a tool)")
        rtype = request.resource_type
        if rtype in NON_API_RESOURCE_TYPES:
            reasons.append(f"Not an API call (resource type: {rtype})")
        elif rtype == "document" and request.method == "GET" and not self.include_pages:
            reasons.append("HTML page load (use --include-pages to keep)")
        if self.is_third_party(request):
            reasons.append(f"Third-party site {_host(request.url)} (use --allow-host to keep)")
        return reasons

    def is_third_party(self, request: CapturedRequest) -> bool:
        """True for calls to a site other than the one recorded, unless they look like its backend."""
        if not self._first_party:
            return False
        host = _host(request.url)
        site = site_of(host)
        if site in self._first_party:
            return False
        if any(host == h or host.endswith("." + h) for h in self.allowed_hosts):
            return False
        # Apps often call a hosted backend (Supabase, Firebase, AWS) with an
        # auth header. Keep those, but not the calls embedded widgets make.
        has_auth = any(k.lower() in AUTH_HEADERS for k in request.request_headers)
        return not has_auth or site in WIDGET_SITES

    def _should_reject(self, r: CapturedRequest) -> bool:
        return bool(self.get_rejection_reason(r))

    def _is_blocked_domain(self, url: str) -> bool:
        host = _host(url)
        return any(host == d or host.endswith("." + d) for d in self.blocked_domains)

    def _is_auth_flow(self, url: str) -> bool:
        segments = [s.lower() for s in urlparse(url).path.split("/") if s]
        return any(seg in AUTH_FLOW_SEGMENTS for seg in segments) or bool(
            segments and _CREDENTIAL_SEGMENT.search(segments[-1])
        )

    def _is_blocked_pattern(self, url: str) -> bool:
        segments = [s.lower() for s in urlparse(url).path.split("/") if s]
        if segments and _HASHED_FILE.search(segments[-1]):
            return True
        return any(
            seg in BLOCKED_SEGMENTS or seg.startswith(BLOCKED_SEGMENT_PREFIXES)
            for seg in segments
        )
