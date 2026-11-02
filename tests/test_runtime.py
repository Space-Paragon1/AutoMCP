import json
import time

import httpx
import pytest
from fastmcp import Client

from core.auth.replay import build_request_headers, collect_host_headers
from core.runtime import executor
from core.runtime.middleware import ToolError
from core.storage.db import get_db
from core.storage.models import CapturedRequest, RecordingSession, ToolInput, ToolSpec


# ---------------------------------------------------------------- URL + payload

def test_build_url_fills_and_encodes_placeholders():
    url = executor.build_url("https://x.com/b/{board_id}?q={q}", {"board_id": "a/b", "q": "x y"})
    assert url == "https://x.com/b/a%2Fb?q=x%20y"
    with pytest.raises(ToolError):
        executor.build_url("https://x.com/b/{board_id}", {})


def test_build_payload_maps_inputs_and_skips_url_params():
    spec = {
        "url_template": "https://x.com/boards/{board_id}/cards",
        "request_mapping": {"name": "$input.title", "pos": "bottom", "token": "$csrf"},
    }
    payload = executor.build_payload(spec, {"board_id": "1", "title": "Hi", "desc": None, "due": "x"})
    assert payload == {"name": "Hi", "pos": "bottom", "due": "x"}


# ---------------------------------------------------------------- auth replay

def test_cookies_are_scoped_to_matching_domains():
    state = {"cookie_jar": [
        {"name": "sid", "value": "app", "domain": ".example.com", "path": "/", "expires": -1},
        {"name": "sid", "value": "other", "domain": "tracker.net", "path": "/", "expires": -1},
        {"name": "old", "value": "x", "domain": "example.com", "path": "/", "expires": time.time() - 10},
        {"name": "admin", "value": "y", "domain": "example.com", "path": "/admin", "expires": -1},
    ]}
    headers = build_request_headers(state, "https://api.example.com/v1/me")
    assert headers["cookie"] == "sid=app"


def test_csrf_header_tracks_current_cookie():
    state = {
        "cookie_jar": [{"name": "XSRF-TOKEN", "value": "fresh%3D", "domain": "x.com", "path": "/"}],
        "host_headers": {"x.com": {"x-xsrf-token": "stale", "user-agent": "UA"}},
    }
    headers = build_request_headers(state, "https://x.com/api")
    assert headers["x-xsrf-token"] == "fresh="
    assert headers["user-agent"] == "UA"


def test_host_headers_keep_auth_and_common_headers_only():
    def req(headers, rtype="fetch"):
        return CapturedRequest(session_id="s", method="GET", url="https://api.x.com/a",
                               response_status=200, request_headers=headers, resource_type=rtype)
    collected = collect_host_headers([
        req({"authorization": "Bearer 1", "x-client": "v1", "cookie": "a=b"}),
        req({"authorization": "Bearer 2", "x-client": "v1", "x-upload": "once"}),
        req({"x-client": "v1"}),
        req({"x-page-only": "y"}, rtype="document"),
    ])
    assert collected == {"api.x.com": {"authorization": "Bearer 2", "x-client": "v1"}}


def test_legacy_sessions_still_replay():
    state = {"cookies": {"sid": "1"}, "replay_headers": {"cookie": "stale", "x-app": "a"}}
    headers = build_request_headers(state, "https://x.com/api")
    assert headers == {"x-app": "a", "cookie": "sid=1"}


# ---------------------------------------------------------------- end to end

def _field(obj, name, legacy_name):
    """MCP SDK v2 renamed camelCase fields to snake_case; support both."""
    return getattr(obj, name) if name in type(obj).model_fields else getattr(obj, legacy_name)


async def _seed(spec_kwargs_list):
    session = RecordingSession(
        url="https://app.example.com/",
        browser_context_state={
            "cookie_jar": [{"name": "session", "value": "abc", "domain": "app.example.com", "path": "/"}],
            "host_headers": {"app.example.com": {"x-csrf-token": "tok"}},
        },
    )
    specs = [ToolSpec(session_id=session.id, approved=True, auth_strategy="cookies", **kw)
             for kw in spec_kwargs_list]
    async with get_db() as db:
        await db.save_session(session)
        for spec in specs:
            await db.save_tool_spec(spec)
    return session, specs


def _fake_api(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "session=abc" not in request.headers.get("cookie", ""):
            return httpx.Response(401)
        if request.method == "GET":
            board_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"id": board_id, "q": request.url.params.get("q")})
        return httpx.Response(200, json={"created": json.loads(request.content)})
    return httpx.MockTransport(handler)


async def test_generate_with_short_session_id_marks_tools_valid():
    from apps.cli.main import _generate

    session, _ = await _seed([dict(
        tool_name="get_board", purpose="Get a board", method="GET",
        url_template="https://app.example.com/api/boards/{board_id}",
        inputs=[ToolInput(name="board_id")],
    )])
    await _generate(session.id[:8], None)
    await _generate(session.id[:8], None)  # running twice must not duplicate records

    async with get_db() as db:
        tools = await db.get_generated_tools()
    assert [(t.tool_name, t.validation_status) for t in tools] == [("get_board", "valid")]


async def test_served_tools_call_api_with_recorded_login():
    from apps.cli.main import _generate
    from core.runtime.server import create_server

    session, _ = await _seed([
        dict(tool_name="get_board", purpose="Get a board", method="GET",
             url_template="https://app.example.com/api/boards/{board_id}",
             inputs=[ToolInput(name="board_id", description="Board ID"),
                     ToolInput(name="q", required=False)]),
        dict(tool_name="create_card", purpose="Create a card", method="POST",
             url_template="https://app.example.com/api/boards/{board_id}/cards",
             inputs=[ToolInput(name="board_id"), ToolInput(name="title")],
             request_mapping={"name": "$input.title"}),
        dict(tool_name="delete_card", purpose="Delete a card", method="DELETE",
             url_template="https://app.example.com/api/cards/{card_id}",
             inputs=[ToolInput(name="card_id")]),
    ])
    await _generate(session.id, None)

    seen: list[httpx.Request] = []
    executor._transport = _fake_api(seen)
    mcp = await create_server()

    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {"get_board", "create_card", "delete_card"}
        # Auth plumbing is not exposed to the AI as parameters
        schema = _field(tools["get_board"], "input_schema", "inputSchema")
        assert set(schema["properties"]) == {"board_id", "q"}
        assert _field(tools["get_board"].annotations, "read_only_hint", "readOnlyHint") is True
        assert _field(tools["delete_card"].annotations, "destructive_hint", "destructiveHint") is True

        result = await client.call_tool("get_board", {"board_id": "42", "q": "hi"})
        assert json.loads(result.content[0].text) == {"id": "42", "q": "hi"}

        result = await client.call_tool("create_card", {"board_id": "42", "title": "Hello"})
        assert json.loads(result.content[0].text) == {"created": {"name": "Hello"}}

    assert str(seen[0].url) == "https://app.example.com/api/boards/42?q=hi"
    assert seen[1].headers["x-csrf-token"] == "tok"
    assert seen[1].url.path == "/api/boards/42/cards"

    async with get_db() as db:
        logged = await db.get_executions()
    assert sorted(e.tool_name for e in logged) == ["create_card", "get_board"]
    assert all(e.success for e in logged)


async def test_expired_login_gives_clear_error():
    from apps.cli.main import _generate
    from core.runtime.tool_loader import ToolLoader

    session, _ = await _seed([dict(
        tool_name="get_board", purpose="Get a board", method="GET",
        url_template="https://app.example.com/api/boards/{board_id}",
        inputs=[ToolInput(name="board_id")],
    )])
    await _generate(session.id, None)
    executor._transport = httpx.MockTransport(lambda r: httpx.Response(401))

    async with get_db() as db:
        tool = (await db.get_generated_tools())[0]
    fn = ToolLoader().load_and_get(__import__("pathlib").Path(tool.file_path), "get_board")
    with pytest.raises(ToolError, match=f"auth-refresh {session.id[:8]}"):
        await fn(board_id="1")


# ---------------------------------------------------------------- real-site fixes (GitHub)

async def test_dotted_fields_become_a_nested_json_body():
    seen = []

    def api(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    executor._transport = httpx.MockTransport(api)
    spec = {"tool_name": "create_issue", "method": "POST", "session_id": None,
            "url_template": "https://x.com/_ghui/issues", "response_type": "json",
            "request_mapping": {"input.title": "$input.title", "input.isDuplicated": False,
                                "fetchParent": False}}
    await executor.execute(spec, {"title": "Bug"})
    assert seen == [{"input": {"title": "Bug", "isDuplicated": False}, "fetchParent": False}]


async def test_form_posts_read_a_fresh_token_from_the_forms_page():
    """Like GitHub's star button: each form on the page has its own token."""
    page = (
        '<html><meta name="csrf-token" content="page-wide">'
        '<form action="/me/Alpha/star" method="post">'
        '<input type="hidden" name="authenticity_token" value="tok-alpha"></form>'
        '<form action="https://x.com/me/Beta/star" method="post">'
        '<input type="hidden" name="authenticity_token" value="tok-beta"></form></html>'
    )
    posted = []

    def site(request):
        if request.method == "GET":
            assert "x-requested-with" not in request.headers  # A plain page load
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})
        posted.append((request.url.path, request.headers["content-type"], request.content))
        return httpx.Response(200, json={"starred": True})

    executor._transport = httpx.MockTransport(site)
    spec = {"tool_name": "star_repo", "method": "POST", "session_id": None,
            "url_template": "https://x.com/me/{name}/star", "response_type": "json",
            "body_encoding": "multipart", "request_mapping": {"context": "other"},
            "headers": {"x-requested-with": "XMLHttpRequest"},
            "csrf_field": "authenticity_token", "csrf_page": "https://x.com/me?tab=repositories"}
    assert await executor.execute(spec, {"name": "Beta"}) == {"starred": True}

    path, content_type, body = posted[0]
    assert path == "/me/Beta/star" and content_type.startswith("multipart/form-data")
    assert b'name="authenticity_token"\r\n\r\ntok-beta' in body  # Beta's own token
    assert b'name="context"\r\n\r\nother' in body


def test_analysis_finds_the_form_token_field_and_page():
    from core.analyzer.tool_spec_builder import _form_csrf

    spec = ToolSpec(tool_name="star_repo", purpose="p", method="POST", auth_strategy="cookies",
                    url_template="https://x.com/me/{name}/star", body_encoding="multipart")
    rep = CapturedRequest(
        session_id="s", method="POST", url="https://x.com/me/Alpha/star", response_status=200,
        request_headers={"referer": "https://x.com/me/Alpha?tab=code"},
        request_body=('--b\r\nContent-Disposition: form-data; name="authenticity_token"\r\n\r\nT\r\n'
                      '--b\r\nContent-Disposition: form-data; name="context"\r\n\r\nother\r\n--b--'),
    )
    assert _form_csrf(spec, rep) == ("authenticity_token", "https://x.com/me/{name}?tab=code")
