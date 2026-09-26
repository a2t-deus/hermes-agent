"""A prompt submitted from one client reaches every other viewer of that chat before the reply.

Real ``hermes serve`` (generated token, isolated HOME/HERMES_HOME, recording fake LLM) and real
``/api/ws`` clients: A sends, B watches the same live runtime, C holds a different chat.
"""

from __future__ import annotations

import sys

import pytest

from tests.e2e.core.terminal._gateway_client import Backend, etype
from tests.fakes.fake_llm_provider import Text

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group lifecycle for the spawned backend")


@pytest.fixture
def backend(tmp_path):
    be = Backend(tmp_path, lambda _record: Text("peer reply streaming now DONE", chunk_chars=4))
    be.start()
    try:
        yield be
    finally:
        be.stop()


def _turn(client, sid, text):
    start = len(client.snapshot())
    client.call("prompt.submit", session_id=sid, text=text)
    client.wait_for(lambda f: etype(f) == "message.complete" and f["params"]["session_id"] == sid,
                    timeout=90, start=start, what="message.complete")
    return start


def test_peer_receives_user_text_before_assistant_output_and_other_chats_do_not(backend):
    a, b, c = backend.connect("A"), backend.connect("B"), backend.connect("C")
    created = a.call("session.create", cols=100, source="desktop")
    sid, key = created["session_id"], created["stored_session_id"]
    _turn(a, sid, "first message makes the chat durable")
    assert b.call("session.resume", session_id=key, cols=100)["session_id"] == sid
    other = c.call("session.create", cols=100, source="desktop")["session_id"]
    _turn(c, other, "unrelated chat")

    b_start = len(b.snapshot())
    c_start = len(c.snapshot())
    a_start = _turn(a, sid, "sent from the iPad")

    b_turn = [f for f in b.snapshot()[b_start:] if f.get("method") == "event" and f["params"]["session_id"] == sid]
    kinds = [etype(f) for f in b_turn]
    assert "user.prompt" in kinds, kinds
    echo = b_turn[kinds.index("user.prompt")]["params"]["payload"]
    assert echo["text"] == "sent from the iPad"
    assert isinstance(echo["row_id"], int)
    assert kinds.count("user.prompt") == 1
    assert kinds.index("user.prompt") < kinds.index("message.delta")
    assert kinds.index("user.prompt") < kinds.index("message.start")
    # The sender gets the same frame (one shape for every viewer); it dedupes against its optimistic row.
    assert [etype(f) for f in a.snapshot()[a_start:]].count("user.prompt") == 1
    assert not [f for f in c.snapshot()[c_start:] if etype(f) == "user.prompt"], "echo leaked to another chat"
