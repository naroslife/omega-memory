"""Hook socket path resolution.

Both the daemon (binding) and ``fast_hook`` (connecting) call
``resolve_hook_socket_path()`` independently and must agree on the path.

History: the original scheme keyed off ``os.getppid()`` and assumed both
the MCP server and ``fast_hook`` were direct children of Claude Code. In
production this assumption fails — ``fast_hook`` is spawned via an
intermediate shell/wrapper, so its parent PID does not match the MCP
server's parent PID and the per-instance socket cannot be found. PPID has
been dropped as a scoping key.

The current scheme keys off Claude Code's ``CLAUDE_PROJECT_DIR`` env var,
which is process-tree-independent and inherited by every descendant of
the Claude Code process for the lifetime of the session. Different
projects naturally get different sockets; same-project multi-window
instances fall through to a session-scoped or legacy discovery symlink
written by the daemon.
"""

import hashlib
import os
import sys
from pathlib import Path

_OMEGA_DIR = Path.home() / ".omega"
_LEGACY_SOCK = _OMEGA_DIR / "hook.sock"

# Session-id chars allowed when constructing the session-scoped socket name.
# Session ids come from Claude Code's hook payload — treat as external input.
_SAFE_SESSION_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


def _sanitize_session_id(sid: str) -> str | None:
    """Return ``sid`` unchanged if every char is in ``[A-Za-z0-9_-]``, else None.

    Rejects empty strings. The caller should fall through to the next
    resolution layer when this returns None.
    """
    if not sid:
        return None
    for ch in sid:
        if ch not in _SAFE_SESSION_CHARS:
            return None
    return sid


def _project_scoped_path() -> Path | None:
    """Return ``~/.omega/hook-project-<hash>.sock`` when ``CLAUDE_PROJECT_DIR``
    is set in the environment, else ``None``.

    The hash is the first 16 hex chars of SHA-256(``CLAUDE_PROJECT_DIR``).
    16 hex chars = 64 bits → negligible collision probability for the
    handful of projects a user typically works on, while keeping the
    socket filename short. Computed identically by daemon and client so no
    coordination is required between the two.
    """
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project:
        return None
    digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:16]
    return _OMEGA_DIR / f"hook-project-{digest}.sock"


def resolve_hook_socket_path() -> Path:
    """Return the hook socket path for this process.

    Resolution order (first matching layer wins):

    1. ``OMEGA_HOOK_SOCK`` environment override (tests, advanced users).
    2. **Primary** — project-scoped socket
       ``~/.omega/hook-project-<sha256(CLAUDE_PROJECT_DIR)[:16]>.sock``.
       Claude Code exports ``CLAUDE_PROJECT_DIR`` into the environment of
       all its descendants (MCP server, hooks, and any wrappers in between).
       The daemon binds here on startup; ``fast_hook`` connects here. No
       symlink needed — both sides independently compute the same path.
       Returned even if the file does not yet exist so the daemon can
       create it on bind.
    3. Session-scoped symlink ``~/.omega/hook-session-<SESSION_ID>.sock``.
       Daemon-written discovery pointer for the rare same-project
       multi-Claude-Code scenario. ``fast_hook`` exports ``SESSION_ID``
       from the hook stdin payload. Only used when the symlink exists.
    4. Legacy discovery socket ``~/.omega/hook.sock`` — written by the
       daemon at startup as a symlink to its actual bound socket. Final
       fallback for non-Claude invocations, older daemons, or
       environments where ``CLAUDE_PROJECT_DIR`` is unset.

    On Windows the daemon uses TCP loopback, so socket-path resolution is
    irrelevant — we return the legacy path purely for compatibility with
    callers that ``str()`` the result.
    """
    if sys.platform == "win32":
        return _LEGACY_SOCK
    override = os.environ.get("OMEGA_HOOK_SOCK")
    if override:
        return Path(override)
    # Layer 2 (primary): project-scoped, deterministic from CLAUDE_PROJECT_DIR.
    project_path = _project_scoped_path()
    if project_path is not None:
        return project_path
    # Layer 3: session-scoped discovery symlink (same-project multi-instance).
    sid_raw = os.environ.get("SESSION_ID", "")
    sid = _sanitize_session_id(sid_raw)
    if sid:
        sess_candidate = _OMEGA_DIR / f"hook-session-{sid}.sock"
        if sess_candidate.exists():
            return sess_candidate
    # Layer 4: legacy discovery symlink (written by daemon on start).
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
