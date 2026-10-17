"""A standalone ``hermes serve`` beside the host's gateway edits sessions through that owner.

Every Deus host runs one multiplexing ``hermes gateway run`` (the store's owner) and a separate
``hermes serve`` for the tailnet. The serve has no composed authority, so its session edits took the
offline path, whose exclusive maintenance lock the gateway always holds: every PATCH answered 409
"Exclusive maintenance refused" (MacBook rollout, 2026-10-17). With a ready owner serving the home the
serve now sends the edit as ``session.mutate`` to it, as ``hermes sessions prune`` does; with no owner
the offline path is unchanged.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from hermes_state import SessionDB
from tests.gateway.test_normal_runtime_boot import control


def _row(home):
    with SessionDB(db_path=home / 'state.db') as db:
        return db.get_session('s1')


@pytest.mark.platforms('linux', 'macos')
def test_serve_patch_routes_to_the_separate_gateway_owner(tmp_path, monkeypatch, _isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip('fastapi/starlette not installed')
    import hermes_state
    import hermes_cli.web_routers.sessions as routes
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    home, user = get_hermes_home(), tmp_path / 'user'
    user.mkdir()
    home.chmod(0o700)
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', home / 'state.db')
    monkeypatch.setattr(routes, '_legacy_sidebar_logged', False)
    model = {'model': {'provider': 'custom', 'default': 'm', 'base_url': 'http://127.0.0.1:1/v1'},
             'auxiliary': {'title_generation': {'enabled': False}}}
    (home / 'config.yaml').write_text(json.dumps({'gateway': {'multiplex_profiles': True}, **model}))
    with SessionDB(db_path=home / 'state.db') as db:
        db.create_session(session_id='s1', source='cli')
        db.append_message(session_id='s1', role='user', content='hi')
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               PYTHONUNBUFFERED='1')
    serve = TestClient(app)
    serve.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    with (tmp_path / 'gateway.log').open('w+') as log:
        process = subprocess.Popen([sys.executable, '-m', 'gateway.run'], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline, descriptor = time.monotonic() + 60, {}
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    descriptor = control(home, 'identify')
                    if descriptor.get('state') == 'ready':
                        break
                except (OSError, ValueError, AssertionError):
                    pass
                time.sleep(.1)
            log.seek(0)
            assert descriptor.get('state') == 'ready', (descriptor, log.read())

            # The installed Deus app's identity-less bodies (C6 shim), then upstream's fenced body.
            for body, check in (({'pinned': True, 'profile': None}, lambda r: r['pinned']),
                                ({'pinned': False}, lambda r: not r['pinned']),
                                ({'unread': False}, lambda r: r['last_read_at'])):
                response = serve.patch('/api/sessions/s1', json=body)
                assert response.status_code == 200, (body, response.text)
                assert check(_row(home)), body
            revision = serve.get('/api/sessions/s1/mutation-snapshot').json()['runtime_revision']
            assert revision == 3
            response = serve.patch('/api/sessions/s1', json={
                'title': 'renamed', 'request_id': 'r-title', 'expected_revision': revision})
            assert response.status_code == 200, response.text
            assert _row(home)['title'] == 'renamed'
            stale = serve.patch('/api/sessions/s1', json={
                'archived': True, 'request_id': 'r-stale', 'expected_revision': revision})
            assert stale.status_code == 409 and stale.json()['detail'] == 'revision_conflict', stale.text
            assert not _row(home)['archived']
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    # No owner: the offline receipt path, exactly as before.
    response = serve.patch('/api/sessions/s1', json={'pinned': True})
    assert response.status_code == 200, response.text
    assert _row(home)['pinned']
