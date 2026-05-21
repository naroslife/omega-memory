"""Per-instance hook socket path resolution.

Both the daemon (binding) and fast_hook (connecting) call resolve_hook_socket_path()
independently and must produce the same path. Scoping is by os.getppid() so that
each Claude Code instance owns its own ~/.omega/hook-<ppid>.sock.

Falls back to the legacy ~/.omega/hook.sock when scoping is not possible
(e.g., Windows, where we use TCP; or PPID lookup fails). Legacy path keeps
existing single-instance setups working.
"""

import os
import sys
from pathlib import Path

_OMEGA_DIR = Path.home() / ".omega"
_LEGACY_SOCK = _OMEGA_DIR / "hook.sock"


def resolve_hook_socket_path() -> Path:
    """Return the per-instance hook socket path for this process.

    The scoping key is the parent PID. The daemon and fast_hook must both
    be direct children of Claude Code for this to align — verified by the
    project's existing assumption (see handlers.py's ``caller_pid = os.getppid()``
    for stdio transport).
    """
    if sys.platform == "win32":
        # Windows uses TCP loopback elsewhere; preserve existing behavior.
        return _LEGACY_SOCK
    # Allow explicit override (tests, advanced users).
    override = os.environ.get("OMEGA_HOOK_SOCK")
    if override:
        return Path(override)
    try:
        ppid = os.getppid()
        if ppid <= 1:
            # Orphaned (parent died); fall back to legacy single-socket mode.
            return _LEGACY_SOCK
        return _OMEGA_DIR / f"hook-{ppid}.sock"
    except OSError:
        return _LEGACY_SOCK


def sweep_stale_sockets(max_age_days: int = 2) -> int:
    """Remove ``~/.omega/hook-*.sock`` files older than ``max_age_days`` (default 2).

    Returns count removed. Safe to call from any context; swallows errors.
    """
    import time

    removed = 0
    cutoff = time.time() - (max_age_days * 86400)
    try:
        for p in _OMEGA_DIR.glob("hook-*.sock"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed
