"""AutoMCP 2.0 Web Dashboard."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from core.codegen.naming import is_safe_identifier, to_snake_case
from core.config import settings
from core.runtime.inputs import coerce_input
from core.runtime.tool_loader import ToolLoader
from core.storage.db import get_db

BASE_DIR = Path(__file__).parent

app_web = FastAPI(title="AutoMCP Dashboard")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
_loader = ToolLoader()

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Set by run_dashboard; "0.0.0.0" means the user chose to serve their network
_bind_host = "127.0.0.1"


@app_web.middleware("http")
async def only_this_dashboard(request: Request, call_next):
    """Refuse requests that other websites make through the user's browser.

    The dashboard can run tools with the user's saved logins. It only answers
    requests addressed to this machine (stops DNS rebinding), and changes must
    be JSON from the dashboard's own page (stops cross-site form posts).
    """
    host_header = request.headers.get("host", "")
    hostname = urlparse("//" + host_header).hostname or ""
    if _bind_host not in ("0.0.0.0", "::") and hostname not in LOCAL_HOSTS | {_bind_host}:
        return JSONResponse({"detail": "Unknown host"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != host_header:
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
        if not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"detail": "Send JSON"}, status_code=415)
    return await call_next(request)


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

    return templates.TemplateResponse(request, "dashboard.html", {
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
    async with db, db.conn.execute(
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
    if "tool_name" in updates:
        updates["tool_name"] = to_snake_case(str(updates["tool_name"]))
    db = get_db()
    async with db:
        await db.update_tool_spec(spec_id, **updates)
    return {"ok": True}


@app_web.get("/api/tools")
async def api_tools():
    db = get_db()
    async with db:
        tools = await db.get_generated_tools()
        specs = {s.spec_id: s for s in await db.get_tool_specs()}
    out = []
    for tool in tools:
        item = tool.model_dump(mode="json")
        spec = specs.get(tool.spec_id)
        if spec:
            # What the dashboard needs to show a "try it" form
            item.update(
                method=spec.method,
                url_template=spec.url_template,
                graphql=spec.graphql.model_dump() if spec.graphql else None,
                is_readonly=spec.is_readonly,
                purpose=spec.purpose,
                session_id=spec.session_id,
                inputs=[i.model_dump() for i in spec.inputs],
            )
        out.append(item)
    return out


class GenerateRequest(BaseModel):
    session_id: str | None = None


@app_web.post("/api/generate")
async def api_generate(body: GenerateRequest):
    """Write tool files for approved specs (one session, or all of them)."""
    from core.codegen.pipeline import generate_tools

    db = get_db()
    async with db:
        specs = await db.get_tool_specs(session_id=body.session_id)
    approved = [s for s in specs if s.approved]
    if not approved:
        raise HTTPException(status_code=400, detail="No approved specs. Approve some first.")
    records = await generate_tools(approved)
    return {
        "total": len(records),
        "valid": sum(r.validation_status == "valid" for r in records),
        "tools": [
            {"tool_name": r.tool_name, "valid": r.validation_status == "valid",
             "errors": r.validation_errors}
            for r in records
        ],
    }


class RunRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


@app_web.post("/api/tools/{tool_name}/run")
async def api_run_tool(tool_name: str, body: RunRequest):
    """Call a generated tool with the given inputs, like an AI agent would."""
    if not is_safe_identifier(tool_name):
        raise HTTPException(status_code=400, detail="Invalid tool name")
    db = get_db()
    async with db:
        tools = await db.get_generated_tools()
        latest = max(
            (t for t in tools if t.tool_name == tool_name and t.validation_status == "valid"),
            key=lambda t: t.generated_at, default=None,
        )
        spec = await db.get_tool_spec(latest.spec_id) if latest else None
    if latest is None or spec is None or not Path(latest.file_path).exists():
        raise HTTPException(status_code=404, detail="No valid tool with that name. Generate it first.")

    kwargs: dict[str, Any] = {}
    problems: list[str] = []
    for inp in spec.inputs:
        value = body.inputs.get(inp.name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if inp.required:
                problems.append(f"{inp.name} is required")
            continue
        try:
            kwargs[inp.name] = coerce_input(value, inp.type) if isinstance(value, str) else value
        except ValueError as e:
            problems.append(f"{inp.name}: {e}")
    if problems:
        raise HTTPException(status_code=400, detail="; ".join(problems))

    fn = _loader.load_and_get(Path(latest.file_path), tool_name)
    started = time.monotonic()
    try:
        result = await fn(**kwargs)
    except Exception as e:  # The tool's own error (HTTP failure, expired login) is the answer
        return {"ok": False, "error": str(e), "duration_ms": (time.monotonic() - started) * 1000}
    return {"ok": True, "result": result, "duration_ms": (time.monotonic() - started) * 1000}


@app_web.get("/api/tools/{tool_name}/source")
async def api_tool_source(tool_name: str):
    if not is_safe_identifier(tool_name):
        raise HTTPException(status_code=400, detail="Invalid tool name")
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
    global _bind_host
    _bind_host = host
    uvicorn.run(app_web, host=host, port=port, log_level="warning")
