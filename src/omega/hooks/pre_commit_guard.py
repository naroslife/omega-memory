#!/usr/bin/env python3
"""OMEGA PreToolUse hook — Git commit coordination guard (fallback mode).

BLOCKS commits that stage files claimed by other sessions (exit code 2).
WARNS when staging files not in own claim list (exit code 0).
Prevents mixed-author commits where one agent captures another's work.
"""
import json
import os
import re
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


def main():
    tool_name = os.environ.get("TOOL_NAME", "")
    tool_input = os.environ.get("TOOL_INPUT", "{}")

    if tool_name != "Bash":
        return

    try:
        input_data = json.loads(tool_input)
    except (json.JSONDecodeError, TypeError):
        return

    command = input_data.get("command", "")

    # Only trigger on git commit commands
    if not re.search(r"\bgit\s+commit\b", command):
        return

    session_id = os.environ.get("SESSION_ID", "")
    project = os.environ.get("PROJECT_DIR", os.getcwd())

    # --- Get staged files (needed by both scope check and peer check) ---
    import subprocess

    staged_files = []
    try:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project,
        )
        staged_files = [
            f.strip() for f in staged.stdout.strip().split("\n") if f.strip()
        ]
    except Exception:
        pass

    # --- Commit scope / atomicity check (no coordination needed) ---
    if staged_files and not os.environ.get("OMEGA_SKIP_SCOPE_CHECK"):
        command = json.loads(os.environ.get("TOOL_INPUT", "{}")).get("command", "")
        is_merge = "--amend" in command or "merge" in command.lower()
        if not is_merge:
            dir_groups = {}
            for sf in staged_files:
                parts = sf.split("/")
                top_dir = parts[0] if len(parts) > 1 else "(root)"
                dir_groups.setdefault(top_dir, []).append(sf)

            num_dirs = len(dir_groups)
            num_files = len(staged_files)

            total_lines = 0
            try:
                stat_result = subprocess.run(
                    ["git", "diff", "--cached", "--shortstat"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=project,
                )
                if stat_result.returncode == 0:
                    for m in re.finditer(r"(\d+) (?:insertion|deletion)", stat_result.stdout):
                        total_lines += int(m.group(1))
            except Exception:
                pass

            scope_blocked = (num_files > 10 and num_dirs >= 3) or (total_lines > 500 and num_dirs >= 3)

            if scope_blocked:
                lines = [
                    f"[COMMIT-SCOPE] BLOCKED: {num_files} files across {num_dirs} directories "
                    f"({total_lines} lines). Try smaller, atomic commits.",
                    "",
                    "Suggested splits:",
                ]
                for dir_name, files in sorted(dir_groups.items(), key=lambda x: -len(x[1])):
                    file_list = ", ".join(os.path.basename(f) for f in files[:4])
                    if len(files) > 4:
                        file_list += f" +{len(files) - 4}"
                    lines.append(f"  {dir_name}/ ({len(files)} files): {file_list}")
                lines.append("")
                lines.append("To override: set OMEGA_SKIP_SCOPE_CHECK=1")
                print("\n".join(lines))
                exit(2)

    # --- Peer coordination check ---
    try:
        from omega_platform.orchestrator.coordination import get_manager

        mgr = get_manager()
        sessions = mgr.list_sessions(auto_clean=True)
        peers = [s for s in sessions if s.get("session_id") != session_id]

        if not peers:
            # Solo mode: still warn about unclaimed files
            if staged_files and session_id:
                try:
                    own_claims = mgr.get_session_claims(session_id)
                    own_files = own_claims.get("file_claims", [])
                    if own_files:
                        unclaimed_solo = []
                        for sf in staged_files:
                            full_path = os.path.join(project, sf)
                            if full_path not in own_files and sf not in own_files:
                                unclaimed_solo.append(sf)
                        if unclaimed_solo:
                            lines = [
                                f"[COMMIT-SCOPE] WARNING: {len(unclaimed_solo)} staged file(s) not in your claim list:",
                            ]
                            for fname in unclaimed_solo[:10]:
                                lines.append(f"  {fname}")
                            if len(unclaimed_solo) > 10:
                                lines.append(f"  +{len(unclaimed_solo) - 10} more")
                            lines.append("")
                            lines.append("Did you author these changes? If not, unstage with: git reset HEAD <file>")
                            print("\n".join(lines))
                except Exception:
                    pass
            return

        # Check for peer-claimed file overlaps
        overlapping = []
        for peer in peers:
            try:
                claims = mgr.get_session_claims(peer["session_id"])
                peer_files = claims.get("file_claims", [])
                for sf in staged_files:
                    full_path = os.path.join(project, sf)
                    if full_path in peer_files or sf in peer_files:
                        overlapping.append(
                            (sf, peer["session_id"][:16])
                        )
            except Exception:
                pass

        # Check for files not in own claim list
        unclaimed_by_self = []
        try:
            own_claims = mgr.get_session_claims(session_id)
            own_files = own_claims.get("file_claims", [])
            for sf in staged_files:
                full_path = os.path.join(project, sf)
                if full_path not in own_files and sf not in own_files:
                    unclaimed_by_self.append(sf)
        except Exception:
            pass

        # BLOCK if staging peer-claimed files
        if overlapping:
            lines = [f"[COMMIT-GUARD] BLOCKED: staging {len(overlapping)} file(s) claimed by other agent(s):"]
            for fname, peer_sid in overlapping[:10]:
                lines.append(f"  {fname} (claimed by {peer_sid})")
            lines.append("")
            lines.append("Unstage peer files with: git reset HEAD <file>")
            lines.append("Or coordinate via omega_send_message to request file release.")
            print("\n".join(lines))
            exit(2)

        # Build info message
        lines = [f"[COMMIT-COORD] {len(peers)} peer(s) active:"]
        for p in peers:
            p_task = (p.get("task") or "idle")[:50]
            p_proj = os.path.basename(p.get("project", ""))
            lines.append(f"  - {p['session_id'][:16]}: {p_task} [{p_proj}]")

        if unclaimed_by_self:
            lines.append(f"  ?? {len(unclaimed_by_self)} staged file(s) not in your claim list:")
            for fname in unclaimed_by_self[:5]:
                lines.append(f"     {os.path.basename(fname)}")
            lines.append("  Consider: did you author these changes?")

        print("\n".join(lines))

    except ImportError:
        pass  # Coordination module not available — fail open
    except Exception as e:
        _log_hook_error("pre_commit_guard", e)


if __name__ == "__main__":
    _t0 = time.monotonic()
    main()
    _log_timing("pre_commit_guard", (time.monotonic() - _t0) * 1000)
