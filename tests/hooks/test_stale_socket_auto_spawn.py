"""Regression tests for the W4 stale-socket auto-spawn fix.

Commit ``81b67c6`` ("fix(hooks,mcp): unblock daemon auto-spawn on stale
socket; bump RSS limit to 4 GB") restructured ``fast_hook.main()`` so that
stale-socket detection no longer bypasses the connect-retry loop.

Before the fix:

* ``_is_socket_stale()`` returning True set ``result = None`` and SKIPPED
  the retry loop entirely — meaning ``_auto_start_hook_daemon`` was never
  invoked, and the legacy ``_fallback()`` path ran instead.

After the fix:

* Stale detection still unlinks the dead socket file, but execution falls
  through to the retry loop. The very first ``delegate()`` call raises
  ``FileNotFoundError`` (the socket is gone), which triggers the
  ``_auto_start_hook_daemon`` branch — exactly the same path used when the
  socket was missing to begin with.

These three tests pin that behavior so a future refactor cannot silently
re-introduce the bypass bug.

The fixture / monkeypatch tactics here mirror
``tests/hooks/test_auto_start_daemon.py::test_fast_hook_connect_failure_triggers_auto_start``
(the W2-B integration test) — that pattern is the proven shape for driving
``fast_hook.main()`` under full stubs.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------


def _success_result() -> dict:
    """Minimal successful delegate response (single-hook shape)."""
    return {"output": "", "exit_code": 0}


def _install_common_stubs(monkeypatch: pytest.MonkeyPatch, fast_hook) -> None:
    """Stub side-effecty helpers so ``main()`` is driven by our mocks only."""
    monkeypatch.setattr(fast_hook, "_check_daemon_health_marker", lambda: None)
    monkeypatch.setattr(fast_hook, "_parse_payload", lambda: {"session_id": "t"})
    monkeypatch.setattr(fast_hook, "_log_timing", lambda *a, **k: None)
    monkeypatch.setattr(fast_hook, "_emit_daemon_down_notice", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# (a) Stale socket falls through to retry loop and triggers auto-spawn
# ---------------------------------------------------------------------------


@pytest.mark.unix
@pytest.mark.skipif(sys.platform == "win32", reason="hook daemon is UDS-only")
def test_stale_socket_falls_through_to_retry_loop_and_triggers_auto_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W4 regression: a stale socket must NOT bypass ``_auto_start_hook_daemon``.

    Pre-fix, ``_is_socket_stale -> True`` set ``result = None`` and skipped
    the retry loop. Post-fix, it falls through; the loop's
    ``FileNotFoundError`` handler then invokes auto-start exactly once.
    """
    import omega.hooks.fast_hook as fast_hook
    importlib.reload(fast_hook)

    # Stale socket: file exists, nothing listening → ConnectionRefusedError →
    # _is_socket_stale unlinks it and returns True. Use the SOCK_PATH symbol
    # used inside main() (string, not Path).
    sock_path = tmp_path / "stale.sock"
    sock_path.touch()
    monkeypatch.setattr(fast_hook, "SOCK_PATH", str(sock_path))

    auto_start_calls = {"n": 0}

    def fake_auto_start() -> bool:
        auto_start_calls["n"] += 1
        return True

    # Bind under both import paths the fast-path may resolve to.
    monkeypatch.setattr(
        "omega.hooks._fallback_bridge._auto_start_hook_daemon",
        fake_auto_start,
    )

    delegate_calls = {"n": 0}

    def fake_delegate(hook_names, payload, timeout=5.0):
        delegate_calls["n"] += 1
        if delegate_calls["n"] == 1:
            raise FileNotFoundError("simulated missing socket post-unlink")
        return _success_result()

    monkeypatch.setattr(fast_hook, "delegate", fake_delegate)
    _install_common_stubs(monkeypatch, fast_hook)

    monkeypatch.setattr(sys, "argv", ["fast_hook.py", "session_start"])

    fast_hook.main()  # exit_code=0 path → returns normally

    assert auto_start_calls["n"] == 1, (
        f"auto-start should fire exactly once after stale-socket fall-through, "
        f"got {auto_start_calls['n']}"
    )
    assert delegate_calls["n"] == 2, (
        f"delegate should be called twice (initial FNFE + post-spawn retry), "
        f"got {delegate_calls['n']}"
    )
    # _is_socket_stale should have unlinked the dead socket file.
    assert not sock_path.exists(), (
        "stale socket file should have been unlinked by _is_socket_stale"
    )


# ---------------------------------------------------------------------------
# (b) No-stale-socket path (existing W2-B behavior) still works
# ---------------------------------------------------------------------------


@pytest.mark.unix
@pytest.mark.skipif(sys.platform == "win32", reason="hook daemon is UDS-only")
def test_no_stale_socket_existing_w2b_path_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: W4 must not have broken the pre-existing W2-B path.

    Setup: socket file is simply missing (not stale). ``_is_socket_stale``
    returns False fast, the loop's first ``delegate`` raises
    ``FileNotFoundError``, auto-start fires once, retry succeeds.
    """
    import omega.hooks.fast_hook as fast_hook
    importlib.reload(fast_hook)

    sock_path = tmp_path / "missing.sock"  # never created
    monkeypatch.setattr(fast_hook, "SOCK_PATH", str(sock_path))

    auto_start_calls = {"n": 0}

    def fake_auto_start() -> bool:
        auto_start_calls["n"] += 1
        return True

    monkeypatch.setattr(
        "omega.hooks._fallback_bridge._auto_start_hook_daemon",
        fake_auto_start,
    )

    delegate_calls = {"n": 0}

    def fake_delegate(hook_names, payload, timeout=5.0):
        delegate_calls["n"] += 1
        if delegate_calls["n"] == 1:
            raise FileNotFoundError("no socket")
        return _success_result()

    monkeypatch.setattr(fast_hook, "delegate", fake_delegate)
    _install_common_stubs(monkeypatch, fast_hook)

    monkeypatch.setattr(sys, "argv", ["fast_hook.py", "session_start"])

    fast_hook.main()

    assert auto_start_calls["n"] == 1
    assert delegate_calls["n"] == 2


# ---------------------------------------------------------------------------
# (c) Stale socket + auto-start fails → legacy fallback bridge runs
# ---------------------------------------------------------------------------


@pytest.mark.unix
@pytest.mark.skipif(sys.platform == "win32", reason="hook daemon is UDS-only")
def test_stale_socket_auto_start_fails_falls_through_to_legacy_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If auto-spawn fails after a stale socket, the legacy ``_fallback`` runs.

    This is the safety net that keeps PreToolUse guards (e.g. pre_file_guard)
    enforceable even when no daemon can be brought up. Uses a blocking hook
    so the fallback branch is observable (purely-informational hooks skip
    fallback to avoid the stampede pathology).
    """
    import omega.hooks.fast_hook as fast_hook
    importlib.reload(fast_hook)

    sock_path = tmp_path / "stale.sock"
    sock_path.touch()
    monkeypatch.setattr(fast_hook, "SOCK_PATH", str(sock_path))

    auto_start_calls = {"n": 0}

    def fake_auto_start() -> bool:
        auto_start_calls["n"] += 1
        return False  # spawn failed

    monkeypatch.setattr(
        "omega.hooks._fallback_bridge._auto_start_hook_daemon",
        fake_auto_start,
    )

    delegate_calls = {"n": 0}

    def fake_delegate(hook_names, payload, timeout=5.0):
        delegate_calls["n"] += 1
        raise FileNotFoundError("no socket, no daemon")

    monkeypatch.setattr(fast_hook, "delegate", fake_delegate)
    _install_common_stubs(monkeypatch, fast_hook)

    # Record any _fallback invocation: the assertion target for the legacy path.
    fallback_calls: list[tuple[str, dict]] = []

    def fake_fallback(hook_name, payload):
        fallback_calls.append((hook_name, payload))

    monkeypatch.setattr(fast_hook, "_fallback", fake_fallback)

    # pre_file_guard is in _BLOCKING_HOOKS — its fallback is mandatory.
    monkeypatch.setattr(sys, "argv", ["fast_hook.py", "pre_file_guard"])

    fast_hook.main()

    assert auto_start_calls["n"] == 1, (
        f"auto-start should be attempted exactly once, got {auto_start_calls['n']}"
    )
    # delegate only runs once: first FNFE → auto-start (fails) → break.
    # (The retry-after-spawn block only executes when ``started`` is True.)
    assert delegate_calls["n"] == 1, (
        f"delegate should be called once when auto-start fails, got {delegate_calls['n']}"
    )
    assert fallback_calls and fallback_calls[0][0] == "pre_file_guard", (
        f"legacy _fallback should run for blocking hook, got {fallback_calls!r}"
    )
    assert sock_path.exists() is False, (
        "stale socket file should still have been unlinked"
    )
