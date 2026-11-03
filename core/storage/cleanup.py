"""Delete recordings and tools, along with everything made from them."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.auth.relogin import login_secret_names
from core.config import settings
from core.storage.db import AsyncDatabase
from core.storage.models import GeneratedTool, RecordingSession


@dataclass
class Deletion:
    """What a delete removes. Worked out first so it can be shown before anything goes."""
    sessions: list[RecordingSession] = field(default_factory=list)
    request_count: int = 0
    spec_ids: list[str] = field(default_factory=list)
    tools: list[GeneratedTool] = field(default_factory=list)
    # Tool files and spec summaries that nothing else still uses
    files: list[Path] = field(default_factory=list)


async def plan_session_deletion(db: AsyncDatabase, session_ids: list[str]) -> Deletion:
    """A recording goes with its requests, specs, tools and saved login."""
    deletion = Deletion()
    for session_id in session_ids:
        session = await db.get_session(session_id)
        if session is None:
            continue
        deletion.sessions.append(session)
        deletion.request_count += await db.count_requests(session.id)
        deletion.spec_ids += [s.spec_id for s in await db.get_tool_specs(session_id=session.id)]
        deletion.tools += await db.get_generated_tools(session_id=session.id)
        summary = settings.generated_specs_dir / f"{session.id}_specs.json"
        if summary.exists():
            deletion.files.append(summary)
    deletion.files += await _unshared_files(db, deletion.tools)
    return deletion


async def plan_tool_deletion(db: AsyncDatabase, tool_names: list[str]) -> Deletion:
    """A tool goes with its spec, so generating that recording again doesn't bring it back."""
    tools = [t for t in await db.get_generated_tools() if t.tool_name in tool_names]
    return Deletion(
        spec_ids=list(dict.fromkeys(t.spec_id for t in tools)),
        tools=tools,
        files=await _unshared_files(db, tools),
    )


async def _unshared_files(db: AsyncDatabase, tools: list[GeneratedTool]) -> list[Path]:
    """The tools' files, minus any that a tool staying behind still points to.

    Files are named after the tool, so another recording's tool can share one.
    """
    leaving = {t.id for t in tools}
    kept = {
        Path(t.file_path).resolve()
        for t in await db.get_generated_tools()
        if t.id not in leaving and t.file_path
    }
    files: list[Path] = []
    for tool in tools:
        path = Path(tool.file_path) if tool.file_path else None
        if path and path.exists() and path.resolve() not in kept and path not in files:
            files.append(path)
    return files


async def apply_deletion(db: AsyncDatabase, deletion: Deletion) -> None:
    """Delete the records, then the files and saved logins.

    Records go first: a leftover file is never loaded without its record,
    but a record without its file makes the server warn on every start.
    """
    await db.delete_records([s.id for s in deletion.sessions], deletion.spec_ids)
    for path in deletion.files:
        path.unlink(missing_ok=True)
        # Python's cached copy of a tool file
        for cached in path.parent.glob(f"__pycache__/{path.stem}.*.pyc"):
            cached.unlink(missing_ok=True)

    secrets = [
        name
        for session in deletion.sessions
        if (session.browser_context_state or {}).get("login")
        for name in login_secret_names(session.id)
    ]
    if secrets:
        from core.auth.vault import get_vault

        vault = get_vault()
        for name in secrets:
            vault.delete(name)
