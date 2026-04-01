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

    async def acquire(self) -> None:
        now = time.monotonic()
        # Drop timestamps older than 60 seconds
        while self._timestamps and now - self._timestamps[0] > 60.0:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.calls_per_minute:
            wait = 60.0 - (now - self._timestamps[0])
            if wait > 0:
                await asyncio.sleep(wait)
        self._timestamps.append(time.monotonic())


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
            if status == 429:
                # Respect Retry-After header if present
                retry_after = float(e.response.headers.get("retry-after", cfg.base_delay))
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
