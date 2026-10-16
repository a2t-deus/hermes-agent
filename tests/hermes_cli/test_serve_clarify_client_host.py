"""On the serve, a hosted Group Chat member's clarify still returns at once: being a client host does not make a
room question answerable (room controls expose approvals only), while the serve's own chats keep waiting."""

from __future__ import annotations

from tests.tui_gateway.test_clarify_no_answerer import _ask, _resolved
from tui_gateway import server


def test_serve_room_member_clarify_is_undelivered_while_its_chats_wait(monkeypatch):
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", False)

    room, room_frames, room_hooks, _ = _ask(monkeypatch, "serve-room", server._DropTransport(), source="bot_room")
    chat, chat_frames, chat_hooks, _ = _ask(monkeypatch, "serve-chat", server._DropTransport(), source="desktop")

    assert (room_frames, room["outcome"], _resolved(room_hooks)) == ([], "undelivered", ["undelivered"])
    assert [f["method"] for f in chat_frames] == ["clarify"]
    assert (chat["outcome"], _resolved(chat_hooks)) == ("timed_out", ["timed_out"])
