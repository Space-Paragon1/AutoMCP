"""Runs HTTP requests for generated tools.

Generated tool files only declare their inputs and a SPEC dict, then call
`execute(SPEC, inputs)`. Keeping the request logic here means bug fixes reach
every tool without regenerating it, and the generated files stay small.
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any
from urllib.parse import quote

import httpx

from core.auth.replay import build_request_headers
from core.config import settings
from core.runtime.middleware import RateLimiter, RetryConfig, ToolError, with_retry

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
MAX_TEXT_CHARS = 50_000
AUTH_CACHE_SECONDS = 30.0

# Tests can point this at an httpx.MockTransport
_transport: httpx.AsyncBaseTransport | None = None

_rate_limiter = RateLimiter(calls_per_minute=settings.rate_limit_per_minute)
_auth_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def placeholders(url_template: str) -> list[str]:
    return PLACEHOLDER.findall(url_template)


def build_url(url_template: str, args: dict[str, Any]) -> str:
    """Fill {placeholders} in the URL with URL-encoded argument values."""
    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        value = args.get(name)
        if value is None:
            raise ToolError(f"Missing required URL parameter '{name}'")
        return quote(str(value), safe="")
    return PLACEHOLDER.sub(_sub, url_template)


def build_payload(spec: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Map tool inputs to request fields, skipping inputs already used in the URL."""
    in_url = set(placeholders(spec["url_template"]))
    payload: dict[str, Any] = {}
    mapped: set[str] = set()

    for target, source in (spec.get("request_mapping") or {}).items():
        if isinstance(source, str) and source.startswith("$input."):
            name = source[len("$input."):]
            mapped.add(name)
            if name not in in_url and args.get(name) is not None:
                payload[target] = args[name]
        elif not (isinstance(source, str) and source.startswith("$")):
            # A fixed value the browser always sent (e.g. {"pos": "bottom"})
            payload[target] = source

    for name, value in args.items():
        if name in in_url or name in mapped or value is None:
            continue
        payload[name] = value
    return payload


async def load_auth_state(session_id: str | None) -> dict[str, Any]:
    """Load a session's recorded auth state, cached briefly so refreshes apply quickly."""
    if not session_id:
        return {}
    cached = _auth_cache.get(session_id)
    if cached and time.monotonic() - cached[0] < AUTH_CACHE_SECONDS:
        return cached[1]

    from core.storage.db import get_db

    async with get_db() as db:
        session = await db.get_session(session_id)
    state = session.browser_context_state if session else {}
    _auth_cache[session_id] = (time.monotonic(), state)
    return state


def clear_auth_cache() -> None:
    _auth_cache.clear()


def _parse_response(response: httpx.Response, response_type: str) -> Any:
    if not response.content:
        return {"status": response.status_code, "ok": True}
    if response_type == "json":
        try:
            return response.json()
        except ValueError:
            pass  # Fall through to text: some "JSON" endpoints return HTML on error
    if response_type == "binary":
        return {
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "size_bytes": len(response.content),
        }
    text = response.text
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + f"\n...[truncated, {len(response.text)} chars total]"
    return text


async def _send(spec: dict[str, Any], args: dict[str, Any]) -> Any:
    method = spec["method"].upper()
    url = build_url(spec["url_template"], args)
    payload = build_payload(spec, args)
    session_id = spec.get("session_id")

    headers = build_request_headers(await load_auth_state(session_id), url)
    headers.setdefault(
        "accept", "application/json" if spec.get("response_type") == "json" else "*/*"
    )

    request_kwargs: dict[str, Any] = {"headers": headers}
    if method in ("GET", "DELETE", "HEAD"):
        if payload:
            request_kwargs["params"] = payload
    elif payload:
        if spec.get("body_encoding") == "form":
            request_kwargs["data"] = payload
        else:
            request_kwargs["json"] = payload

    async def _call() -> Any:
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, transport=_transport
        ) as client:
            response = await client.request(method, url, **request_kwargs)
        if response.status_code in (401, 403):
            short = (session_id or "")[:8]
            raise ToolError(
                f"Authentication failed (HTTP {response.status_code}). The recorded login "
                f"may have expired. Refresh it with: automcp auth-refresh {short}",
                status_code=response.status_code,
                retryable=False,
            )
        response.raise_for_status()
        return _parse_response(response, spec.get("response_type", "json"))

    await _rate_limiter.acquire()
    return await with_retry(_call, RetryConfig(max_attempts=3))


async def _log(tool_name: str, args: dict[str, Any], started: float,
               result: Any = None, error: str | None = None) -> None:
    from core.storage.db import get_db
    from core.storage.models import ToolExecution

    try:
        result_text = None
        if error is None:
            result_text = json.dumps(result, default=str)[:10_000]
        async with get_db() as db:
            await db.save_execution(ToolExecution(
                tool_name=tool_name,
                inputs={k: v for k, v in args.items() if v is not None},
                result=result_text,
                error=error,
                duration_ms=(time.monotonic() - started) * 1000,
                success=error is None,
            ))
    except Exception as e:  # Logging must never break a tool call
        print(f"[automcp] could not log execution of {tool_name}: {e}", file=sys.stderr)


async def execute(spec: dict[str, Any], args: dict[str, Any]) -> Any:
    """Run a tool's HTTP request and log the outcome to the execution history."""
    started = time.monotonic()
    try:
        result = await _send(spec, args)
    except Exception as e:
        await _log(spec["tool_name"], args, started, error=str(e))
        raise
    await _log(spec["tool_name"], args, started, result=result)
    return result
