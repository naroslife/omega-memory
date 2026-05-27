"""Tests for omega.cli._inject_settings_hooks event-aware migration.

Regression guard for follow-up #9: the session_stop hook was moved from
Stop -> SessionEnd in hooks-core.json. The injector must MOVE existing
Stop entries to SessionEnd on re-run instead of dual-binding.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from omega import cli


HOOKS_MANIFEST = {
    "Stop": [
        {"script": "fast_hook.py assistant_capture", "timeout": 3000, "matcher": ""},
    ],
    "SessionEnd": [
        {"script": "fast_hook.py session_stop", "timeout": 5000, "matcher": ""},
    ],
}


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect SETTINGS_JSON_PATH + DATA_DIR to a tmp area with a synthetic manifest."""
    settings_path = tmp_path / "settings.json"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "hooks-core.json").write_text(json.dumps(HOOKS_MANIFEST))

    monkeypatch.setattr(cli, "SETTINGS_JSON_PATH", settings_path)
    monkeypatch.setattr(cli, "DATA_DIR", data_dir)
    # Pin the python path so generated commands are deterministic.
    monkeypatch.setattr(cli, "_resolve_python_path", lambda: "/usr/bin/python3")
    # Force core-only path (no commercial modules).
    monkeypatch.setattr(cli, "_has_commercial_modules", lambda: False)
    return settings_path


def _hooks_dir(tmp_path: Path) -> Path:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    return hooks_dir


def _build_hook_entry(python_path: str, hooks_src: Path, script: str, timeout: int, matcher: str = "") -> dict:
    # Mirror cli._inject_settings_hooks: emit the portable Python module form
    # ("python -m omega.hooks.fast_hook <event>") rather than a file path so the
    # command works for global-wheel installs without an on-disk script path.
    parts = script.split()
    module = "omega.hooks." + Path(parts[0]).stem
    command = " ".join([python_path, "-m", module, *parts[1:]])
    return {
        "hooks": [
            {
                "command": command,
                "timeout": timeout,
                "type": "command",
            }
        ],
        "matcher": matcher,
    }


def test_already_migrated_no_dual_bind(isolated_settings, tmp_path, capsys):
    """settings.json is already in the post-#4 shape; re-running setup must be a no-op."""
    hooks_src = _hooks_dir(tmp_path)
    python_path = "/usr/bin/python3"

    settings = {
        "hooks": {
            "Stop": [
                _build_hook_entry(python_path, hooks_src, "fast_hook.py assistant_capture", 3000),
            ],
            "SessionEnd": [
                _build_hook_entry(python_path, hooks_src, "fast_hook.py session_stop", 5000),
            ],
        }
    }
    isolated_settings.write_text(json.dumps(settings))

    cli._inject_settings_hooks(hooks_src)

    result = json.loads(isolated_settings.read_text())
    stop = result["hooks"]["Stop"]
    session_end = result["hooks"]["SessionEnd"]

    # Stop has ONLY assistant_capture
    assert len(stop) == 1
    assert "assistant_capture" in stop[0]["hooks"][0]["command"]
    assert all("session_stop" not in h["command"] for entry in stop for h in entry["hooks"])

    # SessionEnd has exactly one session_stop (no duplication)
    assert len(session_end) == 1
    assert "session_stop" in session_end[0]["hooks"][0]["command"]

    out = capsys.readouterr().out
    assert "already configured" in out
    assert "migrated" not in out  # no migration on already-migrated state
    assert "hook(s) configured" not in out  # no new "configured" message
    assert "repaired" not in out


def test_pre_migration_config_is_migrated(isolated_settings, tmp_path, capsys):
    """Old shape (session_stop under Stop) -> migrated to SessionEnd."""
    hooks_src = _hooks_dir(tmp_path)
    python_path = "/usr/bin/python3"

    settings = {
        "hooks": {
            "Stop": [
                _build_hook_entry(python_path, hooks_src, "fast_hook.py assistant_capture", 3000),
                _build_hook_entry(python_path, hooks_src, "fast_hook.py session_stop", 5000),
            ],
        }
    }
    isolated_settings.write_text(json.dumps(settings))

    cli._inject_settings_hooks(hooks_src)

    result = json.loads(isolated_settings.read_text())
    stop = result["hooks"]["Stop"]
    session_end = result["hooks"]["SessionEnd"]

    # Stop has only assistant_capture
    assert len(stop) == 1
    assert "assistant_capture" in stop[0]["hooks"][0]["command"]

    # SessionEnd was created and holds session_stop
    assert len(session_end) == 1
    assert "session_stop" in session_end[0]["hooks"][0]["command"]
    assert session_end[0]["hooks"][0]["timeout"] == 5000

    out = capsys.readouterr().out
    assert "1 hook(s) migrated" in out


def test_fresh_install(isolated_settings, tmp_path, capsys):
    """Empty settings.json -> both events created with distinct top-level keys."""
    hooks_src = _hooks_dir(tmp_path)
    isolated_settings.write_text("{}")

    cli._inject_settings_hooks(hooks_src)

    result = json.loads(isolated_settings.read_text())
    assert "Stop" in result["hooks"]
    assert "SessionEnd" in result["hooks"]
    # Distinct top-level keys (not nested)
    assert result["hooks"]["Stop"] is not result["hooks"]["SessionEnd"]

    stop = result["hooks"]["Stop"]
    session_end = result["hooks"]["SessionEnd"]

    assert len(stop) == 1
    assert "assistant_capture" in stop[0]["hooks"][0]["command"]
    assert "session_stop" not in stop[0]["hooks"][0]["command"]

    assert len(session_end) == 1
    assert "session_stop" in session_end[0]["hooks"][0]["command"]

    out = capsys.readouterr().out
    assert "2 hook(s) configured" in out


def test_path_drift_on_migrated_hook(isolated_settings, tmp_path, capsys):
    """Stale python path on session_stop entry under SessionEnd is repaired."""
    hooks_src = _hooks_dir(tmp_path)
    current_python = "/usr/bin/python3"
    stale_python = "/old/path/python"

    settings = {
        "hooks": {
            "Stop": [
                _build_hook_entry(current_python, hooks_src, "fast_hook.py assistant_capture", 3000),
            ],
            "SessionEnd": [
                _build_hook_entry(stale_python, hooks_src, "fast_hook.py session_stop", 5000),
            ],
        }
    }
    isolated_settings.write_text(json.dumps(settings))

    cli._inject_settings_hooks(hooks_src)

    result = json.loads(isolated_settings.read_text())
    session_end = result["hooks"]["SessionEnd"]

    assert len(session_end) == 1
    cmd = session_end[0]["hooks"][0]["command"]
    assert cmd.startswith(current_python)
    assert stale_python not in cmd
    assert "session_stop" in cmd

    # No accidental duplication or migration.
    stop = result["hooks"]["Stop"]
    assert len(stop) == 1
    assert "session_stop" not in stop[0]["hooks"][0]["command"]

    out = capsys.readouterr().out
    assert "repaired" in out
    assert "migrated" not in out
