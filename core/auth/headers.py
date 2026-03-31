from __future__ import annotations

from dataclasses import dataclass, field

from core.storage.models import CapturedRequest

# Headers that identify the browser — never replay these
BROWSER_ONLY_HEADERS = frozenset({
    "host",
    "connection",
    "keep-alive",
    "upgrade-insecure-requests",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "dnt",
    "te",
    "transfer-encoding",
    "trailer",
    "proxy-authorization",
    "proxy-connection",
    "if-modified-since",
    "if-none-match",
    "cache-control",
    "pragma",
    "range",
    "expect",
})

# Static headers — replay as-is (they don't carry auth but help with compatibility)
STATIC_HEADERS = frozenset({
    "content-type",
    "accept",
    "accept-language",
    "accept-encoding",
    "origin",
    "referer",
})

# Headers that look like authentication
AUTH_HEADERS = frozenset({
    "authorization",
    "x-api-key",
    "x-auth-token",
    "x-access-token",
    "x-token",
    "api-key",
    "bearer",
    "x-session-token",
    "x-user-token",
    "x-app-token",
})


@dataclass
class ReplayRule:
    header_name: str
    header_value: str
    is_auth: bool = False
    frequency: float = 1.0  # fraction of requests that contained this header


class HeaderReplayRules:
    """Encapsulates which headers should be replayed and their values."""

    def __init__(self, rules: list[ReplayRule] | None = None):
        self.rules: list[ReplayRule] = rules or []

    @classmethod
    def analyze(cls, requests: list[CapturedRequest]) -> "HeaderReplayRules":
        """
        Identify headers that are consistently present across requests
        and classify them as auth or static.
        """
        if not requests:
            return cls()

        total = len(requests)
        header_values: dict[str, list[str]] = {}

        for req in requests:
            for name, value in req.request_headers.items():
                key = name.lower()
                if key in BROWSER_ONLY_HEADERS:
                    continue
                header_values.setdefault(key, []).append(value)

        rules: list[ReplayRule] = []
        for name, values in header_values.items():
            frequency = len(values) / total
            # Only keep headers present in at least 50% of requests
            if frequency < 0.5:
                continue
            # Use the most common value
            most_common = max(set(values), key=values.count)
            is_auth = name in AUTH_HEADERS
            rules.append(
                ReplayRule(
                    header_name=name,
                    header_value=most_common,
                    is_auth=is_auth,
                    frequency=frequency,
                )
            )

        return cls(rules=rules)

    def build_replay_headers(
        self,
        cookies: dict[str, str],
        token_store: dict[str, str],
    ) -> dict[str, str]:
        """
        Build a final headers dict for replaying requests.

        cookies  — dict of cookie name -> value
        token_store — dict of header name -> dynamic value (e.g. from storage)
        """
        headers: dict[str, str] = {}

        for rule in self.rules:
            # If we have a dynamic value in the token store, prefer that
            dynamic_value = token_store.get(rule.header_name)
            if dynamic_value:
                headers[rule.header_name] = dynamic_value
            else:
                headers[rule.header_name] = rule.header_value

        # Inject cookies if any auth cookies are present
        if cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            headers["cookie"] = cookie_str

        return headers

    @property
    def auth_rules(self) -> list[ReplayRule]:
        return [r for r in self.rules if r.is_auth]

    @property
    def static_rules(self) -> list[ReplayRule]:
        return [r for r in self.rules if not r.is_auth]
