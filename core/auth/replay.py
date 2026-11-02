"""Turn a recorded browser auth state into headers for replaying a request.

The recorder stores, per session:
  cookie_jar    full cookies with domain/path, so each request only gets the
                cookies the browser would have sent to that host
  host_headers  the latest replayable request headers per host (Authorization,
                CSRF tokens, custom x-* headers, user-agent, ...)

Older sessions only have a flat `cookies` dict and global `auth_headers` /
`replay_headers`; those are used as a fallback.
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any
from urllib.parse import unquote, urlparse

from core.auth.csrf import CSRF_COOKIES, CSRF_HEADERS
from core.auth.headers import AUTH_HEADERS, BROWSER_ONLY_HEADERS
from core.storage.models import CapturedRequest

# Headers we never replay: the HTTP client sets them, or they're tied to one request.
NON_REPLAY_HEADERS = BROWSER_ONLY_HEADERS | {
    "cookie",
    "content-length",
    "content-type",
    "accept-encoding",
    "priority",
}

API_RESOURCE_TYPES = {"xhr", "fetch"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _replayable(name: str) -> bool:
    return not name.startswith(":") and name not in NON_REPLAY_HEADERS


def collect_host_headers(requests: list[CapturedRequest]) -> dict[str, dict[str, str]]:
    """Find the headers worth replaying for each host.

    Auth and CSRF headers are always kept (latest value wins). Other headers are
    kept when they appear on at least half of that host's API requests, which
    filters out one-off headers like upload metadata.
    """
    by_host: dict[str, list[CapturedRequest]] = {}
    for req in requests:
        if req.resource_type and req.resource_type not in API_RESOURCE_TYPES:
            continue
        by_host.setdefault(_host(req.url), []).append(req)

    result: dict[str, dict[str, str]] = {}
    for host, reqs in by_host.items():
        reqs = sorted(reqs, key=lambda r: r.timestamp)
        counts: Counter[str] = Counter()
        latest: dict[str, str] = {}
        for req in reqs:
            for name, value in req.request_headers.items():
                key = name.lower()
                if not _replayable(key):
                    continue
                counts[key] += 1
                latest[key] = value
        keep = {
            k: v for k, v in latest.items()
            if k in AUTH_HEADERS or k in CSRF_HEADERS or counts[k] / len(reqs) >= 0.5
        }
        if keep:
            result[host] = keep
    return result


def _domain_matches(host: str, cookie_domain: str) -> bool:
    domain = cookie_domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


def _cookies_for_url(auth_state: dict[str, Any], url: str) -> dict[str, str]:
    jar = auth_state.get("cookie_jar")
    if not jar:
        return dict(auth_state.get("cookies") or {})

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    now = time.time()
    matched: dict[str, str] = {}
    # Longer paths first, so the most specific cookie wins on name clashes
    for c in sorted(jar, key=lambda c: len(c.get("path") or "/"), reverse=True):
        if not _domain_matches(host, c.get("domain", "")):
            continue
        if not path.startswith(c.get("path") or "/"):
            continue
        expires = c.get("expires", -1)
        if expires not in (None, -1) and expires < now:
            continue
        if c.get("secure") and parsed.scheme != "https" and host not in ("localhost", "127.0.0.1"):
            continue
        matched.setdefault(c["name"], c["value"])
    return matched


def build_request_headers(auth_state: dict[str, Any], url: str) -> dict[str, str]:
    """Return lowercase headers (including Cookie) to send with a request to *url*."""
    host = _host(url)
    host_headers = auth_state.get("host_headers") or {}

    headers: dict[str, str] = {}
    if host in host_headers:
        headers.update(host_headers[host])
    else:
        # Legacy sessions: global headers, minus ones that must not be replayed
        for source in ("replay_headers", "auth_headers"):
            for k, v in (auth_state.get(source) or {}).items():
                if _replayable(k.lower()):
                    headers[k.lower()] = v

    cookies = _cookies_for_url(auth_state, url)
    if cookies:
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        # Double-submit CSRF: the header must match the current CSRF cookie
        csrf_cookie = next(
            (v for k, v in cookies.items() if k.lower() in CSRF_COOKIES), None
        )
        if csrf_cookie:
            for name in CSRF_HEADERS:
                if name in headers:
                    headers[name] = unquote(csrf_cookie)

    return headers
