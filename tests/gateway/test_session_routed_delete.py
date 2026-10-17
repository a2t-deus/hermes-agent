"""Fork (D27): phase 1 of a delete whose routes live in another store, and the ``live_guard`` predicate
that admits phase 2 only for the ids phase 1 retired (``gateway/session_routed_delete.py``)."""
import json
from types import SimpleNamespace

import pytest

from gateway.session_routed_delete import admits_delete, retire_routes_first
from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError


@pytest.fixture
def stores(tmp_path):
    """A named profile's store beside the owner's launch-home routing store, each chat routed there."""
    routing = SessionDB(db_path=tmp_path / 'state.db')
    (tmp_path / 'profiles' / 'p').mkdir(parents=True)
    profile = SessionDB(db_path=tmp_path / 'profiles' / 'p' / 'state.db')
    retired = []
    store = SimpleNamespace(_routing_db=routing, retire_runtime_sessions=retired.extend)
    for sid in ('_verify-a', '_verify-b'):
        profile.create_session(sid, 'chaosfake')
        routing.save_gateway_routing_entry(f'agent:p:chaosfake:dm:{sid}', json.dumps({'session_id': sid}), scope='s')
    yield SimpleNamespace(db=profile, runner=SimpleNamespace(session_store=store)), routing, retired
    routing.close()
    profile.close()


def _params(authority, request_id, revision_skew=0):
    row = authority.db.get_session('_verify-a')
    return {'session_id': '_verify-a', 'request_id': request_id,
            'expected_revision': row['runtime_revision'] + revision_skew,
            'expected_generation': row['runtime_generation']}


def test_phase_two_is_admitted_for_exactly_the_ids_phase_one_retired(stores):
    authority, routing, retired = stores
    retire_routes_first(authority, 'basic:u', _params(authority, 'r1'))
    assert list(routing.load_gateway_routing_entries(scope='s')) == ['agent:p:chaosfake:dm:_verify-b']
    assert retired == ['_verify-a'], 'the owner drops the in-memory route too'
    assert admits_delete(authority, 'basic:u', '_verify-a', 'r1', ['_verify-a'])
    for principal, request_id, targets in (('basic:u', 'r1', ['_verify-a', '_verify-b']),  # an id outside the set
                                           ('basic:u', 'r2', ['_verify-a']),  # another request
                                           ('basic:other', 'r1', ['_verify-a'])):  # another principal
        assert not admits_delete(authority, principal, '_verify-a', request_id, targets), (principal, request_id)


def test_a_delete_phase_two_would_refuse_keeps_its_route(stores):
    authority, routing, retired = stores
    with pytest.raises(RuntimeStoreError, match='revision_conflict'):
        retire_routes_first(authority, 'basic:u', _params(authority, 'r1', revision_skew=1))
    assert len(routing.load_gateway_routing_entries(scope='s')) == 2 and retired == []
    assert not admits_delete(authority, 'basic:u', '_verify-a', 'r1', ['_verify-a'])
