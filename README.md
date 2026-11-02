# AutoMCP 2.0

**AutoMCP 2.0** turns browser sessions into MCP tools. Record a session on any web app, let an AI (Claude, Gemini or OpenAI) analyze the traffic, review the generated specs, and serve them as production-ready [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) tools — no API docs, no SDKs required.

---

## How it works

```
automcp record  →  automcp analyze  →  automcp review  →  automcp generate  →  automcp serve
     │                   │                   │                   │                   │
  Playwright          LLM API            Approve /           Jinja2 →           FastMCP
  captures            produces           rename /            .py files           server (stdio
  traffic             ToolSpec JSON      skip each           (AST validated)     or HTTP)
                      (not raw code)     spec
```

All state persists to a local SQLite database. Nothing leaves your machine except the LLM API call during `analyze`, and that call never includes cookie values, auth headers, passwords or tokens: they are redacted first.

Served tools reuse the login you recorded: each call sends the cookies and auth/CSRF headers the browser used for that host, so the AI never sees or handles credentials.

---

## Installation

Requires Python 3.11+ (macOS ships 3.9, so use Homebrew's `python3.12` or similar).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env
# Edit .env — set AUTOMCP_ANTHROPIC_API_KEY=sk-ant-...
```

**No Anthropic credits?** Google Gemini has a free tier. Install `pip install -e ".[gemini]"`, get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey), and set `AUTOMCP_LLM_PROVIDER=gemini`, `AUTOMCP_GEMINI_API_KEY=...` and `AUTOMCP_LLM_MODEL=gemini-flash-latest` in `.env`. Google may use free-tier requests to improve its products, and `analyze` sends recorded traffic (with secrets removed), so avoid recording private sites on the free tier.

Run the tests with `pytest`.

---

## Quick start: your first tool

This walkthrough uses [pokeapi.co](https://pokeapi.co), a public site with no login, so nothing private is sent to the AI. Run each command in a terminal opened in this folder, after `source .venv/bin/activate`.

1. **Record.** Run `automcp record https://pokeapi.co`. A browser window opens. In the "Try it now" box, type `pokemon/ditto` and click **Submit**. Repeat with `pokemon/pikachu` and `type/3`, then close the browser window. The terminal prints a short session ID, such as `1a2b3c4d`.
2. **Analyze.** Run `automcp analyze <session_id>`. The AI turns the recorded requests into tool specs. On Gemini's free tier this can take a few minutes.
3. **Review.** Run `automcp review <session_id>` and answer `approve` for the specs you want.
4. **Generate.** Run `automcp generate <session_id>`. This writes the tool files to `generated/tools/`.
5. **Try a tool.** Run `automcp test <tool_name>` with a name from step 4. It asks for inputs and shows the site's response.
6. **Use it from Claude.** Run `automcp mcp-config` and follow the printed instructions.
7. **See it all in the dashboard.** Run `automcp ui`, then open http://127.0.0.1:7860 in your web browser to see your sessions, specs, tools and test runs. Press **Ctrl+C** in the terminal to stop it.

If `analyze` reports "No useful requests remain after filtering", the site didn't make any API calls while you recorded. Record again and use the site more, or add `--include-pages` for sites that load whole pages instead of calling an API.

---

## Full command reference

### `record` — Capture browser traffic

```bash
automcp record https://app.example.com
automcp record https://app.example.com --project myapp
automcp record https://app.example.com --headless
```

A Chromium window opens. Log in, click around, perform the actions you want to automate. Close the browser window (or press Ctrl+C) when done. AutoMCP saves every HTTP request/response plus your login state (cookies, auth headers, localStorage tokens) to SQLite. Login state is snapshotted every few seconds while you record, so it's kept even if the browser is closed abruptly.

Session IDs are printed after recording. Use the first 8 characters as a shorthand in all subsequent commands.

---

### `sessions` — List recorded sessions

```bash
automcp sessions
```

---

### `analyze` — Generate tool specs with an LLM

```bash
automcp analyze <session_id>
automcp analyze <session_id> --min-confidence 0.7
automcp analyze <session_id> --output my_specs.json
automcp analyze <session_id> --include-pages   # also use HTML page loads
automcp analyze <session_id> --allow-host api.partner.com   # keep calls to another site
```

Filters out noise (analytics, trackers, static assets, page loads, redirects, errors), clusters endpoints by URL pattern, then calls the LLM (Claude, Gemini or OpenAI; see [Multi-model support](#multi-model-support)) to produce structured `ToolSpec` JSON for each endpoint — not raw code. Every provider is held to a fixed JSON schema (structured outputs), so answers can't come back malformed. Endpoints are sent in batches, so large sessions work. Specs are saved to `generated/specs/` and the database.

Requests that are part of logging in (login pages, 2FA polling, anything sending a password) and endpoints that hand out tokens are never turned into tools. Names that vary in addresses become inputs: starring two repos gives one `star_repository(name)` tool, not two.

Only the recorded site's own calls are analyzed (its subdomains count, so `app.example.com` keeps `api.example.com`). Calls to other sites — fonts, chat widgets, payment forms, ads — are skipped, and `analyze` lists the ones it skipped. A call to another site that carries an auth header (a hosted backend like Supabase or Firebase) is kept. If the app's real API lives on another domain, keep it with `--allow-host`.

`--include-pages` keeps full HTML page loads, for server-rendered sites that don't have a JSON API. Re-running `analyze` replaces specs you haven't reviewed yet and keeps the ones you approved.

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

Renders a Jinja2 template for each approved spec, producing a small async Python file in `generated/tools/`. The file declares the tool's inputs and calls the shared runtime (`core/runtime/executor.py`), which builds the request, adds your recorded login, retries, rate-limits and logs each call. Every value from the spec is written as a quoted literal, so spec text can never become code, and each file is checked with AST analysis (no `exec()`, only expected imports).

---

### `serve` — Start the MCP server

```bash
automcp serve                      # Streamable HTTP at http://127.0.0.1:8000/mcp
automcp serve --port 9000
automcp serve --transport stdio    # for Claude Desktop / Claude Code
automcp serve --transport sse      # legacy SSE at /sse
```

Loads all validated tool files via `importlib` and registers them with a FastMCP server. Tools are marked read-only (GET, or marked `readonly` in review) or destructive (DELETE) so MCP clients can ask before risky actions. Every call is logged (see `automcp logs`).

**Calls are sent the way the browser sent them:** JSON (including nested bodies), classic form posts and `multipart/form-data`, with the extra headers the page's own scripts added (like GitHub's `github-verified-fetch`). Query-string values a write call always carried stay on its address too, such as the `?queryId=` GitHub uses to name the saved query a call runs. For classic web forms protected by a one-time security token (Rails `authenticity_token`, Django `csrfmiddlewaretoken`), the tool loads the page the form was on before each call and uses that form's fresh token.

**Large responses are shortened** so one call can't fill the AI's context. A result over 20,000 characters (about 5,000 tokens; change it with `AUTOMCP_MAX_RESPONSE_CHARS`) keeps every field, but long lists show only their first items, long text is cut and deeply nested data is summarized, with a note telling the AI what was left out. For example, pokeapi's 309 KB Pikachu record comes back as 18 KB with its name, types and stats intact. HTML pages come back as their readable text, without markup, scripts or menus.

**Connecting Claude Desktop or Claude Code:** run

```bash
automcp mcp-config
```

It prints a ready-to-paste `claude_desktop_config.json` entry (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`) and the `claude mcp add` command for Claude Code. Both launch `automcp serve --transport stdio` themselves, with `AUTOMCP_HOME` pointing at this folder so the database and tools are found from any directory. Restart Claude Desktop after editing the config.

---

### `test` — Run a tool interactively

```bash
automcp test <tool_name>
```

Prompts for inputs, executes the tool against the live site using your recorded login, and prints the response. Execution is logged to the database.

---

### `ui` — Web dashboard

```bash
automcp ui
automcp ui --port 7860
```

Starts a dark-theme dashboard on your computer. The browser doesn't open by itself: once the terminal shows `AutoMCP Dashboard → http://127.0.0.1:7860`, open that address in your web browser. Keep the terminal open while you use it, and press **Ctrl+C** there to stop it. If the port is taken, run `automcp ui --port 7861` and open that port instead.

What you can do there:
- **Sessions:** click one to see only its specs
- **Specs:** approve specs or mark them read-only, then press **Generate tools** to write the tool files (the same as `automcp generate`)
- **Tools:** click a tool to fill in its inputs and **Run** it with your recorded login, and to read its source code. Tools that change data (POST, PUT, DELETE) ask before sending.
- **Executions:** every tool run, with success rate and duration
- **Projects:** sessions grouped by project

The dashboard only answers requests addressed to this computer, and only accepts changes from its own page, so other websites you visit can't use it to run your tools.

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

## Auth management

### Check if credentials are still valid
```bash
automcp auth-check <session_id>
automcp auth-check <session_id> --probe https://trello.com/1/members/me
```

### Automatic renewal
When a tool gets HTTP 401 or 403 (logged out), AutoMCP renews the login in a hidden browser and retries the call once:

1. It opens the site with the session's saved cookies and storage. Many sites keep a long-lived "remember me" cookie and hand out a fresh session on page load, so this alone often works.
2. If you saved a password with `auth-login` (below), it also fills in the site's login form.

Calls that fail at the same moment share one renewal, and a session is renewed at most once every 2 minutes. Turn it off with `AUTOMCP_AUTO_RELOGIN=false`. Run a renewal by hand with `automcp auth-refresh <session_id> --auto`.

### Save a login so it renews by itself
```bash
automcp auth-login <session_id> --login-url https://example.com/login --username me@example.com
automcp auth-login <session_id> --clear          # forget it
```
Asks for the password, stores it encrypted in the local vault, and tries a renewal straight away to check it works. The login form's fields are found automatically, including forms that ask for the username and password on separate pages; pass `--username-selector` / `--password-selector` (CSS selectors) if they aren't. Logins that need a one-time code, captcha or passkey can't be automated — use `auth-refresh` for those.

### Log in again yourself
```bash
automcp auth-refresh <session_id>
```
Re-opens the browser so you can log in again, then updates that session's stored login. Running tools (including a live `automcp serve`) pick up the new login within 30 seconds; nothing needs regenerating.

### Check for API drift
```bash
automcp drift-check <session_id>
```
Replays the recorded request for each approved GET endpoint and compares the response schema to what was recorded. Flags any endpoints where the API has changed.

## Multi-model support

```bash
automcp analyze <session_id>                          # uses AUTOMCP_LLM_PROVIDER (default: anthropic)
automcp analyze <session_id> --provider openai        # requires AUTOMCP_OPENAI_API_KEY and pip install openai
automcp analyze <session_id> --provider gemini        # requires AUTOMCP_GEMINI_API_KEY and pip install -e ".[gemini]"
```

If `AUTOMCP_LLM_MODEL` isn't a model for the chosen provider, OpenAI falls back to `gpt-4o` and Gemini to `gemini-flash-latest`. Gemini requests retry automatically when the free tier's per-minute limit is hit.

---

## Architecture

```
core/
  recorder/       Playwright session, network capture, DOM snapshot, action mapper
  analyzer/       Event classifier, endpoint clusterer, LLM spec builder
  auth/           Cookies, CSRF, headers, login replay, localStorage/sessionStorage, vault
  codegen/        Jinja2 generator, AST validator, templates/
  runtime/        Request executor, retry/rate limiting, importlib loader, FastMCP server
  storage/        Pydantic v2 models, aiosqlite database

tests/            pytest suite (analysis, codegen safety, runtime, login renewal, dashboard)

apps/
  cli/            Typer CLI (record, analyze, review, generate, serve, test, ui, ...)
  web/            FastAPI dashboard + Jinja2 templates

generated/
  specs/          JSON tool spec files (intermediate representation)
  tools/          Generated Python MCP tool files
```

**Key design decisions:**
- The LLM produces structured JSON specs only — never raw code
- Code is generated from Jinja2 templates deterministically
- No `exec()` — tools load via `importlib.util`
- Generated tools are thin; request logic lives in one shared runtime, so fixes apply without regenerating
- Auth is a pluggable strategy engine (cookies, bearer, API key, CSRF)
- Tool quality scoring: usefulness × stability × side-effect risk

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `AUTOMCP_HOME` | current directory | Folder holding `.env`, the database and generated files |
| `AUTOMCP_LLM_PROVIDER` | `anthropic` | AI used by `analyze`: `anthropic`, `openai` or `gemini` |
| `AUTOMCP_ANTHROPIC_API_KEY` | *(required for Anthropic)* | Anthropic API key |
| `AUTOMCP_GEMINI_API_KEY` | *(required for Gemini)* | Google AI Studio API key |
| `AUTOMCP_OPENAI_API_KEY` | *(required for OpenAI)* | OpenAI API key |
| `AUTOMCP_LLM_MODEL` | `claude-opus-5-5` | Model for analysis |
| `AUTOMCP_ANALYZE_BATCH_SIZE` | `12` | Endpoints sent to the LLM per request |
| `AUTOMCP_RATE_LIMIT_PER_MINUTE` | `60` | Max tool calls per minute (shared) |
| `AUTOMCP_AUTO_RELOGIN` | `true` | Renew an expired login in a hidden browser and retry when a tool gets HTTP 401/403 |
| `AUTOMCP_MAX_RESPONSE_CHARS` | `20000` | Longest tool result sent to the AI; bigger ones are shortened (`0` = no limit) |
| `AUTOMCP_DB_PATH` | `automcp.db` | SQLite database path |
| `AUTOMCP_SERVER_HOST` | `127.0.0.1` | MCP server host |
| `AUTOMCP_SERVER_PORT` | `8000` | MCP server port |
| `AUTOMCP_MIN_CONFIDENCE_THRESHOLD` | `0.5` | Minimum spec confidence |
| `AUTOMCP_LOG_LEVEL` | `INFO` | Log verbosity |
