#!/usr/bin/env python3
"""OMEGA Coordination PostToolUse hook — Auto-claim files on Edit/Write.

Fires after Edit|Write. Automatically claims the edited file so other agents
see it as taken, without requiring explicit omega_file_claim calls.
"""
import json
import os
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
    tool_name = os.environ.get("TOOL_NAME", "")
    if tool_name not in ("Edit", "Write", "NotebookEdit"):
        return

    session_id = os.environ.get("SESSION_ID", "")
    if not session_id:
        return

    try:
        input_data = json.loads(os.environ.get("TOOL_INPUT", "{}"))
    except (json.JSONDecodeError, TypeError):
        return

    file_path = input_data.get("file_path", input_data.get("notebook_path", ""))
    if not file_path:
        return

    try:
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()
        result = mgr.claim_file(session_id, file_path, task="auto-claimed on edit")
        if result.get("conflict"):
            owner = result["claimed_by"][:20]
            owner_task = result.get("task") or "unknown task"
            print(
                f"[CONFLICT] {os.path.basename(file_path)} is claimed by session "
                f"{owner} ({owner_task}). Coordinate before editing."
            )
        elif result.get("success"):
            # Auto-announce intent for coordination visibility
            try:
                mgr.announce_intent(
                    session_id=session_id,
                    description=f"Editing {os.path.basename(file_path)}",
                    intent_type="edit",
                    target_files=[file_path],
                    ttl_minutes=5,
                )
            except Exception:
                pass  # Intent announcement is best-effort
    except ImportError:
        pass
    except Exception as e:
        error_str = str(e)
        if "already claimed" not in error_str.lower():
            _log_hook_error("auto_claim_file", e)


if __name__ == "__main__":
    main()
