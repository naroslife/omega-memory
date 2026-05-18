#!/usr/bin/env python3
"""OMEGA PreToolUse hook — Guard checks BEFORE editing a file.

Fires before Edit|Write. Checks read-before-write discipline, project
constraints, and coordination file claims. Memory surfacing is handled
by the PostToolUse hook (surface_memories.py) to avoid duplicate queries.
"""
import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path


def _log_hook_error(hook_name, error):
    try:
        log_path = Path.home() / ".omega" / "hooks.log"
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        timestamp = datetime.now().isoformat(timespec="seconds")
        tb = traceback.format_exc()
        data = f"[{timestamp}] {hook_name}: {error}\n{tb}\n"
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def _log_timing(hook_name, elapsed_ms):
    try:
        log_path = Path.home() / ".omega" / "hooks.log"
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        timestamp = datetime.now().isoformat(timespec="seconds")
        data = f"[{timestamp}] {hook_name}: OK ({elapsed_ms:.0f}ms)\n"
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def _check_read_before_write(file_path, session_id):
    """Check if this file was read before attempting to edit it."""
    try:
        reads_dir = Path.home() / ".omega" / "session-reads"
        safe_id = session_id.replace("/", "_").replace("..", "_")[:64]
        reads_file = reads_dir / f"{safe_id}.json"

        if not reads_file.exists():
            return False

        read_paths = set(json.loads(reads_file.read_text()))
        return file_path in read_paths
    except Exception:
        # If we can't check, don't block — assume read
        return True


def _check_constraints(file_path, project):
    """Check per-project constraints for this file."""
    try:
        from omega.bridge import check_constraints
        return check_constraints(file_path, project)
    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("pre_edit_constraints", e)
    return []


def _check_claim(file_path, session_id):
    """Check if another agent has claimed this file."""
    try:
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()
        info = mgr.check_file(file_path)
        if info.get("claimed") and info.get("session_id") != session_id:
            return info
    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("pre_edit_check_claim", e)
    return None


def main():
    tool_name = os.environ.get("TOOL_NAME", "")
    if tool_name not in ("Edit", "Write"):
        return

    try:
        input_data = json.loads(os.environ.get("TOOL_INPUT", "{}"))
    except (json.JSONDecodeError, TypeError):
        return

    file_path = input_data.get("file_path", "")
    if not file_path:
        return

    session_id = os.environ.get("SESSION_ID", "")
    project = os.environ.get("PROJECT_DIR", os.getcwd())
    filename = os.path.basename(file_path)

    output_parts = []

    # Check read-before-write discipline
    if session_id and not _check_read_before_write(file_path, session_id):
        output_parts.append(
            f"  [READ-FIRST] {filename} was not read this session — read before editing to avoid blind changes"
        )

    # Check project constraints
    constraints = _check_constraints(file_path, project)
    for c in constraints:
        icon = "!!" if c.get("severity") == "error" else "?"
        output_parts.append(
            f"  [{icon} CONSTRAINT] {c.get('constraint', '')} (pattern: {c.get('pattern', '*')}, source: {c.get('source', '?')})"
        )

    # Check file claims from other agents
    claim = _check_claim(file_path, session_id)
    if claim:
        owner = claim.get("session_id", "unknown")[:12]
        task = claim.get("task", "")
        task_str = f" ({task})" if task else ""
        output_parts.append(
            f"  CLAIMED by session {owner}{task_str} — coordinate before editing"
        )

    # Note: memory and lesson surfacing is handled by the PostToolUse hook
    # (surface_memories.py) to avoid duplicate queries on the same file.

    if output_parts:
        print(f"\n[PRE-EDIT] {filename}:")
        for part in output_parts:
            print(part)


if __name__ == "__main__":
    _t0 = time.monotonic()
    main()
    _log_timing("pre_edit_surface", (time.monotonic() - _t0) * 1000)
