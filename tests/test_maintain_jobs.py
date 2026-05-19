"""Tests for the async-job path on long-running omega_maintain actions.

Verifies that heavy maintenance actions default to non-blocking job submission
(prevents MCP client RPC timeouts / "Server disconnected") and that job_status
polling returns the eventual result.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _is_error(result: dict) -> bool:
    return bool(result.get("isError"))


@pytest.mark.asyncio
async def test_consolidate_returns_job_id_when_async():
    """Default (no wait) submits a job and returns a job_id."""
    from omega.server.handlers import handle_omega_maintain

    with patch("omega.bridge.consolidate", return_value="OK"):
        result = await handle_omega_maintain({"action": "consolidate"})

    assert not _is_error(result)
    text = _text(result)
    assert text.startswith("Job submitted:"), text
    assert "Poll with: omega_maintain action=job_status job_id=" in text


@pytest.mark.asyncio
async def test_consolidate_blocks_when_wait_true():
    """wait=True bypasses the job registry and returns the result inline."""
    from omega.server.handlers import handle_omega_maintain

    with patch("omega.bridge.consolidate", return_value="Consolidation Report"):
        result = await handle_omega_maintain({"action": "consolidate", "wait": True})

    assert not _is_error(result)
    assert _text(result) == "Consolidation Report"


@pytest.mark.asyncio
async def test_job_status_returns_succeeded_after_completion():
    """Submit, poll until done, verify result is included."""
    from omega.server.handlers import handle_omega_maintain

    with patch("omega.bridge.backfill_embeddings", return_value={"processed": 7}):
        submit = await handle_omega_maintain({"action": "backfill_embeddings"})

    job_line = next(
        line for line in _text(submit).splitlines() if line.startswith("Job submitted:")
    )
    job_id = job_line.split(":", 1)[1].strip()

    # Poll up to 5s
    final = None
    for _ in range(50):
        status = await handle_omega_maintain({"action": "job_status", "job_id": job_id})
        assert not _is_error(status)
        text = _text(status)
        if "Status: succeeded" in text or "Status: failed" in text:
            final = text
            break
        time.sleep(0.1)

    assert final is not None, "Job did not finish within 5s"
    assert "Status: succeeded" in final, final
    assert "'processed': 7" in final


@pytest.mark.asyncio
async def test_job_status_missing_job_id():
    """job_status without job_id returns a usage error."""
    from omega.server.handlers import handle_omega_maintain

    result = await handle_omega_maintain({"action": "job_status"})
    assert _is_error(result)
    assert "job_id is required" in _text(result)


@pytest.mark.asyncio
async def test_job_status_unknown_id():
    """Unknown job_id returns a not-found error."""
    from omega.server.handlers import handle_omega_maintain

    result = await handle_omega_maintain({"action": "job_status", "job_id": "deadbeef00"})
    assert _is_error(result)
    assert "not found" in _text(result).lower()


@pytest.mark.asyncio
async def test_job_records_failure_when_bridge_raises():
    """Bridge exceptions are captured as Job.error, not raised to the caller."""
    from omega.server.handlers import handle_omega_maintain

    with patch("omega.bridge.compact", side_effect=RuntimeError("boom")):
        submit = await handle_omega_maintain({"action": "compact"})

    job_id = next(
        line for line in _text(submit).splitlines() if line.startswith("Job submitted:")
    ).split(":", 1)[1].strip()

    final = None
    for _ in range(50):
        status = await handle_omega_maintain({"action": "job_status", "job_id": job_id})
        text = _text(status)
        if "Status: succeeded" in text or "Status: failed" in text:
            final = text
            break
        time.sleep(0.1)

    assert final is not None
    assert "Status: failed" in final
    assert "RuntimeError: boom" in final


def test_job_registry_evicts_expired():
    """Finished jobs older than TTL are evicted on next submit."""
    from omega.server import jobs

    registry = jobs.JobRegistry(_DirectExecutor())
    j = registry.submit("test", lambda: "ok")
    # Force expiry
    j.finished_at = time.time() - (jobs.JOB_TTL_SECONDS + 10)
    registry.submit("test2", lambda: "ok2")
    assert registry.get(j.id) is None


class _DirectExecutor:
    """Synchronous stub executor for registry-eviction unit test."""

    def submit(self, fn):
        class _F:
            pass

        fn()
        return _F()
