"""OMEGA diagnostics — functional and liveness probes for ``omega doctor``.

Each probe is a zero-argument callable returning a :class:`ProbeResult` dict.
The :func:`run_probes` orchestrator wraps every probe in a hard timeout so a
hung daemon cannot hang ``omega doctor``.

Heavy imports (embedding, reranker, socket) are deferred to the probe bodies
so importing this module is cheap.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import time
from pathlib import Path
from typing import Callable, List, Literal, Optional, TypedDict

logger = logging.getLogger("omega.diagnostics")

OMEGA_DIR = Path(os.environ.get("OMEGA_HOME") or (Path.home() / ".omega"))

Status = Literal["ok", "warn", "fail", "skip"]


class ProbeResult(TypedDict):
    name: str
    status: Status
    detail: str
    latency_ms: float


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_probes(
    timeout: float,
    probes: List[Callable[[], ProbeResult]],
) -> List[ProbeResult]:
    """Run each probe with a hard timeout. Exceptions and timeouts become ``fail``.

    Uses a non-blocking executor shutdown so a hung worker thread does not block
    the caller — the daemon thread is abandoned and the process exits cleanly.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout

    results: List[ProbeResult] = []
    for probe in probes:
        name = getattr(probe, "__name__", "probe").removeprefix("probe_")
        start = time.monotonic()
        ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="doctor-probe")
        try:
            future = ex.submit(probe)
            try:
                result = future.result(timeout=timeout)
            except _Timeout:
                elapsed_ms = (time.monotonic() - start) * 1000.0
                results.append(ProbeResult(
                    name=name,
                    status="fail",
                    detail=f"timeout after {timeout:.1f}s",
                    latency_ms=elapsed_ms,
                ))
                continue
            except Exception as e:
                elapsed_ms = (time.monotonic() - start) * 1000.0
                results.append(ProbeResult(
                    name=name,
                    status="fail",
                    detail=f"{type(e).__name__}: {e}",
                    latency_ms=elapsed_ms,
                ))
                continue
        finally:
            ex.shutdown(wait=False)
        if "latency_ms" not in result or result["latency_ms"] == 0.0:
            result["latency_ms"] = (time.monotonic() - start) * 1000.0
        results.append(result)
    return results


def _ok(name: str, detail: str, t0: float) -> ProbeResult:
    return ProbeResult(name=name, status="ok", detail=detail,
                       latency_ms=(time.monotonic() - t0) * 1000.0)


def _warn(name: str, detail: str, t0: float) -> ProbeResult:
    return ProbeResult(name=name, status="warn", detail=detail,
                       latency_ms=(time.monotonic() - t0) * 1000.0)


def _fail(name: str, detail: str, t0: float) -> ProbeResult:
    return ProbeResult(name=name, status="fail", detail=detail,
                       latency_ms=(time.monotonic() - t0) * 1000.0)


def _skip(name: str, detail: str, t0: float) -> ProbeResult:
    return ProbeResult(name=name, status="skip", detail=detail,
                       latency_ms=(time.monotonic() - t0) * 1000.0)


# ---------------------------------------------------------------------------
# Factories (monkeypatch seams for tests)
# ---------------------------------------------------------------------------

def _embed_client_factory():
    """Return a fresh embedding-daemon client, or None if daemon disabled."""
    if os.environ.get("OMEGA_EMBEDDING_DAEMON") == "0":
        return None
    try:
        from omega_platform.embedding_client import EmbeddingClient
    except ImportError:
        return None
    return EmbeddingClient()


def _generate_embedding(text: str) -> List[float]:
    """Generate one embedding via the in-process model. Monkeypatch seam."""
    from omega.embedding import generate_embedding
    return list(generate_embedding(text))


def _reranker_score(query: str, candidates: List[str]) -> List[float]:
    """Score candidates against query. Monkeypatch seam.

    Raises ImportError if the reranker module is unavailable, FileNotFoundError
    (or similar OSError) if the model file is missing, ValueError on broken
    output — the probe maps these to skip/skip/fail respectively.
    """
    from omega.reranker import cross_encoder_score
    return list(cross_encoder_score(query, candidates))


def _hook_sock_path() -> Optional[Path]:
    """Return the hook server UDS path, or None if hook server uses TCP."""
    try:
        from omega_platform.server.hook_server import SOCK_PATH
    except ImportError:
        return None
    return SOCK_PATH


def _hook_tcp_addr() -> Optional[tuple]:
    """Return (host, port) for the hook server TCP socket, or None if UDS."""
    try:
        from omega_platform.server.hook_server import HOOK_HOST, HOOK_PORT
    except ImportError:
        return None
    if HOOK_HOST and HOOK_PORT:
        return (HOOK_HOST, int(HOOK_PORT))
    return None


def _list_active_pids() -> List[dict]:
    """List live MCP server PIDs from the registry. Monkeypatch seam."""
    try:
        from omega_platform.server.pid_registry import list_active_pids
    except ImportError:
        return []
    return list(list_active_pids())


# ---------------------------------------------------------------------------
# Probe: embed daemon liveness
# ---------------------------------------------------------------------------

def probe_embed_daemon() -> ProbeResult:
    """Connect to the embed daemon UDS and request a health response."""
    t0 = time.monotonic()
    name = "embed_daemon"

    client = _embed_client_factory()
    if client is None:
        return _skip(name, "daemon disabled or client unavailable", t0)

    try:
        health = client.health()
        if not health:
            return _fail(name, "daemon unreachable (no response)", t0)

        sock_path = OMEGA_DIR / "embed.sock"
        pid_path = OMEGA_DIR / "embed.pid"
        pid_hint = ""
        if pid_path.exists():
            try:
                pid_hint = f", pid={pid_path.read_text().strip()}"
            except OSError:
                pass
        detail = f"healthy (socket={sock_path}{pid_hint})"
        if isinstance(health, dict) and "model" in health:
            detail = f"healthy, model={health['model']}{pid_hint}"
        return _ok(name, detail, t0)
    finally:
        try:
            client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Probe: embedding semantic similarity
# ---------------------------------------------------------------------------

_EMBEDDING_DIM = 384
_SIMILAR_PAIR = (
    "the dog ran across the yard",
    "a dog sprinted through the garden",
)
_UNRELATED_PAIR = (
    "the dog ran across the yard",
    "Fourier transform of a Gaussian function",
)
_SIMILAR_COSINE_MIN = 0.5


def _cosine(a: List[float], b: List[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"vector dim mismatch: {len(a)} vs {len(b)}")
    if not a:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(len(a)))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def probe_embedding() -> ProbeResult:
    """Embed two similar strings, assert dim correct and cosine ≥ threshold."""
    t0 = time.monotonic()
    name = "embedding"

    v1 = _generate_embedding(_SIMILAR_PAIR[0])
    v2 = _generate_embedding(_SIMILAR_PAIR[1])

    if len(v1) != _EMBEDDING_DIM or len(v2) != _EMBEDDING_DIM:
        return _fail(name,
                     f"wrong embedding dim: got {len(v1)}/{len(v2)}, "
                     f"expected {_EMBEDDING_DIM}", t0)

    if not all(isinstance(x, (int, float)) for x in v1[:8]):
        return _fail(name, "embedding contains non-numeric values", t0)

    cos = _cosine(v1, v2)
    if cos < _SIMILAR_COSINE_MIN:
        return _fail(name,
                     f"cosine of similar texts too low: {cos:.3f} "
                     f"< {_SIMILAR_COSINE_MIN}", t0)

    return _ok(name, f"dim={_EMBEDDING_DIM}, cosine={cos:.3f}", t0)


# ---------------------------------------------------------------------------
# Probe: reranker ranking sanity
# ---------------------------------------------------------------------------

_RERANK_QUERY = "What is a cat?"
_RERANK_CANDIDATES = [
    "Photosynthesis is the process plants use to convert sunlight.",  # off-topic
    "A cat is a small domesticated carnivorous mammal.",              # relevant
]
_RERANK_RELEVANT_IDX = 1


def probe_reranker() -> ProbeResult:
    """Score one query against two candidates; assert relevant ranks first."""
    t0 = time.monotonic()
    name = "reranker"

    try:
        scores = _reranker_score(_RERANK_QUERY, _RERANK_CANDIDATES)
    except ImportError as e:
        return _skip(name, f"reranker module unavailable ({e})", t0)
    except FileNotFoundError as e:
        return _skip(name, f"model not downloaded: {e}", t0)
    except OSError as e:
        # HuggingFace LocalEntryNotFoundError subclasses OSError. Narrow to
        # the file-existence errnos so network / permission errors surface
        # as real failures rather than being silently skipped.
        if e.errno in (errno.ENOENT, errno.EISDIR, None):
            return _skip(name, f"model not downloaded ({type(e).__name__})", t0)
        return _fail(name, f"reranker raised {type(e).__name__}: {e}", t0)
    except Exception as e:
        return _fail(name, f"reranker raised {type(e).__name__}: {e}", t0)

    if not scores or len(scores) != len(_RERANK_CANDIDATES):
        return _fail(name, f"reranker returned {len(scores) if scores else 0} "
                           f"scores, expected {len(_RERANK_CANDIDATES)}", t0)

    best_idx = max(range(len(scores)), key=lambda i: scores[i])
    if best_idx != _RERANK_RELEVANT_IDX:
        return _fail(name,
                     f"ranking inverted: candidate {best_idx} scored highest "
                     f"({scores[best_idx]:.3f}) over relevant candidate "
                     f"{_RERANK_RELEVANT_IDX} ({scores[_RERANK_RELEVANT_IDX]:.3f})",
                     t0)

    margin = scores[_RERANK_RELEVANT_IDX] - scores[1 - _RERANK_RELEVANT_IDX]
    return _ok(name, f"relevant ranked first (margin={margin:+.3f})", t0)


# ---------------------------------------------------------------------------
# Probe: hook server liveness
# ---------------------------------------------------------------------------

def probe_hook_server() -> ProbeResult:
    """Connect to the hook server (UDS on linux/mac, TCP on windows)."""
    t0 = time.monotonic()
    name = "hook_server"

    tcp_addr = _hook_tcp_addr()
    if tcp_addr is not None:
        host, port = tcp_addr
        try:
            with socket.create_connection((host, port), timeout=1.0):
                pass
            return _ok(name, f"reachable on {host}:{port}", t0)
        except OSError as e:
            return _fail(name, f"tcp connect to {host}:{port} failed: {e}", t0)

    sock_path = _hook_sock_path()
    if sock_path is None:
        return _skip(name, "no UDS path and no TCP address configured", t0)

    # Attempt connect directly — race-free vs a pre-existence check.
    # Hook server is embedded in the MCP process: socket-absent (ENOENT) and
    # connection-refused (ECONNREFUSED) both mean MCP isn't running, which is
    # a warning, not a hard failure. Any other OSError is a real fault.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        sock.connect(str(sock_path))
        return _ok(name, f"reachable on {sock_path}", t0)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ECONNREFUSED):
            return _warn(name,
                         f"hook server not running (MCP not active?): {e.strerror or e}",
                         t0)
        return _fail(name, f"uds connect to {sock_path} failed: {e}", t0)
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Probe: MCP server liveness
# ---------------------------------------------------------------------------

def probe_mcp_server() -> ProbeResult:
    """Count live MCP servers in the PID registry; optionally hit HTTP healthcheck."""
    t0 = time.monotonic()
    name = "mcp_server"

    pids = _list_active_pids()
    if not pids:
        return _warn(name, "no MCP server processes registered", t0)

    by_transport: dict = {}
    for p in pids:
        by_transport.setdefault(p.get("transport", "unknown"), []).append(p)

    parts = [f"{len(v)} {k}" for k, v in sorted(by_transport.items())]
    detail = f"{len(pids)} live ({', '.join(parts)})"

    http_pids = by_transport.get("http", [])
    if http_pids:
        port = http_pids[0].get("port", 8377)
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=1.0):
                pass
            detail = f"{detail}, http port {port} reachable"
        except OSError as e:
            return _warn(name, f"{detail}, http port {port} unreachable: {e}", t0)

    return _ok(name, detail, t0)


# ---------------------------------------------------------------------------
# Convenience: full probe set in display order
# ---------------------------------------------------------------------------

ALL_PROBES: List[Callable[[], ProbeResult]] = [
    probe_embedding,
    probe_reranker,
    probe_embed_daemon,
    probe_hook_server,
    probe_mcp_server,
]


def compute_exit_code(errors: int, warnings: int, strict: bool) -> int:
    """Doctor exit-code policy. Errors always fail; warns fail only under --strict."""
    return 1 if (errors + (warnings if strict else 0)) > 0 else 0
