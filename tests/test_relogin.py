"""Automatic login renewal: executor retry logic, plus real-browser runs against a local site."""
import asyncio
import secrets
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.auth import relogin
from core.auth.vault import get_vault
from core.config import settings
from core.runtime import executor
from core.runtime.middleware import ToolError
from core.runtime.tool_loader import ToolLoader
from core.storage.db import get_db
from core.storage.models import RecordingSession, ToolSpec


async def _make_tool(url: str, cookie_jar: list[dict], login: dict | None = None):
    """Seed a session with a saved (expired) login and generate a get_me tool for it."""
    from apps.cli.main import _generate

    state = {"cookie_jar": cookie_jar}
    if login:
        state["login"] = login
    session = RecordingSession(url=url, browser_context_state=state)
    spec = ToolSpec(
        session_id=session.id, approved=True, auth_strategy="cookies",
        tool_name="get_me", purpose="Who am I", method="GET",
        url_template=url.rsplit("/", 1)[0] + "/api/me",
    )
    async with get_db() as db:
        await db.save_session(session)
        await db.save_tool_spec(spec)
    await _generate(session.id, None)
    async with get_db() as db:
        tool = (await db.get_generated_tools())[0]
    return session, ToolLoader().load_and_get(Path(tool.file_path), "get_me")


# ---------------------------------------------------------------- retry logic

async def test_tool_renews_login_once_and_retries(monkeypatch):
    monkeypatch.setattr(settings, "auto_relogin", True)
    session, get_me = await _make_tool(
        "https://app.example.com/home",
        [{"name": "sid", "value": "old", "domain": "app.example.com", "path": "/"}],
    )
    renewals = []

    async def fake_renew(session_id):
        renewals.append(session_id)
        async with get_db() as db:
            await db.update_session_auth(session_id, {"cookie_jar": [
                {"name": "sid", "value": "new", "domain": "app.example.com", "path": "/"}
            ]})
        return True

    executor._renew_login = fake_renew
    executor._transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"user": "alice"})
        if "sid=new" in r.headers.get("cookie", "") else httpx.Response(401)
    )
    assert await get_me() == {"user": "alice"}
    assert renewals == [session.id]


async def test_failed_renewal_explains_what_to_do(monkeypatch):
    monkeypatch.setattr(settings, "auto_relogin", True)
    session, get_me = await _make_tool("https://app.example.com/home", [])

    async def useless_renew(session_id):
        return True  # Saved something, but the site still says no

    executor._renew_login = useless_renew
    executor._transport = httpx.MockTransport(lambda r: httpx.Response(401))
    with pytest.raises(ToolError, match="even after AutoMCP renewed the login") as err:
        await get_me()
    assert f"auth-login {session.id[:8]}" in str(err.value)


async def test_parallel_failures_share_one_renewal(monkeypatch):
    calls = []

    async def slow_renew(session_id):
        calls.append(session_id)
        await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr(relogin, "_renew", slow_renew)
    results = await asyncio.gather(*(relogin.renew_login("s1") for _ in range(5)))
    assert results == [True] * 5
    assert calls == ["s1"]  # One browser, not five
    assert await relogin.renew_login("s1") is True and calls == ["s1"]  # Cooldown
    assert await relogin.renew_login("s1", force=True) and calls == ["s1", "s1"]


# ---------------------------------------------------------------- real browser

def _site() -> FastAPI:
    """A tiny site with a session cookie, a remember-me cookie and two login forms."""
    app = FastAPI()
    sessions: dict[str, str] = {}
    remember = {"rem-1": "alice"}

    def new_session(response, user):
        sid = secrets.token_hex(8)
        sessions[sid] = user
        response.set_cookie("sid", sid, httponly=True)
        return response

    @app.get("/app")
    async def page(request: Request):
        if request.cookies.get("sid") in sessions:
            return HTMLResponse("<h1>Board</h1>")
        user = remember.get(request.cookies.get("remember", ""))
        if user:  # Long-lived cookie: hand out a fresh session
            return new_session(HTMLResponse("<h1>Board</h1>"), user)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login")
    async def login_form():
        return HTMLResponse(
            '<form method="post" action="/login">'
            '<input name="email" type="email"><input name="pw" type="password">'
            "<button>Sign in</button></form>"
        )

    @app.post("/login")
    async def login(email: str = Form(...), pw: str = Form(...)):
        if (email, pw) != ("alice@example.com", "hunter2"):
            return HTMLResponse("bad login", status_code=401)
        return new_session(RedirectResponse("/app", status_code=303), "alice")

    @app.get("/login2")
    async def step_one():
        return HTMLResponse('<form method="post" action="/login2">'
                            '<input name="email" type="email"><button>Next</button></form>')

    @app.post("/login2")
    async def step_two(email: str = Form(...)):
        return HTMLResponse('<form method="post" action="/login">'
                            f'<input name="email" type="hidden" value="{email}">'
                            '<input name="pw" type="password"><button>Sign in</button></form>')

    @app.get("/api/me")
    async def me(request: Request):
        user = sessions.get(request.cookies.get("sid", ""))
        if not user:
            return JSONResponse({"error": "logged out"}, status_code=401)
        return {"user": user}

    return app


@pytest.fixture(scope="module")
def site_url():
    pytest.importorskip("playwright")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(_site(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
async def browser_available():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception:
            pytest.skip("Chromium is not installed (run: playwright install chromium)")
        await browser.close()


def _cookie(name, value):
    return {"name": name, "value": value, "domain": "127.0.0.1", "path": "/", "expires": -1}


async def test_remember_me_cookie_renews_session_without_a_password(
    site_url, browser_available, monkeypatch, capfd
):
    monkeypatch.setattr(settings, "auto_relogin", True)
    _, get_me = await _make_tool(
        f"{site_url}/app", [_cookie("sid", "expired"), _cookie("remember", "rem-1")]
    )
    capfd.readouterr()  # Drop the generate step's output
    assert await get_me() == {"user": "alice"}
    # stdout carries the MCP protocol in stdio mode; renewal must not print to it
    assert capfd.readouterr().out == ""


@pytest.mark.parametrize("login_path", ["/login", "/login2"])
async def test_saved_password_logs_in_by_itself(
    site_url, browser_available, monkeypatch, login_path
):
    monkeypatch.setattr(settings, "auto_relogin", True)
    user_key, pass_key = "test.username", "test.password"
    get_vault().set(user_key, "alice@example.com")
    get_vault().set(pass_key, "hunter2")
    _, get_me = await _make_tool(
        f"{site_url}/app", [_cookie("sid", "expired")],
        login={"url": site_url + login_path, "username_secret": user_key,
               "password_secret": pass_key},
    )
    assert await get_me() == {"user": "alice"}


async def test_wrong_saved_password_fails_with_clear_error(site_url, browser_available, monkeypatch):
    monkeypatch.setattr(settings, "auto_relogin", True)
    get_vault().set("test.password", "wrong")
    _, get_me = await _make_tool(
        f"{site_url}/app", [_cookie("sid", "expired")],
        login={"url": f"{site_url}/login", "password_secret": "test.password"},
    )
    with pytest.raises(ToolError, match="even after AutoMCP renewed the login"):
        await get_me()
