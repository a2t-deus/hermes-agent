"""Fork (D18/D19): the serve's bulk session endpoints beside a real, separate ``gateway.run`` owning the profile.

Bulk-delete, delete-empty, prune and owner-backfill took the offline exclusive-maintenance lock, which the
owner holds, so each answered 409 "Exclusive maintenance refused" where the old fork answered 200 (F5 r2).
Each now runs on the owner through a single-use ticket bound to its exact id set or filter hash; the owner
re-evaluates the selection itself. ``[serve]`` rows are chats the serve created; ``[local]`` rows are
cold CLI history the store already held.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid

import pytest

from tests.hermes_cli.test_serve_beside_gateway_edits import (
    _USER, _generation, _private, _revision, _row, _serve_chat, pytestmark, run_topology)


@pytest.fixture(scope='module')
def topology(tmp_path_factory):
    yield from run_topology(tmp_path_factory)


def _target(be, which, label):
    if which == 'serve':
        return _serve_chat(be, label)[2]
    key = f'_verify-local-{label}-{uuid.uuid4().hex[:8]}'
    _write(be, 'INSERT INTO sessions (id, source, started_at, ended_at, message_count, title) '
               "VALUES (?, 'cli', ?, ?, 1, ?)", (key, time.time() - 120, time.time() - 60, key))
    _write(be, "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'hi', ?)",
           (key, time.time() - 120))
    return key


def _write(be, sql, args):
    """Shape a fixture row the way F5 does on its shadow store (the endpoint under test never writes here)."""
    con = sqlite3.connect(str(be.hermes_home / 'state.db'), timeout=30)
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def _owner_logged(be, operation, count):
    log = (be.hermes_home / 'logs' / 'agent.log').read_text(errors='replace')
    return f'operation={operation} ' in log and f'count={count}' in log


@pytest.mark.parametrize('which', ['serve', 'local'])
def test_bulk_delete_runs_on_the_owner(topology, which):
    be, http, _ = topology
    key = _target(be, which, 'bulk')
    response = http.post('/api/sessions/bulk-delete', json={'ids': [key]})
    assert response.status_code == 200, (response.text, be.logs(20))
    assert response.json()['deleted'] == 1, response.json()
    assert _row(be, key) is None
    assert _owner_logged(be, 'bulk_delete', 1)


@pytest.mark.parametrize('which', ['serve', 'local'])
def test_delete_empty_runs_on_the_owner(topology, which):
    be, http, _ = topology
    key = _target(be, which, 'empty')
    _write(be, 'UPDATE sessions SET message_count = 0, ended_at = ?, archived = 0 WHERE id = ?', (time.time() - 60, key))
    _write(be, 'DELETE FROM messages WHERE session_id = ?', (key,))
    assert http.get('/api/sessions/empty/count').json()['count'] >= 1
    response = http.delete('/api/sessions/empty')
    assert response.status_code == 200, (response.text, be.logs(20))
    assert response.json()['deleted'] >= 1, response.json()
    assert _row(be, key) is None


@pytest.mark.parametrize('which', ['serve', 'local'])
def test_prune_runs_on_the_owner_with_its_own_selection(topology, which):
    be, http, _ = topology
    key = _target(be, which, 'prune')
    start = 978307200 + (1 if which == 'local' else 0)  # a 2001 start nothing else has
    _write(be, "UPDATE sessions SET started_at = ?, ended_at = ?, archived = 0, pinned = 0, "
               "end_reason = '_verify-parity' WHERE id = ?", (start, start + 1, key))
    window = {'started_after': start - 0.5, 'started_before': start + 0.5, 'include_archived': True}
    dry = http.post('/api/sessions/prune', json={**window, 'dry_run': True}).json()
    assert dry['matched'] == 1, dry
    response = http.post('/api/sessions/prune', json=window)
    assert response.status_code == 200, (response.text, be.logs(20))
    assert response.json()['removed'] == 1, response.json()
    assert _row(be, key) is None
    assert _owner_logged(be, 'prune', 1)


@pytest.mark.parametrize('which', ['serve', 'local'])
def test_owner_backfill_runs_on_the_owner(topology, which):
    be, http, _ = topology
    key = _target(be, which, 'backfill')
    _write(be, 'UPDATE sessions SET profile_name = NULL WHERE id = ?', (key,))
    response = http.post('/api/sessions/owner-backfill', json={})
    assert response.status_code == 200, (response.text, be.logs(20))
    assert response.json()['stamped'] >= 1, response.json()
    stamped = be.db_rows('SELECT profile_name FROM sessions WHERE id=?', (key,))[0][0]
    assert stamped == response.json()['profile'], stamped


def test_reimport_after_delete_is_refused_explicitly_then_runs_once_the_tombstone_expires(topology):
    """F5 rest_import: export, DELETE, re-import the same id ~30 ms later answered 503 runtime_draining on
    every retry: the delete left its cold-history claim behind and the import's own claim insert failed.
    Inside the retired id's horizon upstream refuses the reuse (``admission_conflict``, 0a55e37320)."""
    be, http, _ = topology
    _, _, key, _ = _serve_chat(be, 'reimport')
    exported = http.get(f'/api/sessions/{key}/export')
    assert exported.status_code == 200, exported.text
    deleted = http.delete(f'/api/sessions/{key}', params={
        'request_id': 'ri-delete', 'expected_revision': _revision(http, key),
        'expected_generation': _generation(http, key)})
    assert deleted.status_code == 200, deleted.text
    body = {'request_id': 'ri-import', 'expected_revision': 0, 'sessions': [exported.json()]}
    early = http.post('/api/sessions/import', json=body)
    assert (early.status_code, early.json()['detail']) == (409, 'admission_conflict'), early.text
    # The 7-day horizon ran out (``prune_retired_tombstones``).
    _write(be, 'DELETE FROM state_meta WHERE key=?', ('gateway.retired_session.v1.' + key,))
    response = http.post('/api/sessions/import', json={**body, 'request_id': 'ri-import-late'})
    assert response.status_code == 200, (response.text, be.logs(30))
    assert _row(be, key) is not None


def _bulk_binding(home, operation, params, subject='auth:v1:probe'):
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'profile_id': str(home), 'subject': subject, 'operation': operation, 'params_sha256': digest}


def test_bulk_tickets_are_single_use_and_bound_to_their_exact_selection(topology):
    be, _, _ = topology
    home = be.hermes_home.resolve()
    keep, other = (_target(be, 'local', label) for label in ('keep', 'other'))

    def mint(binding):
        reply = _private(home, 'delegated-edit-ticket', binding)
        assert reply['ok'], reply
        return reply['result']['ticket']

    def edit(binding, ticket, params):
        reply = _private(home, 'delegated-edit', {**binding, 'ticket': ticket, 'params': params})
        assert reply['ok'], reply
        return reply['result']

    bound = _bulk_binding(home, 'bulk_delete', {'ids': [keep]})
    assert edit(bound, mint(bound), {'ids': [other]}) == {'refused': 'permission_denied'}, 'wrong id set'
    assert edit(bound, mint(bound), {'ids': [keep, other]}) == {'refused': 'permission_denied'}, 'superset'
    filters = {'started_after': 1.0, 'started_before': 2.0, 'archived': None}
    pruned = _bulk_binding(home, 'prune', filters)
    assert edit(pruned, mint(pruned), {**filters, 'started_before': 9e9}) == {'refused': 'permission_denied'}
    assert _row(be, keep) is not None and _row(be, other) is not None
    ticket = mint(bound)
    assert edit(bound, ticket, {'ids': [keep]})['result']['deleted'] == 1
    assert edit(bound, ticket, {'ids': [keep]}) == {'refused': 'permission_denied'}, 'replayed ticket'
    # A bulk ticket never authorizes a per-session edit, nor the reverse.
    sidebar = {'profile_id': str(home), 'subject': 'basic:' + _USER, 'session_id': other, 'operation': 'sidebar'}
    assert edit(_bulk_binding(home, 'bulk_delete', {'ids': [other]}), mint(sidebar), {'ids': [other]}) == {
        'refused': 'permission_denied'}
    assert _row(be, other) is not None
