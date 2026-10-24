"""
backend/metrics.py — In-memory request & LLM call counters for Poly-QL.

Tracks:
  - HTTP request counts by (method, path, status_code)
  - HTTP request latency histograms (approximate, bucketed)
  - LLM call counts by provider
  - LLM error counts by provider

Exposes `render_prometheus()` which returns a Prometheus text-format string
suitable for scraping by Prometheus or a compatible agent.

Thread-safe: all mutations go through a single `threading.Lock`.
"""
import threading
from collections import defaultdict

# ---------------------------------------------------------------------------
# Prometheus histogram buckets (latency in ms)
# ---------------------------------------------------------------------------
_LATENCY_BUCKETS = (50, 100, 250, 500, 1000, 2500, 5000, float("inf"))


class _Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        # {(method, path, status_code): count}
        self._request_counts: dict = defaultdict(int)
        # {(method, path): [latency_ms, ...]} — we store sums + counts for mean
        self._latency_sum: dict = defaultdict(float)
        self._latency_count: dict = defaultdict(int)
        # {(method, path, bucket): count}  bucket ∈ _LATENCY_BUCKETS
        self._latency_hist: dict = defaultdict(int)
        # {provider: count}
        self._llm_calls: dict = defaultdict(int)
        self._llm_errors: dict = defaultdict(int)
        # {provider: times the circuit breaker tripped open}
        self._circuit_open: dict = defaultdict(int)
        # {route: count}
        self._rate_limited: dict = defaultdict(int)

    # ------------------------------------------------------------------
    # Record helpers (called from middleware / endpoint handlers)
    # ------------------------------------------------------------------

    def record_request(self, method: str, path: str, status_code: int, latency_ms: float) -> None:
        key = (method.upper(), path, status_code)
        lat_key = (method.upper(), path)
        with self._lock:
            self._request_counts[key] += 1
            self._latency_sum[lat_key] += latency_ms
            self._latency_count[lat_key] += 1
            for bucket in _LATENCY_BUCKETS:
                if latency_ms <= bucket:
                    self._latency_hist[(method.upper(), path, bucket)] += 1

    def record_llm_call(self, provider: str, error: bool = False) -> None:
        with self._lock:
            self._llm_calls[provider] += 1
            if error:
                self._llm_errors[provider] += 1

    def record_rate_limited(self, route: str) -> None:
        with self._lock:
            self._rate_limited[route] += 1

    def record_circuit_open(self, provider: str) -> None:
        with self._lock:
            self._circuit_open[provider] += 1

    # ------------------------------------------------------------------
    # Prometheus text format renderer
    # ------------------------------------------------------------------

    def render_prometheus(self) -> str:
        lines = []

        # --- request totals ---
        lines.append("# HELP polyql_http_requests_total Total HTTP requests by method, path, and status")
        lines.append("# TYPE polyql_http_requests_total counter")
        with self._lock:
            req_snapshot = dict(self._request_counts)
            lat_sum_snap = dict(self._latency_sum)
            lat_cnt_snap = dict(self._latency_count)
            hist_snap = dict(self._latency_hist)
            llm_calls_snap = dict(self._llm_calls)
            llm_err_snap = dict(self._llm_errors)
            circuit_snap = dict(self._circuit_open)
            rate_limited_snap = dict(self._rate_limited)

        for (method, path, status), count in sorted(req_snapshot.items()):
            lines.append(
                f'polyql_http_requests_total{{method="{method}",path="{path}",status="{status}"}} {count}'
            )

        # --- latency summary ---
        lines.append("")
        lines.append("# HELP polyql_http_request_duration_ms_sum Sum of request durations in ms")
        lines.append("# TYPE polyql_http_request_duration_ms_sum gauge")
        for (method, path), total in sorted(lat_sum_snap.items()):
            lines.append(f'polyql_http_request_duration_ms_sum{{method="{method}",path="{path}"}} {total:.2f}')

        lines.append("")
        lines.append("# HELP polyql_http_request_duration_ms_count Number of observed requests")
        lines.append("# TYPE polyql_http_request_duration_ms_count counter")
        for (method, path), count in sorted(lat_cnt_snap.items()):
            lines.append(f'polyql_http_request_duration_ms_count{{method="{method}",path="{path}"}} {count}')

        # --- latency histogram ---
        lines.append("")
        lines.append("# HELP polyql_http_request_duration_ms_bucket Latency histogram buckets (ms)")
        lines.append("# TYPE polyql_http_request_duration_ms_bucket histogram")
        for (method, path, bucket), count in sorted(hist_snap.items(), key=lambda x: (x[0][0], x[0][1], x[0][2] if x[0][2] != float("inf") else 1e18)):
            le = "+Inf" if bucket == float("inf") else str(int(bucket))
            lines.append(
                f'polyql_http_request_duration_ms_bucket{{method="{method}",path="{path}",le="{le}"}} {count}'
            )

        # --- LLM call counts ---
        lines.append("")
        lines.append("# HELP polyql_llm_calls_total Total LLM calls by provider")
        lines.append("# TYPE polyql_llm_calls_total counter")
        for provider, count in sorted(llm_calls_snap.items()):
            lines.append(f'polyql_llm_calls_total{{provider="{provider}"}} {count}')

        lines.append("")
        lines.append("# HELP polyql_llm_errors_total Total LLM call errors by provider")
        lines.append("# TYPE polyql_llm_errors_total counter")
        for provider, count in sorted(llm_err_snap.items()):
            lines.append(f'polyql_llm_errors_total{{provider="{provider}"}} {count}')

        lines.append("")
        lines.append("# HELP polyql_rate_limited_total Requests rejected by the rate limiter, by route")
        lines.append("# TYPE polyql_rate_limited_total counter")
        for route, count in sorted(rate_limited_snap.items()):
            lines.append(f'polyql_rate_limited_total{{route="{route}"}} {count}')

        lines.append("")
        lines.append("# HELP polyql_circuit_open_total Times a provider circuit breaker tripped open")
        lines.append("# TYPE polyql_circuit_open_total counter")
        for provider, count in sorted(circuit_snap.items()):
            lines.append(f'polyql_circuit_open_total{{provider="{provider}"}} {count}')

        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Reset all counters — intended for tests only."""
        with self._lock:
            self._request_counts.clear()
            self._latency_sum.clear()
            self._latency_count.clear()
            self._latency_hist.clear()
            self._llm_calls.clear()
            self._llm_errors.clear()
            self._circuit_open.clear()
            self._rate_limited.clear()


# Singleton instance — import and use this directly
metrics = _Metrics()
