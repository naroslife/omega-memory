#!/usr/bin/env python3
"""OMEGA PreToolUse hook — Task declaration guard for multi-agent coordination.

Triggered on Edit/Write/NotebookEdit. Blocks the tool call unless the session
has an active (in_progress) task. Enforcement is opt-in per project: only
activates once a project has at least one non-terminal task.

Exit code 2 = block the tool call in Claude Code.
Exit code 0 = allow (including fail-open on any error).

Design: Fail-open — OMEGA unavailable must never block edits.
"""
import json
import os
import sys
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


def _block_no_task(file_path, project):
    """Print block message and exit with code 2."""
    filename = os.path.basename(file_path)
    project_name = os.path.basename(project) if project else "unknown"
    print(
        f"\n[TASK-GUARD] BLOCKED: No active task for this session on {project_name}.\n"
        f"  Create and claim a task before editing {filename}:\n"
        f"    1. omega_task_create(title=\"Your task\", project=\"{project}\")\n"
        f"    2. omega_task_claim(task_id=<id>, session_id=\"<session-id>\")\n"
        f"  Or complete/cancel existing tasks to disable enforcement."
    )
    sys.exit(2)


def main():
    tool_name = os.environ.get("TOOL_NAME", "")
    if tool_name not in ("Edit", "Write", "NotebookEdit"):
        return

    session_id = os.environ.get("SESSION_ID", "")
    if not session_id:
        # Single-agent mode — no enforcement
        return

    try:
        input_data = json.loads(os.environ.get("TOOL_INPUT", "{}"))
    except (json.JSONDecodeError, TypeError):
        return

    file_path = input_data.get("file_path", input_data.get("notebook_path", ""))
    if not file_path:
        return

    project_dir = os.environ.get("PROJECT_DIR", "")
    if not project_dir:
        return

    # Skip if file is outside the project directory
    try:
        if not os.path.abspath(file_path).startswith(os.path.abspath(project_dir)):
            return
    except Exception:
        return

    try:
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()

        # Opt-in check: only enforce if project has active tasks
        if not mgr.project_has_active_tasks(project_dir):
            return

        # Check if session has an in_progress task
        result = mgr.has_active_task(session_id)
        if result.get("has_task"):
            return  # Has active task — allow

        # No active task — BLOCK
        _block_no_task(file_path, project_dir)

    except ImportError:
        # OMEGA not installed — fail-open
        pass
    except Exception as e:
        # Any error — fail-open, never block when OMEGA is unavailable
        _log_hook_error("pre_task_guard", e)


if __name__ == "__main__":
    _t0 = time.monotonic()
    main()
    _log_timing("pre_task_guard", (time.monotonic() - _t0) * 1000)
