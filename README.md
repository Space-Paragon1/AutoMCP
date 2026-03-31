# AutoMCP 2.0

**AutoMCP 2.0** is a browser-to-MCP tool generator. Record a browser session, let AutoMCP analyze the captured HTTP traffic with Claude, and get production-ready [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) tool files — ready to serve to any MCP client.

---

## Installation

```bash
# Install the package and all dependencies
pip install -e .

# Install Playwright's Chromium browser
playwright install chromium
```

Copy the environment file and fill in your Anthropic API key:

```bash
cp .env.example .env
# Edit .env and set AUTOMCP_ANTHROPIC_API_KEY=sk-ant-...
```

---

## Quick Start

AutoMCP has four commands that form a pipeline:

### 1. `record` — Capture browser traffic

```bash
automcp record https://app.example.com
```

A Chromium window opens. Log in, click around, perform the actions you want to turn into tools. When done, close the browser window. AutoMCP saves all network requests and auth state to a local SQLite database.

**Options:**
- `--headless` — run without a visible browser window

### 2. `analyze` — Generate tool specs with Claude

```bash
automcp analyze <session_id>
```

> `session_id` is printed after recording. You can use the full UUID or just the first 8 characters (e.g. `b1643f98`).

AutoMCP filters noise (analytics, redirects, errors), clusters requests by endpoint, then sends them to Claude to generate structured `ToolSpec` objects describing each API operation.

**Options:**
- `--output <path>` — write specs JSON to a custom file path
- `--min-confidence 0.7` — only keep specs above this confidence score (default: 0.5)

### 3. `generate` — Render Python tool files

```bash
automcp generate <session_id>
```

Renders a Jinja2 template for each spec, producing a standalone async Python function file in `generated/tools/`. Each file is validated with AST analysis before being marked valid.

**Options:**
- `--output-dir <dir>` — write generated files to a custom directory

### 4. `serve` — Start the MCP server

```bash
automcp serve
```

Loads all validated tool files and registers them with a FastMCP server, accessible over SSE at `http://127.0.0.1:8000`.

**Options:**
- `--host <host>` — override the server host
- `--port <port>` — override the server port

---

## Architecture

```
Browser Session
      │
      ▼
┌─────────────────────┐
│  NetworkCapture      │  Playwright event hooks — captures every
│  DomSnapshotter      │  HTTP request/response and DOM state
│  ActionMapper        │
└────────┬────────────┘
         │  CapturedRequest[]
         ▼
┌─────────────────────┐
│  EventClassifier     │  Drop analytics, redirects, server errors
│  EndpointClusterer   │  Group by method + normalised URL template
└────────┬────────────┘
         │  EndpointCluster[]
         ▼
┌─────────────────────┐
│  ToolSpecBuilder     │  Send clusters to Claude → ToolSpec[]
│  (Anthropic API)     │  Quality scoring + confidence filtering
└────────┬────────────┘
         │  ToolSpec[]
         ▼
┌─────────────────────┐
│  PythonMcpGenerator  │  Jinja2 template → .py files
│  CodeValidator       │  AST-based syntax + safety check
└────────┬────────────┘
         │  GeneratedTool[]
         ▼
┌─────────────────────┐
│  ToolLoader          │  importlib dynamic loading (no exec/eval)
│  ToolRegistry        │  In-memory tool catalogue
│  FastMCP server      │  SSE transport for MCP clients
└─────────────────────┘
```

All state is persisted to a local SQLite database (`automcp.db` by default).

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AUTOMCP_ANTHROPIC_API_KEY` | *(required)* | Your Anthropic API key |
| `AUTOMCP_LLM_MODEL` | `claude-opus-4-5` | Claude model for spec generation |
| `AUTOMCP_DB_PATH` | `automcp.db` | SQLite database file path |
| `AUTOMCP_SERVER_HOST` | `127.0.0.1` | MCP server bind host |
| `AUTOMCP_SERVER_PORT` | `8000` | MCP server port |
| `AUTOMCP_MIN_CONFIDENCE_THRESHOLD` | `0.5` | Minimum spec confidence to keep |
| `AUTOMCP_LOG_LEVEL` | `INFO` | Log verbosity |

All variables are prefixed with `AUTOMCP_` and can be set in a `.env` file at the project root.

---

## Project Structure

```
AutoMCP/
├── core/
│   ├── config.py              # Pydantic settings
│   ├── storage/
│   │   ├── models.py          # Pydantic data models
│   │   └── db.py              # Async SQLite (aiosqlite)
│   ├── auth/
│   │   ├── cookies.py         # Cookie extraction & replay
│   │   ├── csrf.py            # CSRF token detection
│   │   ├── headers.py         # Header replay rules
│   │   └── storage_tokens.py  # localStorage/sessionStorage tokens
│   ├── recorder/
│   │   ├── network_capture.py # Playwright network hooks
│   │   ├── dom_snapshot.py    # DOM element snapshot
│   │   ├── action_mapper.py   # Action label inference
│   │   └── browser_session.py # Main recording context manager
│   ├── analyzer/
│   │   ├── event_classifier.py   # Filter noise requests
│   │   ├── endpoint_clusterer.py # Group by URL template
│   │   └── tool_spec_builder.py  # LLM-powered spec generation
│   ├── codegen/
│   │   ├── templates/
│   │   │   ├── tool.py.jinja2
│   │   │   └── server_init.py.jinja2
│   │   ├── python_mcp_generator.py
│   │   └── validator.py
│   └── runtime/
│       ├── tool_loader.py     # importlib dynamic loading
│       ├── tool_registry.py   # In-memory tool registry
│       └── server.py          # FastMCP server setup
├── apps/
│   └── cli/
│       └── main.py            # Typer CLI commands
├── generated/
│   ├── specs/                 # JSON spec files
│   └── tools/                 # Generated Python tool files
├── pyproject.toml
├── .env.example
└── README.md
```
