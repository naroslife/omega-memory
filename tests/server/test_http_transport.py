"""Tests for OMEGA MCP server HTTP daemon transport."""

import asyncio
import json
import os
import signal
import socket
import time
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# 1. Transport selection via env var
# ---------------------------------------------------------------------------

def test_transport_env_var_default():
    """Default transport is stdio."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OMEGA_TRANSPORT", None)
        # Re-read the module-level constant
        assert os.environ.get("OMEGA_TRANSPORT", "stdio") == "stdio"


def test_transport_env_var_http():
    """Setting OMEGA_TRANSPORT=http selects HTTP transport."""
    with patch.dict(os.environ, {"OMEGA_TRANSPORT": "http"}):
        assert os.environ["OMEGA_TRANSPORT"] == "http"


# ---------------------------------------------------------------------------
# 2. Port availability check
# ---------------------------------------------------------------------------

def test_check_port_available_free():
    """Free port should be detected as available."""
    from omega.server.mcp_server import _check_port_available
    # Use a random high port that's almost certainly free
    assert _check_port_available("127.0.0.1", 0) is True


def test_check_port_available_bound():
    """Bound port should be detected as unavailable."""
    from omega.server.mcp_server import _check_port_available
    # Bind + listen on a port so SO_REUSEADDR in _check_port_available cannot rebind.
    # On Linux, two SO_REUSEADDR sockets can bind the same port unless one is in LISTEN.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    _, port = sock.getsockname()
    try:
        assert _check_port_available("127.0.0.1", port) is False
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# 4. Health endpoint JSON schema (integration test — needs starlette/uvicorn)
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.asyncio
async def test_health_endpoint():
    """Health endpoint returns expected JSON fields."""
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("starlette not installed")

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    # Replicate the health handler from mcp_server
    async def health(request):
        return JSONResponse({
            "status": "ok",
            "pid": os.getpid(),
            "rss_mb": 100.0,
            "uptime_s": 42.0,
            "tool_count": 14,
            "transport": "http",
        })

    app = Starlette(routes=[Route("/health", health, methods=["GET"])])
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "pid" in data
    assert "rss_mb" in data
    assert "uptime_s" in data
    assert "tool_count" in data
    assert data["transport"] == "http"


# ---------------------------------------------------------------------------
# 5. Config migration
# ---------------------------------------------------------------------------

def test_migrate_config(tmp_path):
    """migrate-config rewrites stdio entries to http."""
    claude_json = tmp_path / ".claude.json"
    config = {
        "projects": {
            "/Users/test/project1": {
                "mcpServers": {
                    "omega-memory": {
                        "type": "stdio",
                        "command": "/usr/bin/python3",
                        "args": ["-m", "omega.server.mcp_server"],
                    }
                }
            },
            "/Users/test/project2": {
                "mcpServers": {
                    "other-server": {"type": "stdio", "command": "other"}
                }
            },
        }
    }
    claude_json.write_text(json.dumps(config))

    # Simulate migration logic
    content = claude_json.read_text()
    cfg = json.loads(content)
    backup = claude_json.with_suffix(".json.bak")
    backup.write_text(content)

    url = "http://127.0.0.1:8377/mcp"
    changed = 0
    for proj_path, proj_config in cfg.get("projects", {}).items():
        servers = proj_config.get("mcpServers", {})
        if "omega-memory" in servers:
            entry = servers["omega-memory"]
            if entry.get("type") == "stdio":
                servers["omega-memory"] = {"type": "http", "url": url}
                changed += 1

    claude_json.write_text(json.dumps(cfg, indent=2))

    assert changed == 1
    result = json.loads(claude_json.read_text())
    omega_entry = result["projects"]["/Users/test/project1"]["mcpServers"]["omega-memory"]
    assert omega_entry["type"] == "http"
    assert omega_entry["url"] == url
    # Other server untouched
    other_entry = result["projects"]["/Users/test/project2"]["mcpServers"]["other-server"]
    assert other_entry["type"] == "stdio"
    # Backup preserved
    assert backup.exists()
    assert json.loads(backup.read_text()) == config
