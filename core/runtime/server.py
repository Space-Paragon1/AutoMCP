from __future__ import annotations

import asyncio
from typing import Literal

from fastmcp import FastMCP
from rich.console import Console

from core.config import settings
from core.runtime.tool_registry import ToolRegistry
from core.storage.db import get_db
from core.storage.models import ToolSpec

# stderr: with the stdio transport, stdout carries the MCP protocol itself
console = Console(stderr=True)

Transport = Literal["stdio", "http", "sse"]


def tool_annotations(spec: ToolSpec | None) -> dict[str, bool]:
    """MCP hints so clients can tell safe reads from destructive actions."""
    if spec is None:
        return {"openWorldHint": True}
    method = spec.method.upper()
    read_only = spec.is_readonly or method in ("GET", "HEAD")
    hints = {"readOnlyHint": read_only, "openWorldHint": True}
    if not read_only:
        hints["destructiveHint"] = method == "DELETE"
        hints["idempotentHint"] = method in ("PUT", "DELETE")
    return hints


async def create_server() -> FastMCP:
    """Create and configure the FastMCP server with all registered tools."""
    mcp = FastMCP("AutoMCP")

    async with get_db() as db:
        generated_tools = await db.get_generated_tools()
        specs = {s.spec_id: s for s in await db.get_tool_specs()}

    registry = ToolRegistry()
    loaded = await registry.load_all(generated_tools, specs)

    for tool_name in registry.names():
        fn = registry.get(tool_name)
        if fn is not None:
            mcp.tool(fn, annotations=tool_annotations(registry.get_spec(tool_name)))

    console.print(f"[green]AutoMCP[/] server ready — [bold]{loaded}[/] tools loaded")
    if loaded == 0:
        console.print(
            "[yellow]No tools to serve.[/] Run [bold]automcp generate <session_id>[/] first."
        )
    return mcp


def run_server(
    host: str | None = None,
    port: int | None = None,
    transport: Transport = "http",
) -> None:
    """Run the MCP server synchronously (blocks until terminated)."""
    # create_server is async (needs DB) — run it first, then hand off to FastMCP
    # which starts its own event loop internally.
    mcp = asyncio.run(create_server())

    if transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return

    h = host or settings.server_host
    p = port or settings.server_port
    path = "/mcp" if transport == "http" else "/sse"
    console.print(f"[bold green]AutoMCP[/] listening on [cyan]http://{h}:{p}{path}[/]")
    mcp.run(transport=transport, host=h, port=p, show_banner=False)
