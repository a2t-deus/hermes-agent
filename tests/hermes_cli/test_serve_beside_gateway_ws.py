"""Fork features over the serve's own WS, with a real, separate ``gateway.run`` owning the profile (D18).

The serve runs the Deus app's chats in-process, so none of these paths should change when another process
owns the store; each is driven here exactly as on a Deus host: basic-auth login, ws-ticket, a second window.
"""
from __future__ import annotations

import os
import re
import signal

import pytest

from tests.e2e.core.terminal._gateway_client import etype, poll_until
from tests.hermes_cli.test_serve_beside_gateway_edits import _row, _serve_chat, pytestmark, run_topology


@pytest.fixture(scope='module')
def topology(tmp_path_factory):
    yield from run_topology(tmp_path_factory)


def _complete(ws, sid, start):
    return ws.wait_for(lambda f: etype(f) == 'message.complete' and f['params']['session_id'] == sid,
                       timeout=90, start=start, what='message.complete')


def _second_window(be, name, key):
    b = be.connect(name)
    return b, b.call('session.resume', session_id=key, cols=100)['session_id']


def test_session_list_rows_carry_the_sidebar_fields(topology):
    be, _, _ = topology
    ws, _, key, _ = _serve_chat(be, 'list')
    rows = ws.call('session.list', limit=50)['sessions']
    row = next(r for r in rows if key in (r.get('id'), r.get('session_key'), r.get('session_id')))
    assert all(field in row for field in ('last_active', 'pinned', 'hidden', 'archived')), row


def test_legacy_submit_echoes_the_user_prompt_to_another_window(topology):
    be, _, _ = topology
    a, sid, key, _ = _serve_chat(be, 'echo-a')
    b, b_sid = _second_window(be, 'echo-b', key)
    assert b_sid == sid
    start = len(b.snapshot())
    a.call('prompt.submit', session_id=sid, text='sent from the iPad')
    echo = b.wait_for(lambda f: etype(f) == 'user.prompt' and f['params']['session_id'] == sid,
                      timeout=90, start=start, what='user.prompt echo')
    assert echo['params']['payload']['text'] == 'sent from the iPad'
    _complete(b, sid, start)


def test_clarify_is_answered_from_another_window_by_its_request_id(topology):
    be, _, _ = topology
    a, sid, key, start = _serve_chat(be, 'answer', 'CLARIFY please')
    ask = a.wait_for(lambda f: f.get('method') == 'clarify', timeout=90, start=start, what='clarify request')
    b, _ = _second_window(be, 'answer-b', key)
    assert b.call('request.answer', id=ask['id'], result={'answers': {'q0': 'blue'}}) == {'status': 'ok'}
    done = _complete(a, sid, start)
    assert 'blue' in str(done['params'].get('payload', {}).get('text')), done['params']


def test_approval_is_answered_with_approval_respond(topology):
    be, _, _ = topology
    victim = be.root / 'victim'
    victim.mkdir()
    a, sid, key, start = _serve_chat(be, 'approve', f'RMRF {victim}')
    ask = a.wait_for(lambda f: f.get('method') == 'approval', timeout=90, start=start, what='approval request')
    b, b_sid = _second_window(be, 'approve-b', key)
    resolved = b.call('approval.respond', session_id=b_sid, choice='deny', request_id=ask['params']['request_id'])
    assert resolved.get('resolved'), resolved
    _complete(a, sid, start)
    assert victim.exists(), 'a denied command ran'


def test_settle_rpc_settles_and_cancels_the_open_clarify(topology):
    be, _, _ = topology
    a, sid, key, start = _serve_chat(be, 'settle-rpc', 'CLARIFY please')
    a.wait_for(lambda f: f.get('method') == 'clarify', timeout=90, start=start, what='clarify request')
    result = a.call('session.set_settled', session_id=key, settled=True)
    assert (result['settled'], result['cancelled_requests']) == (True, 1), result
    assert _row(be, key)['settled_at']
    _complete(a, sid, start)


def test_sigterm_closes_with_1012_and_books_the_cut_turn_to_serve_shutdown(topology):
    """Last: it stops the serve."""
    be, _, _ = topology
    a, _, _, _ = _serve_chat(be, 'drain', 'HANG here')
    poll_until(lambda: any('HANG here' in str(body.get('messages')) for body in be.llm.main_requests()) or None,
               timeout=60, what='the turn is waiting on the provider')
    os.killpg(be.proc.pid, signal.SIGTERM)
    poll_until(lambda: a.closed or None, timeout=30, what='serve closed the WS')
    assert a._ws.close_code == 1012, a._ws.close_code
    be.proc.wait(timeout=30)
    logs = '\n'.join(p.read_text(errors='replace') for p in (be.hermes_home / 'logs').glob('*.log'))
    # Cut mid-provider-call: ``interrupted_during_api_call(serve_shutdown)``; between calls ``interrupted_by_system``.
    assert re.search(r'Turn ended: reason=interrupted_\w+\(serve_shutdown\)', logs), logs[-3000:]
