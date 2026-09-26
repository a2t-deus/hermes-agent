"""/api/health and /api/status report the fork's own version + running checkout sha."""

import subprocess

import pytest

import hermes_cli.fork_version as fv


@pytest.fixture
def client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


@pytest.fixture(autouse=True)
def _fresh_commit_cache():
    fv.fork_commit.cache_clear()
    yield
    fv.fork_commit.cache_clear()


@pytest.mark.parametrize("path", ["/api/health", "/api/status"])
def test_reports_fork_version_and_commit(client, monkeypatch, path):
    monkeypatch.setattr(fv.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "154f6e8\n", ""))
    data = client.get(path).json()
    assert data["version"]  # upstream key untouched
    assert data["fork_version"] == fv.__fork_version__
    assert data["commit"] == "154f6e8"


@pytest.mark.parametrize("path", ["/api/health", "/api/status"])
def test_failed_git_lookup_reports_null_commit(client, monkeypatch, path):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(128, a[0])
    monkeypatch.setattr(fv.subprocess, "run", boom)
    data = client.get(path).json()
    assert data["fork_version"] == fv.__fork_version__
    assert data["commit"] is None


def test_commit_resolved_once(monkeypatch):
    calls = []
    def run(*a, **k):
        calls.append(a)
        return subprocess.CompletedProcess(a, 0, "abc1234\n", "")
    monkeypatch.setattr(fv.subprocess, "run", run)
    assert fv.fork_commit() == fv.fork_commit() == "abc1234"
    assert len(calls) == 1
