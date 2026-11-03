"""Deleting recordings and tools (delete-session, delete-tool, tools)."""
import asyncio
from pathlib import Path

from typer.testing import CliRunner

from apps.cli.main import _generate, app
from core.auth.relogin import login_secret_names
from core.auth.vault import get_vault
from core.config import settings
from core.storage.db import get_db
from core.storage.models import (
    CapturedRequest,
    EndpointCluster,
    GeneratedTool,
    RecordingSession,
    ToolInput,
    ToolSpec,
)

runner = CliRunner()


async def _record(tool_name: str, session_id: str | None = None, login: bool = False):
    """A recording with one request, one cluster, one generated tool and its spec summary."""
    session = RecordingSession(url=f"https://{tool_name}.example.com/")
    if session_id:
        session.id = session_id
    if login:
        user_key, pass_key = login_secret_names(session.id)
        session.browser_context_state = {
            "login": {"url": session.url, "username_secret": user_key, "password_secret": pass_key}
        }
        get_vault().set(user_key, "me@example.com")
        get_vault().set(pass_key, "hunter2")
    request = CapturedRequest(
        session_id=session.id, method="GET", url=f"{session.url}api/items/1", response_status=200
    )
    spec = ToolSpec(
        session_id=session.id, approved=True, auth_strategy="cookies",
        tool_name=tool_name, purpose="Get an item", method="GET",
        url_template=f"{session.url}api/items/{{item_id}}", inputs=[ToolInput(name="item_id")],
    )
    async with get_db() as db:
        await db.save_session(session)
        await db.save_request(request)
        await db.save_endpoint_cluster(EndpointCluster(
            method="GET", url_template=spec.url_template, representative_request_id=request.id,
        ), session.id)
        await db.save_tool_spec(spec)
    await _generate(session.id[:8], None)
    summary = settings.generated_specs_dir / f"{session.id}_specs.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("[]")
    return session


async def _counts(session_id: str) -> dict[str, int]:
    async with get_db() as db:
        counts = {}
        for table, column in [
            ("sessions", "id"), ("captured_requests", "session_id"),
            ("endpoint_clusters", "session_id"), ("tool_specs", "session_id"),
        ]:
            async with db.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (session_id,)
            ) as cursor:
                counts[table] = (await cursor.fetchone())[0]
        counts["generated_tools"] = len(await db.get_generated_tools(session_id=session_id))
    return counts


def _tool_file(name: str) -> Path:
    return settings.generated_tools_dir / f"{name}.py"


def test_delete_session_removes_the_recording_and_everything_made_from_it():
    old = asyncio.run(_record("get_board", login=True))
    keep = asyncio.run(_record("get_user"))
    assert _tool_file("get_board").exists()

    # Answering no leaves everything alone
    result = runner.invoke(app, ["delete-session", old.id[:8]], input="n\n")
    assert result.exit_code == 0, result.output
    assert "1 recorded requests" in result.output
    assert "get_board" in result.output
    assert "saved login" in result.output
    assert "Nothing deleted" in result.output
    assert asyncio.run(_counts(old.id))["sessions"] == 1

    result = runner.invoke(app, ["delete-session", old.id[:8], "--yes"])
    assert result.exit_code == 0, result.output
    assert "Deleted 1 recording" in result.output
    assert set(asyncio.run(_counts(old.id)).values()) == {0}
    assert not _tool_file("get_board").exists()
    assert not (settings.generated_specs_dir / f"{old.id}_specs.json").exists()
    assert not set(login_secret_names(old.id)) & set(get_vault().list_keys())

    # The other recording is untouched
    assert set(asyncio.run(_counts(keep.id)).values()) == {1}
    assert _tool_file("get_user").exists()


def test_a_tool_file_another_recording_still_uses_is_kept():
    old = asyncio.run(_record("get_board"))
    other = RecordingSession(url="https://other.example.com/")
    spec = ToolSpec(session_id=other.id, tool_name="get_board", purpose="p", method="GET",
                    url_template="https://other.example.com/x", auth_strategy="none")

    async def share_the_file():
        async with get_db() as db:
            await db.save_session(other)
            await db.save_tool_spec(spec)
            await db.save_generated_tool(GeneratedTool(
                spec_id=spec.spec_id, tool_name="get_board",
                file_path=str(_tool_file("get_board")), validation_status="valid",
            ))
    asyncio.run(share_the_file())

    result = runner.invoke(app, ["delete-session", old.id[:8], "--yes"])
    assert result.exit_code == 0, result.output
    assert asyncio.run(_counts(old.id))["sessions"] == 0
    assert _tool_file("get_board").exists()


def test_unclear_or_unknown_ids_delete_nothing():
    asyncio.run(_record("get_board", session_id="aaaa1111-0000-4000-8000-000000000001"))
    asyncio.run(_record("get_user", session_id="aaaa2222-0000-4000-8000-000000000002"))

    result = runner.invoke(app, ["delete-session", "aaaa", "--yes"])
    assert result.exit_code == 1
    assert "matches 2 recordings" in result.output

    for prefix in ("zzzz", "_", "%"):  # No wildcard matching
        result = runner.invoke(app, ["delete-session", prefix, "--yes"])
        assert result.exit_code == 1
        assert "Recording not found" in result.output

    # One bad ID stops the whole command
    result = runner.invoke(app, ["delete-session", "aaaa1111", "zzzz", "--yes"])
    assert result.exit_code == 1
    assert asyncio.run(_counts("aaaa1111-0000-4000-8000-000000000001"))["sessions"] == 1


def test_delete_tool_removes_the_tool_and_its_spec_but_keeps_the_recording():
    session = asyncio.run(_record("get_board"))
    asyncio.run(_record("get_user"))

    result = runner.invoke(app, ["tools"])
    assert result.exit_code == 0, result.output
    assert "get_board" in result.output and "get_user" in result.output

    result = runner.invoke(app, ["delete-tool", "get_nothing", "--yes"])
    assert result.exit_code == 1
    assert "No generated tool named" in result.output

    result = runner.invoke(app, ["delete-tool", "get_board", "--yes"])
    assert result.exit_code == 0, result.output
    assert f"automcp analyze {session.id[:8]}" in result.output
    assert not _tool_file("get_board").exists()
    counts = asyncio.run(_counts(session.id))
    assert counts["tool_specs"] == counts["generated_tools"] == 0
    assert counts["sessions"] == counts["captured_requests"] == 1

    result = runner.invoke(app, ["tools"])
    assert "get_board" not in result.output and "get_user" in result.output
