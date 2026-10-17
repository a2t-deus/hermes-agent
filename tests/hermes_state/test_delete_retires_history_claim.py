"""Deleting a row retires its cold-history claim with the rest of the deletion fence.

Kept past the row, the claim made every later import of that id fail its own claim insert (UNIQUE
``state_meta.key``), long after the retired-id tombstone had expired: the serve beside its owner read
that as ``runtime_draining``, 503 on every retry (F5 rest_import).
"""
from functools import partial

import pytest

import hermes_state_runtime as rt
from gateway.session_contract import Principal
from hermes_state import SessionDB
from hermes_state_mutation_binding import BINDING_PREFIX, authorize_history, import_history_control
from hermes_state_mutation_retirement import prune_retired_tombstones

_ACTOR = Principal('basic:_verify-user', 'profile', frozenset({'session:create', 'session:control'}), 'http')


def test_reimport_after_delete_is_refused_by_the_tombstone_then_runs_once_it_expires(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        db.create_session('s', source='cli')
        rt.mutate_runtime_session(db, epoch=epoch, principal_id=_ACTOR.subject, session_id='s', request_id='delete',
            expected_revision=0, expected_generation=0, operation='delete', payload={},
            _authorize_write=lambda conn: authorize_history(conn, _ACTOR, 's', claim=True))
        assert db._read_one('SELECT COUNT(*) FROM state_meta WHERE key=?', (BINDING_PREFIX + 's',))[0] == 0

        def reimport(request_id):
            return rt.mutate_runtime_session(db, epoch=epoch, principal_id=_ACTOR.subject, session_id='s',
                request_id=request_id, expected_revision=0, operation='import',
                payload={'sessions': [{'id': 's', 'source': 'cli'}]},
                _authorize_write=partial(import_history_control, actor=_ACTOR, session_ids=('s',)))

        with pytest.raises(rt.RuntimeStoreError, match='admission_conflict'):
            reimport('import-early')
        db._execute_write(lambda conn: prune_retired_tombstones(conn, older_than=-1))
        assert reimport('import-late')['imported_ids'] == ['s']
        assert db.get_session('s') is not None
