"""Tests for omega.cli._probe_hook_socket — live hook-daemon liveness probe.

Follow-up #6 from the doctor-live-probe plan: `omega doctor` historically
reported "MCP Server registered" based purely on ~/.claude.json config
presence. This produced false positives whenever the daemon was dead or
stuck. The probe added in cli._probe_hook_socket performs a short UNIX
connect to the resolved hook socket and reports three distinct outcomes:

    * OK   — socket accepting connections
    * NOT FOUND — no socket file (daemon down)
    * BOUND but not accepting — socket file exists, no listener (stale/stuck)
"""
from __future__ import annotations

import socket
import threading

from omega import cli


# ---------------------------------------------------------------------------
# Helpers


class _CapturingReporter:
    """Collects ok/fail/warn calls so we can assert on them."""

    def __init__(self) -> None:
        self.ok_msgs: list[str] = []
        self.fail_msgs: list[str] = []
        self.warn_msgs: list[str] = []

    def ok(self, msg: str) -> None:
        self.ok_msgs.append(msg)

    def fail(self, msg: str) -> None:
        self.fail_msgs.append(msg)

    def warn(self, msg: str) -> None:
        self.warn_msgs.append(msg)


def _start_listener(sock_path) -> tuple[socket.socket, threading.Event]:
    """Bind+listen on a UNIX socket; return (server_sock, stop_event)."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(4)
    server.settimeout(1.0)
    stop = threading.Event()

    def _accept_loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except (socket.timeout, OSError):
                continue
            try:
                conn.close()
            except OSError:
                pass

    t = threading.Thread(target=_accept_loop, daemon=True)
    t.start()
    return server, stop


# ---------------------------------------------------------------------------
# Tests


def test_probe_reports_not_found_when_socket_missing(tmp_path, monkeypatch):
    """No socket file => NOT FOUND (daemon down)."""
    sock_path = tmp_path / "missing.sock"
    assert not sock_path.exists()

    monkeypatch.setattr(
        "omega.socket_path.resolve_hook_socket_path",
        lambda: sock_path,
    )

    r = _CapturingReporter()
    cli._probe_hook_socket(r.ok, r.fail, r.warn)

    assert r.ok_msgs == []
    assert r.warn_msgs == []
    assert len(r.fail_msgs) == 1
    msg = r.fail_msgs[0]
    assert "hook socket" in msg
    assert "NOT FOUND" in msg
    assert str(sock_path) in msg


def test_probe_reports_ok_when_socket_accepting(tmp_path, monkeypatch):
    """Listener bound and accept()ing => OK."""
    sock_path = tmp_path / "live.sock"
    server, stop = _start_listener(sock_path)
    try:
        monkeypatch.setattr(
            "omega.socket_path.resolve_hook_socket_path",
            lambda: sock_path,
        )

        r = _CapturingReporter()
        cli._probe_hook_socket(r.ok, r.fail, r.warn)

        assert r.fail_msgs == []
        assert r.warn_msgs == []
        assert len(r.ok_msgs) == 1
        msg = r.ok_msgs[0]
        assert "hook socket" in msg
        assert "OK" in msg
        assert "accepting" in msg
        assert str(sock_path) in msg
    finally:
        stop.set()
        try:
            server.close()
        except OSError:
            pass


def test_probe_reports_stale_when_socket_bound_but_no_listener(tmp_path, monkeypatch):
    """Socket file present but nobody is accept()ing => BOUND but not accepting."""
    sock_path = tmp_path / "stale.sock"
    # Touch the file so .exists() is True; do NOT bind+listen. A regular file
    # at the path cannot be connected to via AF_UNIX/SOCK_STREAM and the OS
    # returns ConnectionRefusedError, which is exactly the "stale" signal.
    sock_path.touch()
    assert sock_path.exists()

    monkeypatch.setattr(
        "omega.socket_path.resolve_hook_socket_path",
        lambda: sock_path,
    )

    r = _CapturingReporter()
    cli._probe_hook_socket(r.ok, r.fail, r.warn)

    assert r.ok_msgs == []
    assert r.warn_msgs == []
    assert len(r.fail_msgs) == 1
    msg = r.fail_msgs[0]
    assert "hook socket" in msg
    # Either "BOUND but not accepting" or "connect failed" is acceptable —
    # both convey the stale/stuck distinction from NOT FOUND.
    assert "NOT FOUND" not in msg
    assert (
        "BOUND but not accepting" in msg
        or "connect failed" in msg
        or "stuck" in msg
    )
    assert str(sock_path) in msg
