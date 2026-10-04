"""A clarify on a session nobody can ever answer returns at once instead of blocking for clarify_timeout.

Regression for the headless Group Chat member hosted by ``hermes gateway run``: its session bottoms out at
stdio in a process whose stdout is a log sink, so the question froze the agent for the full hour.
"""

import json
import time

import pytest

from tools.clarify_tool import NO_ANSWERER_NOTICE, clarify_tool
from tui_gateway import server, server_requests


class _Peer:
    """A live client transport (anything not stdio, not the detached sentinel, not closed)."""
    _closed = False

    def write(self, obj):
        return True


def _ask(monkeypatch, sid, transport):
    frames, hooks = [], []
    server_requests.reset_for_tests()
    monkeypatch.setattr(server, "_clarify_timeout_seconds", lambda: 0)
    monkeypatch.setattr(server_requests, "_write", frames.append)
    monkeypatch.setattr(server_requests, "_emit", lambda event, sid, payload: None)
    monkeypatch.setattr(server_requests, "_answerable", lambda sid: True)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda name, **kw: hooks.append((name, kw)))
    monkeypatch.setitem(server._sessions, sid, {"session_key": f"key-{sid}", "transport": transport})
    started = time.monotonic()
    result = json.loads(clarify_tool(
        [{"question": "Which env?", "choices": ["staging", "prod"]}],
        callback=lambda questions, request_id=None: server._clarify_block(sid, questions, request_id=request_id),
        session_id=f"key-{sid}", platform="serve"))
    return result, frames, [kw["outcome"] for name, kw in hooks if name == "post_clarify_response"], \
        time.monotonic() - started


def test_headless_session_returns_no_answerer_immediately(monkeypatch):
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", False)
    monkeypatch.setattr(server, "_clarify_timeout_seconds", lambda: 3600)
    result, frames, post, elapsed = _ask(monkeypatch, "headless", server._stdio_transport)

    assert elapsed < 1.0
    assert frames == []  # nothing was sent to a client that does not exist
    assert result["outcome"] == "no_answerer"
    assert result["notice"] == NO_ANSWERER_NOTICE
    assert [r["status"] for r in result["responses"]] == ["unanswered"]
    assert post == ["no_answerer"]  # observers clear the ask


@pytest.mark.parametrize("label, transport, stdio_rpc", [
    ("serve-bound", _Peer(), False),
    ("ws-detached-awaiting-reconnect", server._detached_ws_transport, False),
    ("stdio-tui", server._stdio_transport, True),
])
def test_session_with_an_answerer_still_sends_the_request(monkeypatch, label, transport, stdio_rpc):
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", stdio_rpc)
    result, frames, post, _ = _ask(monkeypatch, label, transport)

    assert [f["method"] for f in frames] == ["clarify"]
    assert result["outcome"] == "timed_out"
    assert post == ["timeout"]
