from __future__ import annotations

import asyncio

from fastmcp import FastMCP
from rich.console import Console

from core.config import settings
from core.runtime.tool_registry import ToolRegistry
from core.storage.db import get_db

console = Console()
registry = ToolRegistry()


async def create_server() -> FastMCP:
    """Create and configure the FastMCP server with all registered tools."""
    mcp = FastMCP("AutoMCP 2.0")

    # Load all valid generated tools from the database
    db = get_db()
    async with db:
        generated_tools = await db.get_generated_tools()

    loaded = await registry.load_all(generated_tools)

    # Register each loaded tool function with FastMCP
    for tool_name in list(registry._tools.keys()):
        fn = registry.get(tool_name)
        if fn is not None:
            mcp.tool(fn)

    console.print(
        f"[green]AutoMCP 2.0[/] server ready — [bold]{loaded}[/] tools loaded"
    )
    return mcp


def run_server(host: str | None = None, port: int | None = None) -> None:
    """Run the MCP server synchronously (blocks until terminated)."""
    h = host or settings.server_host
    p = port or settings.server_port

    console.print(
        f"[bold green]AutoMCP 2.0[/] starting on [cyan]{h}:{p}[/]"
    )

    async def _run() -> None:
        mcp = await create_server()
        # FastMCP run — transport="sse" for HTTP Server-Sent Events
        mcp.run(transport="sse", host=h, port=p)

    asyncio.run(_run())
