"""Settled chat state (fleet-notifications issue 006, D28/D29).

``settled_at`` is a manual, lineage-wide chat state next to archived/pinned: NULL = active.
Settling clears a pin and withdraws the live runtime's open clarify/approval requests as
*cancelled* (the agent continues); any new user/assistant message un-settles the chat.
"""

import json
import sqlite3
import threading
import time

import pytest

import tui_gateway.server as srv
import tui_gateway.methods_session  # noqa: F401  (registers the RPC methods)
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    database = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(srv, "_get_db", lambda: database)
    try:
        yield database
    finally:
        database.close()


def _call(method: str, params: dict) -> dict:
    return srv._methods[method](1, params)


def _seed(db, sid: str, **kwargs) -> None:
    db.create_session(sid, source="desktop", **kwargs)
    db.append_message(sid, "user", "hello")


def _settled_at(db, sid):
    return db.get_session(sid)["settled_at"]


# ── schema + SessionDB ──────────────────────────────────────────────────────────────────────


def test_old_db_without_settled_at_gains_the_column(tmp_path):
    """A pre-006 state.db (no ``settled_at``) opens, gains the column, and old rows read active."""
    path = tmp_path / "state.db"
    database = SessionDB(path)
    database.create_session("old-chat", source="desktop")
    database.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE sessions DROP COLUMN settled_at")
    conn.commit()
    assert "settled_at" not in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()

    reopened = SessionDB(path)
    try:
        assert _settled_at(reopened, "old-chat") is None
        assert reopened.set_session_settled("old-chat", True)
        assert _settled_at(reopened, "old-chat") is not None
    finally:
        reopened.close()


def test_set_session_settled_sets_clears_and_drops_the_pin(db):
    _seed(db, "chat")
    db.set_session_pinned("chat", True)
    before = time.time()
    assert db.set_session_settled("chat", True)
    row = db.get_session("chat")
    assert row["settled_at"] >= before
    assert row["pinned"] == 0  # settling removes a pin (T3)
    assert row["archived"] == 0  # independent of archived/hidden

    assert db.set_session_settled("chat", False)
    assert _settled_at(db, "chat") is None


def test_settle_covers_the_compression_lineage(db):
    _seed(db, "root")
    db.end_session("root", "compression")
    db.create_session("tip", source="desktop", parent_session_id="root")
    db.set_session_settled("tip", True)
    assert _settled_at(db, "root") is not None
    assert _settled_at(db, "tip") is not None


# ── un-settle on new activity ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_new_message_unsettles(db, role):
    _seed(db, "chat")
    db.set_session_settled("chat", True)
    db.append_message("chat", role, "more")
    assert _settled_at(db, "chat") is None


def test_batch_append_unsettles(db):
    _seed(db, "chat")
    db.set_session_settled("chat", True)
    db.append_messages_batch("chat", [{"role": "assistant", "content": "done"}])
    assert _settled_at(db, "chat") is None


def test_new_message_on_compression_tip_unsettles_the_lineage(db):
    _seed(db, "root")
    db.end_session("root", "compression")
    db.create_session("tip", source="desktop", parent_session_id="root")
    db.set_session_settled("tip", True)
    db.append_message("tip", "user", "back again")
    assert _settled_at(db, "root") is None
    assert _settled_at(db, "tip") is None


def test_assistant_on_compression_tip_unsettles_a_root_settled_before_compression(db):
    """Settle the root, compress, then the tip speaks: the whole lineage (and session.list) is active."""
    _seed(db, "root")
    db.set_session_settled("root", True)
    db.publish_compression_child(
        parent_session_id="root", child_session_id="tip", source="desktop",
        messages=[{"role": "user", "content": "[CONTEXT COMPACTION] summary"}], require_compression_lease=False)
    assert _settled_at(db, "tip") == _settled_at(db, "root")  # the child inherits the lineage state

    db.append_message("tip", "assistant", "still going")
    assert _settled_at(db, "root") is None and _settled_at(db, "tip") is None
    rows = _call("session.list", {})["result"]["sessions"]
    assert rows and not any(r["settled"] for r in rows)


def test_unsettle_reads_the_whole_lineage(db):
    """A lineage member minted without the state (pre-copy compression child) still un-settles the lineage."""
    _seed(db, "root")
    db.end_session("root", "compression")
    db.create_session("tip", source="desktop", parent_session_id="root")
    db.set_session_settled("root", True)
    with db._lock:
        db._conn.execute("UPDATE sessions SET settled_at = NULL WHERE id = 'tip'")
        db._conn.commit()
    db.append_message("tip", "assistant", "back")
    assert _settled_at(db, "root") is None


@pytest.mark.parametrize("suppressed, settled_after", [(False, False), (True, True)])
def test_delegation_delivery_unsettles_unless_hidden(db, suppressed, settled_after):
    _seed(db, "chat")
    db.set_session_settled("chat", True)
    db.append_delegation_delivery("chat", "result", {"delegation_id": "d1", "presentation_suppressed": suppressed})
    assert (_settled_at(db, "chat") is not None) is settled_after


def _settle_during_turn(db, holder: str) -> None:
    _seed(db, "chat")
    assert db.try_acquire_session_turn_lease("chat", holder)
    time.sleep(0.01)  # the settle strictly postdates the lease's acquired_at
    db.set_session_settled("chat", True)


def test_settle_sticks_against_the_turn_running_at_settle_time(db):
    _settle_during_turn(db, "turn-a")
    db.append_message("chat", "assistant", "late reply", turn_lease_holder="turn-a")
    db.append_messages_batch("chat", [{"role": "assistant", "content": "more"}], turn_lease_holder="turn-a")
    assert _settled_at(db, "chat") is not None

    db.release_session_turn_lease("chat", "turn-a")
    assert db.try_acquire_session_turn_lease("chat", "turn-b")  # a turn started after the settle
    db.append_message("chat", "assistant", "new turn", turn_lease_holder="turn-b")
    assert _settled_at(db, "chat") is None


@pytest.mark.parametrize("batch", [False, True])
def test_user_row_inside_the_running_turn_unsettles(db, batch):
    """The stick rule spares only the running turn's own output: a ``/steer`` (user row saved under that
    turn's lease) is new user activity."""
    _settle_during_turn(db, "turn-a")
    if batch:
        db.append_messages_batch("chat", [{"role": "assistant", "content": "partial"},
                                          {"role": "user", "content": "steer"}], turn_lease_holder="turn-a")
    else:
        db.append_message("chat", "user", "steer", turn_lease_holder="turn-a")
    assert _settled_at(db, "chat") is None


def test_running_turn_unsettles_when_the_stick_rule_is_off(db, monkeypatch):
    import hermes_state_messages

    monkeypatch.setattr(hermes_state_messages, "SETTLE_STICKS_ACROSS_RUNNING_TURN", False)
    _settle_during_turn(db, "turn-a")
    db.append_message("chat", "assistant", "late reply", turn_lease_holder="turn-a")
    assert _settled_at(db, "chat") is None


def test_tool_row_alone_does_not_unsettle(db):
    _seed(db, "chat")
    db.set_session_settled("chat", True)
    db.append_message("chat", "tool", "output", tool_call_id="call-1")
    assert _settled_at(db, "chat") is not None


# ── session.list ────────────────────────────────────────────────────────────────────────────


def test_session_list_rows_carry_settled(db):
    _seed(db, "active-chat")
    _seed(db, "settled-chat")
    db.set_session_settled("settled-chat", True)

    rows = {r["id"]: r for r in _call("session.list", {})["result"]["sessions"]}
    assert rows["active-chat"]["settled"] is False
    assert rows["settled-chat"]["settled"] is True

    rows = {r["id"] for r in _call("session.list", {"include_settled": False})["result"]["sessions"]}
    assert rows == {"active-chat"}


def test_session_list_contract_accepts_the_settled_field_and_filter(db):
    _seed(db, "settled-chat")
    db.set_session_settled("settled-chat", True)
    resp = srv.handle_request({"id": "1", "method": "session.list", "params": {"include_settled": True}})
    assert "error" not in resp, resp
    assert resp["result"]["sessions"][0]["settled"] is True


def test_session_list_include_settled_false_through_handle_request(db):
    _seed(db, "active-chat")
    _seed(db, "settled-chat")
    db.set_session_settled("settled-chat", True)
    resp = srv.handle_request({"id": "1", "method": "session.list", "params": {"include_settled": False}})
    assert "error" not in resp, resp
    assert {r["id"] for r in resp["result"]["sessions"]} == {"active-chat"}


# ── session.set_settled RPC ─────────────────────────────────────────────────────────────────


def test_set_settled_rpc_stored_session(db):
    _seed(db, "stored-chat")
    db.set_session_pinned("stored-chat", True)
    resp = srv.handle_request(
        {"id": "1", "method": "session.set_settled", "params": {"session_id": "stored-chat", "settled": True}})
    assert "error" not in resp, resp
    assert resp["result"] == {"settled": True, "session_key": "stored-chat", "cancelled_requests": 0}
    row = db.get_session("stored-chat")
    assert row["settled_at"] is not None and row["pinned"] == 0

    resp = srv.handle_request(
        {"id": "2", "method": "session.set_settled", "params": {"session_id": "stored-chat", "settled": False}})
    assert resp["result"]["settled"] is False
    assert _settled_at(db, "stored-chat") is None


def test_set_settled_rpc_requires_flag_and_known_id(db):
    resp = srv.handle_request({"id": "1", "method": "session.set_settled", "params": {"session_id": "x"}})
    assert resp["error"]["code"] == 4021  # settled is required (a default would settle on a dropped flag)
    resp = srv.handle_request(
        {"id": "2", "method": "session.set_settled", "params": {"session_id": "nope", "settled": True}})
    assert resp["error"]["code"] == 4001, resp


# ── settle cancels open requests ────────────────────────────────────────────────────────────


class _HookRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, hook_name, **kwargs):
        self.calls.append((hook_name, kwargs))
        return []


def _wait_open(server_requests, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with server_requests._lock:
            req = next(iter(server_requests._open.values()), None)
        if req is not None:
            return req
        time.sleep(0.01)
    raise AssertionError("server request never registered")


def test_settle_cancels_open_clarify_with_cancelled_outcome(db, monkeypatch):
    from tools.clarify_tool import clarify_tool
    from tui_gateway import server_requests

    recorder = _HookRecorder()
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", recorder)
    cancels = []
    monkeypatch.setattr(server_requests, "_emit", lambda event, sid, payload: cancels.append((event, payload)))
    monkeypatch.setattr(srv, "_clarify_timeout_seconds", lambda: 30)

    _seed(db, "stored-key")
    sid = "live-1"
    srv._sessions[sid] = {"session_key": "stored-key", "history": [], "running": True}
    box = {}

    def ask():
        cb = lambda q, c, multi_select=False, request_id=None: srv._clarify_block(  # noqa: E731
            sid, q, c, multi_select=multi_select, request_id=request_id)
        box["result"] = clarify_tool("Ship it?", choices=["yes", "no"], callback=cb,
                                     session_id="stored-key", platform="serve")

    thread = threading.Thread(target=ask, daemon=True)
    try:
        thread.start()
        req = _wait_open(server_requests)
        assert req.method == "clarify"

        envelope = _call("session.set_settled", {"session_id": sid, "settled": True})
        assert "error" not in envelope, envelope
        assert envelope["result"]["cancelled_requests"] == 1
        thread.join(5)
        assert not thread.is_alive()
    finally:
        srv._sessions.pop(sid, None)
        with server_requests._lock:
            server_requests._open.pop(req.id, None)  # only ours: the map is process-global

    assert json.loads(box["result"])["user_response"] == ""  # dismissed, the agent continues
    post = [kw for name, kw in recorder.calls if name == "post_clarify_response"]
    assert [p["outcome"] for p in post] == ["cancelled"]
    assert ("request.cancel", {"id": req.id, "method": "clarify", "reason": "settled"}) in cancels
    assert _settled_at(db, "stored-key") is not None


def test_settle_withdraws_pending_gateway_approval(db, monkeypatch):
    """An open approval is withdrawn (cancelled, never a user deny) so the agent moves on."""
    from tools import approval as _approval

    _seed(db, "appr-key")
    sid = "live-appr"
    srv._sessions[sid] = {"session_key": "appr-key", "history": [], "running": True}
    decision = {}

    def wait():
        decision.update(_approval._await_gateway_decision(
            "appr-key", lambda data: None, {"command": "rm -rf /tmp/x", "description": "d"}, surface="gateway"))

    monkeypatch.setattr(_approval, "_get_approval_config", lambda: {"gateway_timeout": 30}, raising=False)
    thread = threading.Thread(target=wait, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _approval.list_gateway_approvals("appr-key"):
            time.sleep(0.01)
        assert _approval.list_gateway_approvals("appr-key")
        envelope = _call("session.set_settled", {"session_id": sid, "settled": True})
        assert "error" not in envelope, envelope
        thread.join(5)
        assert not thread.is_alive()
    finally:
        srv._sessions.pop(sid, None)
        _approval.clear_session("appr-key")

    assert decision.get("cancelled")
    assert "settled" in decision["cancelled"]
    assert not _approval.list_gateway_approvals("appr-key")


def test_settle_counts_an_approval_with_an_open_card_once(db, monkeypatch):
    """The approval's card (an open ``approval`` server request) is withdrawn too, but counted once."""
    from tools import approval as _approval
    from tui_gateway import server_requests

    _seed(db, "appr-key")
    sid = "live-appr-card"
    srv._sessions[sid] = {"session_key": "appr-key", "history": [], "running": True}
    monkeypatch.setattr(server_requests, "_emit", lambda event, sid, payload: None)
    monkeypatch.setattr(_approval, "_get_approval_config", lambda: {"gateway_timeout": 30}, raising=False)
    thread = threading.Thread(target=lambda: _approval._await_gateway_decision(
        "appr-key", lambda data: None, {"command": "rm -rf /tmp/x", "description": "d"}, surface="gateway"),
        daemon=True)
    card = None
    try:
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _approval.list_gateway_approvals("appr-key"):
            time.sleep(0.01)
        [pending] = _approval.list_gateway_approvals("appr-key")
        card = server_requests.ServerRequest(sid, "approval", {"request_id": pending["request_id"]})
        other = server_requests.ServerRequest(sid, "clarify", {"question": "q", "choices": None})
        with server_requests._lock:
            server_requests._open[card.id] = card
            server_requests._open[other.id] = other
        envelope = _call("session.set_settled", {"session_id": sid, "settled": True})
        assert "error" not in envelope, envelope
        assert envelope["result"]["cancelled_requests"] == 2  # the approval + the clarify
        thread.join(5)
    finally:
        srv._sessions.pop(sid, None)
        _approval.clear_session("appr-key")
        with server_requests._lock:
            for req in (card, locals().get("other")):
                if req is not None:
                    server_requests._open.pop(req.id, None)
    assert card.settle_reason == "settled"


def test_set_settled_live_id_respects_the_requested_profile(db, monkeypatch, tmp_path):
    """An explicit ``profile`` never reaches another profile's live runtime (no settle, no cancel)."""
    from tui_gateway import server_requests

    other_home = tmp_path / "profiles" / "other"
    other_home.mkdir(parents=True)
    monkeypatch.setattr(server_requests, "_emit", lambda event, sid, payload: None)
    monkeypatch.setattr(srv, "_profile_home", lambda profile: other_home if profile == "other" else None)
    _seed(db, "launch-key")
    sid = "live-launch"  # a launch-profile runtime (no profile_home)
    srv._sessions[sid] = {"session_key": "launch-key", "history": [], "running": True}
    req = server_requests.ServerRequest(sid, "clarify", {"question": "q", "choices": None})
    with server_requests._lock:
        server_requests._open[req.id] = req
    try:
        resp = _call("session.set_settled", {"session_id": sid, "settled": True, "profile": "other"})
        assert resp["error"]["code"] == 4001, resp  # the runtime id is no stored id in profile "other"
        assert not req.event.is_set()
        assert _settled_at(db, "launch-key") is None

        resp = _call("session.set_settled", {"session_id": sid, "settled": True})  # its own profile
        assert "error" not in resp, resp
        assert resp["result"]["cancelled_requests"] == 1
    finally:
        srv._sessions.pop(sid, None)
        with server_requests._lock:
            server_requests._open.pop(req.id, None)


# ── PATCH /api/sessions/{id} ────────────────────────────────────────────────────────────────


class TestSettledRestFlag:
    @pytest.fixture(autouse=True)
    def _client(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")
        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        database = SessionDB()
        try:
            database.create_session(session_id="s1", source="cli")
            database.append_message(session_id="s1", role="user", content="hi")
            database.set_session_pinned("s1", True)
        finally:
            database.close()

    def _row(self):
        database = SessionDB()
        try:
            return database.get_session("s1")
        finally:
            database.close()

    def test_patch_settled_true_settles_and_unpins(self):
        resp = self.client.patch("/api/sessions/s1", json={"settled": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["settled"] is True
        row = self._row()
        assert row["settled_at"] is not None and row["pinned"] == 0
        rows = self.client.get("/api/sessions?limit=100").json()["sessions"]
        assert next(r for r in rows if r["id"] == "s1")["settled"] is True

    def test_patch_settled_cancels_the_live_runtime_requests(self, monkeypatch):
        from tui_gateway import server_requests

        cancels = []
        monkeypatch.setattr(server_requests, "_emit", lambda event, sid, payload: cancels.append(payload))
        srv._sessions["live-s1"] = {"session_key": "s1", "history": [], "running": True}
        req = server_requests.ServerRequest("live-s1", "clarify", {"question": "q", "choices": None})
        with server_requests._lock:
            server_requests._open[req.id] = req
        try:
            resp = self.client.patch("/api/sessions/s1", json={"settled": True})
            assert resp.status_code == 200, resp.text
        finally:
            srv._sessions.pop("live-s1", None)
            with server_requests._lock:
                server_requests._open.pop(req.id, None)
        assert req.event.is_set() and req.settle_reason == "settled"
        assert {"id": req.id, "method": "clarify", "reason": "settled"} in cancels

    def test_patch_settled_default_profile_on_custom_home_cancels_launch_requests(self, monkeypatch):
        """Custom launch HERMES_HOME (the hermetic home is outside the platform default): an explicit
        ``profile=default`` names this process's own store, so its live runtime is still cancelled."""
        from hermes_constants import get_hermes_home
        from tui_gateway import server_requests

        monkeypatch.setattr(srv, "_hermes_home", get_hermes_home())
        monkeypatch.setattr(server_requests, "_emit", lambda event, sid, payload: None)
        srv._sessions["live-s1"] = {"session_key": "s1", "history": [], "running": True}
        req = server_requests.ServerRequest("live-s1", "clarify", {"question": "q", "choices": None})
        with server_requests._lock:
            server_requests._open[req.id] = req
        try:
            resp = self.client.patch("/api/sessions/s1", json={"settled": True, "profile": "default"})
            assert resp.status_code == 200, resp.text
        finally:
            srv._sessions.pop("live-s1", None)
            with server_requests._lock:
                server_requests._open.pop(req.id, None)
        assert self._row()["settled_at"] is not None
        assert req.event.is_set() and req.settle_reason == "settled"

    def test_patch_settled_does_not_start_multi_profile_hosting(self, monkeypatch):
        monkeypatch.setattr(srv, "_profile_home", lambda profile: pytest.fail("mutating profile resolver"))
        resp = self.client.patch("/api/sessions/s1", json={"settled": True})
        assert resp.status_code == 200, resp.text

    def test_patch_settled_false_unsettles(self):
        self.client.patch("/api/sessions/s1", json={"settled": True})
        resp = self.client.patch("/api/sessions/s1", json={"settled": False})
        assert resp.status_code == 200, resp.text
        assert self._row()["settled_at"] is None
        rows = self.client.get("/api/sessions?limit=100").json()["sessions"]
        assert next(r for r in rows if r["id"] == "s1")["settled"] is False
