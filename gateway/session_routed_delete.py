"""Fork (D27): delete a session whose routes live in another store, as two committed phases.

The gateway keeps ONE routing index, in its launch home (``SessionStore._routing_db``); a multiplexed
named profile's rows live in that profile's own store. Upstream retires a deleted session's routes inside
the delete's own transaction, which cannot span two SQLite files, so its ``live_guard`` refuses that
delete (10b155bfc0). Here phase 1 commits, in the routing store, the routes' retirement (with its fence
marker, so a delayed whole-index save cannot write them back) and the exact ids under the delete's
request identity; phase 2 is the normal fenced delete, which ``live_guard`` admits only for that recorded
set. A failure between the phases leaves a live row without a route (its next native message starts
fresh), never a route to a deleted id; an exact retry replays its receipt before either phase acts.
"""
import hashlib
import json
import time

_PREFIX = 'fork.routed_delete.v1.'


def _foreign_routing_db(owner_db, store):
    """The routing store when it is not *owner_db* itself, else None (the delete retires it in place)."""
    routing = getattr(store, '_routing_db', None) if store is not None else None
    return routing if routing is not None and routing.db_path != owner_db.db_path else None


def _key(principal_id, session_id, request_id):
    raw = json.dumps([principal_id, session_id, request_id], separators=(',', ':'))
    return _PREFIX + hashlib.sha256(raw.encode()).hexdigest()


def retire_foreign_routes(routing, ids, record=None):
    """Retire every route naming *ids* in *routing*; *record* = (key, ids) stores phase 1's receipt."""
    from hermes_state_mutation_retirement import RETRY_HORIZON_S, retire_routes
    now = time.time()

    def write(conn):
        retire_routes(conn, ids)
        if record is not None:
            conn.execute("DELETE FROM state_meta WHERE key GLOB ? AND json_extract(value, '$.at') < ?",
                         (_PREFIX + '*', now - RETRY_HORIZON_S))
            conn.execute('INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,?)',
                         (record[0], json.dumps({'ids': record[1], 'at': now})))
    routing._execute_write(write)


class _Admitted(Exception):
    """Phase 2's own checks admitted the delete; raised at its last one, so that write rolls back."""


def retire_routes_first(authority, fields, authorize_write=None):
    """Phase 1 of a canonical delete (off the loop), before its fenced commit. Phase 2's refusals answer
    here first, from phase 2's own code: ``mutate_runtime_session`` with these exact *fields* (parameters,
    epoch, receipt replay, row, revision, generation), *authorize_write* and the delete's idle rule, rolled
    back at its end. So a delete phase 2 would refuse never costs a live chat its route, and a request
    that already holds a receipt is phase 2's to replay."""
    store = getattr(authority.runner, 'session_store', None)
    routing = _foreign_routing_db(authority.db, store)
    if routing is None:
        return
    from hermes_state_mutation_guards import require_idle
    from hermes_state_runtime import mutate_runtime_session
    seen = []

    def admit(conn):
        if authorize_write is not None:
            authorize_write(conn)
        require_idle(authority.db, conn, seen[0])
        raise _Admitted

    try:
        mutate_runtime_session(authority.db, **{**fields, '_live_guard': lambda ids: seen.append(sorted(ids)),
                                                '_authorize_write': admit})
        return  # an exact retry: its receipt replays in phase 2
    except _Admitted:
        targets = seen[0]
    key = _key(fields['principal_id'], fields['session_id'], fields['request_id'])
    retire_foreign_routes(routing, targets, (key, targets))
    store.retire_runtime_sessions(targets)


def admits_delete(authority, principal_id, session_id, request_id, targets):
    """Phase 2's ``live_guard`` predicate: phase 1 committed for exactly *targets*."""
    routing = _foreign_routing_db(authority.db, getattr(authority.runner, 'session_store', None))
    if routing is None:
        return False
    with routing._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?',
                           (_key(principal_id, session_id, request_id),)).fetchone()
    return row is not None and json.loads(row[0])['ids'] == sorted(targets)


def retire_routes_before_direct_delete(db, routing, session_id):
    """Phase 1 for the serve's WS ``session.delete`` (a direct ``delete_session`` writer) of a named
    profile's row: the routes of the scope its retirement fences, skipped when that delete would find
    no row or refuse an active write guard. The owner's in-memory index is its own; the fence marker
    keeps its next save from writing these routes back."""
    if routing is None or routing.db_path == db.db_path:
        return
    from hermes_state_local import owned_lineage_ids
    from hermes_state_sessions import _collect_delegate_child_ids
    with db._read_ctx() as conn:
        if conn.execute('SELECT 1 FROM sessions WHERE id=?', (session_id,)).fetchone() is None:
            return
        scope = owned_lineage_ids(conn, session_id)
        if db._guarded_ids(conn, [*scope, *_collect_delegate_child_ids(conn, scope)]):
            return
    retire_foreign_routes(routing, scope)

