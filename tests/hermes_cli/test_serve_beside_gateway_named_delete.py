"""Fork (D27): delete a ROUTED named-profile chat beside a separate, multiplexed owner.

The owner keeps its one routing index in its launch home; a named profile's rows live in
``profiles/<name>/state.db``. Upstream retires a deleted chat's routes inside the delete's transaction,
which cannot span the two files, so its ``live_guard`` refused the delete (``runtime_coordination_required``):
Desktop/web delete of a named-profile chat answered 409 on a multiplex host (F1 Verify r4). The fork
retires the routes first, then commits the fenced delete (``gateway/session_routed_delete.py``).

The route is a real native one: a user-plugin platform (the chaos suite's) enabled only in the named
profile, so the gateway itself admits the message, routes it and owns the chat.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time

import pytest

from tests.e2e.core.chaos import _gateway_fake_platform as fake_platform
from tests.e2e.core.chaos._gateway_harness import PLUGIN_MANIFEST
from tests.e2e.core.terminal._gateway_client import poll_until
from tests.hermes_cli.test_serve_beside_gateway_edits import _CONFIG, _USER, _private, run_topology

pytestmark = pytest.mark.platforms('linux', 'macos')

_PROFILE = 'r7p'
# The chaos platform, plus one injected crash between the delete's phases: phase 1 has committed,
# the fenced commit never runs (as if the owner died there), once per marked request. While the
# plugin dir holds ``_SWEEP_OFF``, a committed delete leaves the owner's routes naming it, durable and in
# memory: what an owner restarted in the window between the commit and its route sweep holds.
_SWEEP_OFF = 'sweep-off'
_PLUGIN = '''from pathlib import Path

from tests.e2e.core.chaos._gateway_fake_platform import register as _register
import gateway.session_mutations as _mutations
import gateway.session_routed_delete as _routed
from gateway.session import SessionStore as _Store

_real, _crashed = _mutations.mutate_runtime_session, set()
_off = Path(__file__).with_name('sweep-off')
_sweep, _forget = _routed.retire_routes_naming_deleted, _Store.retire_runtime_sessions


def _sweep_unless_off(db, store):
    if not _off.exists():
        _sweep(db, store)


def _forget_unless_off(store, session_ids):
    if not _off.exists():
        _forget(store, session_ids)


def _crash_between_phases(db, **fields):
    if fields.get('operation') == 'delete' and fields['request_id'].startswith('_verify-crash') \\
            and fields['request_id'] not in _crashed:
        _crashed.add(fields['request_id'])
        raise RuntimeError('injected crash between the delete phases')
    return _real(db, **fields)


def register(ctx):
    _mutations.mutate_runtime_session = _crash_between_phases
    _routed.retire_routes_naming_deleted = _sweep_unless_off
    _Store.retire_runtime_sessions = _forget_unless_off
    _register(ctx)
'''


class _Wire:
    """The test side of the fake platform's loopback connection."""

    def __init__(self):
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.events, self._cond, self._conn = [], threading.Condition(), None
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        conn, _ = self.listener.accept()
        self._conn = conn
        with conn.makefile('rb') as stream:
            for line in stream:
                with self._cond:
                    self.events.append(json.loads(line))
                    self._cond.notify_all()

    def wait(self, pred, what, timeout=90):
        def found():
            with self._cond:
                return next((e for e in self.events if pred(e)), None)
        return poll_until(found, timeout=timeout, interval=0.1, what=what)

    def send(self, chat, text, message_id):
        self.wait(lambda e: e['ev'] == 'ready', 'fake platform ready')
        self._conn.sendall(json.dumps({'op': 'msg', 'chat': chat, 'text': text, 'id': message_id}).encode() + b'\n')
        self.wait(lambda e: e['ev'] == 'complete' and e['id'] == message_id, f'turn {message_id} completes')


@pytest.fixture(scope='module')
def named(tmp_path_factory):
    wire = _Wire()

    def prepare(be):
        home = be.hermes_home / 'profiles' / _PROFILE
        plugin = home / 'plugins' / fake_platform.PLUGIN_DIR_NAME
        plugin.mkdir(parents=True)
        (plugin / 'plugin.yaml').write_text(PLUGIN_MANIFEST, encoding='utf-8')
        (plugin / '__init__.py').write_text(_PLUGIN, encoding='utf-8')
        from tests.fakes.fake_llm_provider import write_hermes_home
        write_hermes_home(home, be.llm.base_url, extra_config=_CONFIG + (
            f'plugins:\n  enabled: [{fake_platform.PLUGIN_DIR_NAME}]\n'
            f'platforms:\n  {fake_platform.PLATFORM_NAME}:\n    enabled: true\n'))
        # A served profile reads its own .env, never the launch process's environment.
        with open(home / '.env', 'a', encoding='utf-8') as env_file:
            env_file.write(f'\n{fake_platform.ALLOW_ALL_ENV}=true\n')

    env = {fake_platform.PORT_ENV: str(wire.listener.getsockname()[1]), fake_platform.ALLOW_ALL_ENV: 'true'}
    for be, http, descriptor in run_topology(tmp_path_factory, prepare, env):
        be.wire = wire
        yield be, http
    wire.listener.close()


def _read(path, sql, args=()):
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _routes_naming(be, sid):
    """Routing rows (any scope) in the owner's launch-home index that name *sid*."""
    return [key for key, raw in _read(be.hermes_home / 'state.db', 'SELECT session_key, entry_json FROM gateway_routing')
            if json.loads(raw).get('session_id') == sid]


def _row(be, sid):
    return _read(be.hermes_home / 'profiles' / _PROFILE / 'state.db', 'SELECT id FROM sessions WHERE id=?', (sid,))


def _routed_chat(be, chat):
    """A native named-profile chat: one admitted turn, then its route as the owner persisted it."""
    be.wire.send(chat, 'hello', f'{chat}-1')

    def route():
        for key, raw in _read(be.hermes_home / 'state.db', 'SELECT session_key, entry_json FROM gateway_routing'):
            sid = json.loads(raw).get('session_id')
            if key.endswith(f':{chat}') and sid and _row(be, sid):
                return sid
        return None
    sid = poll_until(route, timeout=30, interval=0.1, what=f'route for {chat}')
    assert f':{_PROFILE}:' in _routes_naming(be, sid)[0], 'a named-profile route'
    return sid


def _delete(http, sid, request_id, snapshot=None):
    snapshot = snapshot or http.get(f'/api/sessions/{sid}/mutation-snapshot', params={'profile': _PROFILE}).json()
    response = http.delete(f'/api/sessions/{sid}', params={
        'profile': _PROFILE, 'request_id': request_id, 'expected_revision': snapshot['runtime_revision'],
        'expected_generation': snapshot['runtime_generation']})
    return response, snapshot


def test_rest_delete_retires_the_route_and_an_exact_retry_replays(named):
    be, http = named
    sid = _routed_chat(be, 'rest')
    response, snapshot = _delete(http, sid, '_verify-del-1')
    assert response.status_code == 200, (response.text, be.logs(20))
    assert not _row(be, sid) and _routes_naming(be, sid) == []
    again, _ = _delete(http, sid, '_verify-del-1', snapshot)
    assert (again.status_code, again.json()) == (200, response.json()), again.text


def test_a_crash_between_the_phases_leaves_the_row_without_a_route(named):
    be, http = named
    sid = _routed_chat(be, 'crash')
    response, snapshot = _delete(http, sid, '_verify-crash-1')
    assert response.status_code != 200, response.text
    assert _row(be, sid) and _routes_naming(be, sid) == [], 'phase 1 committed, phase 2 did not'
    retried, _ = _delete(http, sid, '_verify-crash-1', snapshot)
    assert retried.status_code == 200, (retried.text, be.logs(20))
    assert not _row(be, sid) and _routes_naming(be, sid) == []
    # The chat itself goes on: its next message starts a fresh session, never the deleted one.
    assert _routed_chat(be, 'crash') != sid


def test_ws_session_delete_leaves_no_stale_route(named):
    """The Deus app's path: the serve's own ``delete_session`` on the named profile's store."""
    be, _ = named
    sid = _routed_chat(be, 'ws')
    assert be.connect('ws-delete').call('session.delete', session_id=sid, profile=_PROFILE) == {'deleted': sid}
    assert not _row(be, sid) and _routes_naming(be, sid) == []


def test_a_delete_phase_two_refuses_on_its_parameters_keeps_the_route(named):
    """Security r7 L1, over the owner's ``delegated-edit`` (the WS ``session.mutate`` path): a delete
    phase 2 refuses on a non-empty payload or a bool revision never strips the chat's route first."""
    be, http = named
    home = (be.hermes_home / 'profiles' / _PROFILE).resolve()
    for name, bad in (('payload', {'payload': {'x': 1}}), ('boolrev', {'expected_revision': True})):
        sid = _routed_chat(be, f'bad-{name}')
        # Revision 1, so the bool equals it: only phase 2's ``type(...) is int`` refuses it.
        _write(be, 'UPDATE sessions SET runtime_revision = 1 WHERE id = ?', (sid,))
        snapshot = http.get(f'/api/sessions/{sid}/mutation-snapshot', params={'profile': _PROFILE}).json()
        binding = {'profile_id': str(home), 'subject': f'basic:{_USER}', 'session_id': sid, 'operation': 'delete'}
        minted = _private(be.hermes_home, 'delegated-edit-ticket', binding)
        assert minted['ok'], minted
        mutation = {'session_id': sid, 'request_id': f'_verify-bad-{name}', 'operation': 'delete', 'payload': {},
                    'expected_revision': 1, 'expected_generation': snapshot['runtime_generation'], **bad}
        reply = _private(be.hermes_home, 'delegated-edit',
                         {**binding, 'ticket': minted['result']['ticket'], 'mutation': mutation})
        assert reply['result'] == {'refused': 'invalid_params'}, reply
        assert _row(be, sid) and _routes_naming(be, sid), f'{name}: the refused delete kept the chat and its route'


def _write(be, sql, args):
    """Shape a fixture row in the named profile's store (as the bulk topology tests do)."""
    con = sqlite3.connect(str(be.hermes_home / 'profiles' / _PROFILE / 'state.db'), timeout=30)
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def _aged(be, chat, start):
    """A routed chat, ended with a start time nothing else has (a prune window selects exactly it)."""
    sid = _routed_chat(be, chat)
    _write(be, "UPDATE sessions SET started_at = ?, ended_at = ?, archived = 0, pinned = 0, "
               "end_reason = '_verify-aged' WHERE id = ?", (start, start + 1, sid))
    return sid


def _gone(be, sid):
    assert not _row(be, sid), 'the row is deleted'
    assert _routes_naming(be, sid) == [], 'no owner route names the deleted id'


def test_bulk_delete_leaves_no_route_naming_the_deleted_chat(named):
    be, http = named
    sid = _routed_chat(be, 'bulk')
    response = http.post('/api/sessions/bulk-delete', json={'ids': [sid], 'profile': _PROFILE})
    assert (response.status_code, response.json().get('deleted')) == (200, 1), (response.text, be.logs(20))
    _gone(be, sid)
    assert _routed_chat(be, 'bulk') != sid, 'the chat goes on in a fresh session'


def _log_ends(be):
    return {path: path.stat().st_size for path in be.root.rglob('*.log')}


def _logged_since(ends):
    text = ''
    for path in ends:
        with open(path, encoding='utf-8', errors='replace') as log:
            log.seek(ends[path])
            text += log.read()
    return text


def test_a_stale_route_never_resurrects_the_deleted_chat(named):
    """D29(b): with the sweep held off, a bulk delete leaves the chat's route naming the deleted id (durable
    and live in the owner's memory). The chat's next native message still starts a fresh session; the
    deleted row is never recreated."""
    be, http = named
    sid = _routed_chat(be, 'stale')
    off = be.hermes_home / 'profiles' / _PROFILE / 'plugins' / fake_platform.PLUGIN_DIR_NAME / _SWEEP_OFF
    ends = _log_ends(be)
    off.touch()
    try:
        response = http.post('/api/sessions/bulk-delete', json={'ids': [sid], 'profile': _PROFILE})
        assert (response.status_code, response.json().get('deleted')) == (200, 1), (response.text, be.logs(20))
        assert not _row(be, sid) and _routes_naming(be, sid), 'the deleted chat keeps a stale route'
        fresh = _routed_chat(be, 'stale')
    finally:
        off.unlink()
    assert fresh != sid and not _row(be, sid), 'a fresh session; the deleted row stays absent'
    assert _routes_naming(be, sid) == [], 'the fresh session replaced the stale route'
    logged = _logged_since(ends)
    assert 'IntegrityError' not in logged and 'FOREIGN KEY' not in logged, logged[-4000:]


def test_delete_empty_leaves_no_route_naming_the_deleted_chat(named):
    be, http = named
    sid = _routed_chat(be, 'empty')
    _write(be, 'UPDATE sessions SET message_count = 0, ended_at = ?, archived = 0, title = NULL WHERE id = ?',
           (time.time() - 60, sid))
    _write(be, 'DELETE FROM messages WHERE session_id = ?', (sid,))
    response = http.delete('/api/sessions/empty', params={'profile': _PROFILE})
    assert response.status_code == 200 and response.json()['deleted'] >= 1, (response.text, be.logs(20))
    _gone(be, sid)


def test_rest_prune_leaves_no_route_naming_the_pruned_chat(named):
    be, http = named
    start = 978307200  # 2001: only this chat starts there
    sid = _aged(be, 'prune', start)
    window = {'started_after': start - 0.5, 'started_before': start + 0.5, 'include_archived': True,
              'profile': _PROFILE}
    response = http.post('/api/sessions/prune', json=window)
    assert (response.status_code, response.json().get('removed')) == (200, 1), (response.text, be.logs(20))
    _gone(be, sid)


def test_owner_session_prune_leaves_no_route_naming_the_pruned_chat(named):
    """``hermes sessions prune -p <profile>`` beside a live owner: the owner's ``session.prune``."""
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    from hermes_cli.sessions_owner import owner_prune
    be, _ = named
    start = 978307300
    sid = _aged(be, 'cliprune', start)
    found = discover_gateway_endpoint((be.hermes_home / 'profiles' / _PROFILE).resolve())
    assert found.state == 'ready', found
    result = owner_prune(found.endpoint, filters={'older_than_days': None, 'started_after': start - 0.5,
                                                  'started_before': start + 0.5, 'archived': None})
    assert result['deleted'] == 1, result
    _gone(be, sid)
