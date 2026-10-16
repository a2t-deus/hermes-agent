"""Mounting the serve/dashboard app (its lifespan) makes every in-process session answerable: a clarify raised
by a managed wake during ``session.resume`` (``_DropTransport``, no client attached yet) is sent and kept for
the attach replay, never short-circuited as unanswerable."""

from __future__ import annotations

import pytest

from tests.tui_gateway.test_clarify_no_answerer import _ask, _resolved
from tui_gateway import server


def test_serve_app_lifespan_keeps_a_wake_clarify_for_the_attaching_client(monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app
    monkeypatch.setattr(server, "_ws_client_host", False, raising=False)
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", False)

    with TestClient(app):
        pass
    result, frames, hooks, _ = _ask(monkeypatch, "wake-before-attach", server._DropTransport())

    assert [f["method"] for f in frames] == ["clarify"]
    assert result["outcome"] == "timed_out"
    assert _resolved(hooks) == ["timed_out"]
