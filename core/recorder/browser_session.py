from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from core.auth.cookies import CookieStrategy
from core.auth.headers import HeaderReplayRules
from core.auth.replay import collect_host_headers
from core.auth.storage_tokens import StorageSnapshot, StorageTokenExtractor
from core.recorder.action_mapper import ActionMapper
from core.recorder.network_capture import NetworkCapture
from core.storage.db import get_db
from core.storage.models import CapturedRequest, RecordingSession, utcnow

console = Console()

SNAPSHOT_INTERVAL_SECONDS = 3.0
MAX_STORAGE_VALUE_CHARS = 8192

_READ_STORAGE_JS = """() => ({
    local: Object.fromEntries(Object.entries(localStorage)),
    session: Object.fromEntries(Object.entries(sessionStorage)),
})"""


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


class BrowserSession:
    """
    Async context manager that launches a Playwright browser, records all
    HTTP traffic, extracts auth state on exit, and persists everything to the DB.

    Usage:
        async with BrowserSession(url) as session:
            await session.wait_until_closed()

    With refresh_session_id set, nothing new is saved except fresh auth state
    (cookies, headers, storage) for that existing session.
    """

    def __init__(
        self,
        url: str,
        headless: bool = False,
        user_data_dir: str | None = None,
        project_id: str | None = None,
        refresh_session_id: str | None = None,
    ) -> None:
        self._url = url
        self._headless = headless
        self._user_data_dir = user_data_dir
        self._project_id = project_id
        self._refresh_session_id = refresh_session_id

        self._session_id: str = refresh_session_id or str(uuid.uuid4())
        self._started_at: datetime = utcnow()

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._closed = asyncio.Event()

        self._capture: NetworkCapture = NetworkCapture(self._session_id)
        self._action_mapper: ActionMapper = ActionMapper()

        self._captured_requests: list[CapturedRequest] = []
        self._auth_state: dict[str, Any] = {}
        self._session: RecordingSession | None = None

        # Latest auth snapshot, refreshed while recording so it survives the
        # browser closing (or Ctrl+C) before we get a chance to read it.
        self._latest_cookies: list[dict[str, Any]] = []
        self._latest_storage: dict[str, dict[str, dict[str, str]]] = {}
        self._snapshot_task: asyncio.Task | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    async def __aenter__(self) -> "BrowserSession":
        self._playwright = await async_playwright().start()

        launch_kwargs: dict[str, Any] = {"headless": self._headless}

        if self._user_data_dir:
            # Persistent context preserves cookies/storage across runs
            self._context = await self._playwright.chromium.launch_persistent_context(
                self._user_data_dir,
                **launch_kwargs,
            )
            self._browser = None
        else:
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            self._browser.on("disconnected", lambda _: self._closed.set())
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
        self._context.on("close", lambda _: self._closed.set())

        await self._capture.start(self._context)

        page = await self._context.new_page()
        try:
            await page.goto(self._url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            # Keep recording: the user can still navigate manually
            console.print(f"[yellow]Warning:[/] initial page load did not finish: {e}")

        if not self._refresh_session_id:
            self._session = RecordingSession(
                id=self._session_id,
                url=self._url,
                started_at=self._started_at,
                project_id=self._project_id,
            )
            async with get_db() as db:
                await db.save_session(self._session)

        self._snapshot_task = asyncio.create_task(self._snapshot_loop())

        what = "auth refresh" if self._refresh_session_id else "recording"
        console.print(
            f"[bold green]AutoMCP[/] {what} started — session [cyan]{self._session_id[:8]}[/]\n"
            "Interact with the browser. Close the browser window or press "
            "[bold]Ctrl+C[/] in the terminal to stop."
        )
        return self

    async def wait_until_closed(self) -> None:
        """Return once the browser disconnects or every tab has been closed."""
        while not self._closed.is_set():
            if self._context is not None and not self._context.pages:
                return
            await asyncio.sleep(0.5)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        ended_at = utcnow()

        if self._snapshot_task:
            self._snapshot_task.cancel()
        # One last snapshot; works whenever the context is still alive
        await self._snapshot_auth()

        self._captured_requests = await self._capture.stop()
        self._action_mapper.annotate(self._captured_requests)

        self._build_auth_state()
        await self._persist(ended_at)

        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Auth snapshots
    # ------------------------------------------------------------------

    async def _snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)
            await self._snapshot_auth()

    async def _snapshot_auth(self) -> None:
        if self._context is None or self._closed.is_set():
            return
        try:
            cookies = await self._context.cookies()
            if cookies:
                self._latest_cookies = cookies
        except Exception:
            pass
        for page in list(self._context.pages):
            try:
                origin = _origin(page.url)
                if not origin.startswith("http"):
                    continue
                data = await page.evaluate(_READ_STORAGE_JS)
                self._latest_storage[origin] = {
                    kind: {k: v for k, v in values.items() if len(v) <= MAX_STORAGE_VALUE_CHARS}
                    for kind, values in data.items()
                }
            except Exception:
                pass

    def _cookies_from_requests(self) -> list[dict[str, Any]]:
        """Fallback: rebuild cookies from the Cookie headers the browser sent."""
        jar: dict[tuple[str, str], dict[str, Any]] = {}
        for req in self._captured_requests:
            header = next(
                (v for k, v in req.request_headers.items() if k.lower() == "cookie"), ""
            )
            host = (urlparse(req.url).hostname or "").lower()
            for part in header.split(";"):
                if "=" in part:
                    name, _, value = part.strip().partition("=")
                    jar[(host, name.strip())] = {
                        "name": name.strip(), "value": value.strip(),
                        "domain": host, "path": "/", "expires": -1, "secure": False,
                    }
        return list(jar.values())

    def _build_auth_state(self) -> None:
        jar = self._latest_cookies
        if not jar:
            jar = self._cookies_from_requests()
            if jar:
                console.print("[dim]Cookies rebuilt from captured request headers.[/]")

        slim_jar = [
            {k: c.get(k) for k in ("name", "value", "domain", "path", "expires", "secure")}
            for c in jar
        ]
        # Flat name -> value view (cookies for the recorded site win on name clashes)
        target_host = (urlparse(self._url).hostname or "").lower()
        flat: dict[str, str] = {}
        for c in sorted(slim_jar, key=lambda c: target_host.endswith((c["domain"] or "").lstrip("."))):
            flat[c["name"]] = c["value"]

        cookie_strategy = CookieStrategy()
        header_rules = HeaderReplayRules.analyze(self._captured_requests)

        state: dict[str, Any] = {
            "recorded_at": utcnow().isoformat(),
            "cookies": flat,
            "cookie_jar": slim_jar,
            "auth_cookie_names": cookie_strategy.detect_auth_cookies(flat),
            "host_headers": collect_host_headers(self._captured_requests),
            "auth_headers": {r.header_name: r.header_value for r in header_rules.auth_rules},
        }

        storage = self._latest_storage.get(_origin(self._url))
        if storage is None and self._latest_storage:
            storage = next(reversed(self._latest_storage.values()))
        if storage:
            snapshot = StorageSnapshot(
                local_storage=storage.get("local", {}),
                session_storage=storage.get("session", {}),
                origin=_origin(self._url),
            )
            state["local_storage"] = snapshot.local_storage
            state["session_storage"] = snapshot.session_storage
            state["auth_token_keys"] = StorageTokenExtractor().detect_auth_tokens(snapshot)

        self._auth_state = state

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist(self, ended_at: datetime) -> None:
        if self._refresh_session_id:
            await self._persist_refresh()
            return

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task(
                f"Saving {len(self._captured_requests)} requests to database...", total=None
            )
            async with get_db() as db:
                if self._session:
                    self._session.ended_at = ended_at
                    self._session.request_count = len(self._captured_requests)
                    self._session.browser_context_state = self._auth_state
                    await db.save_session(self._session)
                await db.save_requests(self._captured_requests)

        console.print(
            f"[green]Saved[/] {len(self._captured_requests)} requests for session "
            f"[cyan]{self._session_id[:8]}[/]"
        )

    async def _persist_refresh(self) -> None:
        """Merge fresh auth into the existing session without saving new requests."""
        async with get_db() as db:
            existing = await db.get_session(self._refresh_session_id or "")
            old_state = existing.browser_context_state if existing else {}
            merged = {**old_state, **self._auth_state}
            merged["host_headers"] = {
                **(old_state.get("host_headers") or {}),
                **(self._auth_state.get("host_headers") or {}),
            }
            await db.update_session_auth(self._session_id, merged)
        console.print(
            f"[green]Updated[/] login for session [cyan]{self._session_id[:8]}[/] "
            f"({len(self._auth_state.get('cookie_jar', []))} cookies)"
        )

    async def get_captured_requests(self) -> list[CapturedRequest]:
        return list(self._captured_requests)

    async def get_auth_state(self) -> dict[str, Any]:
        return dict(self._auth_state)
