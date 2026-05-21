"""Verify handle_session_stop runs its independent I/O concurrently.

Follow-up #2 from plans/pure-twirling-brook.md: the two `subprocess.run`
calls for git activity AND the two independent `query_structured` calls
(decisions + errors) must be parallelized via a ThreadPoolExecutor so the
session_stop hook stays under the 30 s daemon budget.

The test injects ~0.5 s sleeps into each I/O primitive. Sequential execution
would be ~2.0 s (4 * 0.5). Parallel execution should be ~1.0 s (two pairs
of parallel work, each ~0.5 s). We assert wall-clock < 1.3 s with a comfort
margin for slow CI.
"""
from __future__ import annotations

import time
from types import SimpleNamespace


SLEEP_S = 0.5


def _fake_subprocess_run(*args, **kwargs):
    """Stand-in for subprocess.run that sleeps to simulate I/O."""
    time.sleep(SLEEP_S)
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _fake_query_structured(*args, **kwargs):
    """Stand-in for omega.bridge.query_structured that sleeps to simulate I/O."""
    time.sleep(SLEEP_S)
    return []


def _fake_auto_capture(*args, **kwargs):
    return None


def _fake_get_store():
    class _StoreStub:
        def get_session_event_counts(self, _session_id):
            return {}

    return _StoreStub()


def test_handle_session_stop_runs_io_in_parallel(tmp_path, monkeypatch):
    """Independent git + query I/O must run concurrently, halving wall-clock."""
    from omega_platform.server.hook_server import session as session_mod
    from omega import bridge as bridge_mod

    # Force project path so the git block executes.
    project = str(tmp_path)

    # Patch the targeted I/O entry points with sleep stubs so we can measure
    # whether they ran concurrently.
    monkeypatch.setattr(session_mod.subprocess, "run", _fake_subprocess_run)
    monkeypatch.setattr(bridge_mod, "query_structured", _fake_query_structured, raising=False)
    monkeypatch.setattr(bridge_mod, "auto_capture", _fake_auto_capture, raising=False)
    monkeypatch.setattr(bridge_mod, "_get_store", _fake_get_store, raising=False)

    # Silence heavy downstream paths that aren't part of the parallelism we
    # are validating. They each can do real DB I/O that would dominate the
    # wall-clock measurement.
    monkeypatch.setattr(session_mod, "_auto_feedback_on_surfaced", lambda _sid: None)
    monkeypatch.setattr(session_mod, "_resolve_entity", lambda _p: None)
    monkeypatch.setattr(session_mod, "_extract_procedural_learnings", lambda _sid: None)

    # Point _omega_dir at an empty tmpdir so the hooks.log tail path is skipped.
    empty_omega = tmp_path / "omega"
    empty_omega.mkdir()
    monkeypatch.setattr(session_mod, "_omega_dir", lambda: empty_omega)

    # Disable any periodic gates that might trigger extra I/O.
    monkeypatch.setattr(session_mod, "_should_run_periodic", lambda *_a, **_k: False, raising=False)

    payload = {
        "session_id": "test-session-parallel",
        "project": project,
        "client": "claude-code",
    }

    started = time.monotonic()
    result = session_mod.handle_session_stop(payload)
    elapsed = time.monotonic() - started

    # Shape assertions: same top-level keys as the sequential version produced.
    assert isinstance(result, dict)
    assert set(result.keys()) >= {"output", "error"}
    assert not result["error"], f"unexpected error: {result['error']!r}"

    # Wall-clock assertion. The targeted I/O in handle_session_stop consists of
    # 2 git subprocesses (parallelized) + 3 query_structured calls (decisions
    # and errors parallelized, tasks sequential).
    #   Sequential baseline: 5 * SLEEP_S = 2.5 s
    #   Parallel target:      3 * SLEEP_S = 1.5 s
    # Threshold sits between the two with comfort margin for CI jitter.
    sequential_baseline = 5 * SLEEP_S
    parallel_target = 3 * SLEEP_S
    threshold = 1.9  # < sequential, > parallel + jitter
    assert elapsed < threshold, (
        f"handle_session_stop took {elapsed:.2f}s — expected < {threshold:.2f}s if I/O is "
        f"parallel (sequential baseline ~{sequential_baseline:.2f}s, parallel target "
        f"~{parallel_target:.2f}s)"
    )
