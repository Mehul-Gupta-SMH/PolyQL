"""B1: per-provider circuit breakers."""
import threading
from unittest.mock import MagicMock, patch

import pytest
import requests as _requests
from fastapi.testclient import TestClient

from APIManager import circuit_breaker as cb
from APIManager.AllAPICaller import CallLLMApi
from APIManager.circuit_breaker import CircuitBreaker, CircuitOpenError
from backend.app import app

client = TestClient(app)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _breaker(threshold=3, cooldown=60):
    clock = FakeClock()
    return CircuitBreaker(threshold, cooldown, clock=clock, provider="p"), clock


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class TestStateMachine:
    def test_opens_after_threshold(self):
        b, _ = _breaker()
        for _i in range(2):
            b.before_call()
            b.record_failure()
        assert b.snapshot()["state"] == "closed"
        b.before_call()
        b.record_failure()
        assert b.snapshot()["state"] == "open"
        with pytest.raises(CircuitOpenError) as ei:
            b.before_call()
        assert ei.value.provider == "p"
        assert ei.value.retry_after == 60

    def test_success_resets_count(self):
        b, _ = _breaker()
        b.record_failure()
        b.record_failure()
        b.record_success()
        b.record_failure()
        b.record_failure()
        assert b.snapshot() == {"state": "closed", "failures": 2, "retry_after": 0.0}

    def test_half_open_after_cooldown_and_success_closes(self):
        b, clock = _breaker()
        for _i in range(3):
            b.record_failure()
        clock.t += 30
        assert b.snapshot()["retry_after"] == 30
        clock.t += 30
        assert b.snapshot()["state"] == "half_open"
        b.before_call()            # the trial
        b.record_success()
        assert b.snapshot()["state"] == "closed"
        b.before_call()            # closed again

    def test_half_open_allows_single_trial(self):
        b, clock = _breaker()
        for _i in range(3):
            b.record_failure()
        clock.t += 61
        b.before_call()
        with pytest.raises(CircuitOpenError):
            b.before_call()

    def test_half_open_failure_reopens_with_fresh_cooldown(self):
        b, clock = _breaker()
        for _i in range(3):
            b.record_failure()
        clock.t += 61
        b.before_call()
        b.record_failure()
        snap = b.snapshot()
        assert snap["state"] == "open" and snap["retry_after"] == 60
        with pytest.raises(CircuitOpenError):
            b.before_call()

    def test_abandoned_trial_is_reclaimed(self):
        b, clock = _breaker()
        for _i in range(3):
            b.record_failure()
        clock.t += 61
        b.before_call()
        b.abandon()
        b.before_call()            # a new trial is allowed

    def test_disabled_when_threshold_zero(self):
        b, _ = _breaker(threshold=0)
        for _i in range(50):
            b.before_call()
            b.record_failure()
        b.before_call()
        assert b.snapshot()["state"] == "closed"

    def test_thread_safety_smoke(self):
        b = CircuitBreaker(10**9, 60, provider="p")

        def work():
            for _i in range(500):
                b.before_call()
                b.record_failure()

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert b.snapshot()["failures"] == 4000

    def test_half_open_single_trial_under_threads(self):
        b, clock = _breaker()
        for _i in range(3):
            b.record_failure()
        clock.t += 61
        allowed = []
        barrier = threading.Barrier(8)

        def work():
            barrier.wait()
            try:
                b.before_call()
                allowed.append(1)
            except CircuitOpenError:
                pass

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(allowed) == 1


class TestRegistry:
    def test_env_config_and_snapshot(self, monkeypatch):
        monkeypatch.setenv("POLYQL_BREAKER_FAILURES", "2")
        monkeypatch.setenv("POLYQL_BREAKER_COOLDOWN_S", "7")
        cb.reset()
        b = cb.get_breaker("OpenAI")
        assert b is cb.get_breaker("openai")
        assert (b.failure_threshold, b.cooldown_seconds) == (2, 7.0)
        b.record_failure()
        b.record_failure()
        assert cb.snapshot()["openai"]["state"] == "open"
        cb.reset()
        assert cb.snapshot() == {}

    def test_bad_env_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("POLYQL_BREAKER_FAILURES", "lots")
        cb.reset()
        assert cb.get_breaker("x").failure_threshold == 5


# ---------------------------------------------------------------------------
# CallService / CallServiceStream integration
# ---------------------------------------------------------------------------

def _llm(service="groq"):
    obj = CallLLMApi.__new__(CallLLMApi)
    obj.llmService = service
    obj.model_override = None
    obj.api_temp_dict = {
        "endpoint": "http://llm.test/v1",
        "headers": {},
        "payload": {"messages": [{"content": ""}]},
    }
    return obj


def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.text = "err"
    r.json.return_value = body or {"choices": [{"message": {"content": "ok"}}]}
    return r


@pytest.fixture
def small_breaker(monkeypatch):
    monkeypatch.setenv("POLYQL_BREAKER_FAILURES", "2")
    monkeypatch.setenv("POLYQL_BREAKER_COOLDOWN_S", "60")
    cb.reset()


class TestCallService:
    def test_opens_then_fails_fast_then_recovers(self, small_breaker):
        llm = _llm()
        clock = FakeClock()
        cb.get_breaker("groq")._clock = clock
        with patch("APIManager.AllAPICaller.requests.post",
                   side_effect=_requests.exceptions.ConnectionError("down")) as post:
            for _i in range(2):
                with pytest.raises(_requests.exceptions.ConnectionError):
                    llm.CallService("hi")
            assert post.call_count == 2
            with pytest.raises(CircuitOpenError):
                llm.CallService("hi")
            assert post.call_count == 2   # no network call while open
        clock.t += 61
        with patch("APIManager.AllAPICaller.requests.post", return_value=_resp()) as post:
            assert llm.CallService("hi") == "ok"
            assert post.call_count == 1
        assert cb.snapshot()["groq"]["state"] == "closed"

    def test_http_4xx_counts_as_failure(self, small_breaker):
        llm = _llm()
        with patch("APIManager.AllAPICaller.requests.post", return_value=_resp(401)):
            for _i in range(2):
                with pytest.raises(ValueError):
                    llm.CallService("hi")
        with pytest.raises(CircuitOpenError):
            llm.CallService("hi")

    @pytest.mark.parametrize("status", [400, 413, 422])
    def test_request_errors_do_not_count(self, small_breaker, status):
        # One user's oversized or malformed prompt must not pause the provider for everyone
        llm = _llm()
        with patch("APIManager.AllAPICaller.requests.post", return_value=_resp(status)):
            for _i in range(3):
                with pytest.raises(ValueError):
                    llm.CallService("hi")
        assert cb.snapshot()["groq"] == {"state": "closed", "failures": 0, "retry_after": 0.0}

    def test_request_error_frees_half_open_trial(self, small_breaker):
        llm = _llm()
        clock = FakeClock()
        cb.get_breaker("groq")._clock = clock
        with patch("APIManager.AllAPICaller.requests.post", return_value=_resp(401)):
            for _i in range(2):
                with pytest.raises(ValueError):
                    llm.CallService("hi")
        clock.t += 61
        with patch("APIManager.AllAPICaller.requests.post", return_value=_resp(400)):
            with pytest.raises(ValueError):
                llm.CallService("hi")     # the trial, no verdict
        with patch("APIManager.AllAPICaller.requests.post", return_value=_resp()):
            assert llm.CallService("hi") == "ok"   # next trial allowed straight away
        assert cb.snapshot()["groq"]["state"] == "closed"

    def test_http_5xx_after_retries_counts_once(self, small_breaker):
        llm = _llm()
        with patch("APIManager.AllAPICaller.requests.post", return_value=_resp(503)), \
                patch("APIManager.AllAPICaller.time.sleep"):
            with pytest.raises(ValueError):
                llm.CallService("hi")
        assert cb.snapshot()["groq"]["failures"] == 1

    def test_success_resets_failures(self, small_breaker):
        llm = _llm()
        with patch("APIManager.AllAPICaller.requests.post",
                   side_effect=_requests.exceptions.Timeout("slow")):
            with pytest.raises(_requests.exceptions.Timeout):
                llm.CallService("hi")
        with patch("APIManager.AllAPICaller.requests.post", return_value=_resp()):
            llm.CallService("hi")
        assert cb.snapshot()["groq"]["failures"] == 0

    def test_empty_prompt_is_not_a_failure(self, small_breaker):
        llm = _llm()
        for _i in range(5):
            with pytest.raises(ValueError):
                llm.CallService("  ")
        assert cb.snapshot() == {}

    def test_providers_are_independent(self, small_breaker):
        with patch("APIManager.AllAPICaller.requests.post",
                   side_effect=_requests.exceptions.ConnectionError("down")):
            for _i in range(2):
                with pytest.raises(_requests.exceptions.ConnectionError):
                    _llm("groq").CallService("hi")
        with patch("APIManager.AllAPICaller.requests.post", return_value=_resp()):
            assert _llm("codex").CallService("hi") == "ok"

    def test_cli_failure_counts_and_open_skips_subprocess(self, small_breaker):
        llm = _llm("claude_code")
        llm.api_temp_dict = {"model": "m"}
        bad = MagicMock(returncode=1, stderr="boom", stdout="")
        with patch("APIManager.AllAPICaller.subprocess.run", return_value=bad) as run:
            for _i in range(2):
                with pytest.raises(ValueError):
                    llm.CallService("hi")
            with pytest.raises(CircuitOpenError):
                llm.CallService("hi")
            assert run.call_count == 2

    def test_balance_checks_do_not_use_breaker(self):
        import backend.balance as balance
        assert "circuit_breaker" not in open(balance.__file__).read()


def _stream_resp(lines, status=200):
    r = MagicMock()
    r.status_code = status
    r.text = "err"
    r.iter_lines.return_value = lines
    r.__enter__.return_value = r
    r.__exit__.return_value = False
    return r


_SSE_OK = [
    b'data: {"choices":[{"delta":{"content":"a"}}]}',
    b'data: {"choices":[{"delta":{"content":"b"}}]}',
    b"data: [DONE]",
]


class TestStream:
    def test_http_error_counts_and_opens(self, small_breaker):
        llm = _llm()
        with patch("APIManager.AllAPICaller.requests.post", return_value=_stream_resp([], 500)) as post:
            for _i in range(2):
                with pytest.raises(ValueError):
                    list(llm.CallServiceStream("hi"))
            with pytest.raises(CircuitOpenError):
                list(llm.CallServiceStream("hi"))
            assert post.call_count == 2

    def test_mid_stream_failure_counts(self, small_breaker):
        llm = _llm()
        resp = _stream_resp([])

        def lines():
            yield _SSE_OK[0]
            raise _requests.exceptions.ChunkedEncodingError("cut")

        resp.iter_lines.return_value = lines()
        with patch("APIManager.AllAPICaller.requests.post", return_value=resp):
            with pytest.raises(_requests.exceptions.ChunkedEncodingError):
                list(llm.CallServiceStream("hi"))
        assert cb.snapshot()["groq"]["failures"] == 1

    def test_completed_stream_resets(self, small_breaker):
        llm = _llm()
        cb.get_breaker("groq").record_failure()
        with patch("APIManager.AllAPICaller.requests.post", return_value=_stream_resp(_SSE_OK)):
            assert "".join(llm.CallServiceStream("hi")) == "ab"
        assert cb.snapshot()["groq"]["failures"] == 0

    def test_early_close_is_not_a_failure(self, small_breaker):
        llm = _llm()
        with patch("APIManager.AllAPICaller.requests.post", return_value=_stream_resp(_SSE_OK)):
            gen = llm.CallServiceStream("hi")
            next(gen)
            gen.close()
        assert cb.snapshot()["groq"]["failures"] == 0


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

_BODY = {
    "messages": [{"role": "user", "content": "show orders"}],
    "provider": "open_ai",
    "query_type": "sql",
}


class TestApi:
    def test_chat_returns_503_with_retry_after(self):
        with patch("backend.app.gatherRequirements",
                   side_effect=CircuitOpenError("open_ai", 42.2)):
            resp = client.post("/api/chat", json=_BODY)
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "43"
        assert resp.json()["detail"] == "open_ai is paused after repeated failures — retrying in 43 s"

    def test_generate_phase_503(self):
        with patch("backend.app.gatherRequirements",
                   return_value={"ready": True, "summary": "s"}), \
                patch("backend.app.generateQuery", side_effect=CircuitOpenError("open_ai", 5)):
            resp = client.post("/api/chat", json=_BODY)
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "5"

    def test_query_returns_503(self):
        with patch("backend.app.generateQuery", side_effect=CircuitOpenError("open_ai", 5)):
            resp = client.post("/api/query", json={"query": "x", "provider": "open_ai", "query_type": "sql"})
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "5"

    def test_stream_emits_error_event(self):
        def boom(*a, **k):
            raise CircuitOpenError("open_ai", 9)
            yield  # pragma: no cover

        with patch("backend.app.gatherRequirements",
                   return_value={"ready": True, "summary": "s"}), \
                patch("backend.app.generateQueryStream", side_effect=boom):
            resp = client.post("/api/chat/stream", json=_BODY)
        assert resp.status_code == 200
        import json
        evt = json.loads(resp.text.split("data: ", 1)[1])
        assert evt["event"] == "error"
        assert evt["detail"] == "open_ai is paused after repeated failures — retrying in 9 s"

    def test_stream_gather_phase_503(self):
        with patch("backend.app.gatherRequirements",
                   side_effect=CircuitOpenError("open_ai", 9)):
            resp = client.post("/api/chat/stream", json=_BODY)
        assert resp.status_code == 503

    def test_providers_shows_breaker_state(self):
        resp = client.get("/api/providers")
        data = resp.json()
        assert "open_ai" in data["providers"]
        assert data["breakers"]["open_ai"]["state"] == "closed"
        b = cb.get_breaker("open_ai")
        for _i in range(b.failure_threshold):
            b.record_failure()
        data = client.get("/api/providers").json()
        assert data["breakers"]["open_ai"]["state"] == "open"
        assert data["breakers"]["open_ai"]["retry_after"] > 0

    def test_metrics_counter(self):
        from backend.metrics import metrics
        metrics.reset()
        b = cb.get_breaker("groq")
        for _i in range(b.failure_threshold):
            b.record_failure()
        assert 'polyql_circuit_open_total{provider="groq"} 1' in metrics.render_prometheus()
