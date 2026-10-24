"""
backend/rate_limit.py — small in-process token-bucket rate limiter.

Guards the endpoints/tools that cost LLM calls. One bucket per caller key
(user id, client IP, MCP token subject).  State is per process.

Config (read at call time, so changes take effect without a restart):
  POLYQL_RATE_LIMIT_PER_MIN  sustained refill rate, default 30
  POLYQL_RATE_LIMIT_BURST    bucket capacity,       default 10
A rate or burst <= 0 disables limiting.
"""
import math
import os
import threading
import time
from collections import OrderedDict
from typing import Callable

from fastapi import Depends, HTTPException, Request

from backend.auth import get_current_user
from backend.metrics import metrics

DEFAULT_RATE_PER_MIN = 30.0
DEFAULT_BURST = 10.0
_MAX_BUCKETS = 10_000          # LRU cap
_IDLE_EVICT_SECONDS = 600.0    # drop buckets idle this long (they are full by then)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class RateLimiter:
    """Thread-safe token bucket keyed by caller.

    ``rate_per_minute`` / ``burst`` may be numbers or zero-arg callables (read
    on every check). A non-positive value for either disables the limiter.
    """

    def __init__(self, rate_per_minute=DEFAULT_RATE_PER_MIN, burst=DEFAULT_BURST,
                 clock: Callable[[], float] = time.monotonic,
                 max_buckets: int = _MAX_BUCKETS,
                 idle_seconds: float = _IDLE_EVICT_SECONDS):
        self._rate = rate_per_minute
        self._burst = burst
        self._clock = clock
        self._max = max_buckets
        self._idle = idle_seconds
        self._lock = threading.Lock()
        # key -> [tokens, last_ts]; ordered by recency of use
        self._buckets: "OrderedDict[str, list]" = OrderedDict()
        self._last_sweep = clock()

    @staticmethod
    def _val(v) -> float:
        return float(v() if callable(v) else v)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)

    def check(self, key: str) -> "tuple[bool, float]":
        """Consume one token. Returns (allowed, retry_after_seconds)."""
        rate = self._val(self._rate)
        burst = self._val(self._burst)
        if rate <= 0 or burst <= 0:
            return True, 0.0
        per_sec = rate / 60.0
        now = self._clock()
        with self._lock:
            self._evict(now)
            b = self._buckets.get(key)
            if b is None:
                tokens = burst
            else:
                tokens = min(burst, b[0] + (now - b[1]) * per_sec)
            if tokens >= 1.0:
                self._buckets[key] = [tokens - 1.0, now]
                allowed, retry = True, 0.0
            else:
                self._buckets[key] = [tokens, now]
                allowed, retry = False, (1.0 - tokens) / per_sec
            self._buckets.move_to_end(key)
            while len(self._buckets) > self._max:
                self._buckets.popitem(last=False)
            return allowed, retry

    def _evict(self, now: float) -> None:
        # Oldest-used entries are first; stop at the first one still recent.
        if now - self._last_sweep < 1.0:
            return
        self._last_sweep = now
        while self._buckets:
            key = next(iter(self._buckets))
            if now - self._buckets[key][1] > self._idle:
                del self._buckets[key]
            else:
                break


limiter = RateLimiter(
    lambda: _env_float("POLYQL_RATE_LIMIT_PER_MIN", DEFAULT_RATE_PER_MIN),
    lambda: _env_float("POLYQL_RATE_LIMIT_BURST", DEFAULT_BURST),
)


def reset() -> None:
    """Clear all buckets (tests)."""
    limiter.reset()


def retry_after_header(retry_after: float) -> int:
    return max(1, math.ceil(retry_after))


def rate_limited(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency: 429 + Retry-After when the caller exceeds the limit."""
    key = None
    if isinstance(user, dict):
        key = user.get("sub") or user.get("uid")
    key = f"user:{key}" if key is not None else \
        f"ip:{request.client.host if request.client else 'unknown'}"
    allowed, retry = limiter.check(key)
    if not allowed:
        secs = retry_after_header(retry)
        route = request.scope.get("route")
        metrics.record_rate_limited(getattr(route, "path", request.url.path))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded — try again in {secs} s.",
            headers={"Retry-After": str(secs)},
        )
    return user
