#!/usr/bin/env python3
"""OMEGA PostToolUse hook — Record tool call trace to coord_audit.

Fallback for when the hook daemon is unavailable. Silent (no user output).
"""
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


def main():
    session_id = os.environ.get("SESSION_ID", "")
    tool_name = os.environ.get("TOOL_NAME", "")
    if not session_id or not tool_name:
        return

    try:
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()
        tool_output = os.environ.get("TOOL_OUTPUT", "")
        tool_input = os.environ.get("TOOL_INPUT", "")
        mgr.log_audit(
            session_id=session_id,
            tool_name=tool_name,
            arguments=None,
            result_summary=tool_output[:200] if tool_output else None,
            input_size=len(tool_input) if tool_input else 0,
        )
    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("trace_capture", e)


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


if __name__ == "__main__":
    _t0 = time.monotonic()
    main()
    _log_timing("trace_capture", (time.monotonic() - _t0) * 1000)
