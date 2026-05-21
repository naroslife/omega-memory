#!/usr/bin/env python3
"""OMEGA PreToolUse hook — Protocol gate (fallback mode).

Degraded fallback for when the hook daemon is unavailable.
Checks if multiple agents are active and reminds the agent to call
omega_welcome / omega_inbox before editing files.

Unlike the daemon version, this has no in-memory debounce state, so it
checks the DB directly (heavier, but only runs in fallback mode).

Exit code 0 always (informational only, never blocks).
Fail-open: any error silently allows.
"""
import os
import sys
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


_GATE_TOOLS = {"Edit", "Write", "NotebookEdit", "Bash"}
# Only remind once per process (fallback runs as a subprocess each time,
# but fast_hook.py may batch multiple hooks in one invocation).
_warned = False


def main():
    global _warned
    # Bridge: try the in-process daemon handler first for zero-drift parity
    # with the hook_server. Falls through to the legacy logic below if the
    # bridge module is unavailable (core-only install) or the handler raises.
    try:
        try:
            from ._fallback_bridge import build_payload_from_env, emit_result, try_daemon_handler
        except Exception:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from _fallback_bridge import build_payload_from_env, emit_result, try_daemon_handler  # type: ignore
        payload = build_payload_from_env()
        result = try_daemon_handler(
            "omega_platform.server.hook_server.guards",
            "handle_pre_protocol_gate",
            payload,
        )
        if result is not None:
            emit_result(result)  # calls sys.exit, never returns
    except Exception:
        pass  # bridge layer itself broke; fall through to legacy logic
    # --- legacy fallback below (unchanged) ---
    if _warned:
        return

    try:
        tool_name = os.environ.get("TOOL_NAME", "")
        if tool_name not in _GATE_TOOLS:
            return

        session_id = os.environ.get("SESSION_ID", "")
        if not session_id:
            return

        from omega_platform.orchestrator.coordination import get_manager

        mgr = get_manager()
        count = mgr.active_session_count()
        if count > 1:
            _warned = True
            print(
                "[PROTOCOL-GATE] You have active peers. "
                "Call omega_inbox() to check for messages before editing files."
            )
        elif count == 1:
            _warned = True
            print(
                "[PROTOCOL-REMINDER] Call omega_welcome() for memory context before starting work."
            )

    except Exception as e:
        _log_hook_error("pre_protocol_gate", e)


if __name__ == "__main__":
    main()
