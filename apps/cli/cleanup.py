"""List generated tools, and delete old recordings and tools."""
from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from core.storage.cleanup import (
    Deletion,
    apply_deletion,
    plan_session_deletion,
    plan_tool_deletion,
)
from core.storage.db import AsyncDatabase, get_db

console = Console()


async def run_tools() -> None:
    async with get_db() as db:
        tools = await db.get_generated_tools()
        specs = {s.spec_id: s for s in await db.get_tool_specs()}

    if not tools:
        console.print("[dim]No tools generated yet.[/]")
        console.print("Run [bold]automcp generate <session_id>[/] after reviewing a recording.")
        return

    table = Table(title="Generated Tools")
    table.add_column("Tool", style="cyan", no_wrap=True)
    table.add_column("Recording", no_wrap=True)
    table.add_column("Valid", justify="center", no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    # Only the purpose wraps when the terminal is narrow
    table.add_column("Purpose")
    for tool in tools:
        spec = specs.get(tool.spec_id)
        reads = spec and (spec.is_readonly or spec.method.upper() in ("GET", "HEAD"))
        table.add_row(
            tool.tool_name,
            (spec.session_id or "")[:8] if spec else "—",
            "[green]✓[/]" if tool.validation_status == "valid" else "[red]✗[/]",
            "reads" if reads else "changes data",
            spec.purpose if spec else "",
        )
    console.print(table)
    console.print("\nTry one with [bold]automcp test <tool>[/]; remove one with "
                  "[bold]automcp delete-tool <tool>[/]")


async def run_delete_sessions(prefixes: list[str], yes: bool) -> None:
    async with get_db() as db:
        session_ids: list[str] = []
        for prefix in prefixes:
            session_id = await _resolve(db, prefix.strip())
            if session_id not in session_ids:
                session_ids.append(session_id)
        deletion = await plan_session_deletion(db, session_ids)

        for session in deletion.sessions:
            console.print(f"Recording [cyan]{session.id[:8]}[/]  {session.url}")
        console.print(f"  {deletion.request_count} recorded requests")
        console.print(f"  {len(deletion.spec_ids)} tool specs")
        _show_tools(deletion)
        if any((s.browser_context_state or {}).get("login") for s in deletion.sessions):
            console.print("  the saved login (username and password in the vault)")

        if not _confirm(yes, "Delete all of this?"):
            return
        await apply_deletion(db, deletion)

    console.print(f"[green]Deleted {_plural(len(deletion.sessions), 'recording')}.[/]")
    _restart_hint(deletion)


async def run_delete_tools(names: list[str], yes: bool) -> None:
    async with get_db() as db:
        deletion = await plan_tool_deletion(db, names)
        found = {t.tool_name for t in deletion.tools}
        missing = [n for n in names if n not in found]
        if missing:
            console.print(f"[red]No generated tool named:[/] {', '.join(missing)}")
            console.print("Run [bold]automcp tools[/] to see the tool names.")
            raise typer.Exit(1)

        recordings = set()
        for tool in deletion.tools:
            spec = await db.get_tool_spec(tool.spec_id)
            recording = (spec.session_id or "")[:8] if spec else ""
            recordings.add(recording)
            console.print(f"Tool [cyan]{tool.tool_name}[/] from recording {recording or '—'}")
        one = len(deletion.tools) == 1
        source = next(iter(recordings)) if len(recordings) == 1 else ""
        console.print(f"  {'Its file and spec are' if one else 'Their files and specs are'} "
                      f"removed. To get {'it' if one else 'them'} back, run "
                      f"[bold]automcp analyze {source or '<session_id>'}[/] again.")

        if not _confirm(yes, "Delete it?" if one else "Delete them?"):
            return
        await apply_deletion(db, deletion)

    console.print(f"[green]Deleted {_plural(len(found), 'tool')}.[/]")
    _restart_hint(deletion)


async def _resolve(db: AsyncDatabase, prefix: str) -> str:
    """The one recording whose ID starts with *prefix*, or exit with a message."""
    matches = await db.find_session_ids(prefix)
    if not matches:
        console.print(f"[red]Recording not found:[/] {prefix or '(empty)'}")
        console.print("Run [bold]automcp sessions[/] to see the recordings.")
        raise typer.Exit(1)
    if len(matches) > 1:
        ids = ", ".join(m[:8] for m in matches)
        console.print(f"[red]'{prefix}' matches {len(matches)} recordings[/] ({ids}). "
                      "Type more of the ID.")
        raise typer.Exit(1)
    return matches[0]


def _show_tools(deletion: Deletion) -> None:
    if deletion.tools:
        names = ", ".join(t.tool_name for t in deletion.tools)
        console.print(f"  {_plural(len(deletion.tools), 'tool')}: {names}")


def _confirm(yes: bool, question: str) -> bool:
    if yes or typer.confirm(question, default=False):
        return True
    console.print("Nothing deleted.")
    return False


def _restart_hint(deletion: Deletion) -> None:
    if deletion.tools:
        console.print("If [bold]automcp serve[/] is running (or Claude has AutoMCP open), "
                      "restart it so the deleted tools disappear.")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"
