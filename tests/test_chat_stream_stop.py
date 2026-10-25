"""Client Stop: a disconnect stops pulling tokens and closes the upstream LLM stream."""
import asyncio
import json
from unittest.mock import MagicMock, patch

from backend import app as app_mod
from backend.app import ChatRequest, post_chat_stream
from backend.metrics import metrics


class _Req:
    """Fake Request: reports a disconnect after `after` is_disconnected() polls."""
    def __init__(self, after):
        self.after = after
        self.polls = 0

    async def is_disconnected(self):
        self.polls += 1
        return self.polls > self.after


def _run(after):
    state = {"yielded": 0, "closed": False}

    def fake_stream(*a, **k):
        try:
            for i in range(100):
                state["yielded"] += 1
                yield {"event": "token", "data": f"t{i}"}
            yield {"event": "done", "type": "sql", "content": "x"}
        finally:
            state["closed"] = True

    body = ChatRequest(messages=[{"role": "user", "content": "q"}], provider="open_ai", query_type="sql")

    async def go():
        with patch.object(app_mod, "gatherRequirements", return_value={"ready": True, "summary": "s"}), \
                patch.object(app_mod, "generateQueryStream", side_effect=fake_stream):
            resp = post_chat_stream(body, _Req(after), {"uid": "u"})
            return [c async for c in resp.body_iterator]

    with patch.object(metrics, "record_llm_call", MagicMock()) as rec:
        chunks = asyncio.run(go())
    return chunks, state, rec


def test_disconnect_stops_consuming_and_closes_upstream():
    chunks, state, rec = _run(after=3)
    assert len(chunks) == 3
    assert state["yielded"] == 3          # not 100
    assert state["closed"] is True
    # gather phase counted once; the stop is neither success nor error
    assert [c.kwargs.get("error", False) for c in rec.call_args_list] == [False]


def test_no_disconnect_runs_to_completion():
    chunks, state, rec = _run(after=10_000)
    assert json.loads(chunks[-1].split("data: ", 1)[1])["event"] == "done"
    assert state["closed"] is True
    assert rec.call_count == 2
