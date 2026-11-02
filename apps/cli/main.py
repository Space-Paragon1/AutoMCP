#!/usr/bin/env python3
"""AutoMCP CLI — Browser-to-MCP tool generator."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.config import settings

app = typer.Typer(
    name="automcp",
    help="AutoMCP — Record browser sessions and generate MCP tools",
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
    project: str = typer.Option(None, "--project", "-p", help="Project name"),
) -> None:
    """Record network traffic from a browser session."""
    try:
        asyncio.run(_record(url, headless, project))
    except KeyboardInterrupt:
        pass  # _record already saved and reported; don't print a traceback


async def _record(url: str, headless: bool, project: str | None = None) -> None:
    from core.recorder.browser_session import BrowserSession
    from core.storage.db import get_db

    console.print(
        Panel(
            f"[bold]Recording session[/]\nURL: [cyan]{url}[/]\n\n"
            "Interact with the browser, then close the window or press "
            "[bold]Ctrl+C[/] to stop.",
            title="AutoMCP — Record",
        )
    )

    project_id = None
    if project:
        async with get_db() as db:
            proj = await db.get_project_by_name(project)
        if proj is None:
            console.print(f"[red]Project '{project}' not found.[/] Run automcp project-create {project}")
            raise typer.Exit(1)
        project_id = proj.id

    session_id: str | None = None
    try:
        async with BrowserSession(url=url, headless=headless, project_id=project_id) as session:
            session_id = session.session_id
            await session.wait_until_closed()
    except asyncio.CancelledError:
        # Ctrl+C: BrowserSession.__aexit__ has already saved what was captured
        console.print("\n[yellow]Recording stopped by user[/]")
    except Exception as e:
        console.print(f"[red]Error:[/] {e}")
        if session_id is None:
            raise typer.Exit(1)

    if session_id:
        short = session_id[:8]
        console.print(
            f"\nNext: [bold]automcp analyze {short}[/] to generate tool specs."
        )


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
    provider: str = typer.Option(
        None, "--provider",
        help="LLM provider: anthropic, openai, gemini (default: AUTOMCP_LLM_PROVIDER)",
    ),
    include_pages: bool = typer.Option(
        False, "--include-pages",
        help="Also analyze full HTML page loads (for server-rendered sites without a JSON API)",
    ),
) -> None:
    """Analyze captured requests and generate tool specs."""
    asyncio.run(_analyze(session_id, output, min_confidence, provider, include_pages))


_PROVIDER_KEYS = {
    "anthropic": ("anthropic_api_key", "AUTOMCP_ANTHROPIC_API_KEY"),
    "openai": ("openai_api_key", "AUTOMCP_OPENAI_API_KEY"),
    "gemini": ("gemini_api_key", "AUTOMCP_GEMINI_API_KEY"),
}


def _is_provider_error(error: Exception) -> bool:
    """True for errors returned by an LLM provider's API (auth, quota, bad model)."""
    import anthropic

    if isinstance(error, anthropic.APIError):
        return True
    try:
        from google.genai import errors as genai_errors
        if isinstance(error, genai_errors.APIError):
            return True
    except ImportError:
        pass
    try:
        import openai
        if isinstance(error, openai.APIError):
            return True
    except ImportError:
        pass
    return False


async def _analyze(
    session_id: str,
    output: Path | None,
    min_confidence: float | None,
    provider: str | None = None,
    include_pages: bool = False,
) -> None:
    from core.analyzer.endpoint_clusterer import EndpointClusterer
    from core.analyzer.event_classifier import EventClassifier
    from core.analyzer.tool_spec_builder import LLMProvider, ToolSpecBuilder
    from core.storage.db import get_db

    provider = (provider or settings.llm_provider).lower()
    if provider not in _PROVIDER_KEYS:
        console.print(f"[red]Unknown provider:[/] {provider} (use anthropic, openai or gemini)")
        raise typer.Exit(1)
    key_field, key_env = _PROVIDER_KEYS[provider]
    if not getattr(settings, key_field):
        console.print(f"[red]Error:[/] {key_env} not set in environment or .env")
        raise typer.Exit(1)

    threshold = min_confidence if min_confidence is not None else settings.min_confidence_threshold

    with console.status("[bold green]Loading captured requests...[/]"):
        async with get_db() as db:
            full_session_id = await db.resolve_session_id(session_id)
            requests = await db.get_requests_for_session(full_session_id) if full_session_id else []
            all_specs = await db.get_tool_specs()

    if not full_session_id:
        console.print(f"[red]Session not found:[/] {session_id}")
        raise typer.Exit(1)
    session_id = full_session_id  # always use full UUID from here on

    if not requests:
        console.print(f"[red]No requests found for session {session_id[:8]}[/]")
        raise typer.Exit(1)

    console.print(f"Loaded [bold]{len(requests)}[/] requests")

    classifier = EventClassifier(include_pages=include_pages)
    filtered = classifier.classify(requests)
    rejected = len(requests) - len(filtered)
    console.print(
        f"Filtered to [bold]{len(filtered)}[/] useful requests "
        f"([dim]{rejected} rejected[/])"
    )

    if not filtered:
        console.print("[yellow]No useful requests remain after filtering.[/]")
        raise typer.Exit(1)

    with console.status("[bold green]Clustering endpoints...[/]"):
        clusters = EndpointClusterer().cluster(filtered)

    console.print(f"Found [bold]{len(clusters)}[/] unique endpoint clusters")

    # Approved specs are kept; tool names must stay unique across all sessions
    approved_here = [s for s in all_specs if s.session_id == session_id and s.approved]
    approved_endpoints = {(s.method, s.url_template) for s in approved_here}
    taken_names = {s.tool_name for s in all_specs if s.approved or s.session_id != session_id}

    try:
        builder = ToolSpecBuilder(provider=LLMProvider(provider))
    except ImportError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1)
    with console.status(f"[bold green]Analyzing with {builder.model}...[/]"):
        requests_map = {r.id: r for r in filtered}
        try:
            specs = await builder.build_specs(clusters, requests_map, session_id, taken_names)
        except Exception as e:
            # Bad key, used-up quota, unknown model: show the provider's message, not a traceback
            if not _is_provider_error(e):
                raise
            console.print(f"[red]{provider} API error:[/] {getattr(e, 'message', None) or e}")
            raise typer.Exit(1)

    good_specs = [
        s for s in specs
        if s.confidence >= threshold and (s.method, s.url_template) not in approved_endpoints
    ]
    console.print(
        f"Generated [bold]{len(good_specs)}[/] specs above confidence threshold {threshold:.2f}"
    )

    if not good_specs:
        console.print("[yellow]No new specs met the confidence threshold.[/]")
        raise typer.Exit(0)

    async with get_db() as db:
        replaced = await db.delete_unapproved_specs(session_id)
        for spec in good_specs:
            await db.save_tool_spec(spec)
    if replaced:
        console.print(f"[dim]Replaced {replaced} unreviewed specs from a previous analysis[/]")

    out_path = Path(output or (settings.generated_specs_dir / f"{session_id}_specs.json"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps([s.model_dump(mode="json") for s in good_specs], indent=2),
        encoding="utf-8",
    )

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
        f"Run [bold]automcp review {session_id[:8]}[/] to approve specs before generating"
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
    from core.codegen.python_mcp_generator import GenerationError, PythonMcpGenerator
    from core.codegen.validator import CodeValidator
    from core.storage.db import get_db
    from core.storage.models import GeneratedTool

    out_dir = Path(output_dir or settings.generated_tools_dir)

    async with get_db() as db:
        specs = await db.get_tool_specs(session_id=session_id)
        all_tools = await db.get_generated_tools()

    if not specs:
        console.print(f"[red]No tool specs found for session {session_id}[/]")
        console.print(f"Run [bold]automcp analyze {session_id}[/] first")
        raise typer.Exit(1)

    specs = [s for s in specs if s.approved]
    if not specs:
        console.print("[yellow]No approved specs found.[/]")
        console.print(f"Run [bold]automcp review {session_id}[/] to approve specs first")
        raise typer.Exit(0)

    # Tool files are named after the tool, so names must be unique across sessions
    spec_ids = {s.spec_id for s in specs}
    names_elsewhere = {
        t.tool_name for t in all_tools
        if t.spec_id not in spec_ids and t.validation_status == "valid"
    }

    generator = PythonMcpGenerator(output_dir=out_dir)
    validator = CodeValidator()

    table = Table(title="Generated Tools")
    table.add_column("Tool Name", style="cyan")
    table.add_column("File")
    table.add_column("Valid", justify="center")
    table.add_column("Notes")

    valid_count = 0
    for spec in specs:
        record = GeneratedTool(spec_id=spec.spec_id, tool_name=spec.tool_name, file_path="")
        if spec.tool_name in names_elsewhere:
            record.validation_status = "invalid"
            record.validation_errors = [
                f"Name '{spec.tool_name}' is already used by a tool from another session; "
                "rename it with automcp review"
            ]
        else:
            try:
                path = generator.generate(spec, output_dir=out_dir)
                record.file_path = str(path.resolve())
                result = validator.validate_file(path, spec)
                record.validation_status = "valid" if result.is_valid else "invalid"
                record.validation_errors = result.errors
            except GenerationError as e:
                record.validation_status = "invalid"
                record.validation_errors = [str(e)]

        async with get_db() as db:
            await db.save_generated_tool(record)

        ok = record.validation_status == "valid"
        valid_count += ok
        notes = ", ".join(record.validation_errors[:2]) if record.validation_errors else "OK"
        file_name = Path(record.file_path).name if record.file_path else "—"
        table.add_row(spec.tool_name, file_name, "[green]✓[/]" if ok else "[red]✗[/]", notes[:80])

    console.print(table)
    console.print(f"\n{valid_count}/{len(specs)} tools written to [cyan]{out_dir}[/]")
    if valid_count:
        console.print("Run [bold]automcp serve[/] to start the MCP server")


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command()
def serve(
    host: str = typer.Option(None, "--host", help="Server host (http/sse only)"),
    port: int = typer.Option(None, "--port", "-p", help="Server port (http/sse only)"),
    transport: str = typer.Option(
        "http", "--transport", "-t",
        help="stdio (Claude Desktop / Claude Code), http (Streamable HTTP at /mcp), or sse (legacy)",
    ),
) -> None:
    """Start the AutoMCP MCP server."""
    from core.runtime.server import run_server

    if transport not in ("stdio", "http", "sse"):
        console.print(f"[red]Unknown transport:[/] {transport} (use stdio, http or sse)")
        raise typer.Exit(1)
    run_server(host=host, port=port, transport=transport)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# mcp-config
# ---------------------------------------------------------------------------


@app.command()
def mcp_config() -> None:
    """Print the config to connect Claude Desktop or Claude Code to AutoMCP."""
    import shutil
    import sys

    from core.config import HOME

    exe = shutil.which("automcp") or str(Path(sys.executable).with_name("automcp"))
    entry = {
        "command": exe,
        "args": ["serve", "--transport", "stdio"],
        "env": {"AUTOMCP_HOME": str(HOME)},
    }
    console.print("[bold]Claude Desktop[/] — add to claude_desktop_config.json:")
    # Plain output: Rich would wrap long paths and break the JSON when copied
    typer.echo(json.dumps({"mcpServers": {"automcp": entry}}, indent=2))
    console.print("\n[bold]Claude Code[/] — run:")
    typer.echo(
        f"claude mcp add automcp --env AUTOMCP_HOME={HOME} -- {exe} serve --transport stdio"
    )


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


@app.command()
def review(
    session_id: str = typer.Argument(..., help="Session ID to review specs for"),
) -> None:
    """Interactively review and approve generated tool specs."""
    from apps.cli.review import run_review
    asyncio.run(run_review(session_id))


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


@app.command()
def test(
    tool_name: str = typer.Argument(..., help="Name of the tool to test"),
) -> None:
    """Run a generated tool interactively to verify it works."""
    from apps.cli.test_tool import run_test
    asyncio.run(run_test(tool_name))


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


@app.command()
def sessions() -> None:
    """List all recorded browser sessions."""
    from apps.cli.sessions import run_sessions
    asyncio.run(run_sessions())


# ---------------------------------------------------------------------------
# ui
# ---------------------------------------------------------------------------


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(7860, "--port", "-p"),
):
    """Open the AutoMCP web dashboard."""
    from apps.web.server import run_dashboard
    console.print(f"[bold green]AutoMCP Dashboard[/] → [cyan]http://{host}:{port}[/]")
    run_dashboard(host=host, port=port)


# ---------------------------------------------------------------------------
# project-create
# ---------------------------------------------------------------------------


@app.command()
def project_create(
    name: str = typer.Argument(..., help="Project name"),
    description: str = typer.Option("", "--description", "-d"),
):
    """Create a new project to group recording sessions."""
    asyncio.run(_project_create(name, description))


async def _project_create(name: str, description: str) -> None:
    from core.storage.db import get_db
    from core.storage.models import Project
    project = Project(name=name, description=description)
    db = get_db()
    async with db:
        await db.save_project(project)
    console.print(f"[green]Project created:[/] {name} (ID: {project.id[:8]})")


# ---------------------------------------------------------------------------
# secret-set / secret-get / secret-list
# ---------------------------------------------------------------------------


@app.command()
def secret_set(
    key: str = typer.Argument(..., help="Secret name"),
    value: str = typer.Argument(..., help="Secret value"),
):
    """Store an encrypted secret in the local vault."""
    from core.auth.vault import get_vault
    get_vault().set(key, value)
    console.print(f"[green]Secret stored:[/] {key}")


@app.command()
def secret_get(
    key: str = typer.Argument(..., help="Secret name"),
):
    """Retrieve a secret from the vault."""
    from core.auth.vault import get_vault
    value = get_vault().get(key)
    if value is None:
        console.print(f"[red]Secret not found:[/] {key}")
        raise typer.Exit(1)
    console.print(value)


@app.command()
def secret_list():
    """List all secret keys stored in the vault."""
    from core.auth.vault import get_vault
    from rich.table import Table
    keys = get_vault().list_keys()
    if not keys:
        console.print("[dim]No secrets stored.[/]")
        return
    table = Table(title="Vault Secrets")
    table.add_column("Key", style="cyan")
    for k in keys:
        table.add_row(k)
    console.print(table)


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


@app.command()
def logs(
    tool: str = typer.Option(None, "--tool", "-t", help="Filter by tool name"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """Show tool execution logs."""
    asyncio.run(_logs(tool, limit))


async def _logs(tool_name: str | None, limit: int) -> None:
    from core.storage.db import get_db
    from rich.table import Table
    db = get_db()
    async with db:
        executions = await db.get_executions(tool_name=tool_name, limit=limit)
    if not executions:
        console.print("[dim]No executions logged yet.[/]")
        return
    table = Table(title="Tool Execution Log")
    table.add_column("ID", style="dim")
    table.add_column("Tool", style="cyan")
    table.add_column("Status")
    table.add_column("Duration")
    table.add_column("Executed At")
    for ex in executions:
        status = "[green]OK[/]" if ex.success else "[red]ERR[/]"
        table.add_row(
            ex.id[:8],
            ex.tool_name,
            status,
            f"{ex.duration_ms:.0f}ms",
            ex.executed_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# auth-check
# ---------------------------------------------------------------------------


@app.command()
def auth_check(
    session_id: str = typer.Argument(..., help="Session ID to check auth for"),
    probe_url: str = typer.Option(None, "--probe", "-u", help="URL to probe (defaults to recorded site URL)"),
):
    """Check if recorded auth credentials are still valid."""
    asyncio.run(_auth_check(session_id, probe_url))


async def _auth_check(session_id: str, probe_url: str | None) -> None:
    from core.auth.replay import build_request_headers
    from core.auth.session_health import SessionHealthChecker
    from core.storage.db import get_db

    async with get_db() as db:
        full_id = await db.resolve_session_id(session_id) or session_id
        session = await db.get_session(full_id)
        specs = await db.get_tool_specs(session_id=full_id)

    if session is None:
        console.print(f"[red]Session not found:[/] {session_id}")
        raise typer.Exit(1)

    state = session.browser_context_state
    if not state.get("cookies") and not state.get("cookie_jar") and not state.get("host_headers"):
        console.print("[yellow]No cookies or auth headers found in session state.[/]")
        console.print("The session may not have captured a login.")
        return

    # A recorded API call is a better probe than the homepage, which often
    # returns 200 even when logged out
    sample = next(
        (s.sample_url for s in specs if s.approved and s.method == "GET" and s.sample_url), None
    )
    url = probe_url or sample or session.url
    headers = build_request_headers(state, url)
    console.print(f"Probing [cyan]{url}[/]...")

    checker = SessionHealthChecker()
    health = await checker.check(url, headers=headers)
    health.session_id = full_id

    age = checker.get_cookies_age_hours(state)

    table = Table(show_header=False, box=None)
    table.add_column("Field", style="dim")
    table.add_column("Value")

    table.add_row("Session", full_id[:8])
    table.add_row("URL", url)
    table.add_row("Status", "[green]Valid[/]" if health.is_valid else "[red]Expired[/]")
    table.add_row("HTTP Status", str(health.status_code or "—"))
    table.add_row("Cookie Age", f"{age:.1f} hours")
    if health.error:
        table.add_row("Error", f"[red]{health.error}[/]")

    console.print(table)

    if not health.is_valid:
        console.print(
            f"\n[yellow]Credentials appear expired.[/] "
            f"Refresh with: [bold]automcp auth-refresh {full_id[:8]}[/]"
        )


# ---------------------------------------------------------------------------
# auth-refresh
# ---------------------------------------------------------------------------


@app.command()
def auth_refresh(
    session_id: str = typer.Argument(..., help="Session ID to refresh auth for"),
):
    """Re-open the browser to refresh expired auth credentials."""
    try:
        asyncio.run(_auth_refresh(session_id))
    except KeyboardInterrupt:
        pass


async def _auth_refresh(session_id: str) -> None:
    from core.recorder.browser_session import BrowserSession
    from core.storage.db import get_db

    async with get_db() as db:
        full_id = await db.resolve_session_id(session_id) or session_id
        session = await db.get_session(full_id)

    if session is None:
        console.print(f"[red]Session not found:[/] {session_id}")
        raise typer.Exit(1)

    console.print(Panel(
        f"[bold]Refreshing auth for session[/] [cyan]{full_id[:8]}[/]\n"
        f"URL: [cyan]{session.url}[/]\n\n"
        "Log in again in the browser window, then close it.",
        title="AutoMCP — Auth Refresh",
    ))

    try:
        async with BrowserSession(url=session.url, refresh_session_id=full_id) as browser:
            await browser.wait_until_closed()
    except asyncio.CancelledError:
        pass  # Ctrl+C: the fresh auth state has still been saved

    console.print("Generated tools use the new login automatically (within 30 seconds).")


# ---------------------------------------------------------------------------
# drift-check
# ---------------------------------------------------------------------------


@app.command()
def drift_check(
    session_id: str = typer.Argument(..., help="Session ID to check for API drift"),
):
    """Check if live API responses still match recorded schemas."""
    asyncio.run(_drift_check(session_id))


async def _drift_check(session_id: str) -> None:
    from core.analyzer.drift_detector import DriftDetector
    from core.storage.db import get_db
    from rich.table import Table

    db = get_db()
    async with db:
        full_id = await db.resolve_session_id(session_id) or session_id
        session = await db.get_session(full_id)
        specs = await db.get_tool_specs(session_id=full_id)

    if not session:
        console.print(f"[red]Session not found:[/] {session_id}")
        raise typer.Exit(1)

    get_specs = [s for s in specs if s.method == "GET" and s.response_schema and s.approved]

    if not get_specs:
        console.print("[yellow]No GET specs with recorded schemas found.[/]")
        console.print("Run [bold]automcp analyze[/] again to capture response schemas.")
        return

    auth_state = session.browser_context_state
    detector = DriftDetector()

    table = Table(title="API Drift Report")
    table.add_column("Tool", style="cyan")
    table.add_column("Status")
    table.add_column("Added Fields")
    table.add_column("Removed Fields")

    with console.status(f"[bold green]Checking {len(get_specs)} endpoints...[/]"):
        for spec in get_specs:
            report = await detector.check(spec, auth_state)
            if report.error:
                status = f"[yellow]? {report.error[:30]}[/]"
            elif report.has_drift:
                status = "[red]⚠ Drift detected[/]"
            else:
                status = "[green]✓ OK[/]"

            added = ", ".join(report.added_fields[:3]) or "—"
            removed = ", ".join(report.removed_fields[:3]) or "—"
            table.add_row(spec.tool_name, status, added, removed)

    console.print(table)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
