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

import pytest

from tests.e2e.core.chaos import _gateway_fake_platform as fake_platform
from tests.e2e.core.chaos._gateway_harness import PLUGIN_MANIFEST
from tests.e2e.core.terminal._gateway_client import poll_until
from tests.hermes_cli.test_serve_beside_gateway_edits import _CONFIG, run_topology

pytestmark = pytest.mark.platforms('linux', 'macos')

_PROFILE = 'r7p'
# The chaos platform, plus one injected crash between the delete's phases: phase 1 has committed,
# the fenced commit never runs (as if the owner died there), once per marked request.
_PLUGIN = '''from tests.e2e.core.chaos._gateway_fake_platform import register as _register
import gateway.session_mutations as _mutations

_real, _crashed = _mutations.mutate_runtime_session, set()


def _crash_between_phases(db, **fields):
    if fields.get('operation') == 'delete' and fields['request_id'].startswith('_verify-crash') \\
            and fields['request_id'] not in _crashed:
        _crashed.add(fields['request_id'])
        raise RuntimeError('injected crash between the delete phases')
    return _real(db, **fields)


def register(ctx):
    _mutations.mutate_runtime_session = _crash_between_phases
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
