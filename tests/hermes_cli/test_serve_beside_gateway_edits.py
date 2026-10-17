"""Fork (D19): a real ``hermes serve`` beside a real, separate ``gateway.run`` owning the profile.

The serve stamps its authenticated subject on the chats it creates, and the owner lets only that principal
edit them, so the serve's REST edits reach the owner as that subject: a single-use ticket bound to
(profile, subject, session, operation), minted and spent only on the owner-only control socket
(``gateway/session_delegated_edit.py``). Before it, every edit of a serve chat answered 409 "Exclusive
maintenance refused" (MacBook rollout, 2026-10-17), then 403 once routed as the socket's uid.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

import tests.e2e.core.terminal._gateway_client as gateway_client
from tests.e2e.core.terminal._gateway_client import REPO_ROOT, Backend, WSClient, _minimal_env, etype, poll_until
from gateway.session_delegated_edit import import_ids_digest
from tests.fakes.fake_llm_provider import Hang, Text, ToolCall, write_hermes_home

pytestmark = pytest.mark.platforms('linux', 'macos')

# As on every Deus host: the serve's REST and WS sit behind basic auth (a public_url engages the gate
# on this loopback bind), so the chats it creates carry ``basic:<user>``.
_CONFIG = ('gateway:\n  multiplex_profiles: true\nclarify:\n  timeout: 120\n'
           'approvals:\n  mode: manual\n  timeout: 120\nterminal:\n  backend: local\n'
           'dashboard:\n  public_url: http://probe.invalid\n')
_USER, _PASSWORD = '_verify-user', '_verify-fake-password-1'
_BASIC = {'HERMES_DASHBOARD_BASIC_AUTH_USERNAME': _USER, 'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD': _PASSWORD,
          'HERMES_DASHBOARD_BASIC_AUTH_SECRET': '_verify-fake-secret-' + '0' * 40}


def _responder(record):
    messages = record['body'].get('messages') or []
    users = [m for m in messages if m.get('role') == 'user']
    text = str((users[-1] if users else {}).get('content') or '')
    if messages and messages[-1].get('role') == 'tool':
        return Text('DONE ' + str(messages[-1].get('content') or '')[:200])
    if 'CLARIFY' in text:
        return ToolCall('clarify', {'questions': [{'question': 'Which colour?', 'choices': ['red', 'blue']}]})
    if text.startswith('RMRF '):
        return ToolCall('terminal', {'command': 'rm -rf ' + text.split(' ', 1)[1]})
    if 'HANG' in text:
        return Hang(120)
    return Text('PONG')


def _private(home, verb, params):
    """One raw control-socket exchange as this (same-uid) peer; the reply, refusals included."""
    pointer = home / 'gateway.sock.path'
    path = pointer.read_text(encoding='utf-8').strip() if pointer.exists() else str(home / 'gateway.sock')
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(10)
        peer.connect(path)
        peer.sendall(json.dumps({'protocol': 1, 'id': 1, 'verb': verb, 'params': params}).encode() + b'\n')
        with peer.makefile('rb') as stream:
            return json.loads(stream.readline())


def run_topology(tmp_path_factory, prepare=None, gateway_env=None):
    """Gateway first, then the serve, as launchd starts them on a host; yields (backend, http, identify).
    *prepare(be)* runs before the gateway starts (profile homes, plugins); *gateway_env* joins its env."""
    be = Backend(tmp_path_factory.mktemp('beside'), _responder, extra_config=_CONFIG, env=_BASIC)
    be.llm.start()
    write_hermes_home(be.hermes_home, be.llm.base_url, extra_config=_CONFIG)
    be.hermes_home.chmod(0o700)
    if prepare is not None:
        prepare(be)
    log = open(be.root / 'gateway.log', 'w+')
    gateway = subprocess.Popen([sys.executable, '-m', 'gateway.run'], cwd=REPO_ROOT,
                               env=_minimal_env(be.home, be.hermes_home, be.token, {**_BASIC, **(gateway_env or {})}),
                               stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def ready():
        if gateway.poll() is not None:
            log.seek(0)
            raise AssertionError(f'gateway exited {gateway.returncode}:\n{log.read()[-4000:]}')
        try:
            reply = _private(be.hermes_home, 'identify', {})
        except (OSError, ValueError):
            return None
        return reply['result'] if reply.get('ok') and reply['result'].get('state') == 'ready' else None

    try:
        descriptor = poll_until(ready, timeout=90, interval=0.1, what='gateway ready')
        be.llm.start = lambda: None  # already serving; the serve reuses it
        be.start()
        http = httpx.Client(base_url=f'http://127.0.0.1:{be.port}', trust_env=False, timeout=60)
        provider = next(p['name'] for p in http.get('/api/auth/providers').json()['providers']
                        if p.get('supports_password'))
        login = http.post('/auth/password-login', json={'provider': provider, 'username': _USER,
                                                        'password': _PASSWORD})
        assert login.status_code < 300 and http.cookies, login.text
        be.connect = lambda name: _connect(be, http, name)
        yield be, http, descriptor
        http.close()
    finally:
        be.stop()
        if gateway.poll() is None:
            os.killpg(gateway.pid, signal.SIGINT)
            try:
                gateway.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(gateway.pid, signal.SIGKILL)
                gateway.wait(timeout=5)
        log.close()


@pytest.fixture(scope='module')
def topology(tmp_path_factory):
    yield from run_topology(tmp_path_factory)


def _connect(be, http, name):
    """The Deus app's WS: a ws-ticket minted over the logged-in cookie session."""
    ticket = http.post('/api/auth/ws-ticket').json()['ticket']
    cookie = '; '.join(f'{k}={v}' for k, v in http.cookies.items())
    plain = gateway_client.ws_connect
    gateway_client.ws_connect = lambda url, **kw: plain(
        url, subprotocols=['hermes-gateway-v1', f'hermes-gateway-ticket.{ticket}'],
        additional_headers={'Cookie': cookie}, **kw)
    try:
        client = WSClient(f'ws://127.0.0.1:{be.port}/api/ws', name)
    finally:
        gateway_client.ws_connect = plain
    be.clients.append(client)
    return client


def _serve_chat(be, name, text='hello'):
    """A chat the serve created (stamped with its principal), made durable by one turn."""
    ws = be.connect(name)
    created = ws.call('session.create', cols=100, source='desktop')
    start = len(ws.snapshot())
    ws.call('prompt.submit', session_id=created['session_id'], text=text)
    if not any(word in text for word in ('CLARIFY', 'RMRF', 'HANG')):
        ws.wait_for(lambda f: etype(f) == 'message.complete', timeout=90, start=start, what='message.complete')
    return ws, created['session_id'], created['stored_session_id'], start


def _row(be, key):
    cols = ('pinned', 'last_read_at', 'title', 'archived', 'settled_at', 'user_id')
    rows = be.db_rows(f'SELECT {",".join(cols)} FROM sessions WHERE id=?', (key,))
    return dict(zip(cols, rows[0])) if rows else None


def _revision(http, key):
    return http.get(f'/api/sessions/{key}/mutation-snapshot').json()['runtime_revision']


def _generation(http, key):
    return http.get(f'/api/sessions/{key}/mutation-snapshot').json()['runtime_generation']


def test_legacy_app_body_edits_a_serve_chat_through_the_owner(topology):
    be, http, _ = topology
    _, _, key, _ = _serve_chat(be, 'legacy')
    assert _row(be, key)['user_id'] == f'basic:{_USER}', 'the serve stamps its principal on its chats'
    for body, check in (({'pinned': True, 'profile': None}, lambda r: r['pinned']),
                        ({'pinned': False}, lambda r: not r['pinned']),
                        ({'unread': False}, lambda r: r['last_read_at'])):
        response = http.patch(f'/api/sessions/{key}', json=body)
        assert response.status_code == 200, (body, response.text, be.logs(20))
        assert check(_row(be, key)), body


def test_identity_bearing_body_keeps_the_owners_revision_fence(topology):
    be, http, _ = topology
    _, _, key, _ = _serve_chat(be, 'fenced')
    revision = _revision(http, key)
    response = http.patch(f'/api/sessions/{key}', json={
        'title': 'renamed beside the owner', 'request_id': 'r-title', 'expected_revision': revision})
    assert response.status_code == 200, response.text
    assert _row(be, key)['title'] == 'renamed beside the owner'
    stale = http.patch(f'/api/sessions/{key}', json={
        'archived': True, 'request_id': 'r-stale', 'expected_revision': revision})
    assert (stale.status_code, stale.json()['detail']) == (409, 'revision_conflict'), stale.text
    assert not _row(be, key)['archived']


def test_settle_patch_settles_and_cancels_the_open_clarify(topology):
    be, http, _ = topology
    ws, sid, key, start = _serve_chat(be, 'settle', 'CLARIFY please')
    ws.wait_for(lambda f: f.get('method') == 'clarify', timeout=90, start=start, what='clarify request')
    response = http.patch(f'/api/sessions/{key}', json={
        'settled': True, 'request_id': 'r-settle', 'expected_revision': _revision(http, key)})
    assert response.status_code == 200, (response.text, be.logs(20))
    assert _row(be, key)['settled_at']
    # The withdrawn clarify lets the agent continue to its reply.
    ws.wait_for(lambda f: etype(f) == 'message.complete' and f['params']['session_id'] == sid,
                timeout=90, start=start, what='turn completes after the settle cancelled its clarify')


def test_delete_runs_on_the_owner(topology):
    be, http, _ = topology
    _, _, key, _ = _serve_chat(be, 'delete')
    response = http.delete(f'/api/sessions/{key}', params={
        'request_id': 'r-delete', 'expected_revision': _revision(http, key),
        'expected_generation': _generation(http, key)})
    assert response.status_code == 200, response.text
    assert _row(be, key) is None


def test_tickets_are_single_use_and_bound_to_their_exact_edit(topology):
    be, http, _ = topology
    _, _, key, _ = _serve_chat(be, 'tickets')
    home, subject = be.hermes_home.resolve(), _row(be, key)['user_id']
    binding = {'profile_id': str(home), 'subject': subject, 'session_id': key, 'operation': 'sidebar'}

    def mutation(request_id, session_id=key, operation='sidebar'):
        return {'session_id': session_id, 'request_id': request_id, 'expected_revision': _revision(http, key),
                'operation': operation, 'payload': {'pinned': True}}

    def edit(ticket, *, request_id, **overrides):
        reply = _private(home, 'delegated-edit', {**binding, **overrides, 'ticket': ticket,
                                                  'mutation': mutation(request_id)})
        assert reply['ok'], reply
        return reply['result']

    def mint(**overrides):
        reply = _private(home, 'delegated-edit-ticket', {**binding, **overrides})
        assert reply['ok'], reply
        assert reply['result']['expires_in_seconds'] <= 30
        return reply['result']['ticket']

    ticket = mint()
    assert 'result' in edit(ticket, request_id='t-1')
    assert edit(ticket, request_id='t-2') == {'refused': 'permission_denied'}, 'replayed ticket'
    for wrong in ({'subject': 'basic:someone-else'}, {'session_id': key + '-other'}, {'operation': 'delete'}):
        assert edit(mint(), request_id='t-3', **wrong) == {'refused': 'permission_denied'}, wrong
    # A ticket for one edit never authorizes a different session or operation in its mutation.
    assert edit(mint(), request_id='t-4') != {'refused': 'permission_denied'}
    reply = _private(home, 'delegated-edit', {**binding, 'ticket': mint(),
                                              'mutation': mutation('t-5', operation='delete')})
    assert reply['result'] == {'refused': 'invalid_params'}, reply
    _, _, other, _ = _serve_chat(be, 'tickets-other')
    audit = f'operation=sidebar session={key} '
    log = be.hermes_home / 'logs' / 'agent.log'
    before = log.read_text(errors='replace').count(audit)
    reply = _private(home, 'delegated-edit', {**binding, 'ticket': mint(),
                                              'mutation': mutation('t-6', session_id=other)})
    assert reply['result'] == {'refused': 'invalid_params'}, reply
    assert not _row(be, other)['pinned']
    assert log.read_text(errors='replace').count(audit) == before, 'the owner acted on a mutation outside its ticket'


def test_ticket_is_never_redeemed_on_a_network_listener(topology):
    be, http, descriptor = topology
    _, _, key, _ = _serve_chat(be, 'listener')
    home = be.hermes_home.resolve()
    ticket = _private(home, 'delegated-edit-ticket', {'profile_id': str(home), 'subject': _row(be, key)['user_id'],
                                                      'session_id': key, 'operation': 'sidebar'})['result']['ticket']
    body = {'pinned': True, 'request_id': 'n-1', 'expected_revision': _revision(http, key)}
    with httpx.Client(base_url=descriptor['api_origin'], trust_env=False, timeout=30) as owner:
        for headers in ({'X-Hermes-Gateway-Ticket': ticket}, {'Authorization': f'Bearer {ticket}'},
                        {'X-Hermes-Session-Token': ticket}):
            response = owner.patch(f'/api/sessions/{key}', json=body, headers=headers)
            assert response.status_code in (401, 403), (headers.keys(), response.status_code, response.text)
    with httpx.Client(base_url=f'http://127.0.0.1:{be.port}', trust_env=False, timeout=30) as stranger:
        for headers in ({'X-Hermes-Gateway-Ticket': ticket}, {'Authorization': f'Bearer {ticket}'},
                        {'X-Hermes-Session-Token': ticket}):
            assert stranger.patch(f'/api/sessions/{key}', json=body, headers=headers).status_code in (401, 403)
    assert not _row(be, key)['pinned']


def test_expired_ticket_is_refused(topology):
    be, http, _ = topology
    _, _, key, _ = _serve_chat(be, 'expired')
    home = be.hermes_home.resolve()
    binding = {'profile_id': str(home), 'subject': _row(be, key)['user_id'], 'session_id': key, 'operation': 'sidebar'}
    ticket = _private(home, 'delegated-edit-ticket', binding)['result']['ticket']
    time.sleep(31)  # health: allow HX013 -- the 30 s ticket TTL running out is the behaviour under test
    reply = _private(home, 'delegated-edit', {**binding, 'ticket': ticket, 'mutation': {
        'session_id': key, 'request_id': 'e-1', 'expected_revision': _revision(http, key),
        'operation': 'sidebar', 'payload': {'pinned': True}}})
    assert reply['result'] == {'refused': 'permission_denied'}, reply
    assert not _row(be, key)['pinned']


def test_import_over_a_megabyte_runs_on_the_owner_from_a_bound_staged_file(topology):
    """A real export outgrows one 64 KiB control-socket line (F5 rest_import: 400 invalid_params)."""
    be, http, _ = topology
    messages = [{'role': role, 'content': f'{role} {n} ' + 'x' * 400_000}
                for n, role in enumerate(('user', 'assistant', 'user'))]
    body = {'request_id': 'i-1', 'expected_revision': 0,
            'sessions': [{'id': '_verify-imported', 'source': 'cli', 'messages': messages}]}
    assert len(json.dumps(body)) > 1_000_000
    response = http.post('/api/sessions/import', json=body)
    assert response.status_code == 200, response.text
    rows = be.db_rows('SELECT COUNT(*), SUM(LENGTH(content)) FROM messages WHERE session_id=?', ('_verify-imported',))
    assert rows[0][0] == 3 and rows[0][1] > 1_200_000, rows
    log = (be.hermes_home / 'logs' / 'agent.log').read_text(errors='replace')
    assert 'operation=import session=_verify-imported' in log
    assert not list((be.hermes_home / 'runtime' / 'delegated-edit').iterdir()), 'staged payload left behind'


def test_a_staged_payload_changed_after_its_ticket_is_refused_and_removed(topology):
    be, _, _ = topology
    home = be.hermes_home.resolve()
    staging = home / 'runtime' / 'delegated-edit'
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {'sessions': [{'id': '_verify-tampered', 'source': 'cli', 'messages': [{'role': 'user', 'content': 'a'}]}]}
    raw = json.dumps(payload).encode()
    binding = {'profile_id': str(home), 'subject': 'basic:' + _USER, 'session_id': '_verify-tampered',
               'operation': 'import', 'payload_sha256': hashlib.sha256(raw).hexdigest(),
               'session_ids_sha256': import_ids_digest(payload['sessions'])}
    ticket = _private(home, 'delegated-edit-ticket', binding)['result']['ticket']
    staged = staging / ('t' * 43 + '.json')
    staged.write_bytes(raw.replace(b'"a"', b'"b"'))
    staged.chmod(0o600)
    reply = _private(home, 'delegated-edit', {**binding, 'ticket': ticket, 'staged': staged.name, 'mutation': {
        'session_id': '_verify-tampered', 'request_id': 'tam-1', 'expected_revision': 0, 'operation': 'import'}})
    assert reply['result'] == {'refused': 'permission_denied'}, reply
    assert not staged.exists()
    assert _row(be, '_verify-tampered') is None


def test_an_owner_refusal_keeps_its_status(topology):
    """The owner refusing an edit is not a drain: an empty import is 400 here as on the serve's own path,
    never 503 runtime_draining (F1 Verify r4: the owner's binding refusal surfaced as a drain)."""
    _, http, _ = topology
    response = http.post('/api/sessions/import', json={'request_id': 'i-empty', 'expected_revision': 0,
                                                       'sessions': []})
    assert (response.status_code, response.json()['detail']) == (400, 'invalid_params'), response.text


def test_an_import_ticket_covers_exactly_the_sessions_imported(topology):
    be, http, _ = topology
    sessions = [{'id': f'_verify-many-{n}', 'source': 'cli', 'messages': [{'role': 'user', 'content': str(n)}]}
                for n in (2, 1)]
    response = http.post('/api/sessions/import', json={'request_id': 'i-many', 'expected_revision': 0,
                                                       'sessions': sessions})
    assert response.status_code == 200 and response.json()['imported'] == 2, response.text
    log = (be.hermes_home / 'logs' / 'agent.log').read_text(errors='replace')
    assert 'session=_verify-many-2 ' in log and f'ids_sha256={import_ids_digest(sessions)[:16]}' in log

    # A ticket naming only the anchor never carries a second session in its (digest-bound) payload.
    home = be.hermes_home.resolve()
    staging = home / 'runtime' / 'delegated-edit'
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    smuggled = [{'id': '_verify-anchor', 'source': 'cli', 'messages': [{'role': 'user', 'content': 'a'}]},
                {'id': '_verify-smuggled', 'source': 'cli', 'messages': [{'role': 'user', 'content': 'b'}]}]
    raw = json.dumps({'sessions': smuggled}).encode()
    binding = {'profile_id': str(home), 'subject': 'basic:' + _USER, 'session_id': '_verify-anchor',
               'operation': 'import', 'payload_sha256': hashlib.sha256(raw).hexdigest(),
               'session_ids_sha256': import_ids_digest(smuggled[:1])}
    ticket = _private(home, 'delegated-edit-ticket', binding)['result']['ticket']
    staged = staging / ('s' * 43 + '.json')
    staged.write_bytes(raw)
    staged.chmod(0o600)
    reply = _private(home, 'delegated-edit', {**binding, 'ticket': ticket, 'staged': staged.name, 'mutation': {
        'session_id': '_verify-anchor', 'request_id': 'sm-1', 'expected_revision': 0, 'operation': 'import'}})
    assert reply['result'] == {'refused': 'permission_denied'}, reply
    assert _row(be, '_verify-anchor') is None and _row(be, '_verify-smuggled') is None
    # An import ticket that binds no id set is refused when minted, as the client's error it is.
    reply = _private(home, 'delegated-edit-ticket', {k: v for k, v in binding.items() if k != 'session_ids_sha256'})
    assert reply['result'] == {'refused': 'invalid_params'}, reply
