"""Whether a clarify can ever be answered is a property of the hosting process, not of the session's transports.

Regression for the headless Group Chat member hosted by ``hermes gateway run``: its session bottoms out at
stdio in a process whose stdout is a log sink, so the question froze the agent for the full hour. On the
serve (WS client host) the question must instead always wait in ``open_requests`` for the attach replay —
even mid-reconnect or when a wake resumed the session before any client attached.
"""

import json
import threading
import time

import pytest

from tools.clarify_tool import NO_ANSWERER_NOTICE, clarify_tool
from tui_gateway import server, server_requests
from tui_gateway.transport import FanoutTransport


class _ClosedPeer:
    """A WS peer whose socket just closed; disconnect teardown has not yet parked the session."""
    _closed = True

    def write(self, obj):
        return False


def _host(monkeypatch, *, ws_client_host, stdio_rpc=False):
    monkeypatch.setattr(server, "_ws_client_host", ws_client_host, raising=False)
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", stdio_rpc)


def _ask(monkeypatch, sid, transport, *, timeout=0.0):
    frames, hooks = [], []
    server_requests.reset_for_tests()
    monkeypatch.setattr(server, "_clarify_timeout_seconds", lambda: timeout)
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


def test_process_without_a_client_endpoint_returns_no_answerer_immediately(monkeypatch):
    """`hermes gateway run` hosting a Group Chat member: no WS endpoint, stdout is a log sink."""
    _host(monkeypatch, ws_client_host=False)
    result, frames, post, elapsed = _ask(monkeypatch, "gateway-room-member", server._stdio_transport, timeout=3600)

    assert elapsed < 1.0
    assert frames == []  # nothing was sent to a client that does not exist
    assert result["outcome"] == "no_answerer"
    assert result["notice"] == NO_ANSWERER_NOTICE
    assert [r["status"] for r in result["responses"]] == ["unanswered"]
    assert post == ["no_answerer"]  # observers clear the ask


@pytest.mark.parametrize("label, make_transport, ws_client_host, stdio_rpc", [
    ("serve-closed-peer-mid-teardown", _ClosedPeer, True, False),
    ("serve-empty-fanout", FanoutTransport, True, False),
    ("serve-wake-drop-transport-before-attach", server._DropTransport, True, False),
    ("serve-detached-awaiting-reconnect", lambda: server._detached_ws_transport, True, False),
    ("stdio-tui", lambda: server._stdio_transport, False, True),
])
def test_client_host_always_sends_the_request(monkeypatch, label, make_transport, ws_client_host, stdio_rpc):
    _host(monkeypatch, ws_client_host=ws_client_host, stdio_rpc=stdio_rpc)
    result, frames, post, _ = _ask(monkeypatch, label, make_transport())

    assert [f["method"] for f in frames] == ["clarify"]
    assert result["outcome"] == "timed_out"
    assert post == ["timeout"]


def test_serve_question_survives_reconnect_and_is_answered_from_the_replay(monkeypatch):
    """A clarify raised while the only peer is closing waits in open_requests; the reattached client answers it."""
    _host(monkeypatch, ws_client_host=True)
    outcome = {}
    asker = threading.Thread(target=lambda: outcome.update(
        zip(("result", "frames", "post", "elapsed"), _ask(monkeypatch, "reconnecting", _ClosedPeer(), timeout=10))))
    asker.start()
    deadline = time.monotonic() + 5
    while not server_requests.open_requests("reconnecting") and time.monotonic() < deadline:
        time.sleep(0.01)
    replayed = server_requests.open_requests("reconnecting")  # what session.resume hands the reattached client
    assert [r["method"] for r in replayed] == ["clarify"]

    qid = replayed[0]["params"]["questions"][0]["qid"]
    assert server_requests.lock_answer(replayed[0]["id"], qid, "prod") == []
    asker.join(5)

    assert outcome["result"]["outcome"] == "submitted"
    assert [r["user_response"] for r in outcome["result"]["responses"]] == ["prod"]
    assert outcome["post"] == ["answered"]
