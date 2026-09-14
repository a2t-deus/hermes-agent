"""Attach the terminal TUI to a *remote* ``hermes serve`` as a thin client.

The local TUI normally spawns its own ``tui_gateway`` child, which claims a per-session
lease on first turn (``tui_gateway/session_lifecycle.py``). Two machines doing that against
one session store collide with ``SESSION_NOT_OWNED``. Attaching instead — dialing the remote
serve's ``/api/ws`` as one more WebSocket client — claims nothing, so a Desktop app, an iOS
app and any number of terminal TUIs can watch the same live sessions of that host.

This module owns the *credential* half of that: resolving ``--connect <name|url>`` against
``remote_gateways`` in config, logging in once with a password, and persisting **only the
cookie set** under ``~/.hermes/remote/<name>/cookies.json`` (0600). The password is never
written anywhere, and never reaches argv — the TUI child is handed the cookie file's *path*.

Why a file rather than an env-carried cookie value: the serve rotates the session cookies
whenever the access token needs refreshing (``dashboard_auth/middleware.py:186-196``), and
the TUI must persist that rotation for its next reconnect and its next launch. An env var is
write-once from the child's perspective; a 0600 file is the only shape that survives both.

The WS ticket itself is *not* minted here. It has a 30s TTL and must be fresh on every dial,
including reconnects, so ``ui-tui/src/remoteAuth.ts`` mints one per dial from the cookie jar.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpx

from hermes_constants import get_hermes_home

#: Cookie jars hold a live session; owner-only, like an ssh private key.
COOKIE_FILE_MODE = 0o600
REMOTE_DIR_MODE = 0o700
#: Login and identity probes are interactive-path calls; fail fast rather than hang the launch.
HTTP_TIMEOUT_SECONDS = 15.0
#: Non-interactive password source. Tests and CI only — an interactive run uses ``getpass``.
PASSWORD_ENV = "HERMES_REMOTE_PASSWORD"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_JAR_VERSION = 1


class RemoteAttachError(RuntimeError):
    """Bad ``--connect`` target, unreachable serve, or rejected credentials."""


class RemoteAuthExpired(RemoteAttachError):
    """The stored cookie set is no longer accepted; the user must re-run with ``--login``."""


@dataclass(frozen=True)
class RemoteTarget:
    """One remote serve: where to dial, who to dial as, and what to call it."""

    name: str
    base_url: str
    username: str = ""


# --- Target resolution ----------------------------------------------------


def _normalize_base_url(raw: str) -> str:
    """Validate a serve origin and return it without a trailing slash.

    Deliberately strict: an origin only. A path, query, fragment or embedded ``user:pass@``
    either means the user mistyped or means a credential is about to be persisted into a
    config file, and both deserve an error rather than a best-effort reinterpretation.
    """
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise RemoteAttachError(f"Remote gateway URL must be http:// or https://, got {raw!r}.")
    if not parts.hostname:
        raise RemoteAttachError(f"Remote gateway URL has no host: {raw!r}")
    if parts.username is not None or parts.password is not None:
        raise RemoteAttachError(
            "Remote gateway URL must not embed credentials; use remote_gateways.<name>.username "
            "and sign in interactively.")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise RemoteAttachError(
            f"Remote gateway URL must be a bare origin (scheme://host:port), got {raw!r}")
    return f"{parts.scheme}://{parts.netloc}"


def _slugify(value: str) -> str:
    """Filesystem-safe jar directory name. Never traverses: ``.``/``..`` become ``_``."""
    slug = _SAFE_NAME_RE.sub("_", value.strip()).strip("._-")
    return slug or "remote"


def resolve_target(connect: str, config: Optional[Dict[str, Any]] = None) -> RemoteTarget:
    """Resolve ``--connect <name|url>`` to a :class:`RemoteTarget`.

    A value containing ``://`` is taken as a literal origin (its jar name is derived from
    host+port). Anything else is a key under ``remote_gateways`` in ``~/.hermes/config.yaml``.
    """
    value = (connect or "").strip()
    if not value:
        raise RemoteAttachError("--connect needs a remote name or URL.")

    if "://" in value:
        base_url = _normalize_base_url(value)
        return RemoteTarget(name=_slugify(urlsplit(base_url).netloc), base_url=base_url)

    if config is None:
        from hermes_cli.config import load_config

        config = load_config()
    gateways = config.get("remote_gateways") or {}
    if not isinstance(gateways, dict) or value not in gateways:
        known = ", ".join(sorted(k for k in gateways if isinstance(gateways, dict))) or "(none)"
        raise RemoteAttachError(
            f"Unknown remote gateway {value!r}. Configured: {known}. "
            "Add one under `remote_gateways:` in ~/.hermes/config.yaml, or pass a full URL.")
    entry = gateways[value]
    if not isinstance(entry, dict) or not str(entry.get("url") or "").strip():
        raise RemoteAttachError(f"remote_gateways.{value} must be a mapping with a `url` key.")
    return RemoteTarget(
        name=_slugify(value),
        base_url=_normalize_base_url(str(entry["url"]).strip()),
        username=str(entry.get("username") or "").strip())


# --- Cookie jar -----------------------------------------------------------


def remote_dir(name: str, *, home: Optional[Path] = None) -> Path:
    base = Path(home if home is not None else get_hermes_home())
    return base / "remote" / _slugify(name)


def cookie_path(name: str, *, home: Optional[Path] = None) -> Path:
    return remote_dir(name, home=home) / "cookies.json"


def load_jar(path: Path) -> Dict[str, Any]:
    """Read a cookie jar, or an empty one. A corrupt jar is *not* fatal — it means re-login."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": _JAR_VERSION, "cookies": {}, "username": "", "url": ""}
    if not isinstance(data, dict):
        return {"version": _JAR_VERSION, "cookies": {}, "username": "", "url": ""}
    cookies = data.get("cookies")
    data["cookies"] = {str(k): str(v) for k, v in cookies.items()} if isinstance(cookies, dict) else {}
    data.setdefault("version", _JAR_VERSION)
    data.setdefault("username", "")
    data.setdefault("url", "")
    return data


def save_jar(path: Path, jar: Dict[str, Any]) -> Path:
    """Persist a jar 0600, atomically.

    The temp file is created inside the destination directory with ``mkstemp`` (0600 by
    construction) so the secret is never briefly world-readable and the ``replace`` is a
    same-filesystem rename.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, REMOTE_DIR_MODE)
    fd, tmp = tempfile.mkstemp(prefix=".cookies-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({**jar, "version": _JAR_VERSION}, handle, indent=2, sort_keys=True)
        os.chmod(tmp, COOKIE_FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.chmod(path, COOKIE_FILE_MODE)
    return path


# --- Login / session --------------------------------------------------------


def _client(base_url: str, cookies: Optional[Dict[str, str]] = None) -> httpx.Client:
    """An origin-pinned client. ``trust_env=False``: a corporate ``HTTPS_PROXY`` must not get
    to MITM a tailnet handshake, and redirects are never followed onto another host."""
    return httpx.Client(
        base_url=base_url, cookies=cookies or {}, timeout=HTTP_TIMEOUT_SECONDS,
        trust_env=False, follow_redirects=False)


def discover_password_provider(client: httpx.Client) -> str:
    """Name of the first password-capable auth provider the serve advertises."""
    try:
        resp = client.get("/api/auth/providers")
    except httpx.HTTPError as exc:
        raise RemoteAttachError(f"Cannot reach the remote serve: {exc}") from None
    if resp.status_code != 200:
        raise RemoteAttachError(
            f"Remote serve did not list auth providers (HTTP {resp.status_code}). "
            "Is it running with authentication configured?")
    try:
        payload = resp.json()
    except ValueError:
        raise RemoteAttachError("Remote serve returned a non-JSON provider list.") from None
    entries = payload.get("providers") if isinstance(payload, dict) else payload
    for entry in entries or ():
        if isinstance(entry, dict) and entry.get("supports_password") and entry.get("name"):
            return str(entry["name"])
    raise RemoteAttachError(
        "Remote serve advertises no password provider; interactive --connect login needs one.")


def _jar_cookies(client: httpx.Client) -> Dict[str, str]:
    """Flatten the client's cookie jar to ``name -> value``.

    Cookies we seed the client with carry no domain; cookies the serve sets carry one. After a
    rotation both exist under the same name, and ``client.cookies.items()`` raises
    ``CookieConflict`` on exactly that. The domain-bearing entry is the one the serve just
    issued, so it wins over the seed it replaces.
    """
    resolved: Dict[str, str] = {}
    from_server: set = set()
    for cookie in client.cookies.jar:
        if cookie.name in from_server and not cookie.domain:
            continue
        resolved[cookie.name] = cookie.value or ""
        if cookie.domain:
            from_server.add(cookie.name)
    return resolved


def login(target: RemoteTarget, username: str, password: str) -> Dict[str, str]:
    """Exchange username/password for the serve's session cookie set.

    Returns the cookie name→value mapping. The password is not retained, logged, or returned.
    """
    with _client(target.base_url) as client:
        provider = discover_password_provider(client)
        try:
            resp = client.post(
                "/auth/password-login",
                json={"provider": provider, "username": username, "password": password})
        except httpx.HTTPError as exc:
            raise RemoteAttachError(f"Login request failed: {exc}") from None
        if resp.status_code == 401:
            raise RemoteAuthExpired("Invalid credentials for the remote serve.")
        if resp.status_code == 429:
            raise RemoteAttachError("Too many login attempts; wait a moment and retry.")
        if resp.status_code != 200:
            raise RemoteAttachError(
                f"Remote login failed (HTTP {resp.status_code}).")
        cookies = _jar_cookies(client)
        if not cookies:
            raise RemoteAttachError(
                "Remote login succeeded but set no session cookies; the serve may be configured "
                "for native-broker sign-in only.")
        return cookies


def probe_session(target: RemoteTarget, cookies: Dict[str, str]) -> Optional[Dict[str, str]]:
    """Return the (possibly rotated) cookie set if the stored session is still live, else None.

    A network failure is *not* a rejection: it raises, so a flaky link never silently discards a
    good cookie jar and re-prompts for a password.
    """
    if not cookies:
        return None
    with _client(target.base_url, cookies) as client:
        try:
            resp = client.get("/api/auth/me")
        except httpx.HTTPError as exc:
            raise RemoteAttachError(f"Cannot reach the remote serve: {exc}") from None
        if resp.status_code == 401:
            return None
        if resp.status_code != 200:
            raise RemoteAttachError(
                f"Remote serve rejected the identity probe (HTTP {resp.status_code}).")
        return _jar_cookies(client)


def _read_password(target: RemoteTarget, username: str) -> str:
    """Password from the env escape hatch, else an interactive prompt. Never from argv."""
    env_value = os.environ.get(PASSWORD_ENV, "")
    if env_value:
        return env_value
    import getpass
    import sys

    if not sys.stdin.isatty():
        raise RemoteAttachError(
            f"No stored session for {target.name!r} and no TTY to prompt on. "
            f"Set {PASSWORD_ENV} or run `hermes --connect {target.name} --login` interactively.")
    return getpass.getpass(f"Password for {username}@{target.name}: ")


def _read_username(target: RemoteTarget, jar: Dict[str, Any]) -> str:
    username = target.username or str(jar.get("username") or "")
    if username:
        return username
    import sys

    if not sys.stdin.isatty():
        raise RemoteAttachError(
            f"No username for {target.name!r}. Set remote_gateways.{target.name}.username "
            "in ~/.hermes/config.yaml.")
    entered = input(f"Username for {target.name}: ").strip()
    if not entered:
        raise RemoteAttachError("A username is required.")
    return entered


def ensure_session(
    target: RemoteTarget, *, force_login: bool = False, home: Optional[Path] = None) -> Path:
    """Guarantee a live cookie jar on disk for ``target`` and return its path.

    Reuses the stored session when the serve still accepts it; prompts only on a real 401 or an
    explicit ``--login``.
    """
    path = cookie_path(target.name, home=home)
    jar = load_jar(path)

    if not force_login:
        cookies = probe_session(target, jar.get("cookies") or {})
        if cookies:
            save_jar(path, {**jar, "cookies": cookies, "url": target.base_url})
            return path

    username = _read_username(target, jar)
    password = _read_password(target, username)
    cookies = login(target, username, password)
    del password
    save_jar(path, {"cookies": cookies, "username": username, "url": target.base_url})
    return path
