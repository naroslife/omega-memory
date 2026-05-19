"""In-memory job registry for long-running MCP maintenance actions.

Long-running tools (consolidate, compact, backfill_embeddings, backup, restore,
discover_connections, synthesize_insights) can exceed the MCP client's per-call
RPC timeout. To avoid "Server disconnected" errors, those actions submit a job
to the shared SQLite executor and return a job_id immediately. Callers poll
status via omega_maintain action=job_status.

Jobs are in-memory only. A server restart loses queued/running state. Heavy
maintenance is rare enough that durable persistence is not justified at v1.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

logger = logging.getLogger("omega.server.jobs")

JOB_TTL_SECONDS = 600  # Keep completed jobs queryable for 10 min after finish.
MAX_JOBS = 64  # Cap registry size; oldest finished jobs evicted first.


class Job:
    __slots__ = (
        "id",
        "name",
        "status",
        "submitted_at",
        "started_at",
        "finished_at",
        "result",
        "error",
        "_future",
    )

    def __init__(self, job_id: str, name: str) -> None:
        self.id = job_id
        self.name = name
        self.status: str = "queued"
        self.submitted_at: float = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.result: Any = None
        self.error: Optional[str] = None
        self._future: Optional[Future] = None

    def to_dict(self) -> dict:
        out = {
            "job_id": self.id,
            "name": self.name,
            "status": self.status,
            "submitted_at": self.submitted_at,
        }
        if self.started_at is not None:
            out["started_at"] = self.started_at
        if self.finished_at is not None:
            out["finished_at"] = self.finished_at
            out["elapsed_seconds"] = round(self.finished_at - (self.started_at or self.submitted_at), 3)
        if self.status == "succeeded":
            out["result"] = self.result
        elif self.status == "failed":
            out["error"] = self.error
        return out


class JobRegistry:
    def __init__(self, executor: ThreadPoolExecutor) -> None:
        self._executor = executor
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, name: str, fn: Callable[[], Any]) -> Job:
        """Submit a sync callable to run on the shared executor. Returns the Job."""
        self._evict_expired_locked()
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id, name)
        with self._lock:
            self._jobs[job_id] = job

        def _runner() -> None:
            job.status = "running"
            job.started_at = time.time()
            try:
                job.result = fn()
                job.status = "succeeded"
            except Exception as e:
                logger.exception("Job %s (%s) failed", job_id, name)
                job.error = f"{type(e).__name__}: {e}"
                job.status = "failed"
            finally:
                job.finished_at = time.time()

        job._future = self._executor.submit(_runner)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.submitted_at, reverse=True)

    def _evict_expired_locked(self) -> None:
        now = time.time()
        with self._lock:
            expired = [
                jid for jid, j in self._jobs.items()
                if j.finished_at is not None and (now - j.finished_at) > JOB_TTL_SECONDS
            ]
            for jid in expired:
                self._jobs.pop(jid, None)
            if len(self._jobs) > MAX_JOBS:
                finished = sorted(
                    (j for j in self._jobs.values() if j.finished_at is not None),
                    key=lambda j: j.finished_at or 0,
                )
                overflow = len(self._jobs) - MAX_JOBS
                for j in finished[:overflow]:
                    self._jobs.pop(j.id, None)


_registry: Optional[JobRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> JobRegistry:
    """Return the process-wide job registry, lazily created against _SQLITE_EXECUTOR."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                from omega.server.mcp_server import _SQLITE_EXECUTOR
                _registry = JobRegistry(_SQLITE_EXECUTOR)
    return _registry
