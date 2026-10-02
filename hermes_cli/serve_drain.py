"""Graceful drain of in-flight desktop/TUI turns when ``hermes serve`` is asked to stop.

``hermes serve`` runs every desktop/TUI turn in-process, so a stop used to cut each live turn at
once (uvicorn closed the sockets with 1012, the exit-flush handler hard-stopped the turn). The
messaging gateway drains instead; this mirrors its two shapes (gateway/restart.py):

* **SIGTERM** (``launchctl kickstart -k``, ``hermes update``, systemd stop): refuse new turns and
  wait for the running ones, bounded by what the supervisor allows. Under launchd that is the
  live ``ExitTimeOut`` minus the cleanup reserve (``resolve_launchd_capped_drain``) applied to
  ``agent.restart_after_turn_timeout``; launchd SIGKILLs at ``ExitTimeOut`` and the gui domain
  clamps it to 60s, so a SIGTERM drain can never be long. Without launchd the budget is the
  operator's ``agent.restart_drain_timeout`` (default 0 = legacy immediate stop), because a
  systemd ``TimeoutStopSec`` this process cannot read would otherwise SIGKILL mid-drain.
* **SIGUSR1** (in-band restart, same signal the gateway uses): refuse new turns, wait up to
  ``agent.restart_after_turn_timeout`` (default 1800s) for running turns, then exit cleanly; the
  supervisor's KeepAlive / Restart= relaunches. No supervisor kill window applies, so the long
  budget is safe.

A second SIGTERM while draining stops waiting and shuts down at once. Turns still running when
the budget ends are stopped by the existing exit path and attributed ``serve_shutdown``; they are
NOT resumed after the restart. Clients announce the cut turn from the WS 1012 close.
"""

from __future__ import annotations

import logging
import signal
import threading
import time

logger = logging.getLogger(__name__)

_POLL_S = 0.25
SERVE_SHUTDOWN_ISSUER = "serve_shutdown"


def _after_turn_timeout() -> float:
    from gateway.restart import parse_restart_after_turn_timeout
    from hermes_cli.gateway import _agent_timeout_setting
    return float(_agent_timeout_setting(
        "HERMES_RESTART_AFTER_TURN_TIMEOUT", "restart_after_turn_timeout", parse_restart_after_turn_timeout))


def _stop_drain_timeout() -> float:
    from hermes_cli.gateway import _get_restart_drain_timeout
    return float(_get_restart_drain_timeout())


def resolve_sigterm_drain_budget(launchd_exit_timeout_s: float | None) -> float:
    """SIGTERM drain budget: launchd-capped after-turn wait, else the operator's stop drain."""
    try:
        if launchd_exit_timeout_s is not None:
            from gateway.restart import resolve_launchd_capped_drain
            return max(0.0, resolve_launchd_capped_drain(_after_turn_timeout(), launchd_exit_timeout_s))
        return max(0.0, _stop_drain_timeout())
    except Exception:
        logger.debug("serve drain budget unavailable; stopping immediately", exc_info=True)
        return 0.0


def _running_turns() -> list[str]:
    try:
        from tui_gateway.server import running_turn_sids
        return running_turn_sids()
    except Exception:
        logger.debug("running-turn probe failed", exc_info=True)
        return []


def _wait_for_turns(budget_s: float, stop_early: threading.Event) -> list[str]:
    """Poll until no turn runs, the budget ends, or ``stop_early`` is set; returns the stragglers."""
    deadline = time.monotonic() + budget_s
    while True:
        running = _running_turns()
        if not running or stop_early.is_set() or time.monotonic() >= deadline:
            return running
        stop_early.wait(min(_POLL_S, max(0.0, deadline - time.monotonic())))


def install_serve_drain(server) -> bool:
    """Wrap ``server.handle_exit`` (uvicorn installs it in ``capture_signals``) with the SIGTERM
    drain and register the SIGUSR1 in-band restart. Main thread only (``signal.signal``); call
    after ``install_exit_flush_signal_handlers`` and before ``server.capture_signals()``. Also
    names this process's exit stops ``serve_shutdown``. Returns False when not installed."""
    if threading.current_thread() is not threading.main_thread():
        return False
    from hermes_cli.backend_retirement import drain
    try:
        from tui_gateway.server import set_serve_exit_issuer
        set_serve_exit_issuer(SERVE_SHUTDOWN_ISSUER)
    except Exception:
        logger.debug("serve exit issuer not set", exc_info=True)

    launchd_exit_timeout_s = None
    try:  # launchctl print is a subprocess: read once at boot, never inside a signal handler
        from gateway.restart import read_launchd_exit_timeout_s
        launchd_exit_timeout_s = read_launchd_exit_timeout_s()
    except Exception:
        logger.debug("launchd exit timeout unavailable", exc_info=True)
    sigterm_budget = resolve_sigterm_drain_budget(launchd_exit_timeout_s)
    original_handle_exit = server.handle_exit
    stop_early = threading.Event()
    state = {"draining": False}

    def _drain_then(budget_s: float, why: str, finish) -> None:
        running = _running_turns()
        drain.begin(why)
        logger.warning("serve %s: draining %d in-flight turn(s) for up to %.0fs before shutdown; "
                       "new turns are refused", why, len(running), budget_s)

        def _run() -> None:
            started = time.monotonic()
            left = []
            try:
                left = _wait_for_turns(budget_s, stop_early)
            finally:
                logger.warning("serve %s: drain ended after %.1fs; %d turn(s) still running will be stopped "
                               "(serve_shutdown) and NOT resumed; clients announce the cut turn from the 1012 close",
                               why, time.monotonic() - started, len(left))
                finish()

        threading.Thread(target=_run, name="hermes-serve-drain", daemon=True).start()

    def handle_exit(sig, frame) -> None:
        if state["draining"]:
            stop_early.set()  # second signal: stop waiting; the drain thread hands off to shutdown
            return
        if sig != signal.SIGTERM or sigterm_budget <= 0 or not _running_turns():
            original_handle_exit(sig, frame)
            return
        state["draining"] = True
        _drain_then(sigterm_budget, "SIGTERM", lambda: original_handle_exit(sig, None))

    def handle_restart(sig, frame) -> None:
        if state["draining"]:
            stop_early.set()
            return
        state["draining"] = True

        def _exit_cleanly() -> None:
            # No captured signal: main_loop returns, shutdown runs, the process exits 0 and the
            # supervisor (launchd KeepAlive / systemd Restart=) relaunches it.
            server.should_exit = True

        try:
            budget = _after_turn_timeout()
        except Exception:
            budget = 0.0
        _drain_then(max(0.0, budget), "SIGUSR1 restart", _exit_cleanly)

    server.handle_exit = handle_exit
    usr1 = getattr(signal, "SIGUSR1", None)
    if usr1 is not None:
        try:
            signal.signal(usr1, handle_restart)
        except (ValueError, OSError, RuntimeError):
            logger.debug("SIGUSR1 restart handler not installed", exc_info=True)
    logger.info("serve drain installed: SIGTERM budget %.0fs (launchd exit timeout %s), SIGUSR1 in-band restart",
                sigterm_budget, launchd_exit_timeout_s)
    return True
