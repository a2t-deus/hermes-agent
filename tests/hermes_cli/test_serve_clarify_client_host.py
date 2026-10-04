"""Binding the serve/dashboard WS endpoint makes every in-process session answerable: a clarify raised by a
managed wake during ``session.resume`` (``_DropTransport``, no client attached yet) is sent and kept for the
attach replay, never short-circuited as ``no_answerer``."""

from __future__ import annotations

import hermes_cli.web_server as web_server
from tests.hermes_cli.test_dashboard_auth_gate import _stub_uvicorn_run
from tests.tui_gateway.test_clarify_no_answerer import _ask
from tui_gateway import server


def test_serve_start_keeps_a_wake_clarify_for_the_attaching_client(monkeypatch):
    monkeypatch.setattr(server, "_ws_client_host", False, raising=False)
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", False)
    monkeypatch.setattr(web_server, "_write_machine_sentinel_line", lambda line: None)
    _stub_uvicorn_run(monkeypatch)

    web_server.start_server(host="127.0.0.1", port=0, open_browser=False, headless=True)
    result, frames, post, _ = _ask(monkeypatch, "wake-before-attach", server._DropTransport())

    assert [f["method"] for f in frames] == ["clarify"]
    assert result["outcome"] == "timed_out"
    assert post == ["timeout"]
