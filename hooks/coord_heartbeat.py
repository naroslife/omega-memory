#!/usr/bin/env python3
"""OMEGA Coordination PostToolUse hook — Update session heartbeat."""
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


def main():
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
            "omega_platform.server.hook_server.heartbeat",
            "handle_coord_heartbeat",
            payload,
        )
        if result is not None:
            emit_result(result)  # calls sys.exit, never returns
    except Exception:
        pass  # bridge layer itself broke; fall through to legacy logic
    # --- legacy fallback below (unchanged) ---
    session_id = os.environ.get("SESSION_ID", "")
    if not session_id:
        return

    try:
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()
        mgr.heartbeat(session_id)
    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("coord_heartbeat", e)


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
    _log_timing("coord_heartbeat", (time.monotonic() - _t0) * 1000)
