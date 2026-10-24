"""tests/test_rate_limit.py — A4: per-user rate limiting of LLM-costing routes."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend import rate_limit
from backend.app import app
from backend.auth import get_current_user
from backend.metrics import metrics
from backend.rate_limit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------------
# Unit: token bucket
# ---------------------------------------------------------------------------

class TestBucket:
    def test_burst_then_reject(self):
        clk = FakeClock()
        rl = RateLimiter(60, 3, clock=clk)
        assert [rl.check("a")[0] for _ in range(3)] == [True] * 3
        allowed, retry = rl.check("a")
        assert not allowed
        assert retry == pytest.approx(1.0)  # 60/min = 1 token/s

    def test_refill_over_time(self):
        clk = FakeClock()
        rl = RateLimiter(60, 2, clock=clk)
        rl.check("a")
        rl.check("a")
        assert not rl.check("a")[0]
        clk.t += 1.0
        assert rl.check("a")[0]
        assert not rl.check("a")[0]

    def test_refill_capped_at_burst(self):
        clk = FakeClock()
        rl = RateLimiter(60, 2, clock=clk)
        rl.check("a")
        clk.t += 3600
        assert rl.check("a")[0] and rl.check("a")[0]
        assert not rl.check("a")[0]

    def test_retry_after_shrinks(self):
        clk = FakeClock()
        rl = RateLimiter(30, 1, clock=clk)  # 1 token / 2 s
        rl.check("a")
        clk.t += 0.5
        allowed, retry = rl.check("a")
        assert not allowed and retry == pytest.approx(1.5)

    @pytest.mark.parametrize("rate,burst", [(0, 10), (-1, 10), (30, 0), (30, -5)])
    def test_disabled(self, rate, burst):
        rl = RateLimiter(rate, burst, clock=FakeClock())
        assert all(rl.check("a") == (True, 0.0) for _ in range(100))
        assert len(rl) == 0

    def test_per_key_isolation(self):
        rl = RateLimiter(60, 1, clock=FakeClock())
        assert rl.check("a")[0]
        assert not rl.check("a")[0]
        assert rl.check("b")[0]

    def test_idle_eviction(self):
        clk = FakeClock()
        rl = RateLimiter(60, 5, clock=clk, idle_seconds=100)
        rl.check("a")
        rl.check("b")
        clk.t += 200
        rl.check("c")
        assert len(rl) == 1

    def test_lru_cap(self):
        rl = RateLimiter(60, 5, clock=FakeClock(), max_buckets=3)
        for k in "abcde":
            rl.check(k)
        assert len(rl) == 3

    def test_env_config_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv("POLYQL_RATE_LIMIT_BURST", "1")
        monkeypatch.setenv("POLYQL_RATE_LIMIT_PER_MIN", "1")
        assert rate_limit.limiter.check("k")[0]
        assert not rate_limit.limiter.check("k")[0]
        monkeypatch.setenv("POLYQL_RATE_LIMIT_PER_MIN", "0")
        assert rate_limit.limiter.check("k")[0]

    def test_bad_env_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("POLYQL_RATE_LIMIT_BURST", "abc")
        assert all(rate_limit.limiter.check("k")[0] for _ in range(10))
        assert not rate_limit.limiter.check("k")[0]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

client = TestClient(app)
_CHAT = {"messages": [{"role": "user", "content": "orders"}], "provider": "open_ai", "query_type": "sql"}


@pytest.fixture
def small_limit(monkeypatch):
    monkeypatch.setenv("POLYQL_RATE_LIMIT_BURST", "2")
    monkeypatch.setenv("POLYQL_RATE_LIMIT_PER_MIN", "1")


@pytest.fixture
def as_user():
    original = app.dependency_overrides[get_current_user]

    def _set(name):
        app.dependency_overrides[get_current_user] = lambda: {"sub": name, "uid": 9}
    yield _set
    app.dependency_overrides[get_current_user] = original


class TestApi:
    def test_chat_429_after_burst(self, small_limit):
        ready = {"ready": True, "summary": "x"}
        with patch("backend.app.gatherRequirements", return_value=ready) as g, \
             patch("backend.app.generateQuery", return_value={"type": "sql", "content": "SELECT 1"}):
            assert client.post("/api/chat", json=_CHAT).status_code == 200
            assert client.post("/api/chat", json=_CHAT).status_code == 200
            before = g.call_count
            resp = client.post("/api/chat", json=_CHAT)
        assert resp.status_code == 429
        assert g.call_count == before  # no LLM call on reject
        assert int(resp.headers["Retry-After"]) >= 1
        assert "Rate limit exceeded" in resp.json()["detail"]
        assert 'polyql_rate_limited_total{route="/api/chat"}' in metrics.render_prometheus()

    def test_other_users_unaffected(self, small_limit, as_user):
        with patch("backend.app.gatherRequirements", return_value={"ready": False, "question": "?"}):
            as_user("alice")
            for _ in range(2):
                client.post("/api/chat", json=_CHAT)
            assert client.post("/api/chat", json=_CHAT).status_code == 429
            as_user("bob")
            assert client.post("/api/chat", json=_CHAT).status_code != 429

    def test_query_and_stream_limited(self, small_limit):
        with patch("backend.app.generateQuery", return_value={"type": "sql", "content": "SELECT 1"}):
            client.post("/api/query", json={"query": "q", "provider": "open_ai"})
            client.post("/api/query", json={"query": "q", "provider": "open_ai"})
            assert client.post("/api/query", json={"query": "q", "provider": "open_ai"}).status_code == 429
        # shared budget: stream is now rejected too
        assert client.post("/api/chat/stream", json=_CHAT).status_code == 429

    def test_cheap_routes_not_limited(self, small_limit):
        for _ in range(5):
            assert client.get("/metrics").status_code == 200
            assert client.get("/api/instances").status_code == 200

    def test_disabled_by_zero(self, monkeypatch):
        monkeypatch.setenv("POLYQL_RATE_LIMIT_PER_MIN", "0")
        with patch("backend.app.gatherRequirements", return_value={"ready": False, "question": "?"}):
            for _ in range(15):
                assert client.post("/api/chat", json=_CHAT).status_code != 429


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

class TestMcp:
    def test_generate_sql_limited(self, small_limit):
        from backend import mcp_server as srv
        with patch("main.generateQuery", return_value={"type": "sql", "content": "SELECT 1"}) as gen:
            for _ in range(2):
                assert srv.generate_sql("q", "anthropic")["error"] == ""
            res = srv.generate_sql("q", "anthropic")
        assert gen.call_count == 2
        assert res["type"] == "error"
        assert "try again in" in res["error"]
