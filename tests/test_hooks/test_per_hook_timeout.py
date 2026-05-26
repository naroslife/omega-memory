"""Per-hook HANDLER_TIMEOUT overrides (follow-up #1 from pure-twirling-brook).

Heavy hooks (session_start, session_stop, surface_memories, auto_capture) get
a wider asyncio.wait_for budget than the tight 4s default that pre-* guards
inherit. Operators can override per-hook values at runtime via the
OMEGA_HOOK_TIMEOUTS_JSON env var (JSON object).
"""
from __future__ import annotations

import importlib
import json
import time

import pytest


# ---------------------------------------------------------------------------
# Unit: _timeout_for() resolution
# ---------------------------------------------------------------------------


def test_default_timeout_for_unknown_hook():
    """Hooks without an override fall back to HANDLER_TIMEOUT (4.0 s)."""
    from omega_platform.server.hook_server import core

    assert core._timeout_for("pre_push_guard") == core.HANDLER_TIMEOUT
    assert core._timeout_for("pre_push_guard") == 4.0


def test_override_returns_custom_budget():
    """Known heavy hooks return their configured budget, not the default."""
    from omega_platform.server.hook_server import core

    assert core._timeout_for("session_stop") == 4.5
    assert core._timeout_for("session_start") == 10.0
    assert core._timeout_for("surface_memories") == 8.0
    assert core._timeout_for("auto_capture") == 6.0


def test_env_var_override_merges_without_clobbering_defaults(monkeypatch):
    """OMEGA_HOOK_TIMEOUTS_JSON merges onto defaults rather than replacing them."""
    monkeypatch.setenv("OMEGA_HOOK_TIMEOUTS_JSON", json.dumps({"my_hook": 2.5}))

    from omega_platform.server.hook_server import core

    importlib.reload(core)
    try:
        assert core._timeout_for("my_hook") == 2.5
        # Built-in defaults survive the merge.
        assert core._timeout_for("session_stop") == 4.5
        assert core._timeout_for("pre_push_guard") == core.HANDLER_TIMEOUT
    finally:
        monkeypatch.delenv("OMEGA_HOOK_TIMEOUTS_JSON", raising=False)
        importlib.reload(core)


def test_env_var_override_ignores_malformed_json(monkeypatch, caplog):
    """Invalid JSON logs a warning and is otherwise inert."""
    monkeypatch.setenv("OMEGA_HOOK_TIMEOUTS_JSON", "not-json{")

    from omega_platform.server.hook_server import core

    with caplog.at_level("WARNING", logger="omega.hook_server"):
        importlib.reload(core)
    try:
        assert core._timeout_for("session_stop") == 4.5
        assert any("OMEGA_HOOK_TIMEOUTS_JSON" in m for m in caplog.messages)
    finally:
        monkeypatch.delenv("OMEGA_HOOK_TIMEOUTS_JSON", raising=False)
        importlib.reload(core)


# ---------------------------------------------------------------------------
# End-to-end (mocked): handle_connection honours the per-hook budget
# ---------------------------------------------------------------------------


class _FakeReader:
    def __init__(self, payload: bytes):
        self._payload = payload
        self._sent = False

    async def read(self, _n: int) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._payload


class _FakeWriter:
    def __init__(self):
        self.buf = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _make_payload(hook: str) -> bytes:
    return json.dumps({"hook": hook, "session_id": "s-per-hook-timeout"}).encode("utf-8")


async def _drive_connection(hook_name: str) -> tuple[dict, float]:
    """Drive handle_connection for one fake hook, returning (response, elapsed)."""
    from omega_platform.server.hook_server import core

    reader = _FakeReader(_make_payload(hook_name))
    writer = _FakeWriter()

    started = time.monotonic()
    await core.handle_connection(reader, writer)
    elapsed = time.monotonic() - started

    response = json.loads(bytes(writer.buf).decode("utf-8")) if writer.buf else {}
    return response, elapsed


@pytest.mark.slow
async def test_handle_connection_honours_per_hook_timeout(monkeypatch):
    """A slow handler completes under the 8s surface_memories budget but trips the 4s default."""
    from omega_platform.server.hook_server import core

    sleep_seconds = 5.0  # > default 4.0 but < surface_memories 8.0

    def _slow_handler(_request):
        # Runs in the ThreadPoolExecutor — block, don't await.
        time.sleep(sleep_seconds)
        return {"output": "ok", "error": ""}

    # Register the same blocking handler for both hook names so the only
    # variable is the per-hook timeout budget.
    monkeypatch.setitem(core.HOOK_HANDLERS, "surface_memories", _slow_handler)
    monkeypatch.setitem(core.HOOK_HANDLERS, "pre_push_guard", _slow_handler)

    # surface_memories has an 8s budget → completes.
    response, elapsed = await _drive_connection("surface_memories")
    assert response.get("output") == "ok", f"expected success, got {response!r}"
    assert response.get("error") == "", f"expected no error, got {response!r}"
    assert elapsed < 7.0, f"expected ~{sleep_seconds}s, got {elapsed:.2f}s"
    assert elapsed >= sleep_seconds - 0.2

    # pre_push_guard inherits the 4s default → times out before the handler returns.
    response, elapsed = await _drive_connection("pre_push_guard")
    assert response.get("error") == "handler timeout", f"expected timeout, got {response!r}"
    assert elapsed < sleep_seconds, (
        f"timeout fired late ({elapsed:.2f}s); should be ~4s, well under {sleep_seconds}s"
    )
