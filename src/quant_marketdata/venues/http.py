"""Minimal JSON-over-HTTP client for public venue APIs: stdlib only, rate limited, with backoff.

Public venue APIs throttle hard (Lighter: ~100 requests in a burst, ~2/s sustained), so historical
downloads are sequential and paced; concurrency would only trade requests for 429s.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
USER_AGENT = "quant-marketdata"


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


class RateLimiter:
    """Spaces calls evenly so that at most ``rate`` requests start per second (thread-safe)."""

    def __init__(self, rate: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._interval = 1.0 / rate
        self._next = 0.0
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            self._sleep(delay)


class JsonClient:
    """GET/POST JSON against one base URL, under a rate limiter, retrying 429/5xx and network errors."""

    def __init__(
        self,
        base_url: str,
        *,
        rate: float = 2.0,
        timeout: float = 30.0,
        attempts: int = 6,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.limiter = RateLimiter(rate, sleep=sleep)
        self.timeout = timeout
        self.attempts = attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self._open = opener or urllib.request.urlopen

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        query = {k: _param(v) for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return self._request(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}))

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        return self._request(urllib.request.Request(url, data=body, headers=headers))

    def _request(self, request: urllib.request.Request) -> Any:
        for attempt in range(1, self.attempts + 1):
            self.limiter.wait()
            try:
                with self._open(request, timeout=self.timeout) as response:  # noqa: S310 (venue hosts)
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace") if exc.fp else ""
                if exc.code not in RETRYABLE_STATUSES or attempt == self.attempts:
                    raise HttpError(exc.code, request.full_url, body) from exc
                reason = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == self.attempts:
                    raise
                reason = type(exc).__name__
            delay = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
            logger.debug("Retrying %s in %.0fs after %s (%d/%d)",
                         request.full_url, delay, reason, attempt, self.attempts)
            self._sleep(delay)
        raise AssertionError("unreachable")


def _param(value: Any) -> Any:
    if isinstance(value, bool):
        return str(value).lower()
    return value


__all__ = ["HttpError", "JsonClient", "RateLimiter", "RETRYABLE_STATUSES"]
