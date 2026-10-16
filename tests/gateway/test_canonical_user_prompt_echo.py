"""A canonical-protocol (``hermes-gateway-v1``) submit echoes its user text to every viewer before the reply.

Desktop's sessions live in the session authority, so the legacy ``prompt.submit`` handler's echo never runs for
them. A ⌘⇧N peer window resumed on the same chat sees the text the source sent exactly once, before
``message.start``; a viewer of another chat sees nothing.
"""
import pytest

from tests.gateway.fixtures.execution_frames_capture import Peer
from tests.gateway.test_local_authority_transitions import local_session


def _flush(peer):
    while not peer.frames.empty():
        peer.frames.get_nowait()


def _turn(peer, session_id):
    return [f['params'] for f in peer.drain('message.complete')
            if f.get('method') == 'event' and f['params']['session_id'] == session_id]


@pytest.mark.asyncio
async def test_canonical_submit_echoes_once_to_every_viewer_before_message_start(tmp_path, monkeypatch):
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session

    authority, owner, shared = await local_session(tmp_path, monkeypatch, source='gui')

    async def answer(event):
        return 'answer:' + event.text
    authority.runner._handle_message = answer
    other = create_local_session(authority, owner.actor, {'request_id': 'create-other', 'source': 'gui',
                                                          'cwd': str(tmp_path), 'model': 'old', 'toolsets': []})
    source, peer, bystander = Peer(), Peer(), Peer()
    conns = []
    for transport, ref in ((source, shared), (peer, shared), (bystander, other)):
        conn = AuthorityConnection(authority, transport, {'user_id': 'owner'})
        await conn.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': ref.session_id}})
        conns.append(conn)
        _flush(transport)
    try:
        submitted = await conns[0].dispatch({'id': 2, 'method': 'prompt.submit', 'params': {
            'session_id': shared.session_id, 'text': 'sent from the source', 'submission_id': 's1'}})
        assert 'result' in submitted, submitted
        await authority.sessions[shared.session_id].task

        for viewer in (peer, source):  # the sender gets the same frame and dedupes it against its optimistic row
            frames = _turn(viewer, shared.session_id)
            kinds = [f['type'] for f in frames]
            assert kinds.count('user.prompt') == 1, kinds
            assert kinds.index('user.prompt') < kinds.index('message.start'), kinds
            assert frames[kinds.index('user.prompt')]['payload'] == {'text': 'sent from the source', 'images': 0}
        leaked = []
        while not bystander.frames.empty():
            leaked.append(bystander.frames.get_nowait())
        assert not [f for f in leaked if f.get('params', {}).get('type') == 'user.prompt'], 'echo leaked to another chat'
    finally:
        for conn in (*conns, owner):
            await conn.close()


def test_automation_hidden_and_messaging_admissions_never_echo():
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_display import admission_user_prompt

    local = SessionSource(platform=Platform.LOCAL, chat_id='c')
    assert admission_user_prompt(local, {'text': 'hi'}) == {'text': 'hi', 'images': 0}
    assert admission_user_prompt(local, {'text': 'hi', 'display_v1': {'kind': 'hidden'}}) is None
    assert admission_user_prompt(local, {'text': 'hi', 'local_automation_v1': {}}) is None
    assert admission_user_prompt(local, {'text': 'hi', 'native_text_v1': {}}) is None
    assert admission_user_prompt(local, {'text': [{'type': 'text', 'text': 'hi'}]}) is None
    assert admission_user_prompt(SessionSource(platform=Platform.TELEGRAM, chat_id='c'), {'text': 'hi'}) is None
