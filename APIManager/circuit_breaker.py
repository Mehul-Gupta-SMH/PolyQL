"""
Per-provider circuit breakers for LLM calls.

States:
    closed     normal; consecutive failures are counted
    open       after N consecutive failures; calls fail fast with CircuitOpenError
    half_open  after the cooldown; exactly one trial call is let through at a
               time. Success closes the breaker, failure re-opens it with a
               fresh cooldown.

Config (read when a breaker is first created; ``reset()`` re-reads it):
    POLYQL_BREAKER_FAILURES    consecutive failures that trip it (default 5; <= 0 disables)
    POLYQL_BREAKER_COOLDOWN_S  seconds to stay open (default 60)
"""
import math
import os
import threading
import time

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

_DEFAULT_FAILURES = 5
_DEFAULT_COOLDOWN_S = 60.0


class CircuitOpenError(Exception):
    """Raised instead of calling a provider whose breaker is open."""

    def __init__(self, provider: str, retry_after: float):
        self.provider = provider
        self.retry_after = max(0.0, float(retry_after))
        secs = max(1, math.ceil(self.retry_after))
        super().__init__(
            f"{provider} is paused after repeated failures — retrying in {secs} s"
        )

    @property
    def retry_after_seconds(self) -> int:
        return max(1, math.ceil(self.retry_after))


class CircuitBreaker:
    def __init__(self, failure_threshold=_DEFAULT_FAILURES,
                 cooldown_seconds=_DEFAULT_COOLDOWN_S, clock=time.monotonic,
                 provider: str = ""):
        self.provider = provider
        self.failure_threshold = int(failure_threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_started = None   # clock value when the half-open trial began

    @property
    def enabled(self) -> bool:
        return self.failure_threshold > 0

    def _retry_after_locked(self) -> float:
        return max(0.0, self._opened_at + self.cooldown_seconds - self._clock())

    def _effective_state_locked(self) -> str:
        if self._state == OPEN and self._retry_after_locked() <= 0:
            return HALF_OPEN
        return self._state

    def before_call(self) -> None:
        """Raise CircuitOpenError if the call must not go ahead."""
        if not self.enabled:
            return
        with self._lock:
            now = self._clock()
            if self._state == OPEN:
                remaining = self._retry_after_locked()
                if remaining > 0:
                    raise CircuitOpenError(self.provider, remaining)
                self._state = HALF_OPEN
                self._trial_started = None
            if self._state == HALF_OPEN:
                # A trial that never reported back (abandoned) is reclaimed
                # after one cooldown so the breaker cannot wedge.
                if (self._trial_started is not None
                        and now - self._trial_started < self.cooldown_seconds):
                    raise CircuitOpenError(self.provider, self.cooldown_seconds)
                self._trial_started = now

    def record_success(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state = CLOSED
            self._failures = 0
            self._trial_started = None

    def record_failure(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._state == HALF_OPEN:
                self._trip_locked()
                return
            self._failures += 1
            if self._state == CLOSED and self._failures >= self.failure_threshold:
                self._trip_locked()

    def abandon(self) -> None:
        """The call ended without a verdict (e.g. consumer closed a stream)."""
        with self._lock:
            if self._state == HALF_OPEN:
                self._trial_started = None

    def _trip_locked(self) -> None:
        self._state = OPEN
        self._opened_at = self._clock()
        self._trial_started = None
        try:
            from backend.metrics import metrics
            metrics.record_circuit_open(self.provider)
        except Exception:
            pass

    def snapshot(self) -> dict:
        with self._lock:
            state = self._effective_state_locked() if self.enabled else CLOSED
            retry = self._retry_after_locked() if state == OPEN else 0.0
            return {"state": state, "failures": self._failures,
                    "retry_after": round(retry, 1)}


_registry: dict = {}
_registry_lock = threading.Lock()


def _env_number(name: str, default, cast):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def get_breaker(provider: str) -> CircuitBreaker:
    key = str(provider).lower()
    with _registry_lock:
        br = _registry.get(key)
        if br is None:
            br = CircuitBreaker(
                _env_number("POLYQL_BREAKER_FAILURES", _DEFAULT_FAILURES, int),
                _env_number("POLYQL_BREAKER_COOLDOWN_S", _DEFAULT_COOLDOWN_S, float),
                provider=key,
            )
            _registry[key] = br
        return br


def snapshot() -> dict:
    with _registry_lock:
        items = list(_registry.items())
    return {k: b.snapshot() for k, b in items}


def reset() -> None:
    with _registry_lock:
        _registry.clear()
