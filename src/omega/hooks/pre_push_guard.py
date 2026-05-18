#!/usr/bin/env python3
"""OMEGA PreToolUse hook — Git guard for push divergence + branch claims.

Triggered on Bash commands. Enforces:
  1. git push: blocks if origin has advanced (divergence guard)
  2. git checkout/switch: blocks if target branch is claimed by another agent
  3. git commit: blocks if current branch is claimed by another agent

Exit code 2 = block the tool call in Claude Code.
"""
import json
import os
import re
import subprocess
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

    # Use word-boundary matching for more robust git command detection
    if re.search(r'\bgit\s+push\b', command):
        _check_push_divergence(command)
        _auto_claim_branch(command)
        return

    if re.search(r'\bgit\s+(?:checkout|switch)\b', command):
        _check_branch_claims(command)
        return

    if re.search(r'\bgit\s+commit\b', command):
        _check_branch_claims(command)


def _check_push_divergence(command):
    """Block git push if origin has advanced."""
    project = os.environ.get("PROJECT_DIR", os.getcwd())
    session_id = os.environ.get("SESSION_ID", "")

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5, cwd=project,
        )
        if result.returncode != 0:
            return

        subprocess.run(
            ["git", "fetch", "origin", "--quiet"],
            capture_output=True, text=True, timeout=15, cwd=project,
        )

        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=project,
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

        behind_result = subprocess.run(
            ["git", "log", f"HEAD..origin/{branch}", "--oneline", "--no-decorate", "-10"],
            capture_output=True, text=True, timeout=5, cwd=project,
        )
        if behind_result.returncode != 0 or not behind_result.stdout.strip():
            _log_push_event(project, branch, session_id)
            return

        behind_lines = behind_result.stdout.strip().split("\n")
        count = len(behind_lines)

        # Log divergence event BEFORE exit (fix: was dead code after sys.exit)
        try:
            from omega_platform.orchestrator.coordination import get_manager
            mgr = get_manager()
            mgr.log_git_event(
                project=project,
                event_type="push_divergence_warning",
                branch=branch,
                message=f"{count} upstream commit(s) detected before push",
                session_id=session_id,
            )
        except (ImportError, Exception):
            pass

        print(f"\n[GIT-GUARD] BLOCKED: origin/{branch} has {count} commit(s) not in HEAD:")
        for line in behind_lines[:5]:
            print(f"  {line}")
        if count > 5:
            print(f"  ... and {count - 5} more")
        print("  Run 'git pull --rebase' before pushing to avoid conflicts.")
        sys.exit(2)

    except subprocess.TimeoutExpired:
        pass
    except FileNotFoundError:
        pass  # git not installed
    except Exception as e:
        _log_hook_error("pre_push_guard", e)


def _check_branch_claims(command):
    """Check branch claims for checkout/switch/commit commands."""
    session_id = os.environ.get("SESSION_ID", "")
    if not session_id:
        return  # No enforcement without session identity

    project = os.environ.get("PROJECT_DIR", os.getcwd())

    try:
        if "git commit" in command:
            branch = _get_current_branch(project)
            if branch:
                _block_if_branch_claimed(session_id, project, branch)
            return

        target = _parse_checkout_target(command)
        if target:
            _block_if_branch_claimed(session_id, project, target)
            _block_if_directory_occupied(session_id, project, target)
            _auto_claim_on_checkout(session_id, project, target)

    except subprocess.TimeoutExpired:
        pass
    except FileNotFoundError:
        pass
    except Exception as e:
        _log_hook_error("branch_guard", e)


def _get_current_branch(project):
    """Get the current git branch name."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, timeout=5, cwd=project,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _parse_checkout_target(command):
    """Parse the target branch from git checkout/switch commands.

    Returns None for new branch creation (-b/-B/-c/-C/--orphan)
    and file restores (-- separator).
    """
    for segment in re.split(r'&&|\|\||;', command):
        segment = segment.strip()
        match = re.match(r'git\s+(checkout|switch)\s+(.*)', segment)
        if not match:
            continue
        args_str = match.group(2).strip()

        # Skip new branch creation
        if re.search(r'(?:^|\s)-[bBcC]\b', args_str):
            return None
        if '--orphan' in args_str:
            return None

        # Skip file restores
        if ' -- ' in args_str or args_str.startswith('-- '):
            return None

        # Extract: skip flags, take first positional arg
        for token in args_str.split():
            if token.startswith('-'):
                continue
            return token

    return None


def _block_if_branch_claimed(session_id, project, branch):
    """Block if the branch is claimed by another agent."""
    try:
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()
        info = mgr.check_branch(project, branch)

        if not info.get("claimed"):
            return

        if info.get("session_id") == session_id:
            return  # Self-claim

        owner = info.get("session_id", "unknown")[:20]
        owner_task = info.get("task") or "unknown task"
        print(
            f"\n[BRANCH-GUARD] BLOCKED: branch '{branch}' is claimed by session {owner} ({owner_task}).\n"
            f"  Options:\n"
            f"    1. Wait for the other agent to finish\n"
            f"    2. Ask other agent to call omega_branch_release\n"
            f"    3. Use a different feature branch"
        )
        sys.exit(2)

    except ImportError:
        pass  # OMEGA not installed — fail-open
    except SystemExit:
        raise  # Re-raise sys.exit(2)
    except Exception as e:
        _log_hook_error("branch_guard", e)


def _is_main_worktree(project):
    """Check if we're in the main git working tree (not a linked worktree)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5, cwd=project,
        )
        common_dir = result.stdout.strip() if result.returncode == 0 else ""
        result2 = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5, cwd=project,
        )
        git_dir = result2.stdout.strip() if result2.returncode == 0 else ""
        # In the main worktree, git-dir == git-common-dir (both .git)
        # In a linked worktree, git-dir is .git/worktrees/<name>
        return os.path.realpath(common_dir) == os.path.realpath(git_dir)
    except Exception:
        return True  # Assume main worktree if detection fails


def _block_if_directory_occupied(session_id, project, target_branch):
    """Block checkout if another agent has a branch claim in this working directory.

    Only enforced in the main working tree (worktrees are isolated).
    Prevents the scenario where Agent A is on branch X and Agent B does
    'git checkout Y' in the same directory, overwriting A's working tree.
    """
    if not _is_main_worktree(project):
        return  # Worktrees are isolated, no contention

    current_branch = _get_current_branch(project)
    if not current_branch or current_branch == target_branch:
        return  # Same branch or detached HEAD, no contention

    try:
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()

        info = mgr.check_branch(project, current_branch)
        if not info.get("claimed"):
            return  # Current branch not claimed, no contention

        if info.get("session_id") == session_id:
            return  # We own the claim — switching our own branch is fine

        owner = info.get("session_id", "unknown")[:20]
        owner_task = info.get("task") or "unknown task"
        safe_target = target_branch.replace("/", "-")
        print(
            f"\n[WORKTREE-GUARD] BLOCKED: This working directory is occupied by another agent.\n"
            f"  Branch '{current_branch}' is claimed by session {owner} ({owner_task}).\n"
            f"  Switching branches in a shared directory causes lost work.\n"
            f"\n  Use a worktree instead:\n"
            f"    git worktree add .claude/worktrees/{safe_target} {target_branch}\n"
            f"    cd .claude/worktrees/{safe_target}\n"
        )
        sys.exit(2)

    except ImportError:
        pass  # OMEGA not installed — fail-open
    except SystemExit:
        raise  # Re-raise sys.exit(2)
    except Exception as e:
        _log_hook_error("worktree_guard", e)


def _auto_claim_on_checkout(session_id, project, branch):
    """Auto-claim the target branch when an agent checks it out.

    This ensures branch claims exist early (not just on push), so the
    worktree guard can detect working directory contention between agents.
    """
    if not session_id or not branch or branch in ("main", "master", "HEAD"):
        return
    try:
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()
        mgr.claim_branch(
            project=project, branch=branch,
            session_id=session_id, task="checked out branch",
        )
    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("auto_claim_checkout", e)


def _auto_claim_branch(command):
    """Auto-claim the current branch before a push succeeds."""
    session_id = os.environ.get("SESSION_ID", "")
    if not session_id:
        return
    project = os.environ.get("PROJECT_DIR", os.getcwd())
    if not project or not os.path.isdir(project):
        return
    try:
        branch = _get_current_branch(project)
        if not branch or branch == "HEAD":
            return
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()
        mgr.claim_branch(project=project, branch=branch, session_id=session_id, task="pushing to remote")
    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("auto_claim_branch", e)


def _log_push_event(project, branch, session_id):
    """Log a push event to coordination."""
    try:
        from omega_platform.orchestrator.coordination import get_manager
        mgr = get_manager()

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=project,
        )
        commit_hash = result.stdout.strip() if result.returncode == 0 else None

        mgr.log_git_event(
            project=project,
            event_type="push",
            commit_hash=commit_hash,
            branch=branch,
            session_id=session_id,
        )
    except ImportError:
        pass
    except Exception as e:
        _log_hook_error("pre_push_guard_log", e)


if __name__ == "__main__":
    _t0 = time.monotonic()
    main()
    _log_timing("pre_git_guard", (time.monotonic() - _t0) * 1000)
