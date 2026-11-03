"""Runs HTTP requests for generated tools.

Generated tool files only declare their inputs and a SPEC dict, then call
`execute(SPEC, inputs)`. Keeping the request logic here means bug fixes reach
every tool without regenerating it, and the generated files stay small.
"""
from __future__ import annotations

import copy
import functools
import json
import re
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from core.auth.replay import build_request_headers
from core.config import settings
from core.runtime.middleware import RateLimiter, RetryConfig, ToolError, with_retry
from core.runtime.forms import find_form_token
from core.runtime.trim import html_to_text, trim_json, trim_text

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
AUTH_CACHE_SECONDS = 30.0

# Tests can point this at an httpx.MockTransport
_transport: httpx.AsyncBaseTransport | None = None
# Renews an expired login, returning True if it saved a fresh one. Defaults to
# core.auth.relogin.renew_login; tests swap in a fake.
_renew_login: Callable[[str], Awaitable[bool]] | None = None

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

    # Inputs with no mapping are sent as-is; for GraphQL that means as variables
    prefix = "variables." if spec.get("graphql") else ""
    for name, value in args.items():
        if name in in_url or name in mapped or value is None:
            continue
        payload[prefix + name] = value
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


async def _try_renew_login(session_id: str | None) -> bool:
    if not session_id or not settings.auto_relogin:
        return False
    renew = _renew_login
    if renew is None:
        from core.auth.relogin import renew_login as renew
    renewed = await renew(session_id)
    if renewed:
        _auth_cache.pop(session_id, None)
    return renewed


def _auth_error(status: int, session_id: str | None, renew_tried: bool) -> ToolError:
    short = (session_id or "")[:8]
    if renew_tried:
        message = (
            f"Authentication failed (HTTP {status}), even after AutoMCP renewed the login "
            f"in the background. Log in again with: automcp auth-refresh {short} "
            f"(or save a login with automcp auth-login {short} so AutoMCP can sign in "
            "by itself)"
        )
    else:
        message = (
            f"Authentication failed (HTTP {status}). The recorded login may have expired. "
            f"Refresh it with: automcp auth-refresh {short}"
        )
    return ToolError(message, status_code=status, retryable=False)


def _parse_response(response: httpx.Response, response_type: str) -> Any:
    if not response.content:
        return {"status": response.status_code, "ok": True}
    limit = settings.max_response_chars
    if response_type == "json":
        try:
            return trim_json(response.json(), limit)
        except ValueError:
            pass  # Fall through to text: some "JSON" endpoints return HTML on error
    if response_type == "binary":
        return {
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "size_bytes": len(response.content),
        }
    text = response.text
    if "html" in response.headers.get("content-type", "").lower():
        text = html_to_text(text)  # The readable page, not its markup and scripts
    return trim_text(text, limit)


def _nest(payload: dict[str, Any]) -> dict[str, Any]:
    """Expand dotted field names into a nested JSON body: {"input.title": x} -> {"input": {"title": x}}."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        node = out
        *parents, leaf = key.split(".")
        for part in parents:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[leaf] = value
    return out


def _graphql_kwargs(spec: dict[str, Any], method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send a GraphQL operation the way the page did."""
    operation = _nest(copy.deepcopy(payload))
    if method in ("GET", "HEAD", "DELETE"):
        # GraphQL over GET puts variables and extensions in the URL as JSON text
        return {"params": {
            k: v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))
            for k, v in operation.items()
        }}
    return {"json": [operation] if spec["graphql"].get("batched") else operation}


def _body_kwargs(method: str, encoding: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    if method in ("GET", "DELETE", "HEAD"):
        return {"params": payload}
    if encoding == "form":
        return {"data": payload}
    if encoding == "multipart":
        # (None, value) makes httpx send a plain form field, not a file
        return {"files": {
            k: (None, v if isinstance(v, str) else json.dumps(v)) for k, v in payload.items()
        }}
    return {"json": _nest(payload)}


async def _fresh_form_token(
    spec: dict[str, Any], args: dict[str, Any], action_url: str, base_headers: dict[str, str]
) -> str | None:
    """Load the page the form was on and read its current security token."""
    pages: list[str] = []
    if spec.get("csrf_page"):
        try:
            pages.append(build_url(spec["csrf_page"], args))
        except ToolError:
            pass
    parent = action_url.rsplit("/", 1)[0]  # /me/repo/star -> /me/repo
    if parent not in pages:
        pages.append(parent)

    # A plain page load: cookies and login, none of the API-call markers
    page_headers = {
        k: v for k, v in base_headers.items()
        if k in ("cookie", "user-agent", "accept-language", "authorization")
    }
    page_headers["accept"] = "text/html,application/xhtml+xml"
    fallback = None
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, transport=_transport) as client:
        for page in pages:
            try:
                response = await client.get(page, headers=page_headers)
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            exact, loose = find_form_token(
                response.text, spec["csrf_field"], str(response.url), action_url
            )
            if exact:
                return exact
            fallback = fallback or loose
    return fallback


# How GraphQL servers say the caller isn't logged in, inside an HTTP 200 answer
_GRAPHQL_LOGGED_OUT_CODES = {
    "UNAUTHENTICATED", "UNAUTHORIZED", "NOT_AUTHENTICATED", "AUTHENTICATION_ERROR",
}
_GRAPHQL_LOGGED_OUT_TEXT = (
    "not authenticated", "unauthenticated", "not logged in", "must be logged in",
    "login required", "authentication required", "requires authentication",
)


def _graphql_answer(response: httpx.Response, batched: bool) -> Any:
    """The JSON a GraphQL server answered with (taken out of its batch), or None."""
    try:
        answer = response.json()
    except ValueError:
        return None
    if batched and isinstance(answer, list) and len(answer) == 1:
        return answer[0]
    return answer


def _graphql_errors(answer: Any) -> list[dict[str, Any]]:
    if isinstance(answer, dict) and isinstance(answer.get("errors"), list):
        return [e for e in answer["errors"] if isinstance(e, dict)]
    return []


def _graphql_logged_out(errors: list[dict[str, Any]]) -> bool:
    for error in errors:
        extensions = error.get("extensions")
        code = str(extensions.get("code", "")) if isinstance(extensions, dict) else ""
        message = str(error.get("message", "")).lower()
        if code.upper() in _GRAPHQL_LOGGED_OUT_CODES or any(
            text in message for text in _GRAPHQL_LOGGED_OUT_TEXT
        ):
            return True
    return False


def _graphql_explains_failure(spec: dict[str, Any], response: httpx.Response) -> bool:
    """GraphQL servers often answer a bad call with HTTP 400 plus the reason; read it."""
    if not spec.get("graphql") or response.status_code == 429:
        return False
    if not 400 <= response.status_code < 500:
        return False
    return bool(_graphql_errors(_graphql_answer(response, spec["graphql"].get("batched", False))))


async def _send(spec: dict[str, Any], args: dict[str, Any]) -> Any:
    method = spec["method"].upper()
    url = build_url(spec["url_template"], args)
    payload = build_payload(spec, args)
    session_id = spec.get("session_id")
    gql = spec.get("graphql")
    needs_form_token = bool(spec.get("csrf_field")) and method not in ("GET", "HEAD")

    async def _call(headers: dict[str, str], body_kwargs: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, transport=_transport
        ) as client:
            response = await client.request(method, url, headers=headers, **body_kwargs)
        if response.status_code not in (401, 403) and not _graphql_explains_failure(spec, response):
            response.raise_for_status()  # 429/5xx are retried by with_retry
        return response

    renew_tried = False
    while True:
        base = build_request_headers(await load_auth_state(session_id), url)
        headers = {**base, **(spec.get("headers") or {})}  # Plus this endpoint's own headers
        headers.setdefault(
            "accept", "application/json" if spec.get("response_type") == "json" else "*/*"
        )
        body = dict(payload)
        if needs_form_token:
            token = await _fresh_form_token(spec, args, url, base)
            if token:
                body[spec["csrf_field"]] = token
        if gql:
            body_kwargs = _graphql_kwargs(spec, method, body)
        else:
            body_kwargs = _body_kwargs(method, spec.get("body_encoding"), body)
        await _rate_limiter.acquire()
        response = await with_retry(
            functools.partial(_call, headers, body_kwargs), RetryConfig(max_attempts=3),
        )
        status = response.status_code
        if status not in (401, 403):
            if not gql:
                return _parse_response(response, spec.get("response_type", "json"))
            answer = _graphql_answer(response, gql.get("batched", False))
            errors = _graphql_errors(answer)
            if answer is None:
                return _parse_response(response, spec.get("response_type", "json"))
            if not _graphql_logged_out(errors):
                if errors and answer.get("data") is None:
                    reasons = "; ".join(str(e.get("message") or e)[:300] for e in errors[:5])
                    raise ToolError(
                        f"The site answered with GraphQL errors: {reasons}",
                        status_code=status, retryable=False,
                    )
                # Partial data with errors is still useful; the AI sees both
                return trim_json(answer, settings.max_response_chars)
            status = 401  # Logged out, said in a GraphQL error rather than the HTTP status
        # Logged out: renew the login once in the background, then try again
        if not renew_tried:
            renew_tried = True
            if await _try_renew_login(session_id):
                continue
            raise _auth_error(status, session_id, renew_tried=False)
        raise _auth_error(status, session_id, renew_tried=True)


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
