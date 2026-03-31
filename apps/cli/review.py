"""Interactive spec review command."""
from __future__ import annotations

import asyncio
from rich.console import Console
from rich.table import Table
from rich.prompt import Confirm, Prompt
from rich.panel import Panel
from rich import print as rprint

from core.storage.db import get_db
from core.storage.models import ToolSpec

console = Console()


async def run_review(session_id: str) -> None:
    db = get_db()
    async with db:
        specs = await db.get_tool_specs(session_id=session_id)

    if not specs:
        console.print(f"[red]No specs found for session {session_id}[/]")
        console.print(f"Run [bold]automcp analyze {session_id}[/] first")
        return

    console.print(Panel(
        f"[bold]Reviewing {len(specs)} tool specs[/]\n"
        "For each spec: approve it, rename it, or skip it.\n"
        "Only approved specs will be generated into MCP tools.",
        title="AutoMCP 2.0 — Review",
    ))

    approved_count = 0
    skipped_count = 0

    for i, spec in enumerate(specs, 1):
        console.rule(f"[bold cyan]Spec {i}/{len(specs)}[/]")
        _print_spec_summary(spec)

        action = Prompt.ask(
            "\nAction",
            choices=["approve", "skip", "rename", "readonly", "quit"],
            default="approve",
        )

        if action == "quit":
            console.print("[yellow]Review stopped.[/]")
            break

        if action == "skip":
            skipped_count += 1
            continue

        updates: dict = {}

        if action == "rename":
            new_name = Prompt.ask("New tool name", default=spec.tool_name)
            new_purpose = Prompt.ask("New purpose", default=spec.purpose)
            updates["tool_name"] = new_name
            updates["purpose"] = new_purpose
            updates["approved"] = True
            approved_count += 1

        elif action == "readonly":
            updates["approved"] = True
            updates["is_readonly"] = True
            approved_count += 1

        elif action == "approve":
            updates["approved"] = True
            approved_count += 1

        if updates:
            db = get_db()
            async with db:
                await db.update_tool_spec(spec.spec_id, **updates)

    console.print(f"\n[green]Review complete:[/] {approved_count} approved, {skipped_count} skipped")
    if approved_count > 0:
        console.print(f"Run [bold]automcp generate {session_id}[/] to generate approved tools")


def _print_spec_summary(spec: ToolSpec) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Field", style="dim")
    table.add_column("Value")

    quality = spec.quality_score
    composite = f"{quality.composite:.2f}" if quality else "—"

    table.add_row("Name", f"[bold cyan]{spec.tool_name}[/]")
    table.add_row("Purpose", spec.purpose)
    table.add_row("Method", f"[yellow]{spec.method}[/]")
    table.add_row("URL", spec.url_template)
    table.add_row("Auth", spec.auth_strategy)
    table.add_row("Confidence", f"{spec.confidence:.2f}")
    table.add_row("Quality", composite)
    table.add_row("Read-only", "[green]yes[/]" if spec.is_readonly else "no")

    if spec.inputs:
        inputs_str = ", ".join(
            f"{inp.name}({'*' if inp.required else '?'})" for inp in spec.inputs
        )
        table.add_row("Inputs", inputs_str)

    console.print(table)
