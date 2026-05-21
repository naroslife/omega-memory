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
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

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
