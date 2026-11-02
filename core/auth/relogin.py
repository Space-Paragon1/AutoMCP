"""Renew an expired login in the background with a headless browser.

When a tool gets HTTP 401/403, the executor calls renew_login(). It opens the
recorded site in a hidden browser, loaded with the session's saved cookies and
storage, and saves whatever login state the browser ends up with:

1. Many sites keep a long-lived "remember me" cookie and hand out a fresh
   session on page load, so just revisiting the site is often enough.
2. If a login was saved with `automcp auth-login`, the site's login form is
   filled in with the username and password from the encrypted vault.

Logins that need a one-time code, a captcha or a passkey can't be renewed this
way; `automcp auth-refresh` (log in yourself in a visible browser) still works.
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

# A session is renewed at most once per cooldown, so a login that keeps
# failing doesn't open a browser on every tool call.
COOLDOWN_SECONDS = 120.0
TIMEOUT_SECONDS = 90.0

_last_attempt: dict[str, tuple[float, bool]] = {}
_locks: dict[tuple[int, str], asyncio.Lock] = {}


def login_secret_names(session_id: str) -> tuple[str, str]:
    """Vault keys that hold a session's saved username and password."""
    short = session_id[:8]
    return f"login.{short}.username", f"login.{short}.password"


def _lock(session_id: str) -> asyncio.Lock:
    # asyncio locks belong to one event loop
    key = (id(asyncio.get_running_loop()), session_id)
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def reset() -> None:
    """Forget past attempts (used by tests)."""
    _last_attempt.clear()
    _locks.clear()


async def renew_login(session_id: str, *, force: bool = False) -> bool:
    """Try to renew a session's login. Returns True when fresh login state was saved.

    Calls for the same session share one attempt: if five tool calls fail at
    once, one browser opens and the other four reuse its result.
    """
    async with _lock(session_id):
        last = _last_attempt.get(session_id)
        if last and not force and time.monotonic() - last[0] < COOLDOWN_SECONDS:
            return last[1]
        try:
            renewed = await asyncio.wait_for(_renew(session_id), TIMEOUT_SECONDS)
        except Exception as e:
            print(f"[automcp] automatic login renewal failed: {e}", file=sys.stderr)
            renewed = False
        _last_attempt[session_id] = (time.monotonic(), renewed)
        return renewed


def _saved_login(state: dict[str, Any]) -> tuple[dict[str, Any], str | None, str | None]:
    login = state.get("login") or {}
    if not login.get("password_secret"):
        return {}, None, None
    from core.auth.vault import get_vault

    vault = get_vault()
    username = vault.get(login.get("username_secret") or "")
    password = vault.get(login["password_secret"])
    return login, username, password


async def _renew(session_id: str) -> bool:
    from core.recorder.browser_session import BrowserSession
    from core.storage.db import get_db

    async with get_db() as db:
        session = await db.get_session(session_id)
    if session is None:
        return False
    state = session.browser_context_state or {}
    login, username, password = _saved_login(state)

    async with BrowserSession(
        session.url,
        headless=True,
        refresh_session_id=session_id,
        restore_state=state,
        quiet=True,
    ) as browser:
        await browser.settle()
        if password:
            await browser.log_in(
                username,
                password,
                login_url=login.get("url"),
                username_selector=login.get("username_selector"),
                password_selector=login.get("password_selector"),
            )
            await browser.settle()
    return True
