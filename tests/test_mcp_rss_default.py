"""Regression: stdio MCP server defaults to 4096 MB RSS limit (W4).

Commit ``81b67c6`` ("fix(hooks,mcp): unblock daemon auto-spawn on stale
socket; bump RSS limit to 4 GB") bumped the stdio-transport default from
1024 MB to 4096 MB while keeping the HTTP-transport default at 8192 MB and
preserving ``OMEGA_RSS_LIMIT_MB`` as an override.

These tests pin all three of those properties.
"""

from __future__ import annotations

import importlib

import pytest


def test_rss_limit_default_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default for stdio transport is 4 GB (was 1 GB before W4)."""
    monkeypatch.delenv("OMEGA_RSS_LIMIT_MB", raising=False)
    monkeypatch.delenv("OMEGA_TRANSPORT", raising=False)
    import omega.server.mcp_server as mcp
    importlib.reload(mcp)
    assert mcp._RSS_LIMIT_BYTES == 4096 * 1024 * 1024, (
        f"Expected 4 GB default for stdio, got {mcp._RSS_LIMIT_BYTES} bytes "
        f"({mcp._RSS_LIMIT_BYTES / 1024 / 1024} MB). "
        "W4 (commit 81b67c6) bumped this from 1024 to 4096."
    )


def test_rss_limit_env_var_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMEGA_RSS_LIMIT_MB env var still overrides the default."""
    monkeypatch.setenv("OMEGA_RSS_LIMIT_MB", "2048")
    monkeypatch.delenv("OMEGA_TRANSPORT", raising=False)
    import omega.server.mcp_server as mcp
    importlib.reload(mcp)
    assert mcp._RSS_LIMIT_BYTES == 2048 * 1024 * 1024


def test_rss_limit_http_default_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP transport default remains 8 GB."""
    monkeypatch.setenv("OMEGA_TRANSPORT", "http")
    monkeypatch.delenv("OMEGA_RSS_LIMIT_MB", raising=False)
    import omega.server.mcp_server as mcp
    importlib.reload(mcp)
    assert mcp._RSS_LIMIT_BYTES == 8192 * 1024 * 1024
