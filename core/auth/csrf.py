from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.storage.models import CapturedRequest


@dataclass
class CsrfRule:
    source: Literal["cookie", "header", "body", "meta"]
    source_key: str
    injection_target: str
    injection_type: Literal["header", "body", "query"]


# Common CSRF header names
CSRF_HEADERS = [
    "x-csrftoken",
    "x-xsrf-token",
    "x-csrf-token",
    "x-request-token",
    "x-antiforgery-token",
]

# Common CSRF body/cookie field names
CSRF_FIELD_NAMES = [
    "_token",
    "csrf_token",
    "csrftoken",
    "xsrf_token",
    "_csrf",
    "authenticity_token",
    "__requestverificationtoken",
]

# Common CSRF cookies
CSRF_COOKIES = [
    "csrftoken",
    "xsrf-token",
    "csrf-token",
    "_csrf",
    "dsc",
]


class CsrfStrategy:
    """Detects and extracts CSRF tokens from captured requests."""

    def detect(self, requests: list[CapturedRequest]) -> CsrfRule | None:
        """
        Look for common CSRF patterns in request headers and bodies.
        Returns a CsrfRule describing how to obtain and inject the token,
        or None if no CSRF pattern is found.
        """
        for req in requests:
            # Check request headers for CSRF header injection
            headers_lower = {k.lower(): v for k, v in req.request_headers.items()}
            for header_name in CSRF_HEADERS:
                if header_name in headers_lower:
                    # Check if there's a corresponding cookie
                    cookie_header = headers_lower.get("cookie", "")
                    for cookie_name in CSRF_COOKIES:
                        if cookie_name in cookie_header.lower():
                            return CsrfRule(
                                source="cookie",
                                source_key=cookie_name,
                                injection_target=header_name,
                                injection_type="header",
                            )
                    # No matching cookie — token must come from body/meta
                    return CsrfRule(
                        source="meta",
                        source_key=header_name,
                        injection_target=header_name,
                        injection_type="header",
                    )

            # Check request body for CSRF field names
            if req.request_body:
                body_lower = req.request_body.lower()
                for field_name in CSRF_FIELD_NAMES:
                    if field_name in body_lower:
                        return CsrfRule(
                            source="body",
                            source_key=field_name,
                            injection_target=field_name,
                            injection_type="body",
                        )

        return None

    def extract_token(
        self,
        rule: CsrfRule,
        cookies: dict[str, str],
        response_body: bytes | None,
    ) -> str | None:
        """
        Attempt to extract the actual CSRF token value based on the rule.
        Returns None if the token cannot be found.
        """
        if rule.source == "cookie":
            # Try exact match first, then case-insensitive
            token = cookies.get(rule.source_key)
            if token:
                return token
            for name, value in cookies.items():
                if name.lower() == rule.source_key.lower():
                    return value
            return None

        if rule.source == "body" and response_body:
            # Try to extract from HTML response body via meta tag or hidden input
            try:
                body_text = response_body.decode("utf-8", errors="ignore")
                import re

                # Look for <meta name="csrf-token" content="...">
                meta_match = re.search(
                    r'<meta[^>]+name=["\']' + re.escape(rule.source_key) + r'["\'][^>]+content=["\']([^"\']+)',
                    body_text,
                    re.IGNORECASE,
                )
                if meta_match:
                    return meta_match.group(1)

                # Look for hidden input
                input_match = re.search(
                    r'<input[^>]+name=["\']' + re.escape(rule.source_key) + r'["\'][^>]+value=["\']([^"\']+)',
                    body_text,
                    re.IGNORECASE,
                )
                if input_match:
                    return input_match.group(1)
            except Exception:
                pass
            return None

        if rule.source == "meta" and response_body:
            try:
                body_text = response_body.decode("utf-8", errors="ignore")
                import re

                meta_match = re.search(
                    r'<meta[^>]+name=["\']csrf[^"\']*["\'][^>]+content=["\']([^"\']+)',
                    body_text,
                    re.IGNORECASE,
                )
                if meta_match:
                    return meta_match.group(1)
            except Exception:
                pass
            return None

        return None
