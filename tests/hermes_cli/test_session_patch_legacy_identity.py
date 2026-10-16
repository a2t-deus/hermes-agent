"""Identity-less pin/read PATCHes from installed Deus apps keep working (contract C6, D15).

Upstream's fenced sidebar PATCH requires ``request_id`` + ``expected_revision``. Only a body whose keys are
within {pinned, unread, profile} is admitted without them; anything else stays a 409.
"""

import logging

import pytest

from hermes_state import SessionDB


@pytest.fixture
def client(monkeypatch, _isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    import hermes_state
    import hermes_cli.web_routers.sessions as routes
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
    monkeypatch.setattr(routes, "_legacy_sidebar_logged", False)
    with SessionDB() as db:
        db.create_session(session_id="s1", source="cli")
        db.append_message(session_id="s1", role="user", content="hi")
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _row():
    with SessionDB() as db:
        return db.get_session("s1")


def test_legacy_pin_unpin_and_read_are_admitted(client, caplog):
    caplog.set_level(logging.INFO, logger="hermes_cli.web_server")
    for body, check in (({"pinned": True}, lambda r: r["pinned"]),
                        ({"pinned": False, "profile": None}, lambda r: not r["pinned"]),
                        ({"unread": False}, lambda r: r["last_read_at"])):
        resp = client.patch("/api/sessions/s1", json=body)
        assert resp.status_code == 200, (body, resp.text)
        assert check(_row()), body
    assert _row()["runtime_revision"] == 3
    assert sum("D15 shim" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("body", [{"settled": True}, {"title": "t"}, {"archived": True},
                                  {"pinned": True, "title": "t"}])
def test_other_identity_less_bodies_stay_409(client, body):
    resp = client.patch("/api/sessions/s1", json=body)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "mutation_identity_required"
    assert _row()["runtime_revision"] == 0


def test_identity_bearing_requests_are_unchanged(client):
    stale = client.patch("/api/sessions/s1", json={"pinned": True, "request_id": "r1", "expected_revision": 5})
    assert stale.status_code == 409 and stale.json()["detail"] == "revision_conflict"
    half = client.patch("/api/sessions/s1", json={"pinned": True, "request_id": "r2"})
    assert half.status_code == 409 and half.json()["detail"] == "mutation_identity_required"
    ok = client.patch("/api/sessions/s1", json={"pinned": True, "request_id": "r3", "expected_revision": 0})
    assert ok.status_code == 200 and _row()["pinned"]
