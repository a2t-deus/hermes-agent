"""Fork (D27/D29): the gateway's state.db maintenance (startup and hourly tick) prunes a multiplexed named
profile's own store, while the routes naming its rows live in the owner's launch-home index
(``gateway/session_routed_delete.py``).

In-process with the real tick body, the real ``SessionStore`` and real stores: the tick runs hourly
(60 housekeeping ticks of 60 s), so a live topology cannot reach it inside a test.
"""
import json
import time

import hermes_state
from gateway.config import GatewayConfig
from gateway.run import GatewayRunner, _housekeeping_state_db_maintenance, _profile_runtime_scope
from gateway.session import SessionStore
from hermes_state_registry import acquire, release_or_close


def _routes_naming(db, sid):
    with db._read_ctx() as conn:
        return [key for key, raw in conn.execute('SELECT session_key, entry_json FROM gateway_routing')
                if json.loads(raw).get('session_id') == sid]


def test_the_maintenance_prune_of_a_named_profile_retires_its_launch_home_routes(tmp_path, monkeypatch):
    launch = tmp_path / 'home'
    profile = launch / 'profiles' / 'p'
    profile.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(launch))
    # Unpinned, as in production: each profile scope's tick resolves its own store (the sandbox pins it).
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    (profile / 'config.yaml').write_text('sessions:\n  auto_prune: true\n  retention_days: 1\n'
                                         '  min_interval_hours: 0\n  vacuum_after_prune: false\n')
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    routing, db = store._routing_db, acquire(profile / 'state.db')
    try:
        old = time.time() - 30 * 86400
        for sid in ('_verify-old', '_verify-kept'):
            db.create_session(sid, 'chaosfake')
            routing.save_gateway_routing_entry(f'agent:p:chaosfake:dm:{sid}', json.dumps({'session_id': sid}))
        db._execute_write(lambda c: c.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = '_verify-old'", (old, old + 1)))
        with _profile_runtime_scope(profile, hydrate_secrets=False):
            _housekeeping_state_db_maintenance(None, runner=type('Runner', (), {'session_store': store})())
        assert db.get_session('_verify-old') is None, 'the tick pruned the aged chat'
        assert _routes_naming(routing, '_verify-old') == [], 'no owner route names the pruned id'
        assert _routes_naming(routing, '_verify-kept') and db.get_session('_verify-kept'), 'a live chat keeps its route'
    finally:
        release_or_close(db)


def test_a_start_retires_a_route_naming_a_deleted_row_with_auto_prune_off(tmp_path, monkeypatch):
    """D29: the startup maintenance sweeps whatever ``sessions.auto_prune`` says, so a route a crash after a
    committed delete or an offline CLI delete left naming a gone named-profile row is gone once the owner starts."""
    launch = tmp_path / 'home'
    profile = launch / 'profiles' / 'p'
    profile.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(launch))
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    (profile / 'config.yaml').write_text('sessions:\n  auto_prune: false\n  auto_archive: false\n')
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    routing, db = store._routing_db, acquire(profile / 'state.db')
    try:
        for sid in ('_verify-gone', '_verify-kept'):
            db.create_session(sid, 'chaosfake')
            routing.save_gateway_routing_entry(f'agent:p:chaosfake:dm:{sid}', json.dumps({'session_id': sid}))
        db.delete_session('_verify-gone')  # the offline CLI's delete: marker kept, the owner's index untouched
        assert _routes_naming(routing, '_verify-gone'), 'the planted stale route'
        config = GatewayConfig(multiplex_profiles=True)
        config._runtime_profile_homes = (('default', launch), ('p', profile))
        runner = object.__new__(GatewayRunner)
        runner.config, runner.session_store = config, store
        runner._init_session_db()
        assert _routes_naming(routing, '_verify-gone') == [], 'the start retired the route naming the deleted id'
        assert _routes_naming(routing, '_verify-kept') and db.get_session('_verify-kept'), 'a live chat keeps its route'
    finally:
        release_or_close(db)
