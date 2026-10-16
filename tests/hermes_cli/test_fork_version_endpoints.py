"""/api/health and /api/status report the fork's version beside upstream's boot ``commit`` (contract C1).

``commit`` is the code the process BOOTED from (upstream ``get_version_info().commit``); ``fork_commit`` is the
checkout HEAD now, so the two differ after an update the process outlived.
"""

import subprocess

import pytest

import hermes_cli.fork_version as fv
from hermes_cli.version_info import get_version_info


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
def _fresh_head_cache(monkeypatch):
    monkeypatch.setattr(fv, "_head_cache", (float("-inf"), None))


def _git_head(sha):
    return lambda *a, **k: subprocess.CompletedProcess(a, 0, f"{sha}\n", "")


@pytest.mark.parametrize("path", ["/api/health", "/api/status"])
def test_commit_is_the_boot_commit_and_fork_commit_the_checkout_head(client, monkeypatch, path):
    monkeypatch.setattr(fv.subprocess, "run", _git_head("feedfac"))
    data = client.get(path).json()
    assert data["version"]  # upstream key untouched
    assert data["commit"] == get_version_info().commit
    assert data["fork_version"] == fv.__fork_version__
    assert data["fork_commit"] == "feedfac"


@pytest.mark.parametrize("path", ["/api/health", "/api/status"])
def test_failed_git_lookup_nulls_only_fork_commit(client, monkeypatch, path):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(128, a[0])
    monkeypatch.setattr(fv.subprocess, "run", boom)
    data = client.get(path).json()
    assert data["fork_version"] == fv.__fork_version__
    assert data["fork_commit"] is None
    assert data["commit"] == get_version_info().commit


def test_checkout_head_is_cached_between_probes(monkeypatch):
    calls = []

    def run(*a, **k):
        calls.append(a)
        return subprocess.CompletedProcess(a, 0, "abc1234\n", "")
    monkeypatch.setattr(fv.subprocess, "run", run)
    assert fv.fork_commit() == fv.fork_commit() == "abc1234"
    assert len(calls) == 1
