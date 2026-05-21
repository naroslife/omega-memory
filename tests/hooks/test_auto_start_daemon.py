"""Tests for hook daemon auto-start from the fast_hook client side (W2-B).

Covers three layers of the auto-start path added in
``src/omega/hooks/_fallback_bridge.py`` and wired into
``src/omega/hooks/fast_hook.py``:

a) ``test_auto_start_spawn_signature`` — pure unit: with subprocess.Popen
   monkeypatched and the socket probe stubbed to "appear after one poll",
   ``_auto_start_hook_daemon()`` invokes Popen exactly once with
   ``start_new_session=True`` and the right ``-m`` target.

b) ``test_fast_hook_connect_failure_triggers_auto_start`` — integration of
   the wrapper in ``fast_hook.main()``'s connect-retry loop: the first
   ``delegate`` call raises ``FileNotFoundError``; ``_auto_start_hook_daemon``
   is invoked exactly once (stubbed True); the retry succeeds and the result
   is consumed.

c) ``test_auto_start_no_double_spawn_with_live_daemon`` — race / real-PID
   integration: spawn the real ``omega_platform.server.hook_daemon`` once
   into an isolated HOME, then call ``_auto_start_hook_daemon()`` from the
   test. It must detect the live daemon via the PID lock + socket probe and
   return True *without* spawning a second daemon.

All on-disk state is redirected to ``tmp_path`` via HOME monkeypatching so the
real ``~/.omega/`` is never touched.
"""

from __future__ import annotations

import importlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _worktree_src_root() -> Path:
    """``src/`` directory of this worktree — pinned for subprocess PYTHONPATH."""
    return Path(__file__).resolve().parents[2] / "src"


def _force_kill(proc: subprocess.Popen) -> None:
    """Best-effort teardown helper — always safe to call."""
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=3)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME to ``tmp_path`` so ``~/.omega/`` paths are sandboxed.

    Reloads ``omega.socket_path`` so its module-level ``_OMEGA_DIR`` picks up
    the new HOME — otherwise ``_hook_daemon_paths()`` would resolve to the
    real home for the duration of the test process.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".omega").mkdir(parents=True, exist_ok=True)
    import omega.socket_path as _sp
    importlib.reload(_sp)
    yield tmp_path
    # Restore module to its on-disk state for subsequent tests.
    importlib.reload(_sp)


@pytest.fixture
def spawned_daemons() -> Iterator[list[subprocess.Popen]]:
    """Track spawned daemons and SIGTERM survivors during teardown."""
    procs: list[subprocess.Popen] = []
    yield procs
    for p in procs:
        _force_kill(p)


# ---------------------------------------------------------------------------
# (a) Unit: spawn-signature test
# ---------------------------------------------------------------------------


@pytest.mark.unix
@pytest.mark.skipif(sys.platform == "win32", reason="hook daemon is UDS-only")
def test_auto_start_spawn_signature(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_auto_start_hook_daemon`` must Popen exactly once with the right args."""
    from omega.hooks import _fallback_bridge as fb
    importlib.reload(fb)  # pick up monkeypatched HOME via reload

    popen_calls: list[tuple[list[str], dict]] = []

    def fake_popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        mock = MagicMock()
        mock.pid = 99999
        return mock

    monkeypatch.setattr(fb.subprocess, "Popen", fake_popen)

    # Simulate "socket appears on the second poll" by having the probe
    # return False once and True thereafter.
    probe_calls = {"n": 0}

    def fake_probe(sock_path):
        probe_calls["n"] += 1
        return probe_calls["n"] >= 2

    monkeypatch.setattr(fb, "_probe_hook_socket", fake_probe)
    # Ensure the "already alive" fast-path is bypassed.
    monkeypatch.setattr(fb, "_is_hook_daemon_alive", lambda _p: False)

    started = fb._auto_start_hook_daemon()

    assert started is True
    assert len(popen_calls) == 1, f"expected exactly one Popen call, got {len(popen_calls)}"
    args, kwargs = popen_calls[0]
    # First arg is the python executable, then the -m flag, then the module path.
    assert args[1:] == ["-m", "omega_platform.server.hook_daemon"], (
        f"unexpected Popen argv: {args!r}"
    )
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("stdout") is fb.subprocess.DEVNULL


# ---------------------------------------------------------------------------
# (b) Integration: connect-failure → auto-start → retry-success
# ---------------------------------------------------------------------------


@pytest.mark.unix
@pytest.mark.skipif(sys.platform == "win32", reason="hook daemon is UDS-only")
def test_fast_hook_connect_failure_triggers_auto_start(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``delegate`` raises FileNotFoundError, auto-start runs and the retry succeeds."""
    from omega.hooks import _fallback_bridge as fb
    from omega.hooks import fast_hook
    importlib.reload(fb)
    importlib.reload(fast_hook)

    # Track auto-start invocations — stub returns True so no real spawn happens.
    auto_start_calls = {"n": 0}

    def fake_auto_start() -> bool:
        auto_start_calls["n"] += 1
        return True

    monkeypatch.setattr(fb, "_auto_start_hook_daemon", fake_auto_start)
    # The wrapper imports via ``from ._fallback_bridge import …`` — bind there too.
    monkeypatch.setattr(
        "omega.hooks._fallback_bridge._auto_start_hook_daemon",
        fake_auto_start,
    )

    # delegate: 1st call raises FileNotFoundError, 2nd returns a successful result.
    delegate_calls = {"n": 0}
    success_result = {"output": "", "exit_code": 0}

    def fake_delegate(hook_names, payload, timeout=5.0):
        delegate_calls["n"] += 1
        if delegate_calls["n"] == 1:
            raise FileNotFoundError("simulated missing socket")
        return success_result

    monkeypatch.setattr(fast_hook, "delegate", fake_delegate)
    monkeypatch.setattr(fast_hook, "_is_socket_stale", lambda _p: False)
    # No-op the health marker so it doesn't touch the real fs.
    monkeypatch.setattr(fast_hook, "_check_daemon_health_marker", lambda: None)
    # Avoid side-effects in _log_timing / _parse_payload by stubbing payload.
    monkeypatch.setattr(fast_hook, "_parse_payload", lambda: {"session_id": "t"})

    # Drive ``main()`` with a single hook name.
    monkeypatch.setattr(sys, "argv", ["fast_hook.py", "session_start"])

    # ``main()`` may call ``sys.exit`` for non-zero results, but with
    # ``exit_code=0`` it returns normally.
    fast_hook.main()

    assert auto_start_calls["n"] == 1, (
        f"auto-start should fire exactly once, got {auto_start_calls['n']}"
    )
    assert delegate_calls["n"] == 2, (
        f"delegate should be called twice (initial + retry), got {delegate_calls['n']}"
    )


# ---------------------------------------------------------------------------
# (c) Race: live daemon under tmp HOME → no duplicate spawn
# ---------------------------------------------------------------------------


def _spawn_real_daemon(home: Path) -> subprocess.Popen:
    """Spawn the real hook daemon under an isolated HOME (mirrors lifecycle test)."""
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["OMEGA_HOOK_DAEMON_IDLE_TIMEOUT_S"] = "60"
    env.setdefault("OMEGA_HOOK_DAEMON_LOG_LEVEL", "WARNING")
    src_root = str(_worktree_src_root())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_root + (os.pathsep + existing if existing else "")
    return subprocess.Popen(
        [sys.executable, "-m", "omega_platform.server.hook_daemon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _wait_for_socket(sock_path: Path, *, timeout: float) -> bool:
    """Poll until the daemon's socket exists and accepts a connect."""
    import socket as _socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect(str(sock_path))
                s.close()
                return True
            except OSError:
                pass
        time.sleep(0.05)
    return False


@pytest.mark.unix
@pytest.mark.slow
@pytest.mark.skipif(sys.platform == "win32", reason="hook daemon is UDS-only")
def test_auto_start_no_double_spawn_with_live_daemon(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawned_daemons: list[subprocess.Popen],
) -> None:
    """A live daemon under HOME must short-circuit auto-start — no second spawn."""
    from omega.hooks import _fallback_bridge as fb
    importlib.reload(fb)

    # Bring up the real daemon once.
    proc = _spawn_real_daemon(isolated_home)
    spawned_daemons.append(proc)

    sock_path, pid_path, _log = fb._hook_daemon_paths()
    assert _wait_for_socket(sock_path, timeout=5.0), (
        f"daemon never came up; stderr={proc.stderr.read(2000) if proc.stderr else b''!r}"
    )
    assert pid_path.exists(), "PID file missing while daemon should be alive"

    # Trip-wire: any Popen call from inside _auto_start_hook_daemon is a bug.
    popen_calls: list[tuple] = []
    real_popen = fb.subprocess.Popen

    def trip_popen(*args, **kwargs):  # pragma: no cover - asserted via len()
        popen_calls.append((args, kwargs))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(fb.subprocess, "Popen", trip_popen)

    t0 = time.monotonic()
    started = fb._auto_start_hook_daemon()
    elapsed = time.monotonic() - t0

    assert started is True, "auto-start should detect the live daemon and return True"
    assert popen_calls == [], (
        f"auto-start spawned a second daemon while one was live: {popen_calls!r}"
    )
    # Should be near-instant since it hits the fast-path (alive + probe ok).
    assert elapsed < 1.0, f"fast-path detection took too long ({elapsed:.2f}s)"

    # Teardown via SIGTERM.
    proc.send_signal(signal.SIGTERM)
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
        pytest.fail("daemon did not exit within 5s of SIGTERM")
    assert rc == 0, f"unexpected exit rc={rc}"
