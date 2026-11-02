import asyncio

import httpx

from apps.web.server import app_web
from core.storage.db import get_db
from core.storage.models import RecordingSession


async def test_dashboard_handles_parallel_requests():
    """The dashboard loads five panels at once; all of them must succeed."""
    async with get_db() as db:
        await db.save_session(RecordingSession(url="https://x.com"))

    transport = httpx.ASGITransport(app=app_web)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:7860") as client:
        paths = ["/api/sessions", "/api/specs", "/api/tools", "/api/executions", "/api/projects"]
        for _ in range(5):
            responses = await asyncio.wait_for(
                asyncio.gather(*(client.get(p) for p in paths)), timeout=10
            )
            assert [r.status_code for r in responses] == [200] * len(paths)


async def test_dashboard_page_renders():
    async with get_db() as db:
        await db.save_session(RecordingSession(url="https://x.com"))

    transport = httpx.ASGITransport(app=app_web)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:7860") as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert '<div class="stat-value" id="stat-sessions">1</div>' in response.text


async def test_tool_source_rejects_bad_names():
    transport = httpx.ASGITransport(app=app_web)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:7860") as client:
        response = await client.get("/api/tools/..%2F..%2Fsecrets/source")
        assert response.status_code in (400, 404)
        response = await client.get("/api/tools/not-valid/source")
        assert response.status_code == 400


# ---------------------------------------------------------------- actions

def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_web), base_url="http://127.0.0.1:7860"
    )


async def _seed_approved_spec():
    from core.storage.models import ToolInput, ToolSpec

    session = RecordingSession(url="https://app.example.com/", browser_context_state={
        "cookie_jar": [{"name": "sid", "value": "abc", "domain": "app.example.com", "path": "/"}],
    })
    spec = ToolSpec(
        session_id=session.id, approved=True, auth_strategy="cookies",
        tool_name="get_board", purpose="Get a board", method="GET",
        url_template="https://app.example.com/api/boards/{board_id}",
        inputs=[ToolInput(name="board_id"),
                ToolInput(name="limit", type="integer", required=False)],
    )
    async with get_db() as db:
        await db.save_session(session)
        await db.save_tool_spec(spec)
    return session


async def test_generate_then_run_a_tool_from_the_dashboard():
    from core.runtime import executor

    session = await _seed_approved_spec()
    seen = []

    def api(request):
        seen.append(request)
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})

    executor._transport = httpx.MockTransport(api)
    async with _client() as client:
        response = await client.post("/api/generate", json={"session_id": session.id[:8]})
        assert response.json() == {
            "total": 1, "valid": 1,
            "tools": [{"tool_name": "get_board", "valid": True, "errors": []}],
        }

        tools = (await client.get("/api/tools")).json()
        assert [i["name"] for i in tools[0]["inputs"]] == ["board_id", "limit"]

        response = await client.post("/api/tools/get_board/run",
                                     json={"inputs": {"board_id": "42", "limit": "5"}})
        body = response.json()
        assert body["ok"] is True and body["result"] == {"id": "42"}

        response = await client.post("/api/tools/get_board/run",
                                     json={"inputs": {"limit": "lots"}})
        assert response.status_code == 400
        assert "board_id is required" in response.json()["detail"]

    assert str(seen[0].url) == "https://app.example.com/api/boards/42?limit=5"
    assert "sid=abc" in seen[0].headers["cookie"]


async def test_run_reports_tool_errors_without_failing_the_request():
    from core.runtime import executor

    session = await _seed_approved_spec()
    executor._transport = httpx.MockTransport(lambda r: httpx.Response(401))
    async with _client() as client:
        await client.post("/api/generate", json={"session_id": session.id})
        response = await client.post("/api/tools/get_board/run", json={"inputs": {"board_id": "1"}})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "auth-refresh" in response.json()["error"]


async def test_other_websites_cannot_drive_the_dashboard():
    await _seed_approved_spec()
    async with _client() as client:
        # A page on another site posting a form or a plain-text fetch
        response = await client.post("/api/generate", content="{}",
                                     headers={"content-type": "text/plain"})
        assert response.status_code == 415
        response = await client.post("/api/generate", json={},
                                     headers={"origin": "https://evil.example"})
        assert response.status_code == 403
        # DNS rebinding: evil.example resolving to 127.0.0.1
        response = await client.get("/api/specs", headers={"host": "evil.example:7860"})
        assert response.status_code == 403
        # The dashboard's own page is fine
        response = await client.post("/api/generate", json={},
                                     headers={"origin": "http://127.0.0.1:7860"})
        assert response.status_code == 200
