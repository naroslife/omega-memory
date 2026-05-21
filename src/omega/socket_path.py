"""Hook socket path resolution.

Both the daemon (binding) and ``fast_hook`` (connecting) call
``resolve_hook_socket_path()`` independently and must agree on the path.

History: the original scheme keyed off ``os.getppid()`` and assumed both
the MCP server and ``fast_hook`` were direct children of Claude Code. In
production this assumption fails — ``fast_hook`` is spawned via an
intermediate shell/wrapper, so its parent PID does not match the MCP
server's parent PID and the per-instance socket cannot be found. PPID has
been dropped as a scoping key.

Current scheme keys off the **outermost Claude Code ancestor** discovered
by walking ``/proc`` (Linux/WSL). This produces a stable identifier per
Claude Code instance even when two windows share a project directory,
and recognizes user wrapper scripts. The (pid, starttime) pair from
``/proc/<pid>/stat`` resists PID recycling.

When ``/proc`` is unavailable or no ancestor matches a known Claude Code
binary name, we fall back to a ``CLAUDE_PROJECT_DIR``-derived hash, then
to a session-id symlink, then to the legacy discovery symlink.
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

# Default argv[0] basenames recognized as a Claude Code process. Override
# via ``OMEGA_CLAUDE_PROC_NAMES`` (comma-separated).
_DEFAULT_CLAUDE_NAMES = ("claude", "claude-code", "claude-cli")

# Maximum depth when walking the parent chain in /proc — guards against
# pathological loops if /proc/<pid>/stat is unreadable or PPIDs cycle.
_PROC_WALK_MAX_DEPTH = 64


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


def _claude_proc_names() -> tuple[str, ...]:
    """Return the set of argv[0] basenames recognized as Claude Code.

    Reads ``OMEGA_CLAUDE_PROC_NAMES`` (comma-separated) if set; otherwise
    returns the built-in defaults. Empty/whitespace entries are discarded.
    """
    env = os.environ.get("OMEGA_CLAUDE_PROC_NAMES")
    if env:
        names = tuple(n.strip() for n in env.split(",") if n.strip())
        if names:
            return names
    return _DEFAULT_CLAUDE_NAMES


def _read_proc_field(pid: int, fname: str) -> str | None:
    """Read ``/proc/<pid>/<fname>`` and return as decoded text, or None on error."""
    try:
        with open(f"/proc/{pid}/{fname}", "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _is_claude_proc(pid: int) -> bool:
    """Return True if ``/proc/<pid>/cmdline`` argv[0] basename looks like Claude Code.

    Matches against the configured allow-list. When the user has overridden
    ``OMEGA_CLAUDE_PROC_NAMES`` we honor only that list (no loose prefix
    match) so users can scope detection tightly. With the default list we
    also accept any ``claude-*`` basename as a courtesy.
    """
    cmdline = _read_proc_field(pid, "cmdline")
    if not cmdline:
        return False
    argv0 = cmdline.split("\0", 1)[0]
    if not argv0:
        return False
    name = os.path.basename(argv0)
    allow = _claude_proc_names()
    if name in allow:
        return True
    # Loose prefix match only when using the built-in defaults.
    if allow is _DEFAULT_CLAUDE_NAMES or allow == _DEFAULT_CLAUDE_NAMES:
        if name == "claude" or name.startswith("claude-"):
            return True
    return False


def _parse_stat_fields_after_comm(pid: int) -> list[str] | None:
    """Return the whitespace-split fields of ``/proc/<pid>/stat`` after the comm.

    The ``comm`` field is wrapped in parens and may itself contain spaces
    and parens, so we split on the LAST ``)`` to be safe. The returned
    list starts at field 3 of stat (``state``), i.e. ``fields[0]`` is the
    state char, ``fields[1]`` is ppid, ``fields[19]`` is starttime.
    """
    stat = _read_proc_field(pid, "stat")
    if not stat:
        return None
    rparen = stat.rfind(")")
    if rparen < 0:
        return None
    return stat[rparen + 2 :].split()  # skip ") "


def _read_ppid(pid: int) -> int:
    """Return parent PID from ``/proc/<pid>/stat``, or 0 on error."""
    fields = _parse_stat_fields_after_comm(pid)
    if not fields:
        return 0
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return 0


def _read_starttime(pid: int) -> int:
    """Return starttime (jiffies since boot) from ``/proc/<pid>/stat``, or 0 on error."""
    fields = _parse_stat_fields_after_comm(pid)
    if not fields:
        return 0
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return 0


def find_claude_instance_pid() -> int | None:
    """Return the PID of the outermost Claude Code ancestor, or None.

    Walks ``/proc/<pid>/stat`` upward from ``os.getpid()``, collecting
    every ancestor whose argv[0] basename matches the configured Claude
    Code name list. Returns the OUTERMOST match (closest to PID 1) so
    that, when a user runs Claude through a wrapper script that the user
    has named ``claude-code``, the wrapper PID anchors the socket — even
    if the wrapper itself re-executes the real ``claude`` binary in a
    child process.

    Returns None when ``/proc`` is absent (Windows, some containers) or
    when no ancestor matches.
    """
    if sys.platform == "win32" or not Path("/proc").is_dir():
        return None
    pid = os.getpid()
    matches: list[int] = []
    seen: set[int] = set()
    for _ in range(_PROC_WALK_MAX_DEPTH):
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        if _is_claude_proc(pid):
            matches.append(pid)
        ppid = _read_ppid(pid)
        if ppid == pid or ppid == 0:
            break
        pid = ppid
    return matches[-1] if matches else None


def _claude_instance_key() -> str | None:
    """Return ``"<pid>-<starttime>"`` for the outermost Claude Code ancestor.

    ``starttime`` (field 22 of ``/proc/<pid>/stat``, in clock ticks since
    boot) makes the key resilient to PID recycling: even if a later
    unrelated process happens to grab the same PID, its starttime will
    differ, yielding a fresh socket path. Returns None if no Claude Code
    ancestor was found or starttime couldn't be parsed.
    """
    pid = find_claude_instance_pid()
    if pid is None:
        return None
    starttime = _read_starttime(pid)
    if starttime == 0:
        return None
    return f"{pid}-{starttime}"


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
    2. **Primary** — outermost-Claude-ancestor scoped socket
       ``~/.omega/hook-claude-<pid>-<starttime>.sock``. Computed by
       walking ``/proc`` to find the topmost ancestor whose argv[0]
       basename matches a known Claude Code name (``claude``,
       ``claude-code``, ``claude-cli`` by default; configurable via
       ``OMEGA_CLAUDE_PROC_NAMES``). Both daemon and ``fast_hook`` walk
       independently and arrive at the same outermost PID, so no symlink
       is required. Distinguishes multiple Claude Code windows opened in
       the same project directory.
    3. Project-scoped fallback
       ``~/.omega/hook-project-<sha256(CLAUDE_PROJECT_DIR)[:16]>.sock``.
       Used when ``/proc`` is unavailable (Windows, isolated containers)
       or no ancestor matches a known Claude name.
    4. Session-scoped symlink ``~/.omega/hook-session-<SESSION_ID>.sock``.
       Daemon-written discovery pointer. The session id is sourced from
       ``CLAUDE_CODE_SESSION_ID`` (preferred — Claude Code sets this
       directly) or ``SESSION_ID`` (legacy, derived by ``fast_hook`` from
       the hook stdin payload). Only used when the symlink exists.
    5. Legacy discovery socket ``~/.omega/hook.sock`` — symlink written
       by the daemon at startup. Final fallback for non-Claude
       invocations or environments where no other scoping key applies.

    On Windows the daemon uses TCP loopback, so socket-path resolution is
    irrelevant — we return the legacy path purely for compatibility with
    callers that ``str()`` the result.
    """
    if sys.platform == "win32":
        return _LEGACY_SOCK
    override = os.environ.get("OMEGA_HOOK_SOCK")
    if override:
        return Path(override)
    # Layer 2 (primary): outermost-Claude-ancestor scoped via /proc walk.
    instance_key = _claude_instance_key()
    if instance_key is not None:
        return _OMEGA_DIR / f"hook-claude-{instance_key}.sock"
    # Layer 3: project-scoped fallback (no /proc, or no claude ancestor).
    project_path = _project_scoped_path()
    if project_path is not None:
        return project_path
    # Layer 4: session-scoped discovery symlink. Prefer Claude Code's
    # native CLAUDE_CODE_SESSION_ID over the legacy SESSION_ID derived
    # from fast_hook's stdin parsing.
    sid_raw = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("SESSION_ID", "")
    sid = _sanitize_session_id(sid_raw)
    if sid:
        sess_candidate = _OMEGA_DIR / f"hook-session-{sid}.sock"
        if sess_candidate.exists():
            return sess_candidate
    # Layer 5: legacy discovery symlink (written by daemon on start).
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
