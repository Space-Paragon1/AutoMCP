"""List and inspect recording sessions."""
from __future__ import annotations

import asyncio
from rich.console import Console
from rich.table import Table

from core.storage.db import get_db

console = Console()


async def run_sessions() -> None:
    db = get_db()
    async with db:
        async with db.conn.execute(
            "SELECT id, url, started_at, ended_at, request_count FROM sessions ORDER BY started_at DESC"
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        console.print("[dim]No sessions recorded yet.[/]")
        console.print("Run [bold]automcp record <url>[/] to start.")
        return

    table = Table(title="Recording Sessions")
    table.add_column("Session ID", style="cyan")
    table.add_column("URL")
    table.add_column("Recorded At")
    table.add_column("Requests", justify="right")

    for row in rows:
        short_id = row["id"][:8]
        recorded_at = row["started_at"][:19].replace("T", " ")
        table.add_row(short_id, row["url"], recorded_at, str(row["request_count"]))

    console.print(table)
    console.print("\nUse the short Session ID with [bold]automcp analyze <session_id>[/]")
