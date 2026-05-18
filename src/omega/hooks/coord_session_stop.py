#!/usr/bin/env python3
"""OMEGA Coordination Stop hook — Deregister session and release all claims."""
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


def _broadcast_session_end(session_id, project, mgr):
    """Broadcast session summary to active peers before deregistering."""
    try:
        # Check if there are active peers to notify
        sessions = mgr.list_sessions(auto_clean=False)
        peers = [s for s in sessions if s.get("session_id") != session_id]
        if not peers:
            return

        # Build a compact summary of what this session did
        summary = _build_end_summary(session_id, project)
        if not summary:
            return

        # Broadcast to project (all active peers will see it in their inbox)
        mgr.send_message(
            from_session=session_id,
            subject=f"Session ended: {summary}",
            msg_type="complete",
            project=project,
            body=summary,
            ttl_minutes=120,  # 2 hours — enough for next session start
        )
    except Exception as e:
        _log_hook_error("broadcast_session_end", e)


def _build_end_summary(session_id, project):
    """Build a compact summary of session activity for the broadcast."""
    try:
        from omega.bridge import query_structured
    except ImportError:
        return None

    decisions = query_structured(
        query_text="decisions made",
        limit=3,
        session_id=session_id,
        project=project,
        event_type="decision",
    )

    if not decisions:
        return None

    items = []
    for d in decisions[:3]:
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
            items.append(first_line[:120])

    if not items:
        return None

    return "; ".join(items)[:400]


def _auto_handoff(session_id, project, mgr):
    """Auto-generate a structured handoff from session state. Best-effort."""
    try:
        from omega.bridge import query_structured

        # Gather decisions from this session
        decisions_raw = query_structured(
            query_text="decisions made",
            limit=5,
            session_id=session_id,
            project=project,
            event_type="decision",
        )
        decisions = []
        for d in (decisions_raw or []):
            content = d.get("content", "")
            for prefix in ("Plan/decision captured: ", "Decision: "):
                if content.startswith(prefix):
                    content = content[len(prefix):]
            stripped = content.lstrip()
            if stripped.startswith(("{", "[", '"filePath')):
                continue
            first_line = content.split("\n")[0].strip()
            if first_line and len(first_line) > 10:
                decisions.append(first_line[:200])
            if len(decisions) >= 5:
                break

        # Gather completed OMEGA tasks
        completed = []
        try:
            tasks = mgr.list_tasks(project=project, status="completed")
            for t in (tasks or [])[:5]:
                if t.get("session_id") == session_id:
                    completed.append(t["title"])
        except Exception:
            pass

        mgr.create_handoff(
            session_id=session_id,
            project=project,
            completed_tasks=completed or None,
            decisions_made=decisions or None,
        )
    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("auto_handoff", e)


def _nudge_handoff(session_id, project, mgr):
    """Nudge agent about incomplete work being orphaned."""
    try:
        all_tasks = mgr.list_tasks(project=project, status="in_progress")
        my_tasks = [t for t in all_tasks if t.get("session_id") == session_id]
        if my_tasks:
            task_list = ", ".join(f"#{t['id']} {t['title']}" for t in my_tasks[:3])
            print(
                f"[HANDOFF] Active work returned to queue: {task_list}\n"
                "  Next time, use omega_handoff(action='create', ...) before ending "
                "to give your successor structured context."
            )
    except Exception as e:
        _log_hook_error("nudge_handoff", e)


def _extract_project_entity(file_path):
    """Extract project name from a file path."""
    import re
    match = re.search(r'/Projects/([^/]+)', file_path)
    return match.group(1) if match else None


def _build_entity_links(claims, current_project):
    """Build cross-project entity links from file claims."""
    projects = set()
    for claim in claims:
        proj = _extract_project_entity(claim.get("file_path", ""))
        if proj and proj != current_project:
            projects.add(proj)

    return [
        {"from": current_project, "to": proj, "relationship": "depends_on"}
        for proj in sorted(projects)
    ]


def _detect_drift(original_task, files_modified, commits):
    """Detect if session work drifted from declared task via keyword overlap."""
    if not original_task or not str(original_task).strip():
        return {"drifted": False, "confidence": 0.0, "reason": "no task registered"}

    import re
    stop_words = {"the", "a", "an", "in", "on", "to", "for", "of", "and", "or", "is", "was", "be", "it", "this", "that", "with", "from", "fix", "add", "update", "implement"}
    task_words = set(w.lower() for w in re.findall(r'[a-zA-Z]{3,}', str(original_task))) - stop_words

    if not task_words:
        return {"drifted": False, "confidence": 0.0, "reason": "task too generic"}

    work_text = " ".join(list(files_modified or []) + list(commits or []))
    work_words = set(w.lower() for w in re.findall(r'[a-zA-Z]{3,}', work_text)) - stop_words

    if not work_words:
        return {"drifted": False, "confidence": 0.0, "reason": "no work tracked"}

    overlap = task_words & work_words
    overlap_ratio = len(overlap) / len(task_words) if task_words else 0
    drifted = overlap_ratio < 0.2
    confidence = round(1.0 - overlap_ratio, 2)
    reason = (f"Only {len(overlap)}/{len(task_words)} task keywords found in work" if drifted
              else f"{len(overlap)}/{len(task_words)} task keywords matched")

    return {"drifted": drifted, "confidence": confidence, "reason": reason}


def main():
    session_id = os.environ.get("SESSION_ID", "")
    if not session_id:
        return

    try:
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()
        project = os.environ.get("PROJECT_DIR", "")
        if project:
            mgr.enrich_session_metadata(session_id, project)
            _nudge_handoff(session_id, project, mgr)
            _auto_handoff(session_id, project, mgr)
            _broadcast_session_end(session_id, project, mgr)

            # Goal drift detection (Part C)
            try:
                session_claims = mgr.get_session_claims(session_id)
                claimed_files = []
                if isinstance(session_claims, dict):
                    for fp in session_claims.get("files", {}).keys():
                        claimed_files.append(fp)
                elif isinstance(session_claims, list):
                    for c in session_claims:
                        claimed_files.append(c.get("file_path", ""))

                git_events = mgr.get_recent_git_events(project=project, limit=20)
                my_commits = [e.get("message", "") for e in (git_events or [])
                              if e.get("session_id") == session_id and e.get("event_type") == "commit"]

                sessions = mgr.list_sessions(auto_clean=False)
                original_task = ""
                for s in sessions:
                    if s.get("session_id") == session_id:
                        original_task = s.get("task", "") or ""
                        break

                drift = _detect_drift(original_task, claimed_files, my_commits)
                if drift["drifted"]:
                    print(f'[OMEGA] Goal drift detected (confidence: {drift["confidence"]}): {drift["reason"]}', file=sys.stderr)
                    try:
                        from omega.bridge import auto_capture
                        auto_capture(
                            content=f"Goal drift: Agent registered task '{original_task}' but work diverged. {drift['reason']}",
                            event_type="lesson_learned", session_id=session_id, project=project,
                            metadata={"source": "auto_drift_check", "confidence": drift["confidence"]},
                        )
                    except Exception:
                        pass
            except Exception:
                pass

            # Auto entity links from cross-project file claims (Part C)
            try:
                claims_data = mgr.get_session_claims(session_id)
                claim_list = []
                if isinstance(claims_data, dict):
                    for fp in claims_data.get("files", {}).keys():
                        claim_list.append({"file_path": fp})
                elif isinstance(claims_data, list):
                    claim_list = [{"file_path": c.get("file_path", "")} for c in claims_data]

                proj_name = _extract_project_entity(project) or ""
                if proj_name:
                    links = _build_entity_links(claim_list, proj_name)
                    for link in links:
                        try:
                            from omega_platform.entity.engine import EntityEngine
                            ee = EntityEngine()
                            ee.add_relationship(link["from"], link["to"], link["relationship"])
                        except Exception:
                            pass
            except Exception:
                pass

        mgr.deregister_session(session_id)
    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("coord_session_stop", e)
        print(f"OMEGA coord_session_stop failed: {e}", file=sys.stderr)


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
    _log_timing("coord_session_stop", (time.monotonic() - _t0) * 1000)
