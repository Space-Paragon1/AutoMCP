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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert '<div class="stat-value" id="stat-sessions">1</div>' in response.text


async def test_tool_source_rejects_bad_names():
    transport = httpx.ASGITransport(app=app_web)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/tools/..%2F..%2Fsecrets/source")
        assert response.status_code in (400, 404)
        response = await client.get("/api/tools/not-valid/source")
        assert response.status_code == 400
