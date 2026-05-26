"""Regression guard: MCP server no longer owns hook lifecycle (follow-up #8b)."""
import inspect

import omega.server.mcp_server as mcp
import omega.server.http_server as http_mod


def test_mcp_server_has_no_hook_executor():
    assert not hasattr(mcp, "_HOOK_EXECUTOR")


def test_mcp_server_has_no_socket_watchdog():
    assert not hasattr(mcp, "_socket_watchdog")


def test_mcp_server_has_no_coordination_tick():
    assert not hasattr(mcp, "_run_coordination_tick")
    assert not hasattr(mcp, "_coordination_loop")


def test_mcp_server_does_not_import_hook_server_lifecycle():
    # neither symbol should be a module attribute
    assert "start_hook_server" not in dir(mcp)
    assert "stop_hook_server" not in dir(mcp)


def test_http_server_run_http_does_not_call_start_hook_server():
    src = inspect.getsource(http_mod.run_http)
    assert "start_hook_server" not in src, (
        "http_server.run_http must not start the in-process hook server; "
        "the standalone hook_daemon is the sole host (follow-up #8b)."
    )
