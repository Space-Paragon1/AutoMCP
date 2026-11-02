"""Runtime middleware: retry logic, rate limiting, error normalization."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Callable, Awaitable
from dataclasses import dataclass, field

import httpx


class ToolError(Exception):
    """Structured error from a tool execution."""
    def __init__(self, message: str, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay: float = 1.0       # seconds
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    retryable_statuses: tuple[int, ...] = (429, 500, 502, 503, 504)


@dataclass
class RateLimiter:
    calls_per_minute: int = 60
    _timestamps: deque = field(default_factory=deque)
    _lock: asyncio.Lock | None = None
    _lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_lock(self) -> asyncio.Lock:
        # asyncio locks belong to one event loop; make a new one if the loop changed
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def acquire(self) -> None:
        # The lock stops concurrent callers from all passing the check at once
        async with self._get_lock():
            now = time.monotonic()
            # Drop timestamps older than 60 seconds
            while self._timestamps and now - self._timestamps[0] > 60.0:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.calls_per_minute:
                wait = 60.0 - (now - self._timestamps[0])
                if wait > 0:
                    await asyncio.sleep(wait)
            self._timestamps.append(time.monotonic())


def _retry_after_seconds(response: httpx.Response, default: float) -> float:
    """Parse Retry-After, which may be a number of seconds or an HTTP date."""
    value = response.headers.get("retry-after")
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        when = parsedate_to_datetime(value)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return default


async def with_retry(
    fn: Callable[[], Awaitable[Any]],
    config: RetryConfig | None = None,
) -> Any:
    """Execute an async callable with exponential backoff retry."""
    cfg = config or RetryConfig()
    last_exc: Exception | None = None

    for attempt in range(cfg.max_attempts):
        try:
            return await fn()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429 and attempt < cfg.max_attempts - 1:
                # Respect Retry-After header if present
                retry_after = _retry_after_seconds(e.response, cfg.base_delay)
                await asyncio.sleep(min(retry_after, cfg.max_delay))
            elif status in cfg.retryable_statuses and attempt < cfg.max_attempts - 1:
                delay = min(cfg.base_delay * (cfg.backoff_factor ** attempt), cfg.max_delay)
                await asyncio.sleep(delay)
            else:
                raise ToolError(
                    f"HTTP {status}: {e.response.text[:200]}",
                    status_code=status,
                    retryable=status in cfg.retryable_statuses,
                ) from e
            last_exc = e
        except httpx.TimeoutException as e:
            if attempt < cfg.max_attempts - 1:
                delay = min(cfg.base_delay * (cfg.backoff_factor ** attempt), cfg.max_delay)
                await asyncio.sleep(delay)
            last_exc = e
        except httpx.RequestError as e:
            raise ToolError(f"Request failed: {e}", retryable=False) from e

    raise ToolError(f"All {cfg.max_attempts} attempts failed: {last_exc}", retryable=True)


# Module-level rate limiter (shared across all tool calls)
_rate_limiter = RateLimiter(calls_per_minute=60)


async def rate_limited_call(fn: Callable[[], Awaitable[Any]]) -> Any:
    """Wrap a tool call with rate limiting."""
    await _rate_limiter.acquire()
    return await fn()
