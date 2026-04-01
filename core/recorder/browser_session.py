from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from core.auth.cookies import CookieStrategy
from core.auth.headers import HeaderReplayRules
from core.auth.storage_tokens import StorageTokenExtractor
from core.recorder.action_mapper import ActionMapper
from core.recorder.dom_snapshot import DomSnapshotter
from core.recorder.network_capture import NetworkCapture
from core.storage.db import get_db
from core.storage.models import CapturedRequest, RecordingSession

console = Console()


class BrowserSession:
    """
    Async context manager that launches a Playwright browser, records all
    HTTP traffic, extracts auth state on exit, and persists everything to the DB.
    """

    def __init__(
        self,
        url: str,
        headless: bool = False,
        user_data_dir: str | None = None,
        project_id: str | None = None,
    ) -> None:
        self._url = url
        self._headless = headless
        self._user_data_dir = user_data_dir
        self._project_id = project_id

        self._session_id: str = str(uuid.uuid4())
        self._started_at: datetime = datetime.utcnow()

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

        self._capture: NetworkCapture = NetworkCapture(self._session_id)
        self._action_mapper: ActionMapper = ActionMapper()
        self._dom_snapshotter: DomSnapshotter = DomSnapshotter()

        self._captured_requests: list[CapturedRequest] = []
        self._auth_state: dict[str, Any] = {}
        self._session: RecordingSession | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    async def __aenter__(self) -> "BrowserSession":
        self._playwright = await async_playwright().start()

        launch_kwargs: dict[str, Any] = {
            "headless": self._headless,
        }

        if self._user_data_dir:
            # Persistent context preserves cookies/storage across runs
            self._context = await self._playwright.chromium.launch_persistent_context(
                self._user_data_dir,
                **launch_kwargs,
            )
            self._browser = None
        else:
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )

        # Start network capture
        await self._capture.start(self._context)

        # Open the initial page
        page = await self._context.new_page()
        await page.goto(self._url, wait_until="domcontentloaded", timeout=30_000)

        # Save initial session record
        self._session = RecordingSession(
            id=self._session_id,
            url=self._url,
            started_at=self._started_at,
            project_id=self._project_id,
        )
        db = get_db()
        async with db:
            await db.save_session(self._session)

        console.print(
            f"[bold green]AutoMCP[/] recording started — session [cyan]{self._session_id[:8]}[/]\n"
            "Interact with the browser. Close the browser window or press "
            "[bold]Ctrl+C[/] in the terminal to stop."
        )

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        ended_at = datetime.utcnow()

        # Stop network capture
        self._captured_requests = await self._capture.stop()

        # Annotate requests with inferred action labels
        self._action_mapper.annotate(self._captured_requests)

        # Extract auth state
        await self._extract_auth_state()

        # Persist to DB
        await self._persist(ended_at)

        # Close browser
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass

    def _extract_cookies_from_requests(self) -> dict[str, str]:
        """Fallback: parse cookies from captured request Cookie headers."""
        cookies: dict[str, str] = {}
        for req in self._captured_requests:
            header = (
                req.request_headers.get("cookie")
                or req.request_headers.get("Cookie")
                or ""
            )
            for part in header.split(";"):
                part = part.strip()
                if "=" in part:
                    k, _, v = part.partition("=")
                    cookies[k.strip()] = v.strip()
        return cookies

    async def _extract_auth_state(self) -> None:
        """Extract cookies, headers, and storage tokens from the browser."""
        if not self._context:
            return

        try:
            cookie_strategy = CookieStrategy()
            try:
                cookies = await cookie_strategy.extract(self._context)
            except Exception:
                # Browser already closed — fall back to request headers
                cookies = self._extract_cookies_from_requests()
                console.print("[dim]Cookies extracted from captured request headers.[/]")

            auth_cookies = cookie_strategy.detect_auth_cookies(cookies)

            header_rules = HeaderReplayRules.analyze(self._captured_requests)

            self._auth_state = {
                "cookies": cookies,
                "auth_cookie_names": auth_cookies,
                "recorded_at": datetime.utcnow().isoformat(),
                "replay_headers": {
                    r.header_name: r.header_value for r in header_rules.rules
                },
                "auth_headers": {
                    r.header_name: r.header_value for r in header_rules.auth_rules
                },
            }

            # Try to extract localStorage/sessionStorage tokens
            try:
                extractor = StorageTokenExtractor()
                from urllib.parse import urlparse

                origin = f"{urlparse(self._url).scheme}://{urlparse(self._url).netloc}"
                snapshot = await extractor.extract(self._context, origin)
                auth_token_keys = extractor.detect_auth_tokens(snapshot)
                self._auth_state["local_storage"] = snapshot.local_storage
                self._auth_state["session_storage"] = snapshot.session_storage
                self._auth_state["auth_token_keys"] = auth_token_keys
            except Exception as e:
                console.print(f"[dim]Storage token extraction skipped: {e}[/]")

        except Exception as e:
            console.print(f"[yellow]Warning:[/] Auth extraction failed: {e}")
            self._auth_state = {}

    async def _persist(self, ended_at: datetime) -> None:
        """Save session and all requests to the database with a progress indicator."""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Saving session to database...", total=None)

            db = get_db()
            async with db:
                # Update session record
                if self._session:
                    self._session.ended_at = ended_at
                    self._session.request_count = len(self._captured_requests)
                    self._session.browser_context_state = self._auth_state
                    await db.save_session(self._session)

                # Save all requests
                for i, req in enumerate(self._captured_requests):
                    await db.save_request(req)
                    if i % 20 == 0:
                        progress.update(
                            task,
                            description=f"Saving requests... {i + 1}/{len(self._captured_requests)}",
                        )

            progress.update(task, description="Done!")

        console.print(
            f"[green]Saved[/] {len(self._captured_requests)} requests for session "
            f"[cyan]{self._session_id[:8]}[/]"
        )

    async def get_captured_requests(self) -> list[CapturedRequest]:
        return list(self._captured_requests)

    async def get_auth_state(self) -> dict[str, Any]:
        return dict(self._auth_state)
