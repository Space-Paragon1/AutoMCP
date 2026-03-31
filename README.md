# AutoMCP 2.0

**AutoMCP 2.0** turns browser sessions into MCP tools. Record a session on any web app, let Claude analyze the traffic, review the generated specs, and serve them as production-ready [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) tools — no API docs, no SDKs required.

---

## How it works

```
automcp record  →  automcp analyze  →  automcp review  →  automcp generate  →  automcp serve
     │                   │                   │                   │                   │
  Playwright          Claude API         Approve /           Jinja2 →           FastMCP
  captures            produces           rename /            .py files           SSE server
  traffic             ToolSpec JSON      skip each           (AST validated)     on :8000
                      (not raw code)     spec
```

All state persists to a local SQLite database. Nothing leaves your machine except the Anthropic API call during `analyze`.

---

## Installation

```bash
pip install -e .
playwright install chromium
cp .env.example .env
# Edit .env — set AUTOMCP_ANTHROPIC_API_KEY=sk-ant-...
```

---

## Full command reference

### `record` — Capture browser traffic

```bash
automcp record https://app.example.com
automcp record https://app.example.com --project myapp
automcp record https://app.example.com --headless
```

A Chromium window opens. Log in, click around, perform the actions you want to automate. Close the browser window when done. AutoMCP saves every HTTP request/response plus cookies and DOM snapshots to SQLite.

Session IDs are printed after recording. Use the first 8 characters as a shorthand in all subsequent commands.

---

### `sessions` — List recorded sessions

```bash
automcp sessions
```

---

### `analyze` — Generate tool specs with Claude

```bash
automcp analyze <session_id>
automcp analyze <session_id> --min-confidence 0.7
automcp analyze <session_id> --output my_specs.json
```

Filters out noise (analytics, redirects, errors), clusters endpoints by URL pattern, then calls Claude to produce structured `ToolSpec` JSON for each endpoint — not raw code. Specs are saved to `generated/specs/` and the database.

---

### `review` — Approve specs interactively

```bash
automcp review <session_id>
```

Walk through each spec one by one. For each one you can:
- `approve` — include it in the next generate run
- `rename` — change the tool name and purpose
- `readonly` — mark it as a read-only tool
- `skip` — exclude it

Only approved specs are generated into tools.

---

### `generate` — Render Python tool files

```bash
automcp generate <session_id>
automcp generate <session_id> --output-dir ./my_tools
```

Renders a Jinja2 template for each approved spec, producing a standalone async Python file in `generated/tools/`. Each file is validated with AST analysis (no `exec()`).

---

### `serve` — Start the MCP server

```bash
automcp serve
automcp serve --port 9000
```

Loads all validated tool files via `importlib` and registers them with a FastMCP server. The MCP endpoint is:

```
http://127.0.0.1:8000/sse
```

**Connecting Claude Desktop:** add this to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "automcp": {
      "url": "http://127.0.0.1:8000/sse"
    }
  }
}
```

Restart Claude Desktop after editing the config. Keep `automcp serve` running in a terminal.

---

### `test` — Run a tool interactively

```bash
automcp test <tool_name>
```

Prompts for inputs, executes the tool against the live site using recorded auth cookies, and prints the response. Execution is logged to the database.

---

### `ui` — Web dashboard

```bash
automcp ui
automcp ui --port 7860
```

Opens a dark-theme dashboard at `http://127.0.0.1:7860` with:
- Sessions browser
- Specs table with inline approve/read-only toggles
- Tools viewer with syntax-highlighted source code
- Execution log with success rate and duration stats
- Projects overview

---

### `project-create` — Organize sessions by project

```bash
automcp project-create myapp --description "My app automation"
automcp record https://myapp.com --project myapp
```

---

### `secret-set` / `secret-get` / `secret-list` — Encrypted vault

```bash
automcp secret-set MY_API_KEY sk-...
automcp secret-get MY_API_KEY
automcp secret-list
```

Secrets are encrypted with Fernet symmetric encryption. The key lives in `.vault.key` and the store in `.vault.json` — both git-ignored.

---

### `logs` — Execution history

```bash
automcp logs
automcp logs --tool get_current_member --limit 50
```

---

## Architecture

```
core/
  recorder/       Playwright session, network capture, DOM snapshot, action mapper
  analyzer/       Event classifier, endpoint clusterer, LLM spec builder
  auth/           Cookies, CSRF, headers, localStorage/sessionStorage, vault
  codegen/        Jinja2 generator, AST validator, templates/
  runtime/        importlib tool loader, registry, FastMCP server
  storage/        Pydantic v2 models, aiosqlite database

apps/
  cli/            Typer CLI (record, analyze, review, generate, serve, test, ui, ...)
  web/            FastAPI dashboard + Jinja2 templates

generated/
  specs/          JSON tool spec files (intermediate representation)
  tools/          Generated Python MCP tool files
```

**Key design decisions:**
- Claude produces structured JSON specs only — never raw code
- Code is generated from Jinja2 templates deterministically
- No `exec()` — tools load via `importlib.util`
- Auth is a pluggable strategy engine (cookies, bearer, API key, CSRF)
- Tool quality scoring: usefulness × stability × side-effect risk

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `AUTOMCP_ANTHROPIC_API_KEY` | *(required)* | Anthropic API key |
| `AUTOMCP_LLM_MODEL` | `claude-opus-4-5` | Claude model for analysis |
| `AUTOMCP_DB_PATH` | `automcp.db` | SQLite database path |
| `AUTOMCP_SERVER_HOST` | `127.0.0.1` | MCP server host |
| `AUTOMCP_SERVER_PORT` | `8000` | MCP server port |
| `AUTOMCP_MIN_CONFIDENCE_THRESHOLD` | `0.5` | Minimum spec confidence |
| `AUTOMCP_LOG_LEVEL` | `INFO` | Log verbosity |
