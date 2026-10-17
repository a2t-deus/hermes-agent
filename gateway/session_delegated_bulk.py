"""Fork (D19): the serve's bulk session endpoints run on the owner that holds the store.

Bulk-delete, delete-empty, prune and owner-backfill are store-wide maintenance. Beside a separate owner the
serve's offline exclusive-maintenance lock refuses them (409), so they arrive here on a single-use ticket
bound to the exact id set or filter hash (``session_delegated_edit``). The owner re-evaluates the selection
on its own connection, through the same store paths its own maintenance uses, retires what it deleted
from memory (as ``session.prune`` does) and logs principal, operation and count. Every delete path leaves
a retired-id tombstone (``retire_sessions``), so the in-memory sessions and routes naming one are exactly
what the edit removed.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import sqlite3

from hermes_state_runtime import RuntimeStoreError

logger = logging.getLogger(__name__)

OPERATIONS = frozenset({'bulk_delete', 'delete_empty', 'prune', 'owner_backfill'})
#: A cron-heavy store prunes tens of thousands of rows in one transaction (``PRUNE_RPC_TIMEOUT``).
TIMEOUT_SECONDS = 600
_MAX_IDS = 500  # the serve's bulk-delete cap


def _sessions_dir(authority):
    directory = Path(authority.profile_id) / 'sessions'
    return directory if directory.exists() else None


def _no_params(params):
    if params != {}:
        raise RuntimeStoreError('invalid_params')


async def _bulk_delete(authority, params):
    ids = params.get('ids') if set(params) == {'ids'} else None
    if (not isinstance(ids, list) or len(ids) > _MAX_IDS
            or not all(isinstance(sid, str) and 0 < len(sid) <= 512 for sid in ids)):
        raise RuntimeStoreError('invalid_params')
    skipped = []
    # On-disk cleanup is left to the next prune, as on the serve's own bulk delete.
    deleted = await asyncio.to_thread(authority.db.delete_sessions, ids, exclude_active_write_guards=True,
                                      skipped_ids=skipped, include_compression_chain=True)
    return {'deleted': deleted, 'skipped_active': skipped}


async def _delete_empty(authority, params):
    _no_params(params)
    return {'deleted': await asyncio.to_thread(authority.db.delete_empty_sessions,
                                               sessions_dir=_sessions_dir(authority))}


async def _prune(authority, params):
    from gateway.session_prune import _filters
    filters = _filters(params)
    skipped_open = await asyncio.to_thread(authority.db.count_open_prune_matches, **filters)
    removed = await asyncio.to_thread(authority.db.prune_sessions, sessions_dir=_sessions_dir(authority),
                                      exclude_active_write_guards=True, **filters)
    return {'removed': removed, 'skipped_open': skipped_open}


async def _owner_backfill(authority, params):
    _no_params(params)
    from gateway.session_authorities import served_profile_name
    # The owner names its own store; the serve never chooses the stamp.
    stamp = served_profile_name(Path(authority.profile_id))
    stamped = await asyncio.to_thread(authority.db.backfill_null_session_profiles, stamp)
    return {'stamped': stamped, 'profile': stamp}


_HANDLERS = {'bulk_delete': _bulk_delete, 'delete_empty': _delete_empty, 'prune': _prune,
             'owner_backfill': _owner_backfill}
_COUNTS = {'bulk_delete': 'deleted', 'delete_empty': 'deleted', 'prune': 'removed', 'owner_backfill': 'stamped'}


def run_bulk(authority, binding, params, loop):
    """The control-socket thread's half: run on the owner loop, wait for the committed answer."""
    future = asyncio.run_coroutine_threadsafe(_run(authority, binding, params), loop)
    return future.result(timeout=TIMEOUT_SECONDS)


async def _run(authority, binding, params):
    from gateway.session_authorities import owner_scope
    from gateway.session_runtime_workers import track_mutation
    try:
        authority._require_admission_open()
        with owner_scope(authority):
            task = track_mutation(authority, _apply(authority, binding, params))
        return {'result': await asyncio.shield(task)}
    except RuntimeStoreError as exc:
        return {'refused': exc.reason}
    except sqlite3.Error:
        logger.exception('delegated bulk edit: storage error')
        return {'refused': 'storage_unavailable'}


def _retired(db, session_ids):
    from hermes_state_mutation_retirement import RETIRED_PREFIX
    with db._read_ctx() as conn:
        return [sid for sid in session_ids
                if conn.execute('SELECT 1 FROM state_meta WHERE key=?', (RETIRED_PREFIX + sid,)).fetchone()]


async def _apply(authority, binding, params):
    from gateway.session_mutations import retire_live_sessions
    operation = binding['operation']
    result = await _HANDLERS[operation](authority, params)
    if operation != 'owner_backfill':
        named = set(authority.sessions)
        store = getattr(authority.runner, 'session_store', None)
        if store is not None:
            with store._lock:
                named.update(entry.session_id for entry in store._entries.values())
        retire_live_sessions(authority, await asyncio.to_thread(_retired, authority.db, sorted(named)))
    logger.info('delegated bulk edit: principal=%s operation=%s count=%d params_sha256=%s profile=%s',
                binding['subject'], operation, result[_COUNTS[operation]], binding['params_sha256'][:16],
                binding['profile_id'])
    return result
