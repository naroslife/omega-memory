"""Tests for omega.diagnostics (the omega doctor probe layer)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"


# ---------------------------------------------------------------------------
# ProbeResult shape + run_probes orchestrator
# ---------------------------------------------------------------------------

class TestProbeResultShape:
    def test_probe_result_keys(self):
        from omega.diagnostics import ProbeResult, run_probes

        results = run_probes(timeout=1.0, probes=[lambda: ProbeResult(
            name="dummy", status="ok", detail="works", latency_ms=0.0
        )])
        assert len(results) == 1
        r = results[0]
        assert set(r.keys()) == {"name", "status", "detail", "latency_ms"}
        assert r["status"] in {"ok", "warn", "fail", "skip"}
        assert isinstance(r["latency_ms"], float)

    def test_run_probes_returns_list(self):
        from omega.diagnostics import ProbeResult, run_probes

        def p1():
            return ProbeResult(name="a", status="ok", detail="", latency_ms=0.0)

        def p2():
            return ProbeResult(name="b", status="warn", detail="meh", latency_ms=0.0)

        out = run_probes(timeout=1.0, probes=[p1, p2])
        assert [r["name"] for r in out] == ["a", "b"]
        assert [r["status"] for r in out] == ["ok", "warn"]


class TestTimeoutEnforcement:
    def test_hung_probe_returns_fail_timeout(self):
        from omega.diagnostics import run_probes

        def slow():
            time.sleep(2.0)
            return {"name": "slow", "status": "ok", "detail": "", "latency_ms": 0.0}

        start = time.monotonic()
        results = run_probes(timeout=0.2, probes=[slow])
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"timeout not enforced (took {elapsed:.2f}s)"
        assert len(results) == 1
        assert results[0]["status"] == "fail"
        assert "timeout" in results[0]["detail"].lower()
        assert results[0]["latency_ms"] >= 200.0

    def test_exception_in_probe_becomes_fail(self):
        from omega.diagnostics import run_probes

        def boom():
            raise RuntimeError("kaboom")

        results = run_probes(timeout=1.0, probes=[boom])
        assert results[0]["status"] == "fail"
        assert "kaboom" in results[0]["detail"]


# ---------------------------------------------------------------------------
# Embed daemon liveness
# ---------------------------------------------------------------------------

class TestEmbedDaemonLiveness:
    def test_ok_when_health_returns_dict(self, monkeypatch):
        from omega import diagnostics

        class FakeClient:
            def health(self):
                return {"status": "ok", "model": "bge-small-en-v1.5"}

            def close(self):
                pass

        monkeypatch.setattr(diagnostics, "_embed_client_factory", lambda: FakeClient())
        result = diagnostics.probe_embed_daemon()
        assert result["status"] == "ok"
        assert result["name"] == "embed_daemon"

    def test_fail_when_client_health_returns_none(self, monkeypatch):
        from omega import diagnostics

        class DeadClient:
            def health(self):
                return None

            def close(self):
                pass

        monkeypatch.setattr(diagnostics, "_embed_client_factory", lambda: DeadClient())
        result = diagnostics.probe_embed_daemon()
        assert result["status"] == "fail"
        assert "no response" in result["detail"].lower() or "unreachable" in result["detail"].lower()

    def test_skip_when_factory_returns_none(self, monkeypatch):
        from omega import diagnostics

        # Simulates: daemon disabled via OMEGA_EMBEDDING_DAEMON=0
        monkeypatch.setattr(diagnostics, "_embed_client_factory", lambda: None)
        result = diagnostics.probe_embed_daemon()
        assert result["status"] == "skip"


# ---------------------------------------------------------------------------
# Embedding functional (semantic similarity)
# ---------------------------------------------------------------------------

class TestEmbeddingFunctional:
    def test_ok_for_similar_pair(self, monkeypatch):
        from omega import diagnostics

        # Orthogonal-ish vectors with high alignment on first dims
        a = [1.0, 1.0] + [0.0] * 382
        b = [1.0, 0.95] + [0.0] * 382
        calls = {"n": 0}

        def fake_embed(text):
            calls["n"] += 1
            return a if "dog" in text else b

        monkeypatch.setattr(diagnostics, "_generate_embedding", fake_embed)
        result = diagnostics.probe_embedding()
        assert result["status"] == "ok", result
        assert calls["n"] == 2

    def test_fail_when_dim_wrong(self, monkeypatch):
        from omega import diagnostics

        monkeypatch.setattr(diagnostics, "_generate_embedding",
                            lambda text: [0.1] * 128)
        result = diagnostics.probe_embedding()
        assert result["status"] == "fail"
        assert "dim" in result["detail"].lower()

    def test_fail_when_similar_pair_has_low_cosine(self, monkeypatch):
        from omega import diagnostics

        # Both inputs in the probe ARE similar — if the model returns orthogonal
        # vectors for them, the cosine threshold should be tripped.
        seq = iter([
            [1.0] + [0.0] * 383,
            [0.0, 1.0] + [0.0] * 382,
        ])
        monkeypatch.setattr(diagnostics, "_generate_embedding",
                            lambda text: next(seq))
        result = diagnostics.probe_embedding()
        assert result["status"] == "fail"
        assert "cosine" in result["detail"].lower()


# ---------------------------------------------------------------------------
# Reranker functional
# ---------------------------------------------------------------------------

class TestRerankerFunctional:
    def test_ok_when_relevant_ranks_first(self, monkeypatch):
        from omega import diagnostics

        # Higher score = more relevant
        def fake_score(query, candidates):
            return [0.9 if "cat" in c else 0.1 for c in candidates]

        monkeypatch.setattr(diagnostics, "_reranker_score", fake_score)
        result = diagnostics.probe_reranker()
        assert result["status"] == "ok"

    def test_fail_when_ranking_inverted(self, monkeypatch):
        from omega import diagnostics

        def bad_score(query, candidates):
            return [0.1 if "cat" in c else 0.9 for c in candidates]

        monkeypatch.setattr(diagnostics, "_reranker_score", bad_score)
        result = diagnostics.probe_reranker()
        assert result["status"] == "fail"

    def test_skip_when_module_missing(self, monkeypatch):
        from omega import diagnostics

        def raise_import(query, candidates):
            raise ImportError("No module named 'omega.reranker'")

        monkeypatch.setattr(diagnostics, "_reranker_score", raise_import)
        result = diagnostics.probe_reranker()
        assert result["status"] == "skip"
        assert "module" in result["detail"].lower()

    def test_skip_when_model_missing(self, monkeypatch):
        from omega import diagnostics

        def raise_fnf(query, candidates):
            raise FileNotFoundError("model.onnx not found")

        monkeypatch.setattr(diagnostics, "_reranker_score", raise_fnf)
        result = diagnostics.probe_reranker()
        assert result["status"] == "skip"
        assert "model" in result["detail"].lower()


# ---------------------------------------------------------------------------
# Hook server liveness
# ---------------------------------------------------------------------------

class TestHookServerLiveness:
    def test_ok_when_socket_accepts(self, tmp_path, monkeypatch):
        from omega import diagnostics
        import threading

        sock_path = tmp_path / "hook.sock"

        # Stand up a minimal UDS that accepts then closes.
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)
        accepted = threading.Event()

        def accept_once():
            try:
                conn, _ = server.accept()
                accepted.set()
                conn.close()
            except OSError:
                pass

        t = threading.Thread(target=accept_once, daemon=True)
        t.start()

        monkeypatch.setattr(diagnostics, "_hook_sock_path", lambda: sock_path)
        monkeypatch.setattr(diagnostics, "_hook_tcp_addr", lambda: None)

        result = diagnostics.probe_hook_server()
        accepted.wait(timeout=1.0)
        server.close()
        assert result["status"] == "ok", result

    def test_warn_when_socket_missing(self, tmp_path, monkeypatch):
        from omega import diagnostics

        monkeypatch.setattr(diagnostics, "_hook_sock_path",
                            lambda: tmp_path / "nope.sock")
        monkeypatch.setattr(diagnostics, "_hook_tcp_addr", lambda: None)

        result = diagnostics.probe_hook_server()
        # Hook server is embedded in MCP; absent socket means MCP not running,
        # which is a configuration/state warning, not a hard failure.
        assert result["status"] == "warn"


# ---------------------------------------------------------------------------
# MCP server liveness
# ---------------------------------------------------------------------------

class TestMcpServerLiveness:
    def test_ok_when_at_least_one_live_pid(self, monkeypatch):
        from omega import diagnostics

        monkeypatch.setattr(diagnostics, "_list_active_pids",
                            lambda: [{"pid": 12345, "transport": "stdio",
                                       "started_at": "2026-01-01T00:00:00+00:00"}])
        result = diagnostics.probe_mcp_server()
        assert result["status"] == "ok"
        assert "1" in result["detail"]

    def test_warn_when_no_live_pids(self, monkeypatch):
        from omega import diagnostics

        monkeypatch.setattr(diagnostics, "_list_active_pids", lambda: [])
        result = diagnostics.probe_mcp_server()
        assert result["status"] == "warn"
        assert "no" in result["detail"].lower()


# ensure socket imported in test (used by hook-server test)
import socket  # noqa: E402


# ---------------------------------------------------------------------------
# CLI integration: --json schema and exit code semantics
# ---------------------------------------------------------------------------

def _run_doctor(*extra_args, env_extra=None):
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "omega.cli", "doctor", *extra_args],
        capture_output=True, text=True, env=env, timeout=60,
    )


@pytest.mark.slow
class TestDoctorCliJson:
    def test_json_includes_probes_array(self):
        proc = _run_doctor("--json", "--probe-timeout", "8")
        # stdout may contain logger output before JSON; parse last JSON object.
        data = json.loads(proc.stdout)
        assert "checks" in data
        assert "probes" in data
        assert isinstance(data["probes"], list)
        # Probe entries carry the new schema.
        for p in data["probes"]:
            assert set(p.keys()) == {"name", "status", "detail", "latency_ms"}
            assert p["status"] in {"ok", "warn", "fail", "skip"}

    def test_legacy_checks_array_shape_unchanged(self):
        proc = _run_doctor("--json", "--skip-probes")
        data = json.loads(proc.stdout)
        # Each entry must have status + message (legacy contract).
        for c in data["checks"]:
            assert "status" in c and "message" in c


class TestExitCodePolicy:
    def test_clean_run_exits_zero(self):
        from omega.diagnostics import compute_exit_code

        assert compute_exit_code(errors=0, warnings=0, strict=False) == 0
        assert compute_exit_code(errors=0, warnings=0, strict=True) == 0

    def test_errors_always_exit_nonzero(self):
        from omega.diagnostics import compute_exit_code

        assert compute_exit_code(errors=1, warnings=0, strict=False) == 1
        assert compute_exit_code(errors=3, warnings=0, strict=True) == 1

    def test_warnings_normally_exit_zero(self):
        from omega.diagnostics import compute_exit_code

        assert compute_exit_code(errors=0, warnings=5, strict=False) == 0

    def test_strict_escalates_warnings(self):
        from omega.diagnostics import compute_exit_code

        assert compute_exit_code(errors=0, warnings=1, strict=True) == 1
        assert compute_exit_code(errors=0, warnings=10, strict=True) == 1


@pytest.mark.slow
class TestDoctorStrictExitCode:
    def test_strict_escalates_warnings(self):
        """--strict turns any warning into a non-zero exit code."""
        proc_normal = _run_doctor("--json", "--skip-probes")
        data_normal = json.loads(proc_normal.stdout)
        # Capture baseline: doctor on this machine has at least one warn
        # (stale sessions or similar) under normal conditions.
        if data_normal["warnings"] == 0 and data_normal["errors"] == 0:
            pytest.skip("environment has no warnings to escalate")

        proc_strict = _run_doctor("--json", "--strict", "--skip-probes")
        data_strict = json.loads(proc_strict.stdout)
        assert data_strict["strict"] is True

        if data_normal["errors"] == 0 and data_normal["warnings"] > 0:
            assert proc_normal.returncode == 0
            assert proc_strict.returncode != 0
