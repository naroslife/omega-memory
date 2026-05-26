"""PID-staleness suppression for daemon-health.json degradation marker.

Follow-up #7: ``~/.omega/daemon-health.json`` is written by the hook daemon
when it detects internal degradation (handler leak, exception, saturation).
``fast_hook._check_daemon_health_marker`` reads it on every dispatch and
fires the daemon-down banner if ``degraded_at`` is within 5 minutes.

Bug: if the daemon (whose PID is in the marker) has died or restarted, the
marker stays on disk and keeps firing the banner — false positive. We fix
this by probing the recorded PID via ``os.kill(pid, 0)`` and suppressing
the banner when the PID is gone.

Tests:
- a) fresh marker + live PID  → banner fires
- b) fresh marker + dead PID  → banner suppressed, debug log emitted
- c) stale marker (old ts)    → suppressed regardless of PID (regression)
- d) missing PID field        → falls through to freshness-only (no PID suppression)
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def fast_hook_module(tmp_path, monkeypatch):
    """Import fast_hook fresh per test with HOME redirected to ``tmp_path``.

    ``_check_daemon_health_marker`` reads ``Path.home() / .omega / daemon-health.json``
    every call, so monkeypatching HOME is sufficient for both fresh imports
    and module reuse.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".omega").mkdir(parents=True, exist_ok=True)

    src_root = Path(__file__).resolve().parents[2] / "src"
    if str(src_root) not in sys.path:
        monkeypatch.syspath_prepend(str(src_root))

    if "omega.hooks.fast_hook" in sys.modules:
        module = importlib.reload(sys.modules["omega.hooks.fast_hook"])
    else:
        module = importlib.import_module("omega.hooks.fast_hook")
    return module


def _write_marker(tmp_path: Path, *, degraded_at: str, pid: object = ...) -> Path:
    """Write a daemon-health.json under ``tmp_path/.omega/``.

    ``pid=...`` (Ellipsis) means omit the field entirely; any other value is
    written as-is.
    """
    marker = tmp_path / ".omega" / "daemon-health.json"
    payload: dict = {"degraded_at": degraded_at, "reason": "test"}
    if pid is not ...:
        payload["pid"] = pid
    marker.write_text(json.dumps(payload))
    return marker


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _old_iso() -> str:
    """A timestamp far enough in the past to be outside the 5-min window."""
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# a) Fresh marker, live PID -> banner fires
# ---------------------------------------------------------------------------


def test_fresh_marker_with_live_pid_fires_banner(fast_hook_module, tmp_path):
    """Marker fresh + recorded PID is the current process (always alive)."""
    _write_marker(tmp_path, degraded_at=_now_iso(), pid=os.getpid())

    with patch.object(fast_hook_module, "_emit_daemon_down_notice") as emit:
        fast_hook_module._check_daemon_health_marker()

    assert emit.call_count == 1, (
        "Live-PID marker must fire the banner; got "
        f"{emit.call_count} calls."
    )
    assert emit.call_args.args[0] == "<degraded>"


# ---------------------------------------------------------------------------
# b) Fresh marker, dead PID -> suppressed + debug log
# ---------------------------------------------------------------------------


def test_fresh_marker_with_dead_pid_suppresses_banner(fast_hook_module, tmp_path, caplog):
    """Marker fresh but recorded PID is gone -> suppress banner, log staleness."""
    # 999_999_999 is well above /proc/sys/kernel/pid_max on Linux (4_194_304
    # by default) -- guaranteed not to exist.
    dead_pid = 999_999_999
    _write_marker(tmp_path, degraded_at=_now_iso(), pid=dead_pid)

    with caplog.at_level(logging.DEBUG, logger="omega.hooks.fast_hook"):
        with patch.object(fast_hook_module, "_emit_daemon_down_notice") as emit:
            fast_hook_module._check_daemon_health_marker()

    assert emit.call_count == 0, (
        "Dead-PID marker must be suppressed; got "
        f"{emit.call_count} banner emissions."
    )
    assert any(str(dead_pid) in rec.getMessage() for rec in caplog.records), (
        "Expected a debug log mentioning the stale PID; got: "
        f"{[rec.getMessage() for rec in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# c) Stale marker (old degraded_at) -> suppressed by existing freshness check
# ---------------------------------------------------------------------------


def test_old_degraded_at_suppresses_regardless_of_pid(fast_hook_module, tmp_path):
    """Regression guard: pre-existing freshness check still suppresses old markers."""
    # Even with a live PID, an old timestamp must not fire (window = 5 min).
    _write_marker(tmp_path, degraded_at=_old_iso(), pid=os.getpid())

    with patch.object(fast_hook_module, "_emit_daemon_down_notice") as emit:
        fast_hook_module._check_daemon_health_marker()

    assert emit.call_count == 0, (
        "Old marker (outside 5-min window) must be suppressed by the existing "
        f"freshness check; got {emit.call_count} calls."
    )


# ---------------------------------------------------------------------------
# d) Missing PID field -> falls through to freshness-only (no PID-based suppression)
# ---------------------------------------------------------------------------


def test_missing_pid_field_falls_through(fast_hook_module, tmp_path):
    """No PID in marker -> behaviour matches the original freshness-only logic."""
    _write_marker(tmp_path, degraded_at=_now_iso(), pid=...)  # omit

    with patch.object(fast_hook_module, "_emit_daemon_down_notice") as emit:
        fast_hook_module._check_daemon_health_marker()

    assert emit.call_count == 1, (
        "Marker without 'pid' field must fall through to the existing "
        f"freshness check; got {emit.call_count} calls."
    )


def test_non_int_pid_field_falls_through(fast_hook_module, tmp_path):
    """Spec: non-int PID also falls through to freshness-only behaviour."""
    _write_marker(tmp_path, degraded_at=_now_iso(), pid="not-an-int")

    with patch.object(fast_hook_module, "_emit_daemon_down_notice") as emit:
        fast_hook_module._check_daemon_health_marker()

    assert emit.call_count == 1
