"""Regression tests for ``pid_registry.kill_orphaned_servers``.

Guards the cross-instance process-kill bug: a LIVE MCP server belonging to
another Claude instance was reaped because the old code keyed off a bare
``ppid==1`` heuristic. The fix requires BOTH a provably-dead recorded parent
AND a cmdline that marks the target as an OMEGA MCP server before SIGTERM.

Hermetic: no real processes are killed and no MCP server is spawned. We
monkeypatch ``os.kill``, the liveness/starttime helpers, and the ``/proc``
cmdline marker check on the ``pid_registry`` module.
"""

import json
import signal

import pytest

from omega_platform.server import pid_registry


@pytest.fixture
def pid_dir(tmp_path, monkeypatch):
    """Point the registry at a temp pid dir and force a non-win32 path."""
    d = tmp_path / "mcp_pids"
    d.mkdir()
    monkeypatch.setattr(pid_registry, "_PID_DIR", d)
    monkeypatch.setattr(pid_registry.sys, "platform", "linux")
    # Stable, unrelated self-pid so no pid file is ever skipped as "me".
    monkeypatch.setattr(pid_registry.os, "getpid", lambda: 999999)
    return d


def _write_pid_file(pid_dir, pid, *, parent_pid, parent_starttime=None, transport="stdio"):
    data = {
        "pid": pid,
        "started_at": "2026-05-27T01:40:00+00:00",
        "parent_pid": parent_pid,
        "transport": transport,
    }
    if parent_starttime is not None:
        data["parent_starttime"] = parent_starttime
    (pid_dir / f"{pid}.pid").write_text(json.dumps(data))


def _patch_world(monkeypatch, *, alive_pids, starttimes, mcp_pids, killed_sink):
    """Wire up the monkeypatched process world.

    alive_pids: set of PIDs that ``_pid_alive`` reports as live.
    starttimes: pid -> current starttime returned by ``_read_starttime``.
    mcp_pids:   set of PIDs whose cmdline marks them an OMEGA MCP server.
    killed_sink: list collecting (pid, sig) tuples passed to os.kill.
    """
    monkeypatch.setattr(pid_registry, "_pid_alive", lambda pid: pid in alive_pids)
    monkeypatch.setattr(pid_registry, "_read_starttime", lambda pid: starttimes.get(pid, 0))
    monkeypatch.setattr(pid_registry, "_is_omega_mcp_server", lambda pid: pid in mcp_pids)

    def fake_kill(pid, sig):
        if sig == 0:
            # liveness probe — honor the alive set
            if pid not in alive_pids:
                raise ProcessLookupError(pid)
            return
        killed_sink.append((pid, sig))

    monkeypatch.setattr(pid_registry.os, "kill", fake_kill)


def test_orphaned_mcp_server_is_sigtermed_and_pidfile_removed(pid_dir, monkeypatch):
    """Dead parent + genuine MCP cmdline → SIGTERM and pid file removed."""
    server_pid = 3607
    dead_parent = 3266
    _write_pid_file(pid_dir, server_pid, parent_pid=dead_parent, parent_starttime=111)

    killed = []
    _patch_world(
        monkeypatch,
        alive_pids={server_pid},          # server live, parent dead
        starttimes={server_pid: 222},
        mcp_pids={server_pid},            # cmdline marks it an MCP server
        killed_sink=killed,
    )

    n = pid_registry.kill_orphaned_servers()

    assert n == 1
    assert killed == [(server_pid, signal.SIGTERM)]
    assert not (pid_dir / f"{server_pid}.pid").exists()


def test_live_parent_server_not_killed_pidfile_kept(pid_dir, monkeypatch):
    """REGRESSION GUARD: parent still alive → NOT signalled, pid file kept.

    This is the actual cross-instance bug: a live sibling instance's MCP
    server must never be reaped just because of a ppid heuristic.
    """
    server_pid = 3607
    live_parent = 15933  # another live Claude instance
    _write_pid_file(pid_dir, server_pid, parent_pid=live_parent, parent_starttime=500)

    killed = []
    _patch_world(
        monkeypatch,
        alive_pids={server_pid, live_parent},
        starttimes={server_pid: 222, live_parent: 500},  # parent starttime matches
        mcp_pids={server_pid},
        killed_sink=killed,
    )

    n = pid_registry.kill_orphaned_servers()

    assert n == 0
    assert killed == []
    assert (pid_dir / f"{server_pid}.pid").exists()


def test_non_mcp_target_refused_even_if_parent_dead(pid_dir, monkeypatch):
    """Parent dead but target cmdline lacks MCP marker → never signalled."""
    target_pid = 4242
    dead_parent = 1000
    _write_pid_file(pid_dir, target_pid, parent_pid=dead_parent, parent_starttime=111)

    killed = []
    _patch_world(
        monkeypatch,
        alive_pids={target_pid},   # target alive, parent dead
        starttimes={target_pid: 222},
        mcp_pids=set(),            # NOT an omega MCP server
        killed_sink=killed,
    )

    n = pid_registry.kill_orphaned_servers()

    assert n == 0
    assert killed == []
    # pid file kept — we only remove files for confirmed-dead PIDs.
    assert (pid_dir / f"{target_pid}.pid").exists()


def test_recycled_parent_pid_treated_as_orphan(pid_dir, monkeypatch):
    """Parent PID alive but starttime differs (recycled) → orphan → SIGTERM."""
    server_pid = 3607
    parent_pid = 3266
    _write_pid_file(pid_dir, server_pid, parent_pid=parent_pid, parent_starttime=111)

    killed = []
    _patch_world(
        monkeypatch,
        alive_pids={server_pid, parent_pid},
        starttimes={server_pid: 222, parent_pid: 888},  # recorded was 111 → recycled
        mcp_pids={server_pid},
        killed_sink=killed,
    )

    n = pid_registry.kill_orphaned_servers()

    assert n == 1
    assert killed == [(server_pid, signal.SIGTERM)]


def test_old_pidfile_without_parent_starttime_kept_if_parent_alive(pid_dir, monkeypatch):
    """Backward compat: old pid file (no parent_starttime), parent alive → kept."""
    server_pid = 3607
    live_parent = 15933
    _write_pid_file(pid_dir, server_pid, parent_pid=live_parent)  # no parent_starttime

    killed = []
    _patch_world(
        monkeypatch,
        alive_pids={server_pid, live_parent},
        starttimes={server_pid: 222, live_parent: 500},
        mcp_pids={server_pid},
        killed_sink=killed,
    )

    n = pid_registry.kill_orphaned_servers()

    assert n == 0
    assert killed == []
    assert (pid_dir / f"{server_pid}.pid").exists()


def test_dead_server_pidfile_cleaned_no_kill(pid_dir, monkeypatch):
    """A server whose PID is already dead → pid file removed, no signal sent."""
    server_pid = 7777
    _write_pid_file(pid_dir, server_pid, parent_pid=1, parent_starttime=111)

    killed = []
    _patch_world(
        monkeypatch,
        alive_pids=set(),       # server itself is dead
        starttimes={},
        mcp_pids={server_pid},
        killed_sink=killed,
    )

    n = pid_registry.kill_orphaned_servers()

    assert n == 0
    assert killed == []
    assert not (pid_dir / f"{server_pid}.pid").exists()


def test_http_transport_never_killed(pid_dir, monkeypatch):
    """HTTP daemon (ppid=1 by design) is skipped even with a dead parent."""
    server_pid = 8888
    _write_pid_file(pid_dir, server_pid, parent_pid=1, parent_starttime=0, transport="http")

    killed = []
    _patch_world(
        monkeypatch,
        alive_pids={server_pid},
        starttimes={server_pid: 222},
        mcp_pids={server_pid},
        killed_sink=killed,
    )

    n = pid_registry.kill_orphaned_servers()

    assert n == 0
    assert killed == []
    assert (pid_dir / f"{server_pid}.pid").exists()


def test_only_sigterm_used_never_sigkill(pid_dir, monkeypatch):
    """Assert the signal sent is SIGTERM, never SIGKILL."""
    server_pid = 3607
    _write_pid_file(pid_dir, server_pid, parent_pid=3266, parent_starttime=111)

    killed = []
    _patch_world(
        monkeypatch,
        alive_pids={server_pid},
        starttimes={server_pid: 222},
        mcp_pids={server_pid},
        killed_sink=killed,
    )

    pid_registry.kill_orphaned_servers()

    assert killed, "expected a signal to be sent"
    for _pid, sig in killed:
        assert sig == signal.SIGTERM
        assert sig != signal.SIGKILL
