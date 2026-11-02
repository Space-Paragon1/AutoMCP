"""Tool testing command — run a generated tool interactively."""
from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax

from core.storage.db import get_db
from core.runtime.tool_loader import ToolLoader

console = Console()


async def run_test(tool_name: str) -> None:
    async with get_db() as db:
        tools = await db.get_generated_tools()

    # Most recent valid build of this tool
    matches = sorted(
        (t for t in tools if t.tool_name == tool_name),
        key=lambda t: t.generated_at,
        reverse=True,
    )
    if not matches:
        console.print(f"[red]Tool '{tool_name}' not found.[/]")
        console.print("Run [bold]automcp generate[/] first, or check [bold]automcp sessions[/]")
        return

    match = next((t for t in matches if t.validation_status == "valid"), None)
    if match is None:
        console.print(f"[red]Tool '{tool_name}' failed validation:[/] {matches[0].validation_errors}")
        return

    file_path = Path(match.file_path)
    if not file_path.exists():
        console.print(f"[red]Tool file not found:[/] {file_path}")
        return

    async with get_db() as db:
        spec = await db.get_tool_spec(match.spec_id)

    if spec is None:
        console.print(f"[red]Spec not found for tool '{tool_name}'[/]")
        return

    console.print(Panel(
        f"[bold]Testing:[/] {tool_name}\n{spec.purpose}",
        title="AutoMCP — Test Tool",
    ))

    kwargs: dict = {}
    for inp in spec.inputs:
        label = f"{inp.name} ({'required' if inp.required else 'optional, Enter to skip'})"
        value = Prompt.ask(label, default="" if not inp.required else ...)
        if not value:
            continue
        try:
            kwargs[inp.name] = _coerce(value, inp.type)
        except ValueError as e:
            console.print(f"[red]Invalid value for {inp.name}:[/] {e}")
            return

    try:
        fn = ToolLoader().load_and_get(file_path, tool_name)
    except Exception as e:
        console.print(f"[red]Failed to load tool:[/] {e}")
        return

    # Auth comes from the recorded session automatically, and the executor
    # logs the call to the execution history.
    console.print("\n[dim]Calling tool...[/]")
    try:
        result = await fn(**kwargs)
    except Exception as e:
        console.print(f"[red]Tool execution failed:[/] {e}")
        return

    if isinstance(result, str):
        console.print(Panel(result[:5000], title="[green]Result[/]"))
    else:
        console.print(Panel(
            Syntax(json.dumps(result, indent=2, default=str), "json", theme="monokai"),
            title="[green]Result[/]",
        ))
    console.print("[dim]Logged to execution history (automcp logs)[/]")


def _coerce(value: str, type_name: str) -> object:
    if type_name == "integer":
        return int(value)
    if type_name == "number":
        return float(value)
    if type_name == "boolean":
        return value.lower() in ("true", "1", "yes", "y")
    if type_name in ("array", "object"):
        return json.loads(value)
    return value
