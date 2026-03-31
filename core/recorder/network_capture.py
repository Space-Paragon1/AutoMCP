from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import datetime
from typing import Any

from playwright.async_api import BrowserContext, Request, Response, Route

from core.config import settings
from core.storage.models import CapturedRequest

# MIME types whose bodies we want to capture
ALLOWED_RESPONSE_MIME_PREFIXES = (
    "application/json",
    "application/x-www-form-urlencoded",
    "text/plain",
    "text/html",
    "text/xml",
    "application/xml",
    "multipart/form-data",
)

# MIME types to skip entirely
SKIP_MIME_PREFIXES = (
    "image/",
    "font/",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/octet-stream",
    "video/",
    "audio/",
    "application/wasm",
)


def _should_capture_body(content_type: str) -> bool:
    ct = content_type.lower().split(";")[0].strip()
    if any(ct.startswith(skip) for skip in SKIP_MIME_PREFIXES):
        return False
    return any(ct.startswith(allowed) for allowed in ALLOWED_RESPONSE_MIME_PREFIXES)


def _is_binary_mime(content_type: str) -> bool:
    ct = content_type.lower().split(";")[0].strip()
    return not any(
        ct.startswith(text)
        for text in ("text/", "application/json", "application/xml", "application/x-www-form-urlencoded")
    )


class NetworkCapture:
    """Attaches to a Playwright BrowserContext and captures HTTP traffic."""

    def __init__(self, session_id: str):
        self._session_id = session_id
        self._captured: list[CapturedRequest] = []
        self._in_flight: dict[str, dict[str, Any]] = {}
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
        self._context: BrowserContext | None = None
        self._pending_action_label: str | None = None

    async def start(self, context: BrowserContext) -> None:
        """Attach event handlers to the browser context."""
        self._context = context
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfailed", self._on_request_failed)

    async def stop(self) -> list[CapturedRequest]:
        """Detach handlers and return all captured requests."""
        if self._context is not None:
            try:
                self._context.remove_listener("request", self._on_request)
                self._context.remove_listener("response", self._on_response)
                self._context.remove_listener("requestfailed", self._on_request_failed)
            except Exception:
                pass
            self._context = None

        # Wait briefly for any in-flight responses to arrive
        for _ in range(10):
            if not self._in_flight:
                break
            await asyncio.sleep(0.2)

        return list(self._captured)

    def set_action_label(self, label: str) -> None:
        """Set a label that will be attached to the next captured request."""
        self._pending_action_label = label

    def _consume_action_label(self) -> str | None:
        label = self._pending_action_label
        self._pending_action_label = None
        return label

    def _on_request(self, request: Request) -> None:
        request_id = id(request)
        self._in_flight[str(request_id)] = {
            "request": request,
            "started_at": datetime.utcnow(),
            "action_label": self._consume_action_label(),
        }

    def _on_request_failed(self, request: Request) -> None:
        self._in_flight.pop(str(id(request)), None)

    def _on_response(self, response: Response) -> None:
        asyncio.ensure_future(self._process_response(response))

    async def _process_response(self, response: Response) -> None:
        async with self._semaphore:
            request = response.request
            key = str(id(request))
            meta = self._in_flight.pop(key, {})

            # Determine content type for filtering
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
            content_type = resp_headers.get("content-type", "")

            # Filter out non-API traffic we don't care about
            method = request.method.upper()
            if method == "OPTIONS":
                return

            # Skip pure static asset URLs
            url = request.url
            lower_url = url.lower().split("?")[0]
            if any(
                lower_url.endswith(ext)
                for ext in (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                            ".ico", ".woff", ".woff2", ".ttf", ".eot", ".webp", ".avif")
            ):
                return

            # Read request body
            req_body: str | None = None
            try:
                post_data = request.post_data
                if post_data:
                    req_body = post_data
            except Exception:
                pass

            # Read response body
            resp_body: str | None = None
            if _should_capture_body(content_type):
                try:
                    if _is_binary_mime(content_type):
                        raw = await response.body()
                        resp_body = base64.b64encode(raw).decode("ascii")
                    else:
                        resp_body = await response.text()
                        # Truncate very large bodies
                        if len(resp_body) > 50_000:
                            resp_body = resp_body[:50_000] + "...[truncated]"
                except Exception:
                    resp_body = None

            # Sanitise headers — remove values that are too long (cookies can be huge)
            req_headers: dict[str, str] = {}
            for k, v in request.headers.items():
                req_headers[k] = v[:4096] if len(v) > 4096 else v

            captured = CapturedRequest(
                id=str(uuid.uuid4()),
                session_id=self._session_id,
                timestamp=meta.get("started_at", datetime.utcnow()),
                method=method,
                url=url,
                request_headers=req_headers,
                request_body=req_body,
                response_status=response.status,
                response_headers=dict(resp_headers),
                response_body=resp_body,
                action_label=meta.get("action_label"),
            )
            self._captured.append(captured)
