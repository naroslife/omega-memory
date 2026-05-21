"""Lifecycle, PID-lock, and idle-shutdown coverage for the standalone hook daemon.

These tests spawn ``python -m omega_platform.server.hook_daemon`` as a real
subprocess (using a temp ``HOME`` to isolate ``~/.omega/`` state) and verify:

  a) ``test_daemon_lifecycle_smoke``      - daemon boots, serves a hook
                                             request, exits cleanly on SIGTERM,
                                             and removes its PID file.
  b) ``test_daemon_pid_lock_contention``  - a second daemon with the same HOME
                                             exits 0 due to PID-lock contention.
  c) ``test_daemon_idle_shutdown``        - daemon self-exits within the idle
                                             window when no traffic arrives.

The daemon's socket path / PID path resolve from ``Path.home()`` at import
time, so overriding ``HOME`` via the subprocess env is sufficient to redirect
all on-disk state into ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Per-test wall-clock ceiling; tests use subprocess.wait(timeout=...) for
# fine-grained guards. This is a coarse safety net since pytest-timeout is
# not a project dependency.
_PROC_HARD_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _worktree_src_root() -> Path:
    """Return this worktree's ``src/`` directory.

    The Python env may be editable-installed from a *different* checkout
    (typical when running tests from a worktree). We force the subprocess to
    pick up the worktree's ``src/`` via ``PYTHONPATH`` so it imports the
    in-progress ``omega_platform.server.hook_daemon`` package under test.
    """
    return Path(__file__).resolve().parents[2] / "src"


def _spawn_daemon(home: Path, *, idle_timeout: str) -> subprocess.Popen:
    """Spawn the hook daemon with an isolated HOME and given idle timeout."""
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["OMEGA_HOOK_DAEMON_IDLE_TIMEOUT_S"] = idle_timeout
    # Reduce log noise on stderr unless debugging.
    env.setdefault("OMEGA_HOOK_DAEMON_LOG_LEVEL", "WARNING")
    # Prepend worktree src to PYTHONPATH so the subprocess imports the daemon
    # package from this checkout rather than whatever editable install the
    # venv happens to point at.
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


def _resolve_socket_under_home(home: Path) -> Path:
    """Resolve the daemon's UDS path with HOME monkeypatched into the env.

    ``omega.socket_path.resolve_hook_socket_path`` reads ``Path.home()`` (and
    optionally walks ``/proc``), so we temporarily set ``HOME`` and clear any
    cached module state. We import lazily and call the resolver fresh.
    """
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        # Force re-resolution: socket_path computes _OMEGA_DIR at module-import
        # time, but resolve_hook_socket_path() itself uses Path.home() each
        # call for the *.omega* base via _OMEGA_DIR — to be safe we reload.
        import importlib

        import omega.socket_path as _sp

        importlib.reload(_sp)
        return _sp.resolve_hook_socket_path()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def _wait_for_socket(sock_path: Path, *, timeout: float) -> bool:
    """Poll until ``sock_path`` exists and accepts a connect()."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect(str(sock_path))
                s.close()
                return True
            except OSError:
                pass
        time.sleep(0.05)
    return False


def _send_hook_request(sock_path: Path, payload: dict, *, timeout: float = 5.0) -> bytes:
    """Send a single JSON hook request and read the full response until EOF."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock_path))
    s.sendall(json.dumps(payload).encode("utf-8"))
    s.shutdown(socket.SHUT_WR)
    buf = b""
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    except (ConnectionResetError, BrokenPipeError):
        pass
    s.close()
    return buf


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon_home(tmp_path: Path) -> Path:
    """Provide an isolated HOME so PID + socket files don't touch the real ~/.omega/."""
    (tmp_path / ".omega").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def spawned_daemons() -> list[subprocess.Popen]:
    """Track every spawned daemon and SIGTERM any survivor in teardown."""
    procs: list[subprocess.Popen] = []
    yield procs
    for p in procs:
        _force_kill(p)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unix
@pytest.mark.skipif(sys.platform == "win32", reason="hook daemon is UDS-only")
def test_daemon_lifecycle_smoke(daemon_home: Path, spawned_daemons: list[subprocess.Popen]) -> None:
    """Boot the daemon, send one hook request, then SIGTERM and assert clean exit."""
    proc = _spawn_daemon(daemon_home, idle_timeout="60")
    spawned_daemons.append(proc)

    sock_path = _resolve_socket_under_home(daemon_home)
    assert _wait_for_socket(sock_path, timeout=5.0), (
        f"socket {sock_path} never appeared; stderr={proc.stderr.read(2000) if proc.stderr else b''!r}"
    )

    # Send a minimal hook request. We don't care about content of the response —
    # the handler may fail because no DB is configured under this temp HOME —
    # we only care that the daemon responds with JSON instead of crashing.
    raw = _send_hook_request(
        sock_path,
        {"hook": "session_start", "session_id": "test-lifecycle", "tool_input": {}},
        timeout=10.0,
    )
    assert raw, "daemon returned empty payload"
    response = json.loads(raw.decode("utf-8"))
    assert isinstance(response, dict)
    # Response must look like a hook reply envelope.
    assert any(k in response for k in ("output", "error", "exit_code")), (
        f"unexpected response shape: {response!r}"
    )

    # Process still alive; PID file present.
    assert proc.poll() is None, "daemon exited prematurely"
    pid_file = daemon_home / ".omega" / "hook-daemon.pid"
    assert pid_file.exists(), "PID file missing while daemon is running"

    # Graceful shutdown.
    proc.send_signal(signal.SIGTERM)
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
        pytest.fail("daemon did not exit within 5s of SIGTERM")

    assert rc == 0, f"unexpected exit code {rc}"
    assert not pid_file.exists(), "PID file should be removed on clean shutdown"


@pytest.mark.unix
@pytest.mark.skipif(sys.platform == "win32", reason="hook daemon is UDS-only")
def test_daemon_pid_lock_contention(
    daemon_home: Path, spawned_daemons: list[subprocess.Popen]
) -> None:
    """A second daemon under the same HOME must exit 0 without taking over."""
    first = _spawn_daemon(daemon_home, idle_timeout="60")
    spawned_daemons.append(first)

    sock_path = _resolve_socket_under_home(daemon_home)
    assert _wait_for_socket(sock_path, timeout=5.0), "first daemon never came up"

    # Now spawn a contender; it should fail the flock and exit with rc 0.
    contender = _spawn_daemon(daemon_home, idle_timeout="60")
    spawned_daemons.append(contender)

    try:
        rc = contender.wait(timeout=5)
    except subprocess.TimeoutExpired:
        contender.kill()
        contender.wait(timeout=2)
        pytest.fail("contender daemon did not exit on PID-lock contention")

    assert rc == 0, f"contender exit rc={rc} (expected 0 per daemon.py:296)"

    # First daemon must still be alive and serving.
    assert first.poll() is None, "first daemon died while contender was running"
    assert sock_path.exists(), "socket disappeared while first daemon was alive"

    # Teardown via SIGTERM on the survivor.
    first.send_signal(signal.SIGTERM)
    try:
        rc1 = first.wait(timeout=5)
    except subprocess.TimeoutExpired:
        first.kill()
        first.wait(timeout=2)
        pytest.fail("first daemon did not exit cleanly on SIGTERM")
    assert rc1 == 0


@pytest.mark.unix
@pytest.mark.skipif(sys.platform == "win32", reason="hook daemon is UDS-only")
def test_daemon_idle_shutdown(daemon_home: Path, spawned_daemons: list[subprocess.Popen]) -> None:
    """With idle_timeout=2s and no traffic, the daemon must self-exit."""
    proc = _spawn_daemon(daemon_home, idle_timeout="2")
    spawned_daemons.append(proc)

    sock_path = _resolve_socket_under_home(daemon_home)
    assert _wait_for_socket(sock_path, timeout=5.0), "daemon never came up"

    # No traffic. Wait for self-shutdown. Idle window is 2s; the watchdog
    # polls at ~max(1.0, idle/4)=1s, so worst-case exit is roughly 3-4s.
    try:
        rc = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
        pytest.fail("daemon did not idle-shutdown within 15s (expected ~3-4s)")

    assert rc == 0, f"idle shutdown exit code = {rc}"
    pid_file = daemon_home / ".omega" / "hook-daemon.pid"
    assert not pid_file.exists(), "PID file lingered after idle-shutdown"
