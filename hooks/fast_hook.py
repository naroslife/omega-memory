#!/usr/bin/env python3
"""OMEGA fast hook client — routes to daemon via UDS, falls back to direct.

This is a thin client that connects to the hook server running inside the
MCP process. It avoids importing any OMEGA modules on the fast path, keeping
startup to ~50ms (Python interpreter only).

If the daemon socket is unavailable (MCP not started yet, or crashed),
falls back to running the original hook script directly.
"""
import json
import os
import socket
import sys
import time

try:
    import fcntl
except ImportError:  # Windows: no fcntl — fall through to best-effort no-lock.
    fcntl = None  # type: ignore[assignment]

# Windows uses TCP loopback; Unix uses domain socket
if sys.platform == "win32":
    SOCK_PATH = None
    HOOK_HOST = "127.0.0.1"
    HOOK_PORT = 19876
else:
    from omega.socket_path import resolve_hook_socket_path
    SOCK_PATH = str(resolve_hook_socket_path())
    HOOK_HOST = None
    HOOK_PORT = None

# Map hook names to their original script modules for fallback
_FALLBACK_SCRIPTS = {
    "session_start": "session_start",
    "session_stop": "session_stop",
    "surface_memories": "surface_memories",
    "auto_capture": "auto_capture",
    "assistant_capture": "assistant_capture",
    "coord_session_start": "coord_session_start",
    "coord_session_stop": "coord_session_stop",
    "coord_heartbeat": "coord_heartbeat",
    "auto_claim_file": "auto_claim_file",
    "pre_file_guard": "pre_file_guard",
    "pre_task_guard": "pre_task_guard",
    "pre_push_guard": "pre_push_guard",
    "pre_deploy_guard": "pre_deploy_guard",
    "pre_commit_guard": "pre_commit_guard",
    "pre_protocol_gate": "pre_protocol_gate",
    "pre_alignment_gate": "pre_alignment_gate",
    "trace_capture": "trace_capture",
}
# Note: pre_irreversible_advisor is intentionally absent — it's daemon-only
# (advisory, never blocks) with no standalone fallback script.
# Note: pre_insight_surface is intentionally absent — it's daemon-only
# (hook_server/insights.py) with no standalone fallback script.

# Hooks that require longer timeouts (e.g., git network operations)
_SLOW_HOOKS = {"pre_push_guard"}

# Safety-critical hooks that MUST run even in fallback mode.
# These are pre-action guards that can block dangerous operations (exit code 2).
# All other hooks are informational and safe to skip if daemon is unavailable.
_BLOCKING_HOOKS = {
    "pre_file_guard", "pre_task_guard", "pre_push_guard",
    "pre_deploy_guard", "pre_commit_guard", "pre_alignment_gate",
    "pre_protocol_gate",
}

# Best-effort hooks run in fallback mode but never block the session.
# These capture high-value content that would otherwise be silently dropped.
_BEST_EFFORT_HOOKS = {
    "assistant_capture",
    "coord_session_stop",
    "trace_capture",         # captures content that would otherwise be lost
    # coord_session_start and coord_heartbeat intentionally excluded:
    # Their fallback paths import heavy OMEGA modules + hit SQLite, causing
    # 36-260s startup delays when 8-10 sessions race (fallback stampede).
    # The daemon handles these when it comes up; skipping fallback is safe.
}

# Retry settings for startup race (hook fires before MCP server opens socket)
# Kept low: retries only help during the narrow window where MCP server is
# actively starting.  A stale socket (daemon crashed/exited) is detected and
# cleaned up immediately — no retries needed for that case.
_CONNECT_RETRIES = 2
_CONNECT_RETRY_DELAY = 0.15  # seconds between retries


def _is_socket_stale(sock_path):
    """Check if a Unix domain socket is stale (no listener).

    A stale socket means the daemon that created it has exited without
    cleaning up.  We detect this via a non-blocking connect: if the OS
    immediately returns ECONNREFUSED, no process is listening.  In that
    case we remove the socket file so subsequent calls get a fast
    FileNotFoundError instead of wasting time on retries.

    Returns True if the socket was stale (and removed), False otherwise.
    """
    if sys.platform == "win32" or not sock_path:
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.setblocking(False)
        try:
            s.connect(sock_path)
        except BlockingIOError:
            # Connection in progress — daemon might be alive, not stale
            return False
        except ConnectionRefusedError:
            # No listener — stale socket
            try:
                os.unlink(sock_path)
            except OSError:
                pass
            return True
        except FileNotFoundError:
            return True  # Already gone
        except OSError:
            return False  # Unknown state, don't remove
        finally:
            s.close()
    except Exception:
        return False
    return False


def _detect_client() -> str:
    """Detect which AI coding client invoked this hook.

    Priority: explicit env var > heuristic detection > "unknown".
    The env var OMEGA_CLIENT is set by `omega setup --client <name>`.
    """
    client = os.environ.get("OMEGA_CLIENT", "")
    if client:
        return client
    # Heuristic: check for known client config paths
    if os.path.exists(os.path.expanduser("~/.claude/settings.json")):
        return "claude-code"
    if os.path.exists(os.path.expanduser("~/.cursor/settings.json")):
        return "cursor"
    return "unknown"


def delegate(hook_names, payload, timeout=5.0):
    """Connect to daemon, send request, return parsed response.

    Accepts a single hook name (str) or multiple (list) for batching.
    Batch requests use {"hooks": [...]} and return {"results": [...]}.
    """
    if sys.platform == "win32":
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((HOOK_HOST, HOOK_PORT))
    else:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCK_PATH)
    try:
        if isinstance(hook_names, list):
            request = json.dumps({"hooks": hook_names, **payload}).encode("utf-8")
        else:
            request = json.dumps({"hook": hook_names, **payload}).encode("utf-8")
        s.sendall(request)
        s.shutdown(socket.SHUT_WR)

        response = b""
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            response += chunk
        try:
            return json.loads(response.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Daemon crashed mid-write — treat as a transient connection error
            # so the retry loop in main() can handle it uniformly.
            raise OSError(f"daemon response parse error: {exc}") from exc
    finally:
        s.close()


def _fallback(hook_name, payload):
    """Run the original hook script directly (cold path).

    Sets env vars from *payload* before calling mod.main() so that
    individual hook scripts (which read os.environ) see the values
    that Claude Code passed via stdin JSON.
    """
    hooks_dir = os.path.dirname(os.path.abspath(__file__))
    script_name = _FALLBACK_SCRIPTS.get(hook_name)
    if not script_name:
        if hook_name in _BLOCKING_HOOKS:
            print(f"OMEGA: blocking hook '{hook_name}' has no fallback script — daemon-only", file=sys.stderr)
        return

    script_path = os.path.join(hooks_dir, f"{script_name}.py")
    if not os.path.exists(script_path):
        return

    # Bridge: set env vars from payload so hook scripts can read them.
    # Claude Code stdin JSON uses different field names than env vars:
    #   stdin: session_id, tool_name, tool_input (dict), tool_response, cwd
    #   env:   SESSION_ID, TOOL_NAME, TOOL_INPUT (str),  TOOL_OUTPUT,  PROJECT_DIR
    # _ENV_MAP is owned by _fallback_bridge — single source of truth.
    # Lazy import so a missing/broken bridge module never breaks the fast path.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from _fallback_bridge import _ENV_MAP  # type: ignore
    except Exception:
        try:
            from omega.hooks._fallback_bridge import _ENV_MAP  # type: ignore
        except Exception:
            _ENV_MAP = {
                "session_id": "SESSION_ID",
                "tool_name": "TOOL_NAME",
                "tool_input": "TOOL_INPUT",
                "tool_response": "TOOL_OUTPUT",
                "tool_output": "TOOL_OUTPUT",
                "cwd": "PROJECT_DIR",
                "project": "PROJECT_DIR",
            }
    for payload_key, env_key in _ENV_MAP.items():
        val = payload.get(payload_key)
        if val:
            # tool_input/tool_response may be dicts from JSON parse — serialize
            if isinstance(val, (dict, list)):
                os.environ[env_key] = json.dumps(val)
            else:
                os.environ[env_key] = str(val)

    # Add hooks dir to path so the script can be imported
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)

    import importlib
    try:
        mod = importlib.import_module(script_name)
        if hasattr(mod, "main"):
            import inspect
            sig = inspect.signature(mod.main)
            if sig.parameters:
                mod.main(payload)
            else:
                mod.main()
    except Exception as e:
        print(f"OMEGA hook fallback error ({hook_name}): {e}", file=sys.stderr)


_FALLBACK_BANNER = (
    "============================================================\n"
    "⚠  OMEGA daemon unavailable — running degraded fallback.\n"
    "   Restart with /mcp inside Claude Code to restore features.\n"
    "============================================================"
)
_FALLBACK_REMINDER_FMT = (
    "⚠  OMEGA fallback in use ({n} events). /mcp reconnect to restore."
)
_DEGRADED_BANNER_WINDOW_SEC = 300  # 5 minutes


def _resolve_session_id():
    """Resolve a stable per-session identifier for fallback bookkeeping."""
    sid = os.environ.get("SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    if sid:
        safe = "".join(c for c in sid if c.isalnum() or c in "-_")
        if safe:
            return safe[:128]
    import hashlib
    seed = f"{os.getppid()}:{int(time.time() // 3600)}"
    return "anon-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _emit_daemon_down_notice(hook_name):
    """Print a daemon-down banner / reminder to stderr (best-effort)."""
    try:
        quiet = os.environ.get("OMEGA_HOOK_QUIET") == "1"
        try:
            interval = int(os.environ.get("OMEGA_FALLBACK_REMINDER_EVERY", "20"))
        except (TypeError, ValueError):
            interval = 20
        if interval < 1:
            interval = 20

        sid = _resolve_session_id()
        from pathlib import Path
        sessions_dir = Path.home() / ".omega" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        counter_path = sessions_dir / f"{sid}.fallback_count"

        fd = os.open(str(counter_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except OSError:
                    pass
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                raw = b""
                while True:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    raw += chunk
                try:
                    current = int(raw.decode("ascii", errors="ignore").strip() or "0")
                except (ValueError, UnicodeDecodeError):
                    current = 0
                new_count = current + 1
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, str(new_count).encode("ascii"))
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
        finally:
            os.close(fd)

        try:
            from pathlib import Path as _P
            log_path = _P.home() / ".omega" / "hooks.log"
            log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            from datetime import datetime as _dt
            ts = _dt.now().isoformat(timespec="seconds")
            line = f"[{ts}] fast_hook/{hook_name}: fallback#{new_count}\n"
            lfd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(lfd, line.encode("utf-8"))
            finally:
                os.close(lfd)
        except Exception:
            pass

        if quiet:
            return
        if new_count == 1:
            try:
                print(_FALLBACK_BANNER, file=sys.stderr)
            except Exception:
                pass
        elif new_count > 1 and (new_count % interval) == 0:
            try:
                print(_FALLBACK_REMINDER_FMT.format(n=new_count), file=sys.stderr)
            except Exception:
                pass
    except Exception:
        pass


def _check_daemon_health_marker():
    """Surface daemon-side degradation marker (~/.omega/daemon-health.json)."""
    try:
        from pathlib import Path
        marker = Path.home() / ".omega" / "daemon-health.json"
        if not marker.exists():
            return
        try:
            with open(marker, "rb") as f:
                data = json.loads(f.read().decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return
        if not isinstance(data, dict):
            return
        ts_raw = data.get("degraded_at")
        if not isinstance(ts_raw, str):
            return
        from datetime import datetime, timezone
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return
        now = datetime.now(timezone.utc)
        age = (now - ts).total_seconds()
        if 0 <= age <= _DEGRADED_BANNER_WINDOW_SEC:
            _emit_daemon_down_notice("<degraded>")
    except Exception:
        pass


def _log_timing(hook_name, elapsed_ms, mode):
    """Log hook timing to ~/.omega/hooks.log."""
    try:
        from datetime import datetime
        from pathlib import Path
        log_path = Path.home() / ".omega" / "hooks.log"
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        timestamp = datetime.now().isoformat(timespec="seconds")
        data = f"[{timestamp}] fast_hook/{hook_name}: OK ({elapsed_ms:.0f}ms, {mode})\n"
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def _parse_payload():
    """Build payload from env vars + stdin JSON."""
    payload = {
        "tool_name": os.environ.get("TOOL_NAME", ""),
        "tool_input": os.environ.get("TOOL_INPUT", "{}"),
        "tool_output": os.environ.get("TOOL_OUTPUT", ""),
        "session_id": os.environ.get("SESSION_ID", ""),
        "project": os.environ.get("PROJECT_DIR", os.getcwd()),
        "client": _detect_client(),
        "caller_pid": os.getppid(),
    }

    # Claude Code sends hook data as JSON on stdin for ALL hook types.
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            if raw.strip():
                try:
                    stdin_data = json.loads(raw)
                    if isinstance(stdin_data, dict):
                        if "tool_response" in stdin_data and "tool_output" not in stdin_data:
                            stdin_data["tool_output"] = stdin_data["tool_response"]
                        if "cwd" in stdin_data and "project" not in stdin_data:
                            stdin_data["project"] = stdin_data["cwd"]
                        if isinstance(stdin_data.get("tool_input"), (dict, list)):
                            stdin_data["tool_input"] = json.dumps(stdin_data["tool_input"])
                        if isinstance(stdin_data.get("tool_output"), (dict, list)):
                            stdin_data["tool_output"] = json.dumps(stdin_data["tool_output"])
                        for key, val in stdin_data.items():
                            if val or not payload.get(key):
                                payload[key] = val
                except json.JSONDecodeError:
                    payload["stdin"] = raw
        except Exception:
            pass

    return payload


def main():
    if len(sys.argv) < 2:
        print("Usage: fast_hook.py <hook_name[+hook_name...]>", file=sys.stderr)
        sys.exit(1)

    # Surface daemon-side degradation BEFORE attempting connection — the
    # daemon may technically respond but be stuck (Issue 4B).
    _check_daemon_health_marker()

    t0 = time.monotonic()
    hook_names = sys.argv[1].split("+")
    payload = _parse_payload()
    is_batch = len(hook_names) > 1

    # Use longer timeout for hooks with network operations (e.g., git fetch)
    timeout = 5.0
    if _SLOW_HOOKS.intersection(hook_names):
        timeout = 20.0

    # Fast-path: if socket is stale (daemon exited without cleanup),
    # remove it immediately and skip to fallback — no retries needed.
    if SOCK_PATH and _is_socket_stale(SOCK_PATH):
        result = None
    else:
        # Try daemon connection with retries (handles startup race where
        # SessionStart hook fires before MCP server opens the socket).
        result = None
        for attempt in range(_CONNECT_RETRIES + 1):
            try:
                result = delegate(hook_names if is_batch else hook_names[0], payload, timeout=timeout)
                break
            except socket.timeout:
                break  # Daemon exists but slow — don't retry, fall through
            except FileNotFoundError:
                break  # Socket file missing — daemon not started, skip retries
            except (ConnectionRefusedError, OSError):
                if attempt < _CONNECT_RETRIES:
                    time.sleep(_CONNECT_RETRY_DELAY)

    elapsed_ms = (time.monotonic() - t0) * 1000

    if result is not None:
        # Daemon responded — process result
        if is_batch:
            outputs = []
            blocking_outputs = []
            exit_code = 0
            for r in result.get("results", []):
                if r.get("output"):
                    outputs.append(r["output"])
                    if r.get("exit_code"):
                        blocking_outputs.append(r["output"])
                if r.get("exit_code") and not exit_code:
                    exit_code = r["exit_code"]
            if outputs:
                print("\n".join(outputs))
            if exit_code and blocking_outputs:
                print("\n".join(blocking_outputs), file=sys.stderr)
            _log_timing("+".join(hook_names), elapsed_ms, "daemon")
            if exit_code:
                sys.exit(exit_code)
        else:
            if result.get("output"):
                print(result["output"])
                if result.get("exit_code"):
                    print(result["output"], file=sys.stderr)
            _log_timing(hook_names[0], elapsed_ms, "daemon")
            exit_code = result.get("exit_code")
            if exit_code:
                sys.exit(exit_code)
    else:
        # Daemon unavailable after retries.
        # Run fallback for safety-critical blocking hooks (pre_* guards)
        # and best-effort hooks (high-value captures that shouldn't be dropped).
        # Skip purely informational hooks to prevent the fallback stampede where
        # concurrent Python processes starve each other on CPU + SQLite locks.
        blocking = [h for h in hook_names if h in _BLOCKING_HOOKS]
        best_effort = [h for h in hook_names if h in _BEST_EFFORT_HOOKS]
        if blocking or best_effort:
            # Daemon-down user notification — fires once per session, then
            # every Nth event. Never raises.
            _emit_daemon_down_notice("+".join(hook_names))
        if blocking:
            for name in blocking:
                _fallback(name, payload)
        if best_effort:
            for name in best_effort:
                try:
                    _fallback(name, payload)
                except Exception:
                    pass  # Never block session for best-effort hooks
        if blocking or best_effort:
            elapsed_ms = (time.monotonic() - t0) * 1000
            _log_timing("+".join(hook_names), elapsed_ms, "fallback")
        else:
            _log_timing("+".join(hook_names), elapsed_ms, "skipped")


if __name__ == "__main__":
    main()
