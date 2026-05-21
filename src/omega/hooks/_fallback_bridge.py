#!/usr/bin/env python3
"""OMEGA fallback bridge — shared helpers for daemon-handler fallback path.

This module is imported by individual standalone hook scripts (session_start.py,
pre_push_guard.py, etc.) AND by fast_hook.py. It must remain stdlib-only at
module load time so that core-only installs (no omega_platform) can still
import it without surfacing optional dependencies.

Public symbols (consumed by Streams C/D):
- ``_ENV_MAP``                — payload-key → env-var-name mapping (single
                                source of truth, was previously duplicated in
                                fast_hook.py).
- ``build_payload_from_env()``— rebuild a payload dict from env vars and/or
                                stdin JSON (inverse of fast_hook's env-set
                                logic).
- ``emit_result(result)``     — translate a daemon-handler-style result dict
                                to the standalone-script protocol (stdout +
                                exit code).
- ``try_daemon_handler(...)`` — import a daemon handler and invoke it on the
                                given payload. Returns the result dict on
                                success or ``None`` if the handler is
                                unavailable / raised.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("omega.hooks._fallback_bridge")

# Payload-key → env-var-name map. Source of truth for both fast_hook's
# env-set logic and the inverse env-read logic used here. Mirrors the
# original block at src/omega/hooks/fast_hook.py:196-204.
_ENV_MAP: dict[str, str] = {
    "session_id": "SESSION_ID",
    "tool_name": "TOOL_NAME",
    "tool_input": "TOOL_INPUT",
    "tool_response": "TOOL_OUTPUT",  # Claude Code calls it tool_response
    "tool_output": "TOOL_OUTPUT",    # legacy/internal name
    "cwd": "PROJECT_DIR",
    "project": "PROJECT_DIR",        # internal name used by some hooks
}


def _log_hook_error(hook_name: str, error: object) -> None:
    """Append a structured error record to ~/.omega/hooks.log. Never raises."""
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


def _maybe_json(value: str) -> object:
    """Best-effort JSON parse; fall through to the raw string on failure."""
    if not value:
        return value
    stripped = value.lstrip()
    if not stripped or stripped[0] not in "{[":
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


def build_payload_from_env() -> dict:
    """Reconstruct a payload dict from env vars (and stdin JSON if present).

    Returns a dict shaped like the payload fast_hook normally hands to a
    daemon handler — lowercase keys ("session_id", "tool_name", "tool_input",
    "cwd", etc.).

    Lookup order per key:
    1. Env vars per ``_ENV_MAP`` (fast path — what fast_hook itself set).
    2. Stdin JSON, only when no TTY is attached and no env var was found.
       Stdin keys overlay env-derived values only when the env value is
       empty / missing.

    Best-effort: malformed stdin JSON is captured in payload["stdin"] as raw.
    """
    payload: dict[str, object] = {}
    for payload_key, env_key in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is None or val == "":
            continue
        # tool_input / tool_output may have been serialized by fast_hook —
        # parse back when it looks like JSON so handlers see structured data.
        if payload_key in ("tool_input", "tool_output", "tool_response"):
            payload[payload_key] = _maybe_json(val)
        else:
            payload[payload_key] = val

    # Default cwd / project to os.getcwd() if neither env var supplied them.
    if "cwd" not in payload and "project" not in payload:
        payload["cwd"] = os.getcwd()
        payload["project"] = os.getcwd()

    # Pull stdin JSON only when env vars didn't fully populate the payload
    # and stdin is connected to a pipe (not a TTY).
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw and raw.strip():
                try:
                    stdin_data = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    payload["stdin"] = raw
                else:
                    if isinstance(stdin_data, dict):
                        # Normalize Claude Code's stdin field names.
                        if "tool_response" in stdin_data and "tool_output" not in stdin_data:
                            stdin_data["tool_output"] = stdin_data["tool_response"]
                        if "cwd" in stdin_data and "project" not in stdin_data:
                            stdin_data["project"] = stdin_data["cwd"]
                        for k, v in stdin_data.items():
                            if v and not payload.get(k):
                                payload[k] = v
    except Exception:
        # Stdin read failures must not break the fallback path.
        pass

    return payload


def emit_result(result: dict | None) -> None:
    """Translate a daemon-handler result dict to the standalone protocol.

    Daemon handlers return ``{"output": str, "error": str, "exit_code": int}``.
    Standalone hooks communicate via stdout / stderr / exit code.

    Never raises. Always terminates via ``sys.exit`` so the caller does not
    need to handle anything afterwards.
    """
    if not isinstance(result, dict):
        sys.exit(0)
    try:
        output = result.get("output", "") or ""
        if output:
            try:
                print(output)
            except Exception:
                pass
        error = result.get("error", "") or ""
        if error:
            try:
                print(error, file=sys.stderr)
            except Exception:
                pass
        try:
            exit_code = int(result.get("exit_code", 0) or 0)
        except (TypeError, ValueError):
            exit_code = 0
    except Exception:
        exit_code = 0
    sys.exit(exit_code)


def try_daemon_handler(module_path: str, func_name: str, payload: dict) -> dict | None:
    """Import a daemon handler module and invoke ``func_name(payload)``.

    Returns the handler's result dict on success. Returns ``None`` if:
    - ``omega_platform`` is not installed (``ImportError``) — silent: this is
      the expected case on core-only installs and the caller drops through to
      its legacy fallback.
    - Any other exception propagates from the handler — logged via
      ``_log_hook_error`` so we can debug subprocess-context mismatches
      (missing in-memory daemon state, etc.), then ``None``.
    """
    try:
        import importlib

        mod = importlib.import_module(module_path)
    except ImportError:
        return None
    try:
        handler = getattr(mod, func_name)
    except AttributeError as exc:
        _log_hook_error(f"{module_path}.{func_name}", exc)
        return None
    try:
        result = handler(payload)
    except Exception as exc:
        _log_hook_error(f"{module_path}.{func_name}", exc)
        return None
    if isinstance(result, dict):
        return result
    return None


# ---------------------------------------------------------------------------
# Hook daemon auto-start (mirrors omega_platform.embedding_client pattern).
#
# When fast_hook's first connect attempt fails (FileNotFoundError or
# ConnectionRefusedError), the in-process MCP hook server is unreachable.
# Spawn the standalone hook daemon as a fallback executor and poll for its
# socket. The daemon's PID lock guarantees at most one instance — concurrent
# fast_hook invocations against a dead daemon will have at most one win the
# spawn race; the others observe the socket appearing and connect.
# ---------------------------------------------------------------------------


def _hook_daemon_paths() -> tuple[Path, Path, Path]:
    """Resolve (socket_path, pid_path, log_path) under the *current* HOME.

    Resolved fresh on each call so tests overriding HOME via monkeypatch see
    the right paths without module reloads.
    """
    from omega.socket_path import resolve_hook_socket_path

    omega_dir = Path.home() / ".omega"
    sock_path = resolve_hook_socket_path()
    pid_path = omega_dir / "hook-daemon.pid"
    log_path = omega_dir / "hook_daemon.log"
    return sock_path, pid_path, log_path


def _is_hook_daemon_alive(pid_path: Path) -> bool:
    """Check whether the PID in ``pid_path`` is a live process."""
    if not pid_path.exists():
        return False
    try:
        pid_text = pid_path.read_text().strip()
        if not pid_text:
            return False
        pid = int(pid_text)
        os.kill(pid, 0)  # Signal 0 — existence probe only
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def _probe_hook_socket(sock_path: Path) -> bool:
    """Best-effort non-blocking connect probe. Returns True if a listener responds."""
    if sys.platform == "win32" or not sock_path.exists():
        return False
    import socket as _socket

    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        s.connect(str(sock_path))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _cleanup_stale_hook_daemon(sock_path: Path, pid_path: Path) -> None:
    """Remove stale socket and PID files for a dead hook daemon. Best-effort."""
    for p in (sock_path, pid_path):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def _auto_start_hook_daemon() -> bool:
    """Spawn the standalone hook daemon if it is not running.

    Returns True if a daemon is reachable by the end of the call (either
    already running or spawned successfully), False otherwise. Best-effort:
    any unexpected exception is swallowed and logged at DEBUG level.

    Mirrors ``omega_platform.embedding_client._auto_start_daemon``:
      * Skip on Windows.
      * If the socket exists and accepts a connect, return True.
      * If PID + socket files look stale, remove them before spawning.
      * Spawn ``python -m omega_platform.server.hook_daemon`` detached.
      * Poll up to 3 s (30 × 0.1 s) for the socket to appear.
    """
    if sys.platform == "win32":
        return False
    try:
        sock_path, pid_path, log_path = _hook_daemon_paths()

        # Fast path: daemon already alive and socket reachable.
        if _is_hook_daemon_alive(pid_path) and _probe_hook_socket(sock_path):
            return True

        # Stale files (dead daemon or crashed startup) — clean before spawn.
        if sock_path.exists() or pid_path.exists():
            logger.debug("Cleaning up stale hook daemon files")
            _cleanup_stale_hook_daemon(sock_path, pid_path)

        # Spawn.
        python = sys.executable or "python3"
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_file = open(log_path, "a")  # noqa: SIM115 — owned by subprocess stderr
        subprocess.Popen(
            [python, "-m", "omega_platform.server.hook_daemon"],
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=True,
        )

        # Poll for the socket to appear and accept connects.
        for _ in range(30):
            time.sleep(0.1)
            if _probe_hook_socket(sock_path):
                return True
        logger.debug("Hook daemon auto-start timed out waiting for socket")
        return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Hook daemon auto-start failed: %s", exc)
        return False
