"""Tool testing command — run a generated tool interactively."""
from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax

from core.runtime.tool_loader import ToolLoader
from core.storage.db import get_db
from core.storage.models import ToolInput

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

    kwargs = _ask_inputs(spec.inputs)

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


def _ask_inputs(inputs: list[ToolInput]) -> dict[str, object]:
    """Prompt for each input, asking again when a required one is empty or a value is invalid."""
    values: dict[str, object] = {}
    for inp in inputs:
        label = f"{inp.name} ({'required' if inp.required else 'optional, Enter to skip'})"
        while True:
            raw = Prompt.ask(label, default="", show_default=False).strip()
            if not raw:
                if not inp.required:
                    break
                console.print(f"[yellow]{inp.name} is required, please enter a value.[/]")
                continue
            try:
                values[inp.name] = _coerce(raw, inp.type)
                break
            except ValueError as e:
                console.print(f"[red]Invalid {inp.type} for {inp.name}:[/] {e}")
    return values


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
