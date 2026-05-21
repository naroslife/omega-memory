#!/usr/bin/env python3
"""OMEGA PreToolUse hook — Deploy guard (fallback mode).

BLOCKS deployment commands unless the coordination gate was cleared by
calling omega_query(event_type="decision") in the current session.

Exit code 2 = block the tool call.
Exit code 0 = allow.
"""
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path


_DEPLOY_PATTERNS = [
    r'\bvercel\s+(?:deploy|link|project\s+add|domains?\s+add)',
    r'\bvercel\s+--prod\b',
    r'\bfly\s+deploy\b',
    r'\bnpm\s+run\s+deploy\b',
    r'\bnpx\s+.*deploy\b',
]

_DEPLOY_RE = [re.compile(p) for p in _DEPLOY_PATTERNS]
_GATE_DIR = Path.home() / ".omega" / "gates"


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


def _is_deploy_command(command):
    for pattern in _DEPLOY_RE:
        if pattern.search(command):
            return True
    return False


def _is_marker_fresh(session_id, suffix, max_age_sec=1800):
    """Check if a gate marker file is recent."""
    try:
        candidates = []
        if session_id:
            candidates.append(_GATE_DIR / f"{session_id}.{suffix}")
        candidates.append(_GATE_DIR / f"default.{suffix}")
        for gate_file in candidates:
            if gate_file.exists():
                ts = float(gate_file.read_text().strip())
                if (time.time() - ts) < max_age_sec:
                    return True
        return False
    except Exception:
        return False


def _is_action_claimed(session_id, max_age_sec=1800):
    """Check if this session has claimed the deploy action."""
    return _is_marker_fresh(session_id, "action_claim", max_age_sec)


def _is_gate_cleared(session_id, max_age_sec=1800):
    """Gate requires decision query, coord_status check, AND action claim."""
    decision_ok = _is_marker_fresh(session_id, "gate", max_age_sec)
    coord_ok = _is_marker_fresh(session_id, "coord", max_age_sec)
    action_ok = _is_action_claimed(session_id, max_age_sec)
    return decision_ok and coord_ok and action_ok


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
            "omega_platform.server.hook_server.guards",
            "handle_pre_deploy_guard",
            payload,
        )
        if result is not None:
            emit_result(result)  # calls sys.exit, never returns
    except Exception:
        pass  # bridge layer itself broke; fall through to legacy logic
    # --- legacy fallback below (unchanged) ---
    tool_name = os.environ.get("TOOL_NAME", "")
    tool_input = os.environ.get("TOOL_INPUT", "{}")

    if tool_name != "Bash":
        return

    try:
        input_data = json.loads(tool_input)
    except (json.JSONDecodeError, TypeError):
        return

    command = input_data.get("command", "")
    if not _is_deploy_command(command):
        return

    session_id = os.environ.get("SESSION_ID", "")

    if _is_gate_cleared(session_id):
        print("[DEPLOY-GATE] Gate cleared. Proceeding.")
        return

    # BLOCK — identify which marker is missing
    missing = []
    if not _is_marker_fresh(session_id, "gate"):
        missing.append("omega_query(event_type='decision', query='<target area>')")
    if not _is_marker_fresh(session_id, "coord"):
        missing.append("omega_coord_status")
    if not _is_action_claimed(session_id):
        missing.append("omega_action_claim(action_type='deploy', action_target='vercel:<project>')")

    print("\n[DEPLOY-GATE] BLOCKED: Coordination gate not cleared.")
    print("  You MUST run ALL of these before deploying:")
    for m in missing:
        print(f"    - {m}  (NOT YET RUN)")
    print("  This prevents duplicate deploys across agents.")
    sys.exit(2)


if __name__ == "__main__":
    _t0 = time.monotonic()
    main()
    _log_timing("pre_deploy_guard", (time.monotonic() - _t0) * 1000)
