"""Shared SQLite utilities for OMEGA.

Centralizes retry logic and connection helpers used across sqlite_store.py
and coordination.py to avoid code duplication.
"""

import logging
import sqlite3
import time

logger = logging.getLogger("omega.db_utils")

# SQLite retry — handles multi-process write contention on shared omega.db.
# WAL mode + busy_timeout handle most cases, but under heavy contention
# (3+ MCP server processes) the busy_timeout can still expire. This wrapper
# retries with exponential backoff before surfacing the error.
DB_RETRY_ATTEMPTS = 5
DB_RETRY_BASE_DELAY = 1.0  # seconds


def retry_on_locked(fn, *args, **kwargs):
    """Call fn with retry on 'database is locked' OperationalError."""
    for attempt in range(DB_RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < DB_RETRY_ATTEMPTS - 1:
                delay = DB_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("database is locked (attempt %d/%d), retrying in %.1fs",
                               attempt + 1, DB_RETRY_ATTEMPTS, delay)
                time.sleep(delay)
            else:
                if "database is locked" in str(e):
                    _enrich_and_raise_lock_error(e)
                raise


def _enrich_and_raise_lock_error(original: Exception) -> None:
    """Re-raise a lock error with active process diagnostic info."""
    try:
        from omega_platform.server.pid_registry import format_lock_diagnostic
        diag = format_lock_diagnostic()
    except Exception:
        diag = "Run `ps aux | grep omega` to check for stale processes"
    raise sqlite3.OperationalError(
        f"database is locked after {DB_RETRY_ATTEMPTS} retries. {diag}. "
        f"If a stale process is holding the lock, kill it and retry."
    ) from original


def retry_write_on_locked(conn, fn, *args, **kwargs):
    """Call fn with retry on 'database is locked', using BEGIN IMMEDIATE.

    Unlike retry_on_locked (which wraps a single statement), this wraps an
    entire write transaction. Uses BEGIN IMMEDIATE so the write lock is
    acquired upfront, allowing busy_timeout to work for the waiting period.
    With deferred transactions (Python's default), the lock isn't requested
    until the first write statement, at which point busy_timeout has no effect
    if another process already holds a conflicting lock.

    On failure, the uncommitted transaction is rolled back before retrying so
    the next attempt starts with a clean slate.
    """
    for attempt in range(DB_RETRY_ATTEMPTS):
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(*args, **kwargs)
            conn.commit()
            return result
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < DB_RETRY_ATTEMPTS - 1:
                delay = DB_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("database is locked (attempt %d/%d), rolling back and retrying in %.1fs",
                               attempt + 1, DB_RETRY_ATTEMPTS, delay)
                try:
                    conn.rollback()
                except Exception:
                    pass
                time.sleep(delay)
            else:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if "database is locked" in str(e):
                    _enrich_and_raise_lock_error(e)
                raise
