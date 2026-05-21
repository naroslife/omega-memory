#!/usr/bin/env python3
"""OMEGA Coordination SessionStart hook — Register agent session."""
import os
import sys
import time
import traceback
from datetime import datetime, timezone
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


def _kill_orphaned_mcp_servers():
    """Kill OMEGA MCP server processes whose parent has exited (PPID=1).

    Delegates to pid_registry.kill_orphaned_servers() which uses PID files
    instead of pgrep, making it faster and more reliable.
    """
    try:
        from omega_platform.server.pid_registry import kill_orphaned_servers
        killed = kill_orphaned_servers()
        if killed > 0:
            _log_hook_error("orphan_cleanup", f"Killed {killed} orphaned MCP server(s)")
    except Exception as e:
        _log_hook_error("orphan_cleanup", e)


def _clean_stale_socket():
    """Delete hook.sock if it exists but nothing is listening."""
    import socket as _socket
    sock_path = os.path.expanduser("~/.omega/hook.sock")
    if not os.path.exists(sock_path):
        return
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(sock_path)
        s.close()  # Socket is alive, leave it
    except (ConnectionRefusedError, OSError, _socket.timeout):
        try:
            os.unlink(sock_path)
        except OSError:
            pass
    finally:
        try:
            s.close()
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
            "omega_platform.server.hook_server.coordination",
            "handle_coord_session_start",
            payload,
        )
        if result is not None:
            emit_result(result)  # calls sys.exit, never returns
    except Exception:
        pass  # bridge layer itself broke; fall through to legacy logic
    # --- legacy fallback below (unchanged) ---
    session_id = os.environ.get("SESSION_ID", "")
    project = os.environ.get("PROJECT_DIR", os.getcwd())

    if not session_id:
        return

    _clean_stale_socket()
    _kill_orphaned_mcp_servers()

    try:
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()
        mgr.list_sessions()  # Force-clean stale sessions (bypasses rate limit)
        result = mgr.register_session(
            session_id=session_id,
            pid=os.getpid(),
            project=project,
        )
        peers = result.get("peers_on_project", 0)

        # Rich peer roster (matches daemon quality)
        if peers > 0:
            print(f"[STANDUP] {peers} peer{'s' if peers != 1 else ''} active")
            _print_peer_roster(session_id, project, mgr)

        # Surface structured handoff from predecessor (takes priority)
        _surface_handoff(session_id, project, mgr)

        # Surface unread inbox messages (show content, not just count)
        if peers > 0:
            _surface_inbox(session_id, mgr)

        # Surface recent decisions from other sessions (catch-up mechanism)
        _surface_recent_peer_decisions(session_id, project)

        # Git sync check: detect upstream commits from uncoordinated agents
        # Run in background — git fetch is network I/O that shouldn't block session start
        import threading
        threading.Thread(
            target=_check_git_sync,
            args=(session_id, project, mgr),
            daemon=True,
        ).start()

        # Check for predecessor session snapshots
        _session_resume(session_id, project, mgr)

        # Surface running background processes (benchmarks, long scripts)
        _check_running_processes(project)

        # Surface pending tasks for this project, with staleness detection
        try:
            tasks = mgr.list_tasks(project=project, status="pending")
            if tasks:
                # Cross-reference tasks against recent decisions to detect completed ones
                stale_ids = set()
                try:
                    from omega import bridge
                    for t in tasks[:5]:
                        title = t.get("title", "")
                        if not title:
                            continue
                        result_str = bridge.query(
                            title, limit=2, event_type="decision",
                            project=project,
                        )
                        if result_str and "No matching memories" not in result_str:
                            lower = result_str.lower()
                            if any(w in lower for w in [
                                "committed", "shipped", "deployed", "done",
                                "completed", "merged", "published", "live",
                                "verified", "set up", "configured", "added",
                            ]):
                                stale_ids.add(t["id"])
                except Exception:
                    pass  # Cross-reference is best-effort

                done_count = len(stale_ids)
                suffix = f" ({done_count} possibly completed)" if done_count else ""
                # Sort by blended score (priority + recency)
                import math as _math_coord
                _now_coord = time.time()
                def _task_score(t):
                    _pri = (t.get("priority") or 3) / 5.0
                    _age_d = 0.0
                    _ca = t.get("created_at", "")
                    if _ca:
                        try:
                            from datetime import datetime, timezone
                            if isinstance(_ca, str):
                                _ct = datetime.fromisoformat(_ca.replace("Z", "+00:00"))
                            else:
                                _ct = datetime.fromtimestamp(_ca, tz=timezone.utc)
                            _age_d = (_now_coord - _ct.timestamp()) / 86400.0
                        except Exception:
                            pass
                    _rec = _math_coord.exp(-0.099 * _age_d)
                    return _pri * 0.5 + _rec * 0.5, _age_d
                scored = [(t, *_task_score(t)) for t in tasks]
                scored.sort(key=lambda x: x[1], reverse=True)

                print(f"\n[TASKS] {len(tasks)} pending{suffix} (sorted by priority + freshness)")
                for t, _sc, _age_d in scored[:5]:
                    prio = f"P{t['priority']}" if t.get("priority") else ""
                    if _age_d < 1:
                        age_str = "today"
                    elif _age_d < 2:
                        age_str = "1d"
                    else:
                        age_str = f"{int(_age_d)}d"
                    tag = "[DONE?] " if t["id"] in stale_ids else ""
                    print(f"  {tag}{prio} {t['title']} ({age_str})")
                if len(tasks) > 5:
                    print(f"  ... and {len(tasks) - 5} more")
        except Exception:
            pass  # Task surfacing is best-effort

        # Standup Action line — tell agent what to do first
        if peers > 0:
            action_parts = ["check omega_inbox"]
            action_parts.append("omega_task_next to claim work")
            action_parts.append("omega_intent_announce to declare your plan")
            print(f"  Action: {', then '.join(action_parts)}")

    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("coord_session_start", e)


def _check_running_processes(project):
    """Detect running benchmark/long-running scripts and surface them."""
    import subprocess

    # Patterns to detect: (search_pattern, label, progress_extractor)
    KNOWN_PATTERNS = [
        ("longmemeval", "LongMemEval benchmark"),
        ("memorystress", "MemoryStress benchmark"),
        ("benchmarks/longmemeval/scripts/longmemeval_official", "LongMemEval harness"),
        ("benchmarks/memorystress/scripts/memorystress_harness", "MemoryStress harness"),
    ]

    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return

        ps_lines = result.stdout.strip().split("\n")
        found = []

        for line in ps_lines:
            # Skip grep/ps itself and this hook
            if "grep" in line or "coord_session_start" in line:
                continue
            for pattern, label in KNOWN_PATTERNS:
                if pattern in line and "python" in line.lower():
                    # Extract PID and runtime
                    parts = line.split()
                    if len(parts) >= 10:
                        pid = parts[1]
                        runtime = parts[9]  # TIME column
                        # Extract output file if --output flag present
                        output_file = ""
                        if "--output" in line:
                            idx = line.index("--output")
                            rest = line[idx + 9:].strip().split()[0] if idx + 9 < len(line) else ""
                            output_file = rest
                        found.append((label, pid, runtime, output_file))
                    break  # Don't double-match

        if not found:
            return

        # Deduplicate by label (prefer the Python process, not the shell wrapper)
        seen_labels = {}
        for label, pid, runtime, output_file in found:
            if label not in seen_labels:
                seen_labels[label] = (pid, runtime, output_file)

        print(f"\n[PROCESSES] {len(seen_labels)} long-running process(es) detected:")
        for label, (pid, runtime, output_file) in seen_labels.items():
            # Try to get progress from output file
            progress = ""
            if output_file:
                try:
                    out_path = os.path.join(project, output_file)
                    if os.path.exists(out_path):
                        line_count = sum(1 for _ in open(out_path))
                        progress = f" [{line_count} lines written]"
                except Exception:
                    pass
            # For known benchmark files, check line count directly
            if not progress and "longmemeval" in label.lower():
                try:
                    import glob as _glob
                    for f in _glob.glob(os.path.join(project, "longmemeval_full_v*_*.jsonl")):
                        if ".eval-results" not in f:
                            line_count = sum(1 for _ in open(f))
                            fname = os.path.basename(f)
                            progress = f" [{fname}: {line_count}/500]"
                            break
                except Exception:
                    pass
            if not progress and "memorystress" in label.lower():
                try:
                    import glob as _glob
                    for d in _glob.glob("/tmp/ms_v*_omega"):
                        if os.path.isdir(d):
                            progress = f" [{os.path.basename(d)}]"
                            break
                except Exception:
                    pass

            print(f"  - {label} (PID {pid}, CPU time {runtime}){progress}")

    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        _log_hook_error("check_running_processes", e)


def _print_peer_roster(session_id, project, mgr):
    """Print a rich peer roster with tasks, files, branches, and heartbeat age."""
    try:
        sessions = mgr.list_sessions(auto_clean=False)
        peers = [s for s in sessions if s.get("session_id") != session_id][:6]
        if not peers:
            return

        # Fetch in-progress coord tasks
        roster_tasks = {}
        try:
            for t in mgr.list_tasks(project=project, status="in_progress"):
                t_sid = t.get("session_id")
                if t_sid and t_sid not in roster_tasks:
                    roster_tasks[t_sid] = t
        except Exception:
            pass

        print(f"[COORD] {len(peers)} peer{'s' if len(peers) != 1 else ''} active:")
        for p in peers:
            p_sid = p["session_id"][:16]
            # Prefer coord_task over session.task
            ct = roster_tasks.get(p["session_id"])
            if ct:
                pct = f" [{ct['progress']}%]" if ct.get("progress") else ""
                p_task = f"#{ct['id']} {ct['title'][:40]}{pct}"
            else:
                p_task = (p.get("task") or "idle")[:50]

            # File and branch claims
            p_files = ""
            p_branch = ""
            try:
                claims = mgr.get_session_claims(p["session_id"])
                file_claims = claims.get("file_claims", [])
                if file_claims:
                    fnames = [os.path.basename(f) for f in file_claims[:2]]
                    if len(file_claims) > 2:
                        fnames.append(f"+{len(file_claims) - 2}")
                    p_files = f" [{', '.join(fnames)}]"
                branch_claims = claims.get("branch_claims", [])
                if branch_claims:
                    p_branch = f" ({branch_claims[0]})"
            except Exception:
                pass

            # Project label when different
            p_proj_label = ""
            p_project = p.get("project") or ""
            if p_project and p_project != project:
                p_proj_label = f" [{os.path.basename(p_project)}]"

            # Heartbeat age
            age_str = ""
            try:
                hb = p.get("last_heartbeat", "")
                if hb:
                    hb_time = datetime.fromisoformat(hb)
                    delta = datetime.now(timezone.utc).replace(tzinfo=None) - hb_time
                    mins = int(delta.total_seconds() / 60)
                    if mins < 1:
                        age_str = " — just now"
                    elif mins < 60:
                        age_str = f" — {mins}m ago"
                    else:
                        age_str = f" — {mins // 60}h{mins % 60}m ago"
            except Exception:
                pass

            print(f"  {p_sid}: {p_task}{p_proj_label}{p_files}{p_branch}{age_str}")
    except Exception as e:
        _log_hook_error("peer_roster", e)


def _surface_handoff(session_id, project, mgr):
    """Surface the latest structured handoff from a predecessor session."""
    try:
        handoff = mgr.get_latest_handoff(project=project, reader_session_id=session_id)
        if not handoff:
            return
        # Skip if it's our own handoff
        if handoff.get("session_id") == session_id:
            return

        # Check age — only surface handoffs from last 72 hours
        try:
            created = datetime.fromisoformat(handoff["created_at"])
            delta = datetime.now(timezone.utc).replace(tzinfo=None) - created.replace(tzinfo=None)
            if delta.total_seconds() > 259200:  # 72 hours
                return
            mins = int(delta.total_seconds() / 60)
            if mins < 60:
                age_str = f" ({mins}m ago)"
            else:
                age_str = f" ({mins // 60}h{mins % 60}m ago)"
        except Exception:
            age_str = ""

        lines = [f"[HANDOFF]{age_str} from {handoff['session_id'][:16]}:"]

        if handoff.get("git_branch"):
            dirty = len(handoff.get("git_dirty_files", []))
            dirty_note = f" ({dirty} uncommitted)" if dirty else ""
            lines.append(f"  Branch: {handoff['git_branch']}{dirty_note}")
        if handoff.get("completed_tasks"):
            lines.append("  Completed:")
            for t in handoff["completed_tasks"][:5]:
                lines.append(f"    - {t}")
        if handoff.get("decisions_made"):
            lines.append("  Decisions:")
            for d in handoff["decisions_made"][:5]:
                lines.append(f"    - {d}")
        if handoff.get("blocked_items"):
            lines.append("  Blocked:")
            for b in handoff["blocked_items"][:3]:
                lines.append(f"    - {b}")
        if handoff.get("next_steps"):
            lines.append("  Next steps:")
            for s in handoff["next_steps"][:5]:
                lines.append(f"    - {s}")
        if handoff.get("files_modified"):
            fnames = [os.path.basename(f) for f in handoff["files_modified"][:5]]
            more = f" +{len(handoff['files_modified']) - 5}" if len(handoff["files_modified"]) > 5 else ""
            lines.append(f"  Files: {', '.join(fnames)}{more}")
        if handoff.get("key_context"):
            lines.append(f"  Context: {handoff['key_context'][:400]}")

        print("\n".join(lines))
    except Exception as e:
        _log_hook_error("surface_handoff", e)


def _surface_inbox(session_id, mgr):
    """Auto-surface unread inbox messages at session start."""
    try:
        msgs = mgr.check_inbox(session_id, unread_only=True, limit=3)
        if not msgs:
            return
        print(f"\n[INBOX] {len(msgs)} unread message(s):")
        for m in msgs[:3]:
            from_sid = m.get("from_session") or "unknown"
            sender = from_sid[:12]
            msg_type = m.get("msg_type", "inform")
            subject = (m.get("subject") or "")[:80]
            body_preview = (m.get("body") or "")[:80]
            content = subject or body_preview
            print(f"  [{msg_type}] from {sender}: {content}")
        # Note: check_inbox marks these as read; any additional messages
        # will still appear when agent calls omega_inbox()
    except Exception as e:
        _log_hook_error("surface_inbox", e)


def _surface_recent_peer_decisions(session_id, project):
    """Surface recent decisions from OTHER sessions in this project.

    This is the catch-up mechanism: if a peer stored important decisions
    while we were offline or in a long wait, we see them at session start.
    """
    try:
        from omega.bridge import query_structured

        # Query recent decisions from this project (last 2 hours)
        decisions = query_structured(
            query_text="recent decisions and outcomes",
            limit=10,
            project=project,
            event_type="decision",
        )
        if not decisions:
            return

        # Filter to decisions from OTHER sessions, within last 24 hours
        peer_decisions = []
        now = datetime.now(timezone.utc)
        for d in decisions:
            d_session = d.get("session_id") or ""
            if d_session == session_id:
                continue  # Skip our own decisions

            # Check age
            created = d.get("created_at", "")
            if created:
                try:
                    d_time = datetime.fromisoformat(created)
                    if hasattr(d_time, "tzinfo") and d_time.tzinfo is None:
                        pass  # naive datetime, compare as-is with naive now
                    delta = now.replace(tzinfo=None) - d_time.replace(tzinfo=None)
                    if delta.total_seconds() > 86400:  # older than 24 hours
                        continue
                except Exception:
                    continue

            content = d.get("content", "")
            # Strip auto-capture prefixes
            for prefix in ("Plan/decision captured: ", "Decision: "):
                if content.startswith(prefix):
                    content = content[len(prefix):]
            # Skip JSON blobs
            stripped = content.lstrip()
            if stripped.startswith(("{", "[", '"filePath')):
                continue
            first_line = content.split("\n")[0].strip()
            if first_line and len(first_line) > 10:
                # Compute how long ago
                age_str = ""
                if created:
                    try:
                        d_time = datetime.fromisoformat(created)
                        delta = now.replace(tzinfo=None) - d_time.replace(tzinfo=None)
                        mins = int(delta.total_seconds() / 60)
                        if mins < 60:
                            age_str = f" ({mins}m ago)"
                        else:
                            age_str = f" ({mins // 60}h{mins % 60}m ago)"
                    except Exception:
                        pass
                peer_decisions.append(f"  - {first_line[:250]}{age_str}")

            if len(peer_decisions) >= 5:
                break

        if peer_decisions:
            print(f"\n[RECENT] {len(peer_decisions)} decision(s) from other sessions:")
            for line in peer_decisions:
                print(line)
    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("surface_recent_peer_decisions", e)


def _check_git_sync(session_id, project, mgr):
    """Detect upstream commits not tracked by coordination."""
    import subprocess

    try:
        # Check if we're in a git repo
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5, cwd=project,
        )
        if result.returncode != 0:
            return

        # Fetch latest from origin (quiet, short timeout to fit within hook timeout)
        subprocess.run(
            ["git", "fetch", "origin", "--quiet"],
            capture_output=True, text=True, timeout=5, cwd=project,
        )

        # Get current branch
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=project,
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

        # Check for upstream commits not in HEAD
        upstream_result = subprocess.run(
            ["git", "log", f"HEAD..origin/{branch}", "--oneline", "--no-decorate", "-20"],
            capture_output=True, text=True, timeout=5, cwd=project,
        )
        if upstream_result.returncode != 0 or not upstream_result.stdout.strip():
            return

        upstream_lines = upstream_result.stdout.strip().split("\n")
        upstream_hashes = [line.split()[0] for line in upstream_lines if line.strip()]

        if not upstream_hashes:
            return

        # Check which are untracked by coordination
        untracked = mgr.detect_untracked_commits(project, upstream_hashes)

        # Log all upstream commits as events
        for line in upstream_lines:
            parts = line.split(None, 1)
            if parts:
                mgr.log_git_event(
                    project=project,
                    event_type="upstream_detected",
                    commit_hash=parts[0],
                    branch=branch,
                    message=parts[1] if len(parts) > 1 else "",
                )

        # Surface warning
        print(f"\n[GIT-SYNC] {len(upstream_hashes)} upstream commit(s) on origin/{branch}:")
        for line in upstream_lines[:5]:
            print(f"  {line}")
        if len(upstream_lines) > 5:
            print(f"  ... and {len(upstream_lines) - 5} more")

        if untracked:
            print(f"  !! {len(untracked)} commit(s) from UNCOORDINATED agents (not tracked by OMEGA)")
            print("  Run 'git pull' before starting work to avoid conflicts.")

    except subprocess.TimeoutExpired:
        pass
    except FileNotFoundError:
        pass  # git not installed
    except Exception as e:
        _log_hook_error("git_sync_check", e)


def _session_resume(session_id, project, mgr):
    """Check for predecessor session snapshots with enriched context."""
    try:
        if not project:
            return
        snapshots = mgr.recover_session(project)
        if not snapshots:
            return

        snap = snapshots[0]
        meta = snap.get("metadata") or {}

        # Compute how long ago the predecessor ended
        age_str = ""
        try:
            ended = datetime.fromisoformat(snap["created_at"])
            delta = datetime.now(timezone.utc).replace(tzinfo=None) - ended
            mins = int(delta.total_seconds() / 60)
            if mins < 60:
                age_str = f" ended {mins}m ago"
            else:
                age_str = f" ended {mins // 60}h{mins % 60}m ago"
        except Exception:
            pass

        lines = [f"[RESUME] Previous session{age_str} ({snap['reason']})"]

        # Git state from enriched metadata
        git_branch = meta.get("git_branch")
        git_dirty = meta.get("git_dirty_files", [])
        if git_branch:
            dirty_note = f" ({len(git_dirty)} uncommitted file{'s' if len(git_dirty) != 1 else ''})" if git_dirty else ""
            lines.append(f"  Branch: {git_branch}{dirty_note}")

        if snap.get("task"):
            lines.append(f"  Task: {snap['task']}")
        if snap.get("file_claims"):
            files = [fc["file_path"] for fc in snap["file_claims"]]
            lines.append(f"  Files in progress: {', '.join(files)}")
        if snap.get("intents"):
            seen_intents = set()
            for intent in snap["intents"]:
                desc = intent["description"]
                if desc not in seen_intents:
                    seen_intents.add(desc)
                    lines.append(f"  Intent: {desc}")

        # Surface recent decisions from predecessor session
        try:
            from omega.bridge import query_structured
            decisions = query_structured(
                query_text="decisions made",
                limit=5,
                session_id=snap["session_id"],
                project=project,
                event_type="decision",
            )
            clean_decisions = []
            for d in (decisions or []):
                content = d.get("content", "")
                # Strip auto-capture prefixes
                for prefix in ("Plan/decision captured: ", "Decision: "):
                    if content.startswith(prefix):
                        content = content[len(prefix):]
                # Skip JSON blobs
                stripped = content.lstrip()
                if stripped.startswith(("{", "[", '"filePath')):
                    continue
                # Take first meaningful line only
                first_line = content.split("\n")[0].strip()
                if first_line and len(first_line) > 10:
                    clean_decisions.append(first_line[:250])
                if len(clean_decisions) >= 3:
                    break
            if clean_decisions:
                lines.append("  Decisions made:")
                for cd in clean_decisions:
                    lines.append(f"    - {cd}")
        except Exception:
            pass  # Decision surfacing is best-effort

        lines.append("  Use omega_session_recover for full details.")
        print("\n".join(lines))
    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("coord_session_start", e)


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
    _log_timing("coord_session_start", (time.monotonic() - _t0) * 1000)
