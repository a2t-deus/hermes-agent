"""Credential half of `hermes --connect` (see :mod:`hermes_cli.remote_attach`).

The invariants worth defending here are security ones: the password is never persisted, the
cookie jar is owner-only, a name from config can never escape its directory, and a network
failure is never mistaken for a rejected credential (which would re-prompt for a password on
every flaky link).
"""
from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from hermes_cli import remote_attach
from hermes_cli.remote_attach import (
    RemoteAttachError, RemoteAuthExpired, RemoteTarget, cookie_path, ensure_session, load_jar,
    login, probe_session, resolve_target, save_jar)

TARGET = RemoteTarget(name="mini", base_url="http://mini.test:9129", username="sagi")


def _patch_client(monkeypatch, handler):
    """Patch the single client factory production reads, so every call is routed to `handler`."""
    def factory(base_url, cookies=None):
        return httpx.Client(
            base_url=base_url, cookies=cookies or {}, transport=httpx.MockTransport(handler),
            follow_redirects=False)

    monkeypatch.setattr(remote_attach, "_client", factory)


def _providers(supports_password=True):
    return httpx.Response(200, json={"providers": [
        {"name": "local", "display_name": "Local", "supports_password": supports_password}]})


# --- Target resolution ----------------------------------------------------


class TestResolveTarget:
    def test_bare_url_needs_no_config_entry(self):
        target = resolve_target("http://100.119.193.95:9129", config={})
        assert target.base_url == "http://100.119.193.95:9129"
        assert target.username == ""
        # Jar directory is derived from host+port, and stays filesystem-safe.
        assert target.name == "100.119.193.95_9129"

    def test_named_entry_carries_the_username(self):
        config = {"remote_gateways": {
            "mini": {"url": "http://100.119.193.95:9129", "username": "sagi"}}}
        assert resolve_target("mini", config=config) == RemoteTarget(
            name="mini", base_url="http://100.119.193.95:9129", username="sagi")

    def test_trailing_slash_is_normalized_away(self):
        target = resolve_target("http://mini.test:9129/", config={})
        assert target.base_url == "http://mini.test:9129"

    def test_unknown_name_lists_what_is_configured(self):
        config = {"remote_gateways": {"mini": {"url": "http://a.test:1"}}}
        with pytest.raises(RemoteAttachError, match="mini"):
            resolve_target("laptop", config=config)

    @pytest.mark.parametrize("bad", [
        "ftp://mini.test",              # not http(s)
        "http://mini.test/dashboard",   # a path, not an origin
        "http://mini.test?x=1",         # query string
        "http://user:pw@mini.test",     # credentials in the URL
        "http://",                      # no host
    ])
    def test_malformed_origins_are_refused_not_reinterpreted(self, bad):
        with pytest.raises(RemoteAttachError):
            resolve_target(bad, config={})

    def test_empty_connect_is_an_error(self):
        with pytest.raises(RemoteAttachError):
            resolve_target("  ", config={})

    def test_entry_without_url_is_an_error(self):
        with pytest.raises(RemoteAttachError, match="url"):
            resolve_target("mini", config={"remote_gateways": {"mini": {"username": "sagi"}}})

    def test_a_traversing_name_cannot_escape_the_remote_directory(self, tmp_path):
        config = {"remote_gateways": {"../../etc": {"url": "http://a.test:1"}}}
        target = resolve_target("../../etc", config=config)
        jar = cookie_path(target.name, home=tmp_path)
        assert jar.parent.parent == tmp_path / "remote"
        assert ".." not in str(jar)


# --- Cookie jar -----------------------------------------------------------


class TestCookieJar:
    def test_saved_jar_is_owner_only(self, tmp_path):
        path = save_jar(cookie_path("mini", home=tmp_path), {"cookies": {"a": "1"}})
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700

    def test_round_trip_keeps_cookies_and_username(self, tmp_path):
        path = cookie_path("mini", home=tmp_path)
        save_jar(path, {"cookies": {"hermes_session_at": "at-1"}, "username": "sagi"})
        jar = load_jar(path)
        assert jar["cookies"] == {"hermes_session_at": "at-1"}
        assert jar["username"] == "sagi"

    def test_missing_or_corrupt_jar_reads_as_empty(self, tmp_path):
        assert load_jar(tmp_path / "nope.json")["cookies"] == {}
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert load_jar(bad)["cookies"] == {}

    def test_non_dict_cookies_are_discarded(self, tmp_path):
        path = tmp_path / "jar.json"
        path.write_text(json.dumps({"cookies": ["not", "a", "map"]}))
        assert load_jar(path)["cookies"] == {}

    def test_no_temp_file_is_left_behind(self, tmp_path):
        path = cookie_path("mini", home=tmp_path)
        save_jar(path, {"cookies": {"a": "1"}})
        save_jar(path, {"cookies": {"a": "2"}})
        assert [p.name for p in path.parent.iterdir()] == ["cookies.json"]


# --- Login / probe --------------------------------------------------------


class TestLogin:
    def test_returns_the_cookie_set_the_serve_issued(self, monkeypatch):
        def handler(request):
            if request.url.path == "/api/auth/providers":
                return _providers()
            assert request.url.path == "/auth/password-login"
            assert json.loads(request.content) == {
                "provider": "local", "username": "sagi", "password": "hunter2"}
            return httpx.Response(
                200, json={"ok": True, "next": "/"},
                headers={"set-cookie": "hermes_session_at=at-1; Path=/; HttpOnly"})

        _patch_client(monkeypatch, handler)
        assert login(TARGET, "sagi", "hunter2") == {"hermes_session_at": "at-1"}

    def test_bad_credentials_raise_the_expiry_error(self, monkeypatch):
        def handler(request):
            if request.url.path == "/api/auth/providers":
                return _providers()
            return httpx.Response(401, json={"detail": "Invalid credentials"})

        _patch_client(monkeypatch, handler)
        with pytest.raises(RemoteAuthExpired):
            login(TARGET, "sagi", "wrong")

    def test_rate_limit_is_distinguished_from_a_wrong_password(self, monkeypatch):
        def handler(request):
            if request.url.path == "/api/auth/providers":
                return _providers()
            return httpx.Response(429, json={"detail": "slow down"})

        _patch_client(monkeypatch, handler)
        with pytest.raises(RemoteAttachError, match="Too many login attempts"):
            login(TARGET, "sagi", "pw")

    def test_a_serve_without_a_password_provider_says_so(self, monkeypatch):
        _patch_client(monkeypatch, lambda request: _providers(supports_password=False))
        with pytest.raises(RemoteAttachError, match="no password provider"):
            login(TARGET, "sagi", "pw")

    def test_login_that_sets_no_cookies_is_a_failure_not_a_silent_success(self, monkeypatch):
        def handler(request):
            if request.url.path == "/api/auth/providers":
                return _providers()
            return httpx.Response(200, json={"ok": True, "next": "/"})

        _patch_client(monkeypatch, handler)
        with pytest.raises(RemoteAttachError, match="no session cookies"):
            login(TARGET, "sagi", "pw")


class TestProbeSession:
    def test_live_session_returns_the_current_cookies(self, monkeypatch):
        _patch_client(monkeypatch, lambda request: httpx.Response(200, json={"user_id": "u1"}))
        assert probe_session(TARGET, {"hermes_session_at": "at-1"}) == {"hermes_session_at": "at-1"}

    def test_rotated_cookies_come_back_from_the_probe(self, monkeypatch):
        _patch_client(monkeypatch, lambda request: httpx.Response(
            200, json={"user_id": "u1"},
            headers={"set-cookie": "hermes_session_at=at-2; Path=/"}))
        assert probe_session(TARGET, {"hermes_session_at": "at-1"})["hermes_session_at"] == "at-2"

    def test_rejected_session_is_none_not_an_exception(self, monkeypatch):
        _patch_client(monkeypatch, lambda request: httpx.Response(401, json={"detail": "nope"}))
        assert probe_session(TARGET, {"hermes_session_at": "stale"}) is None

    def test_empty_jar_short_circuits(self, monkeypatch):
        def handler(request):  # pragma: no cover - must never run
            raise AssertionError("probe should not dial with an empty jar")

        _patch_client(monkeypatch, handler)
        assert probe_session(TARGET, {}) is None

    def test_an_unreachable_serve_raises_rather_than_discarding_good_cookies(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("no route to host")

        _patch_client(monkeypatch, handler)
        with pytest.raises(RemoteAttachError, match="Cannot reach"):
            probe_session(TARGET, {"hermes_session_at": "at-1"})


# --- ensure_session -------------------------------------------------------


class TestEnsureSession:
    def test_a_live_stored_session_is_reused_without_prompting(self, tmp_path, monkeypatch):
        path = cookie_path("mini", home=tmp_path)
        save_jar(path, {"cookies": {"hermes_session_at": "at-1"}, "username": "sagi"})
        _patch_client(monkeypatch, lambda request: httpx.Response(200, json={"user_id": "u1"}))
        monkeypatch.setattr(
            remote_attach, "_read_password",
            lambda *a, **k: pytest.fail("must not prompt for a live session"))

        assert ensure_session(TARGET, home=tmp_path) == path

    def test_a_rejected_session_triggers_a_fresh_login(self, tmp_path, monkeypatch):
        path = cookie_path("mini", home=tmp_path)
        save_jar(path, {"cookies": {"hermes_session_at": "stale"}, "username": "sagi"})
        monkeypatch.setenv(remote_attach.PASSWORD_ENV, "hunter2")

        def handler(request):
            if request.url.path == "/api/auth/me":
                return httpx.Response(401, json={"detail": "nope"})
            if request.url.path == "/api/auth/providers":
                return _providers()
            return httpx.Response(
                200, json={"ok": True}, headers={"set-cookie": "hermes_session_at=at-2; Path=/"})

        _patch_client(monkeypatch, handler)
        jar = load_jar(ensure_session(TARGET, home=tmp_path))
        assert jar["cookies"] == {"hermes_session_at": "at-2"}

    def test_force_login_skips_the_probe_entirely(self, tmp_path, monkeypatch):
        save_jar(cookie_path("mini", home=tmp_path), {"cookies": {"hermes_session_at": "at-1"}})
        monkeypatch.setenv(remote_attach.PASSWORD_ENV, "hunter2")
        seen = []

        def handler(request):
            seen.append(request.url.path)
            if request.url.path == "/api/auth/providers":
                return _providers()
            return httpx.Response(
                200, json={"ok": True}, headers={"set-cookie": "hermes_session_at=fresh; Path=/"})

        _patch_client(monkeypatch, handler)
        jar = load_jar(ensure_session(TARGET, force_login=True, home=tmp_path))
        assert "/api/auth/me" not in seen
        assert jar["cookies"] == {"hermes_session_at": "fresh"}

    def test_the_password_is_never_written_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.setenv(remote_attach.PASSWORD_ENV, "hunter2")

        def handler(request):
            if request.url.path == "/api/auth/providers":
                return _providers()
            return httpx.Response(
                200, json={"ok": True}, headers={"set-cookie": "hermes_session_at=at-1; Path=/"})

        _patch_client(monkeypatch, handler)
        path = ensure_session(TARGET, home=tmp_path)
        assert "hunter2" not in path.read_text()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_no_tty_and_no_env_password_fails_with_an_instruction(self, tmp_path, monkeypatch):
        monkeypatch.delenv(remote_attach.PASSWORD_ENV, raising=False)
        monkeypatch.setattr("sys.stdin", type("F", (), {"isatty": staticmethod(lambda: False)})())
        _patch_client(monkeypatch, lambda request: httpx.Response(401, json={}))

        with pytest.raises(RemoteAttachError, match=remote_attach.PASSWORD_ENV):
            ensure_session(TARGET, home=tmp_path)
