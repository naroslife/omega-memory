#!/usr/bin/env python3
"""OMEGA PreToolUse hook — Alignment gate (fallback mode).

Degraded fallback for when the hook daemon is unavailable.
Checks if the target file's domain has active decisions and surfaces them.

Unlike the daemon version, this has no debounce state, so it checks the DB
on every invocation (heavier, but only runs in fallback mode when daemon is down).

Exit code 0 always (informational only, never blocks).
Fail-open: any error silently allows.
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


_GATE_TOOLS = {"Edit", "Write", "NotebookEdit", "Bash"}

# Simple domain inference from file paths
_DOMAIN_MAP = {
    "coordination": "coordination",
    "bridge": "memory",
    "sqlite_store": "memory",
    "server/": "mcp",
    "hooks/": "hooks",
    "website/": "website",
    "cloud/": "cloud",
    "entity/": "entity",
}


def _infer_domain(file_path: str) -> str | None:
    if not file_path:
        return None
    for pattern, domain in _DOMAIN_MAP.items():
        if pattern in file_path:
            return domain
    return None


def main():
    try:
        tool_name = os.environ.get("TOOL_NAME", "")
        if tool_name not in _GATE_TOOLS:
            return

        session_id = os.environ.get("SESSION_ID", "")
        if not session_id:
            return

        # Single-agent fast path
        from omega_platform.orchestrator.coordination import get_manager

        mgr = get_manager()
        if mgr.active_session_count() <= 1:
            return

        # Extract file path from tool input
        tool_input = os.environ.get("TOOL_INPUT", "")
        file_path = None
        if tool_input:
            try:
                parsed = json.loads(tool_input)
                file_path = parsed.get("file_path", "") or parsed.get("path", "")
            except (json.JSONDecodeError, AttributeError):
                pass

        domain = _infer_domain(file_path or "")
        if not domain:
            return

        project_dir = os.environ.get("PROJECT_DIR", "")
        if not project_dir:
            return

        decisions = mgr.query_decisions(
            project=os.path.basename(project_dir), domain=domain, status="active", limit=3
        )
        if not decisions:
            return

        lines = [f"[ALIGNMENT] {len(decisions)} active decision(s) in domain '{domain}':"]
        for d in decisions:
            lines.append(f"  #{d['id']} [{d['domain']}]: {d['decision'][:120]}")
        lines.append("  Comply with these decisions or supersede with omega_decision_register.")
        print("\n".join(lines))

    except Exception as e:
        _log_hook_error("pre_alignment_gate", e)


if __name__ == "__main__":
    main()
