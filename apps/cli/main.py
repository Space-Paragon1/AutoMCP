#!/usr/bin/env python3
"""AutoMCP 2.0 CLI — Browser-to-MCP tool generator."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.config import settings

app = typer.Typer(
    name="automcp",
    help="AutoMCP 2.0 — Record browser sessions and generate MCP tools",
    no_args_is_help=True,
)
console = Console()


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


@app.command()
def record(
    url: str = typer.Argument(..., help="URL to open in the browser"),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
) -> None:
    """Record network traffic from a browser session."""
    asyncio.run(_record(url, headless))


async def _record(url: str, headless: bool) -> None:
    from core.recorder.browser_session import BrowserSession

    console.print(
        Panel(
            f"[bold]Recording session[/]\nURL: [cyan]{url}[/]\n\n"
            "Interact with the browser, then close the window or press "
            "[bold]Ctrl+C[/] to stop.",
            title="AutoMCP 2.0 — Record",
        )
    )

    try:
        async with BrowserSession(url=url, headless=headless) as session:
            # Keep alive until the browser window is closed
            while True:
                await asyncio.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Recording stopped by user[/]")
        return
    except Exception as e:
        # BrowserSession.__aexit__ already handled persistence
        if "Target page, context or browser has been closed" in str(e):
            pass  # Normal: user closed the browser
        else:
            console.print(f"[red]Error:[/] {e}")
            return

    # Retrieve what was saved
    from core.storage.db import get_db

    db = get_db()
    async with db:
        # We can only get the session_id from the session that was just used;
        # retrieve the session saved to find the id.
        # Since BrowserSession persists on __aexit__, we need the id from it.
        pass

    console.print(
        "\nRun [bold]automcp analyze <session_id>[/] to generate tool specs.\n"
        "Find session IDs with [bold]automcp sessions[/]."
    )


# ---------------------------------------------------------------------------
# sessions (helper command)
# ---------------------------------------------------------------------------


@app.command()
def sessions() -> None:
    """List all recorded sessions."""
    asyncio.run(_sessions())


async def _sessions() -> None:
    from core.storage.db import get_db

    db = get_db()
    async with db:
        async with db.conn.execute(
            "SELECT id, url, started_at, request_count FROM sessions ORDER BY started_at DESC LIMIT 20"
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        console.print("[yellow]No sessions recorded yet.[/]")
        return

    table = Table(title="Recorded Sessions")
    table.add_column("Session ID", style="cyan")
    table.add_column("URL")
    table.add_column("Started At")
    table.add_column("Requests", justify="right")

    for row in rows:
        table.add_row(
            row["id"][:16] + "...",
            row["url"][:60],
            row["started_at"][:19],
            str(row["request_count"]),
        )

    console.print(table)


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


@app.command()
def analyze(
    session_id: str = typer.Argument(..., help="Session ID from the record command"),
    output: Path = typer.Option(None, "--output", "-o", help="Output JSON file path"),
    min_confidence: float = typer.Option(
        None, "--min-confidence", help="Minimum confidence threshold"
    ),
) -> None:
    """Analyze captured requests and generate tool specs."""
    asyncio.run(_analyze(session_id, output, min_confidence))


async def _analyze(
    session_id: str,
    output: Path | None,
    min_confidence: float | None,
) -> None:
    from core.analyzer.endpoint_clusterer import EndpointClusterer
    from core.analyzer.event_classifier import EventClassifier
    from core.analyzer.tool_spec_builder import ToolSpecBuilder
    from core.storage.db import get_db

    threshold = min_confidence if min_confidence is not None else settings.min_confidence_threshold

    with console.status("[bold green]Loading captured requests...[/]"):
        db = get_db()
        async with db:
            requests = await db.get_requests_for_session(session_id)

    if not requests:
        console.print(f"[red]No requests found for session {session_id}[/]")
        raise typer.Exit(1)

    console.print(f"Loaded [bold]{len(requests)}[/] requests")

    # Classify
    classifier = EventClassifier()
    filtered = classifier.classify(requests)
    rejected = len(requests) - len(filtered)
    console.print(
        f"Filtered to [bold]{len(filtered)}[/] useful requests "
        f"([dim]{rejected} rejected[/])"
    )

    if not filtered:
        console.print("[yellow]No useful requests remain after filtering.[/]")
        raise typer.Exit(1)

    # Cluster
    with console.status("[bold green]Clustering endpoints...[/]"):
        clusterer = EndpointClusterer()
        clusters = clusterer.cluster(filtered)

    console.print(f"Found [bold]{len(clusters)}[/] unique endpoint clusters")

    # Build specs with LLM
    if not settings.anthropic_api_key:
        console.print("[red]Error:[/] AUTOMCP_ANTHROPIC_API_KEY not set in environment or .env")
        raise typer.Exit(1)

    with console.status(f"[bold green]Analyzing with {settings.llm_model}...[/]"):
        builder = ToolSpecBuilder()
        requests_map = {r.id: r for r in filtered}
        specs = await builder.build_specs(clusters, requests_map, session_id)

    # Filter by confidence
    good_specs = [s for s in specs if s.confidence >= threshold]
    console.print(
        f"Generated [bold]{len(good_specs)}[/] specs above confidence threshold {threshold:.2f}"
    )

    if not good_specs:
        console.print("[yellow]No specs met the confidence threshold.[/]")
        raise typer.Exit(0)

    # Save to DB
    db = get_db()
    async with db:
        for spec in good_specs:
            await db.save_tool_spec(spec)

    # Write to JSON file
    out_path = output or (settings.generated_specs_dir / f"{session_id}_specs.json")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps([s.model_dump(mode="json") for s in good_specs], indent=2),
        encoding="utf-8",
    )

    # Display summary table
    table = Table(title="Generated Tool Specs")
    table.add_column("Tool Name", style="cyan")
    table.add_column("Method")
    table.add_column("Purpose")
    table.add_column("Confidence", justify="right")
    table.add_column("Quality", justify="right")

    for spec in good_specs:
        quality = spec.quality_score
        composite = f"{quality.composite:.2f}" if quality else "—"
        table.add_row(
            spec.tool_name,
            spec.method,
            spec.purpose[:60] + ("..." if len(spec.purpose) > 60 else ""),
            f"{spec.confidence:.2f}",
            composite,
        )

    console.print(table)
    console.print(f"\nSpecs saved to [cyan]{out_path}[/]")
    console.print(
        f"Run [bold]automcp generate {session_id}[/] to generate MCP tools"
    )


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


@app.command()
def generate(
    session_id: str = typer.Argument(..., help="Session ID to generate tools for"),
    output_dir: Path = typer.Option(
        None, "--output-dir", "-o", help="Output directory for generated tools"
    ),
) -> None:
    """Generate MCP tool Python files from specs."""
    asyncio.run(_generate(session_id, output_dir))


async def _generate(session_id: str, output_dir: Path | None) -> None:
    from core.codegen.python_mcp_generator import PythonMcpGenerator
    from core.codegen.validator import CodeValidator
    from core.storage.db import get_db

    out_dir = Path(output_dir or settings.generated_tools_dir)

    db = get_db()
    async with db:
        specs = await db.get_tool_specs(session_id=session_id)

    if not specs:
        console.print(f"[red]No tool specs found for session {session_id}[/]")
        console.print(f"Run [bold]automcp analyze {session_id}[/] first")
        raise typer.Exit(1)

    generator = PythonMcpGenerator(output_dir=out_dir)
    validator = CodeValidator()

    table = Table(title="Generated Tools")
    table.add_column("Tool Name", style="cyan")
    table.add_column("File")
    table.add_column("Valid", justify="center")
    table.add_column("Notes")

    for spec in specs:
        try:
            path = generator.generate(spec, output_dir=out_dir)
            result = validator.validate_file(path, spec)

            # Update validation status in DB
            db2 = get_db()
            async with db2:
                tools = await db2.get_generated_tools(session_id=session_id)
                for tool in tools:
                    if tool.tool_name == spec.tool_name:
                        await db2.update_generated_tool_validation(
                            tool.id,
                            "valid" if result.is_valid else "invalid",
                            result.errors,
                        )

            status = "[green]✓[/]" if result.is_valid else "[red]✗[/]"
            notes = ", ".join(result.errors[:2]) if result.errors else "OK"
            table.add_row(spec.tool_name, path.name, status, notes)
        except Exception as e:
            table.add_row(spec.tool_name, "—", "[red]✗[/]", str(e)[:60])

    console.print(table)
    console.print(f"\nTools written to [cyan]{out_dir}[/]")
    console.print("Run [bold]automcp serve[/] to start the MCP server")


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command()
def serve(
    host: str = typer.Option(None, "--host", help="Server host"),
    port: int = typer.Option(None, "--port", "-p", help="Server port"),
) -> None:
    """Start the AutoMCP MCP server."""
    from core.runtime.server import run_server

    run_server(host=host, port=port)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
