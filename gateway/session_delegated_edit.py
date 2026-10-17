"""Fork (D19): a standalone ``hermes serve`` beside this gateway edits sessions as its HTTP principal.

The serve authenticates its own users (basic auth, session token) and stamps their subject on the rows it
creates; the owner refuses any other principal on them (``authorize_history``). Its session edits therefore
reach the owner as that subject, never as the socket peer's uid. Both verbs are private: only the
peer-verified owner-only control socket (same uid) reaches them, and no network listener redeems a ticket.

* ``delegated-edit-ticket`` mints a single-use ticket (30 s) bound to the exact profile, subject, session
  and operation.
* ``delegated-edit`` consumes it and runs ``session.mutate`` on the owner's authority as that principal,
  every fence (revision, generation, idle, live-session gate) the owner's own.

An import outgrows one control-socket line (64 KiB; real exports run to megabytes), so the serve stages its
payload in a private file under the profile's ``runtime/delegated-edit/`` and the ticket binds its sha256;
the owner reads it only through that binding and removes it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import secrets
import threading
import time
import uuid

logger = logging.getLogger(__name__)

TICKET_TTL_SECONDS = 30
_MAX_TICKETS = 64
_OPERATIONS = frozenset({'sidebar', 'delete', 'import'})
_BINDING_KEYS = ('profile_id', 'subject', 'session_id', 'operation')
# The same grant the gateway-hosted REST routes give an authenticated HTTP principal.
_CAPABILITIES = frozenset({'session:read', 'session:control', 'session:create', 'session:operator'})
_MUTATION_TIMEOUT = 25
STAGING_DIR = Path('runtime') / 'delegated-edit'
_STAGED_NAME = re.compile(r'[A-Za-z0-9_-]{32,64}\.json')
# Twice the serve's import body cap (``_SESSION_IMPORT_MAX_BYTES``): re-encoding never grows it further.
_STAGED_MAX_BYTES = 50 * 1024 * 1024


class DelegatedEditTickets:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}

    def mint(self, binding: dict) -> str:
        with self._lock:
            now = time.monotonic()
            self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
            if len(self._entries) >= _MAX_TICKETS:
                raise PermissionError('delegated edit capacity exhausted')
            ticket = secrets.token_urlsafe(32)
            self._entries[hashlib.sha256(ticket.encode()).hexdigest()] = (now + TICKET_TTL_SECONDS, binding)
            return ticket

    def consume(self, ticket, binding: dict) -> bool:
        """Single use: a presented ticket is gone whether or not its binding matched."""
        if not isinstance(ticket, str) or len(ticket) > 256:
            return False
        with self._lock:
            entry = self._entries.pop(hashlib.sha256(ticket.encode()).hexdigest(), None)
        return entry is not None and entry[0] > time.monotonic() and entry[1] == binding


def _binding(params) -> dict:
    keys = (*_BINDING_KEYS, 'payload_sha256') if 'payload_sha256' in params else _BINDING_KEYS
    binding = {key: params.get(key) for key in keys}
    if (not all(isinstance(value, str) and value and len(value) <= 512 for value in binding.values())
            or binding['operation'] not in _OPERATIONS
            or ('payload_sha256' in binding and (binding['operation'] != 'import'
                                                 or not re.fullmatch(r'[0-9a-f]{64}', binding['payload_sha256'])))):
        raise PermissionError('invalid delegated edit binding')
    return binding


def _read_staged(path: Path, digest: str):
    """The staged payload, only when it is this user's single-link regular file with the bound digest."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _STAGED_MAX_BYTES
                or (os.name != 'nt' and info.st_uid != os.getuid())):  # windows-footgun: ok — POSIX owner check
            return None
        with os.fdopen(os.dup(fd), 'rb') as stream:  # windows-footgun: ok — binary mode
            raw = stream.read(_STAGED_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if hashlib.sha256(raw).hexdigest() != digest:
        return None
    return json.loads(raw)


def install_delegated_edits(server, runner, loop):
    from gateway.session_authorities import authority_for_home

    tickets = DelegatedEditTickets()

    def select(profile_id):
        home = Path(profile_id)
        authority = authority_for_home(runner, home) if home.is_absolute() and home == home.resolve() else None
        if authority is None or authority.profile_id != profile_id:
            raise PermissionError('profile_mismatch')
        return authority

    def mint(params, peer):
        if set(params) - {'payload_sha256'} != set(_BINDING_KEYS):
            raise PermissionError('invalid delegated edit binding')
        binding = _binding(params)
        select(binding['profile_id'])
        return {'ticket': tickets.mint(binding), 'expires_in_seconds': TICKET_TTL_SECONDS}

    def edit(params, peer):
        staged = 'payload_sha256' in params
        if (set(params) - {'payload_sha256', 'staged'} != {'ticket', 'mutation', *_BINDING_KEYS}
                or staged != ('staged' in params)):
            raise PermissionError('invalid delegated edit request')
        binding = _binding(params)
        name = params.get('staged')
        staged_path = (Path(binding['profile_id']) / STAGING_DIR / name
                       if staged and isinstance(name, str) and _STAGED_NAME.fullmatch(name) else None)
        try:
            return _edit(binding, params, staged_path)
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)

    def _edit(binding, params, staged_path):
        if not tickets.consume(params['ticket'], binding):
            return {'refused': 'permission_denied'}
        mutation = params['mutation']
        staged = 'payload_sha256' in binding
        if (not isinstance(mutation, dict) or mutation.get('session_id') != binding['session_id']
                or mutation.get('operation') != binding['operation'] or staged == ('payload' in mutation)):
            return {'refused': 'invalid_params'}
        authority = select(binding['profile_id'])
        logger.info('delegated session edit: principal=%s operation=%s session=%s profile=%s',
                    binding['subject'], binding['operation'], binding['session_id'], binding['profile_id'])
        if staged:
            payload = _read_staged(staged_path, binding['payload_sha256']) if staged_path is not None else None
            if payload is None:
                return {'refused': 'permission_denied'}
            mutation = {**mutation, 'payload': payload}
        if binding['operation'] == 'import':
            payload = mutation.get('payload') if isinstance(mutation.get('payload'), dict) else {}
            _, errors = authority.db._validate_import_payload(payload.get('sessions'))
            if errors:
                return {'refused': 'invalid_params', 'errors': errors}
        future = asyncio.run_coroutine_threadsafe(_mutate(authority, binding, mutation), loop)
        return future.result(timeout=_MUTATION_TIMEOUT)

    server.private_handlers.update({'delegated-edit-ticket': mint, 'delegated-edit': edit})


async def _mutate(authority, binding, mutation):
    from gateway.session_authorities import owner_scope
    from gateway.session_contract import Principal, SessionRef
    from gateway.session_local_migration import bind_native_transport, unbind_native_transport
    from gateway.session_mutations import mutate_session
    from hermes_state_mutation_guards import MUTATION_GUARD_REFUSALS
    from hermes_state_runtime import RuntimeStoreError

    actor = Principal(binding['subject'], authority.profile_id, _CAPABILITIES, 'delegated-http:' + uuid.uuid4().hex)
    # The serve's offline edit writes old unrouted local history as this principal; beside an owner it
    # claims it the same way, as a native local transport does (one request, then unbound).
    bind_native_transport(authority, actor, {'provider': 'local', 'profile_id': authority.profile_id,
                                             'instance_id': authority.instance_id})
    try:
        with owner_scope(authority):
            return {'result': await mutate_session(authority, actor, SessionRef(authority.profile_id,
                                                                                 binding['session_id']), mutation)}
    except RuntimeStoreError as exc:
        return {'refused': exc.reason}
    except MUTATION_GUARD_REFUSALS:
        return {'refused': 'session_busy'}
    except ValueError as exc:
        return {'refused': 'invalid_params', 'detail': str(exc)}
    finally:
        unbind_native_transport(authority, actor)
