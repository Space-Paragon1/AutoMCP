"""AutoMCP 2.0 Web Dashboard."""
from __future__ import annotations

import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from core.config import settings
from core.storage.db import get_db

BASE_DIR = Path(__file__).parent

app_web = FastAPI(title="AutoMCP 2.0 Dashboard")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app_web.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = get_db()
    async with db:
        sessions_raw = []
        async with db.conn.execute(
            "SELECT id, url, started_at, request_count FROM sessions ORDER BY started_at DESC"
        ) as cur:
            rows = await cur.fetchall()
            for row in rows:
                sessions_raw.append({
                    "id": row["id"],
                    "short_id": row["id"][:8],
                    "url": row["url"],
                    "recorded_at": row["started_at"][:19].replace("T", " "),
                    "request_count": row["request_count"],
                })
        projects = await db.get_projects()
        all_specs = await db.get_tool_specs()
        all_tools = await db.get_generated_tools()
        executions = await db.get_executions(limit=10)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "sessions": sessions_raw,
        "projects": [{"id": p.id, "name": p.name} for p in projects],
        "total_sessions": len(sessions_raw),
        "total_specs": len(all_specs),
        "approved_specs": sum(1 for s in all_specs if s.approved),
        "total_tools": len(all_tools),
        "valid_tools": sum(1 for t in all_tools if t.validation_status == "valid"),
    })


@app_web.get("/api/sessions")
async def api_sessions():
    db = get_db()
    async with db:
        async with db.conn.execute(
            "SELECT id, url, started_at, request_count FROM sessions ORDER BY started_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [{"id": r["id"], "short_id": r["id"][:8], "url": r["url"],
             "started_at": r["started_at"][:19].replace("T", " "),
             "request_count": r["request_count"]} for r in rows]


@app_web.get("/api/specs")
async def api_specs(session_id: str | None = None):
    db = get_db()
    async with db:
        specs = await db.get_tool_specs(session_id=session_id)
    return [s.model_dump(mode="json") for s in specs]


@app_web.patch("/api/specs/{spec_id}")
async def api_update_spec(spec_id: str, request: Request):
    body = await request.json()
    allowed = {"approved", "is_readonly", "tool_name", "purpose"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    db = get_db()
    async with db:
        await db.update_tool_spec(spec_id, **updates)
    return {"ok": True}


@app_web.get("/api/tools")
async def api_tools():
    db = get_db()
    async with db:
        tools = await db.get_generated_tools()
    return [t.model_dump(mode="json") for t in tools]


@app_web.get("/api/tools/{tool_name}/source")
async def api_tool_source(tool_name: str):
    path = Path(settings.generated_tools_dir) / f"{tool_name}.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Tool file not found")
    return {"source": path.read_text(encoding="utf-8")}


@app_web.get("/api/executions")
async def api_executions(tool_name: str | None = None, limit: int = 50):
    db = get_db()
    async with db:
        execs = await db.get_executions(tool_name=tool_name, limit=limit)
    return [e.model_dump(mode="json") for e in execs]


@app_web.get("/api/projects")
async def api_projects():
    db = get_db()
    async with db:
        projects = await db.get_projects()
    return [{"id": p.id, "name": p.name, "description": p.description} for p in projects]


def run_dashboard(host: str = "127.0.0.1", port: int = 7860):
    uvicorn.run(app_web, host=host, port=port, log_level="warning")
