# AutoMCP 2.0

[![Tests](https://github.com/Space-Paragon1/AutoMCP/actions/workflows/tests.yml/badge.svg)](https://github.com/Space-Paragon1/AutoMCP/actions/workflows/tests.yml)

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

To check the install, run `pytest` (see [Check that everything works](#check-that-everything-works)).

---

## Everyday commands

Open a terminal in this folder and run `source .venv/bin/activate` first. Do this in every new terminal window, or the `automcp` command won't be found.

| What you want to do | Command |
|---|---|
| Record a site | `automcp record https://example.com` |
| List your recordings | `automcp sessions` |
| Turn a recording into tool specs | `automcp analyze <session_id>` |
| Approve the specs you want | `automcp review <session_id>` |
| Write the approved tools to files | `automcp generate <session_id>` |
| List your tools | `automcp tools` |
| Try one tool in the terminal | `automcp test <tool_name>` |
| Delete an old recording and its tools | `automcp delete-session <session_id>` |
| Delete one tool | `automcp delete-tool <tool_name>` |
| See past tool calls | `automcp logs` |
| Open the dashboard | `automcp ui`, then open http://127.0.0.1:7860 in your browser |
| Get the setup for Claude Code and Claude Desktop | `automcp mcp-config` |
| Check a recorded login still works | `automcp auth-check <session_id>` |
| Run the MCP server yourself (for other MCP clients) | `automcp serve` |
| Run the automated tests | `pytest` |
| Check code style | `ruff check .` |
| See every command and option | `automcp --help`, or `automcp <command> --help` |

Claude Code and Claude Desktop start the MCP server themselves, so you don't need `automcp serve` for them.

---

## Quick start: your first tool

This walkthrough uses [pokeapi.co](https://pokeapi.co), a public site with no login, so nothing private is sent to the AI. Run each command in a terminal opened in this folder, after `source .venv/bin/activate`.

1. **Record.** Run `automcp record https://pokeapi.co`. A browser window opens. In the "Try it now" box, type `pokemon/ditto` and click **Submit**. Repeat with `pokemon/pikachu` and `type/3`, then close the browser window. The terminal prints a short session ID, such as `1a2b3c4d`.
2. **Analyze.** Run `automcp analyze <session_id>`. The AI turns the recorded requests into tool specs. On Gemini's free tier this can take a few minutes.
3. **Review.** Run `automcp review <session_id>` and answer `approve` for the specs you want. [`review`](#review--approve-specs-interactively) explains what each field means.
4. **Generate.** Run `automcp generate <session_id>`. This writes the tool files to `generated/tools/`.
5. **Try a tool.** Run `automcp test <tool_name>` with a name from step 4, for example `automcp test get_pokemon`. It asks for each input (type `pikachu`), calls the site and shows the response. The response is long, so scroll up to see the top.
6. **Use it from Claude.** See [Use your tools in Claude](#use-your-tools-in-claude).
7. **See it all in the dashboard.** See [Open the dashboard](#open-the-dashboard).

### A second example: Wikipedia

Wikipedia's search box and link previews call small data APIs, so it turns into tools without logging in.

1. Run `automcp record https://en.wikipedia.org`. Stay logged out.
2. In the browser, type `Albert Einstein` in the search box at the top and wait for the suggestion list. You don't need to press Enter. Clear it, type `Moon` and click a suggestion. In the article, hold the mouse over a few blue links until a preview card pops up. Then close the browser window.
3. Run `automcp analyze <session_id>`, `automcp review <session_id>` and `automcp generate <session_id>`.

You should get tools like these (the AI picks the exact names):

| Tool | What it does |
|---|---|
| `get_page_summary(title)` | Short summary of an article, like the link preview card |
| `search_titles(q, limit)` | Title suggestions, like the search box dropdown |
| `search_pages(q, limit)` | Full-text search inside articles |

Try one with `automcp test get_page_summary` and the title `Moon`.

### Picking a site

Any site works. pokeapi and Wikipedia are just easy places to start.

- **Use the site the way you want the tools to work.** Each kind of action you take (search, open an item, save something) can become a tool.
- **Privacy.** Passwords, cookies and tokens are removed before `analyze` sends anything to the AI, but the rest of the recorded traffic is sent. On Gemini's free tier, pick sites you don't mind Google seeing.
- **Sites you log in to work too.** Log in while recording, and the tools reuse that login (see [Auth management](#auth-management)).
- **"No useful requests remain after filtering"** means the site didn't make any API calls while you recorded. Record again and use the site more, or run `analyze` with `--include-pages` for sites that load whole pages instead of calling an API.
- **API on another domain?** Keep those calls with `analyze --allow-host api.thatdomain.com`.
- **Not supported well yet:** sites that send every action to one address (GraphQL). Each action should become its own tool, but today they get grouped together.

---

## Use your tools in Claude

Run `automcp mcp-config`. It prints the setup for both apps, with the paths for your computer filled in. Claude starts the AutoMCP server by itself, so you don't need to run `automcp serve`.

**Claude Code**

1. In this folder, run the `claude mcp add automcp ...` command that `mcp-config` printed.
2. Start a new Claude Code conversation in this folder. A conversation that was already open won't see the tools. Tools added this way only show up in this folder. To use them in every folder, add `--scope user` right after `claude mcp add`.
3. Type `/mcp` to check. You should see **automcp ✔ connected**.
4. To remove it later, run `claude mcp remove automcp -s local` (or `-s user` if you used `--scope user`).

**Claude Desktop**

1. Quit Claude Desktop. It saves its own settings to the same file, so editing while it's open can lose your change.
2. Open the settings file: `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows. Make a copy of it first.
3. Add the printed `"automcp"` entry inside `"mcpServers"`. The file may already hold other settings, such as `"preferences"`. Keep those, and add `"mcpServers"` at the top level if it isn't there yet:

   ```json
   {
     "preferences": { "...": "your existing settings stay as they are" },
     "mcpServers": {
       "automcp": {
         "command": "/path/printed/by/mcp-config/automcp",
         "args": ["serve", "--transport", "stdio"],
         "env": { "AUTOMCP_HOME": "/path/to/this/folder" }
       }
     }
   }
   ```

4. Open Claude Desktop again. AutoMCP's tools load when it starts.

**Prompts to try** (with the pokeapi and Wikipedia tools above):

- *Use the automcp tools to get the Wikipedia summary for the Moon.*
- *Use automcp to search Wikipedia titles for "albert ein" and show me the top 3 matches.*
- *Use automcp to search Wikipedia for "moon landing", pick the most relevant article, get its summary, and explain it in 3 sentences.*
- *Use automcp to look up Pikachu with get_pokemon, then get Pikachu's Wikipedia summary, and tell me its type and where it comes from.*

The first time Claude uses a tool, it asks your permission.

**Every tool in `generated/tools/` is turned on,** including tools for sites you logged in to. Tools that only read are marked read-only. Tools that change things (create, star, merge, delete) aren't, so Claude asks before running them. To turn a tool off, move its file out of `generated/tools/`, then start a new Claude Code conversation or restart Claude Desktop.

---

## Open the dashboard

1. Run `automcp ui` (after `source .venv/bin/activate`).
2. When the terminal shows `AutoMCP Dashboard → http://127.0.0.1:7860`, open http://127.0.0.1:7860 in your web browser. It doesn't open by itself.
3. Keep the terminal open while you use it. Press **Ctrl+C** there to stop it.

In the dashboard you can browse your sessions, approve specs, generate tools, run a tool and see every past call. More in [`ui`](#ui--web-dashboard).

---

## Check that everything works

1. **Automated tests:** run `pytest`. All tests should pass in about 20 seconds. GitHub also runs them, plus `ruff check .`, on every push to `main` (Python 3.11, 3.12 and 3.13). The badge at the top of this page turns red if something fails, and the **Actions** tab on GitHub shows the details.
2. **A tool from the terminal:** run `automcp test get_pokemon` and type `pikachu`. You should see Pikachu's data.
3. **The call was logged:** run `automcp logs`. The call from step 2 is listed with status `OK`.
4. **The dashboard:** [open it](#open-the-dashboard) and check that your sessions and tools appear, and the call from step 2 shows under **Executions**.
5. **Claude:** in a new Claude Code conversation in this folder, `/mcp` shows **automcp ✔ connected**. Then ask one of the [prompts above](#use-your-tools-in-claude).

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

### `delete-session` — Delete old recordings

```bash
automcp delete-session 1ba9148b
automcp delete-session 1ba9148b 834a2117     # several at once
automcp delete-session 1ba9148b --yes        # don't ask first
```

Deletes a recording and everything made from it: its recorded requests, its tool specs, its tool files and its saved login (if you used `auth-login`). It lists what will go and asks before deleting anything. The tool call history in `automcp logs` is kept.

If `automcp serve` is running, or Claude has AutoMCP open, restart it so the deleted tools disappear.

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

**GraphQL sites.** Many modern sites send every action to one address, usually `/graphql`, and name the action inside the request. `analyze` recognises these calls and makes one tool per operation (`GetCountry`, `CreateIssue`, ...) instead of one tool for the whole address. The query text is copied exactly from the recording, so the AI only decides which variables become inputs. Variables that had the same value in every recorded call (page sizes, flags) are kept as they were. Queries are marked read-only, so Claude knows they don't change anything. Saved ("persisted") queries, GraphQL over GET, and several operations sent together in one call all work. When the site answers with GraphQL errors, the tool reports them as a failure. If the errors say you're logged out, AutoMCP renews the login like it does for HTTP 401.

---

### `review` — Approve specs interactively

```bash
automcp review <session_id>
```

Walk through each spec one by one. For each one you can:
- `approve` — include it in the next generate run
- `rename` — change the tool name and purpose (and approve it)
- `readonly` — mark it as a read-only tool (and approve it)
- `skip` — exclude it
- `quit` — stop here; choices you already made are kept

Only approved specs are generated into tools.

What each spec shows:
- **URL:** the address the tool calls. Parts in braces, like `{title}`, are filled in from the tool's inputs.
- **Inputs:** `(*)` is required, `(?)` is optional.
- **Auth:** how the tool sends your recorded login, for example `cookies`.
- **Confidence:** how sure the AI is that it understood the call. `analyze` drops specs below 0.5.
- **Quality:** a rough automatic score based on the method, the number of `{...}` parts in the URL and the risk of side effects. Read-only tools top out around 0.74, so 0.6–0.74 is normal for them.
- **Read-only:** shows `yes` only if you chose `readonly`. GET tools are marked read-only when served anyway, so `no` on a GET tool is fine.

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

It prints a ready-to-paste `claude_desktop_config.json` entry (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`) and the `claude mcp add` command for Claude Code. Both launch `automcp serve --transport stdio` themselves, with `AUTOMCP_HOME` pointing at this folder so the database and tools are found from any directory. Step-by-step setup is in [Use your tools in Claude](#use-your-tools-in-claude).

---

### `test` — Run a tool interactively

```bash
automcp test <tool_name>
automcp test get_pokemon     # asks for name_or_id: type pikachu
```

Asks for each input (press Enter to skip an optional one), runs the tool against the live site using your recorded login, and prints the response. Long responses are shortened the same way as for Claude. Each run is logged; see `automcp logs`.

`test` really runs the tool, so a tool that changes things (like creating an issue) makes that change on the site.

---

### `tools` / `delete-tool` — List and delete tools

```bash
automcp tools                       # every generated tool, its recording, and whether it reads or changes data
automcp delete-tool get_pokemon     # asks first; add --yes to skip the question
```

`delete-tool` removes the tool file and its spec, and leaves the recording alone. To get the tool back, run `automcp analyze` on its recording again, then `review` and `generate`.

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
automcp logs --tool get_page_summary --limit 50
```

Shows the most recent tool calls (20 by default) from Claude, `automcp test` and the dashboard, with status and how long each took.

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
automcp analyze <session_id> --provider openai        # requires AUTOMCP_OPENAI_API_KEY and pip install -e ".[openai]"
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
  storage/        Pydantic v2 models, aiosqlite database, deleting recordings and tools

tests/            pytest suite (analysis, CLI, codegen safety, runtime, response shortening, login renewal, dashboard, GraphQL, deleting)
.github/workflows/ GitHub runs ruff and the tests on every push (tests.yml)

apps/
  cli/            Typer CLI (record, analyze, review, generate, serve, test, tools, delete-session, ui, ...)
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
- Tool quality score: a weighted mix of usefulness, stability and low side-effect risk

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
