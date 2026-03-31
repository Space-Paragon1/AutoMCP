"""Tool testing command — run a generated tool interactively."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax

from core.config import settings
from core.storage.db import get_db
from core.runtime.tool_loader import ToolLoader

console = Console()


async def run_test(tool_name: str) -> None:
    # Find the generated tool file
    db = get_db()
    async with db:
        tools = await db.get_generated_tools()

    match = next((t for t in tools if t.tool_name == tool_name), None)
    if match is None:
        console.print(f"[red]Tool '{tool_name}' not found.[/]")
        console.print("Run [bold]automcp generate[/] first, or check [bold]automcp sessions[/]")
        return

    if match.validation_status != "valid":
        console.print(f"[red]Tool '{tool_name}' failed validation:[/] {match.validation_errors}")
        return

    file_path = Path(match.file_path)
    if not file_path.exists():
        console.print(f"[red]Tool file not found:[/] {file_path}")
        return

    # Load spec for input prompts
    db = get_db()
    async with db:
        spec = await db.get_tool_spec(match.spec_id)
        # Get auth state from the session
        session = await db.get_session(spec.session_id) if spec and spec.session_id else None

    if spec is None:
        console.print(f"[red]Spec not found for tool '{tool_name}'[/]")
        return

    console.print(Panel(
        f"[bold]Testing:[/] {tool_name}\n{spec.purpose}",
        title="AutoMCP 2.0 — Test Tool",
    ))

    # Collect inputs interactively
    kwargs: dict = {}
    for inp in spec.inputs:
        label = f"{inp.name} ({'required' if inp.required else 'optional, Enter to skip'})"
        value = Prompt.ask(label, default="" if not inp.required else ...)
        if value:
            # Coerce types
            if inp.type == "integer":
                kwargs[inp.name] = int(value)
            elif inp.type == "number":
                kwargs[inp.name] = float(value)
            elif inp.type == "boolean":
                kwargs[inp.name] = value.lower() in ("true", "1", "yes")
            else:
                kwargs[inp.name] = value

    # Load cookies from session if available
    cookies = {}
    if session and session.browser_context_state:
        cookies = session.browser_context_state.get("cookies", {})

    kwargs["_cookies"] = cookies

    # Load the tool function
    loader = ToolLoader()
    try:
        fn = loader.load_and_get(file_path, tool_name)
    except Exception as e:
        console.print(f"[red]Failed to load tool:[/] {e}")
        return

    console.print("\n[dim]Calling tool...[/]")
    try:
        result = await fn(**kwargs)
        result_json = json.dumps(result, indent=2, default=str)
        console.print(Panel(
            Syntax(result_json, "json", theme="monokai"),
            title="[green]Result[/]",
        ))
    except Exception as e:
        console.print(f"[red]Tool execution failed:[/] {e}")
