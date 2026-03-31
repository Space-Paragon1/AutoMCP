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

    # create_server is async (needs DB) — run it first, then hand off to FastMCP
    # which starts its own event loop via anyio.run internally.
    mcp = asyncio.run(create_server())
    mcp.run(transport="sse", host=h, port=p)
