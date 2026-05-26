"""Regression guards for Fix A — reaping orphaned hook daemons (W4-style).

The standalone hook daemon reaps daemons whose owning Claude ancestor has
exited so they stop contending for ``omega.db`` (the "database is locked" →
handler-timeout root cause). These tests are hermetic: they monkeypatch
``os.kill`` and ``omega.socket_path._read_starttime`` and operate on a tmp
``~/.omega`` so no real daemon is spawned and no real process is signalled.

Covered:
  a) ``test_reap_unlinks_dead_ancestor_socket`` — a socket whose encoded
     ancestor PID is dead is unlinked and its owner SIGTERMed; a live-ancestor
     socket is left untouched and its owner is never signalled.
  b) ``test_is_omega_hook_daemon_refuses_non_omega`` — ``_is_omega_hook_daemon``
     returns False for a PID whose cmdline is not an OMEGA hook daemon, and
     True only when cmdline contains both markers.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unix,
    pytest.mark.skipif(sys.platform == "win32", reason="hook daemon is UDS-only"),
]


def _make_socket(omega_dir: Path, pid: int, starttime: int) -> Path:
    """Create a fake ``hook-claude-<pid>-<starttime>.sock`` placeholder file."""
    p = omega_dir / f"hook-claude-{pid}-{starttime}.sock"
    p.write_text("")  # plain file stand-in; reaper only stats/unlinks it
    return p


def test_reap_unlinks_dead_ancestor_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omega_platform.server.hook_daemon import daemon as d

    omega_dir = tmp_path / ".omega"
    omega_dir.mkdir(parents=True)
    monkeypatch.setattr(d, "OMEGA_DIR", omega_dir)

    # Two sockets: one with a DEAD ancestor (pid 4001), one with a LIVE ancestor (pid 4002).
    dead_sock = _make_socket(omega_dir, 4001, 111)
    live_sock = _make_socket(omega_dir, 4002, 222)

    # Owner sidecars: each names the daemon PID that bound the socket.
    dead_owner = d._owner_pid_path_for(dead_sock)
    live_owner = d._owner_pid_path_for(live_sock)
    dead_owner.write_text("5001")  # orphaned daemon to be reaped
    live_owner.write_text("5002")  # healthy daemon — must survive

    # os.kill(pid, 0): liveness probe. Dead ancestor raises ProcessLookupError;
    # live ancestor returns. Record SIGTERMs so we can assert who got signalled.
    sigterms: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if pid == 4001:
                raise ProcessLookupError
            if pid == 4002:
                return  # alive
            raise ProcessLookupError
        # Real signal (SIGTERM expected — never SIGKILL).
        assert sig == signal.SIGTERM, f"reaper must use SIGTERM, got {sig}"
        sigterms.append(pid)

    monkeypatch.setattr(d.os, "kill", fake_kill)

    # Starttime check: live ancestor's starttime must MATCH the socket-encoded
    # one (222) so it is recognised as the genuine owner and skipped.
    monkeypatch.setattr(
        "omega.socket_path._read_starttime",
        lambda pid: 222 if pid == 4002 else 0,
    )

    # Owner-PID classification: 5001 (orphaned daemon) is a real omega hook
    # daemon; everything else is not (so only it may be SIGTERMed).
    monkeypatch.setattr(d, "_is_omega_hook_daemon", lambda pid: pid == 5001)

    # getpid() must not equal any owner so the != self guard never short-circuits.
    monkeypatch.setattr(d.os, "getpid", lambda: 99999)

    inst = d.HookDaemon()
    inst._reap_dead_ancestor_daemons()

    # Dead-ancestor daemon SIGTERMed exactly once; live one never touched.
    assert sigterms == [5001], f"expected only orphaned daemon 5001 reaped, got {sigterms}"

    # Dead socket + sidecar removed; live socket + sidecar untouched.
    assert not dead_sock.exists(), "dead-ancestor socket should be unlinked"
    assert not dead_owner.exists(), "dead-ancestor owner sidecar should be unlinked"
    assert live_sock.exists(), "live-ancestor socket must be left alone"
    assert live_owner.exists(), "live-ancestor owner sidecar must be left alone"


def test_is_omega_hook_daemon_refuses_non_omega(monkeypatch: pytest.MonkeyPatch) -> None:
    from omega_platform.server.hook_daemon import daemon as d

    # Build a fake /proc/<pid>/cmdline reader keyed by PID.
    cmdlines = {
        7001: "python\0-m\0omega_platform.server.hook_daemon\0",  # genuine daemon
        7002: "python\0-m\0some.other.thing\0",                   # unrelated process
        7003: "/usr/bin/vim\0notes.txt\0",                        # totally unrelated
        7004: "python\0-c\0import omega_platform; print(1)\0",    # omega but NOT hook_daemon
    }

    real_open = open

    def fake_open(path, *args, **kwargs):  # noqa: ANN001
        if isinstance(path, str) and path.startswith("/proc/") and path.endswith("/cmdline"):
            pid = int(path.split("/")[2])
            if pid not in cmdlines:
                raise FileNotFoundError(path)
            import io

            return io.BytesIO(cmdlines[pid].encode("utf-8"))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)

    # Only the PID whose cmdline contains BOTH markers is accepted.
    assert d._is_omega_hook_daemon(7001) is True
    assert d._is_omega_hook_daemon(7002) is False
    assert d._is_omega_hook_daemon(7003) is False
    assert d._is_omega_hook_daemon(7004) is False  # has "omega_platform" but no "hook_daemon"
    # A missing /proc entry (process already gone) is treated as not-a-daemon.
    assert d._is_omega_hook_daemon(9999) is False
