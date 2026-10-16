"""Whether a clarify can ever be answered is a property of the session's kind, never of its transports now.

Regression for the headless Group Chat member: a hosted room session (``source=bot_room``) has no surface
anyone answers a question on (room controls expose approvals only), so the question froze the agent for the
full hour. Every other session must instead always wait in ``open_requests`` for the attach replay — even
mid-reconnect or when a wake resumed the session before any client attached.
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


def _ask(monkeypatch, sid, transport, *, timeout=0.0, source="desktop"):
    frames, hooks = [], []
    server_requests.reset_for_tests()
    monkeypatch.setattr(server, "_clarify_timeout_seconds", lambda: timeout)
    monkeypatch.setattr(server_requests, "_write", frames.append)
    monkeypatch.setattr(server_requests, "_emit", lambda event, sid, payload: None)
    monkeypatch.setattr(server_requests, "_answerable", lambda sid: True)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda name, **kw: hooks.append((name, kw)))
    monkeypatch.setitem(server._sessions, sid, {"session_key": f"key-{sid}", "transport": transport, "source": source})
    started = time.monotonic()
    result = json.loads(clarify_tool(
        [{"question": "Which env?", "choices": ["staging", "prod"]}],
        callback=lambda questions, request_id=None: server._clarify_block(sid, questions, request_id=request_id)))
    return result, frames, hooks, time.monotonic() - started


def _resolved(hooks):
    return [kw["outcome"] for name, kw in hooks if name == "on_human_input_resolved"]


def test_hosted_room_member_returns_undelivered_immediately(monkeypatch):
    """A Group Chat member's clarify: nobody can answer it, so the tool returns at once."""
    result, frames, hooks, elapsed = _ask(monkeypatch, "room-member", server._stdio_transport, timeout=3600,
                                          source="bot_room")

    assert elapsed < 1.0
    assert frames == []  # nothing was sent to a client that does not exist
    assert result["outcome"] == "undelivered"  # upstream's vocabulary for "nobody can answer" (D5)
    assert result["notice"] == NO_ANSWERER_NOTICE
    assert [r["status"] for r in result["responses"]] == ["unanswered"]
    assert [name for name, _ in hooks] == ["on_human_input_request", "on_human_input_resolved"]
    assert _resolved(hooks) == ["undelivered"]  # observers clear the ask


@pytest.mark.parametrize("label, make_transport", [
    ("closed-peer-mid-teardown", _ClosedPeer),
    ("empty-fanout", FanoutTransport),
    ("wake-drop-transport-before-attach", server._DropTransport),
    ("detached-awaiting-reconnect", lambda: server._detached_ws_transport),
    ("stdio", lambda: server._stdio_transport),
])
def test_a_non_room_session_always_sends_the_request(monkeypatch, label, make_transport):
    result, frames, hooks, _ = _ask(monkeypatch, label, make_transport())

    assert [f["method"] for f in frames] == ["clarify"]
    assert result["outcome"] == "timed_out"
    assert _resolved(hooks) == ["timed_out"]


def test_serve_question_survives_reconnect_and_is_answered_from_the_replay(monkeypatch):
    """A clarify raised while the only peer is closing waits in open_requests; the reattached client answers it."""
    outcome = {}
    asker = threading.Thread(target=lambda: outcome.update(
        zip(("result", "frames", "hooks", "elapsed"), _ask(monkeypatch, "reconnecting", _ClosedPeer(), timeout=10))))
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
    assert _resolved(outcome["hooks"]) == ["submitted"]
    # Contract C3: the id an observer (phone push) answers with is the replayed request's id.
    (request,) = [kw for name, kw in outcome["hooks"] if name == "on_human_input_request"]
    assert request["answer_request_id"] == replayed[0]["id"] != request["request_id"]
