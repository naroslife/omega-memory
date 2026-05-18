"""Tests for OMEGA cross-encoder reranker module — scoring, fallback, lifecycle, download."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import omega.reranker as reranker

onnxruntime = pytest.importorskip("onnxruntime", reason="onnxruntime not installed")
from omega.reranker import (
    cross_encoder_score,
    get_reranker_model_info,
    preload_reranker_model,
    reset_reranker_state,
    download_model,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_model():
    """Download the cross-encoder model if not present."""
    model_dir = Path(os.path.expanduser("~/.cache/omega/models/ms-marco-MiniLM-L-6-v2-onnx"))
    if not (model_dir / "model.onnx").exists():
        result = download_model(str(model_dir))
        assert result is not None, "Failed to download cross-encoder model for tests"


# ---------------------------------------------------------------------------
# 1. TestCrossEncoderScoring
# ---------------------------------------------------------------------------

class TestCrossEncoderScoring:
    """Test cross_encoder_score() returns correct scores."""

    def setup_method(self):
        reset_reranker_state()
        _ensure_model()

    def teardown_method(self):
        reset_reranker_state()

    def test_score_single_pair(self):
        """Scoring a single (query, passage) pair returns a list of one float."""
        scores = cross_encoder_score("What is Python?", ["Python is a programming language"])
        assert scores is not None
        assert isinstance(scores, list)
        assert len(scores) == 1
        assert isinstance(scores[0], float)

    def test_score_multiple_pairs(self):
        """Relevant passages should score higher than irrelevant ones."""
        query = "What is Python?"
        passages = [
            "Python is a programming language",
            "The weather is sunny today",
        ]
        scores = cross_encoder_score(query, passages)
        assert scores is not None
        assert len(scores) == 2
        # The relevant passage should score higher
        assert scores[0] > scores[1], (
            f"Expected relevant passage score ({scores[0]}) > irrelevant ({scores[1]})"
        )

    def test_score_empty_passages(self):
        """Empty passages list should return empty list."""
        scores = cross_encoder_score("What is Python?", [])
        assert scores == []

    def test_score_empty_query(self):
        """Empty query should return scores without crashing."""
        scores = cross_encoder_score("", ["Some passage text"])
        assert scores is not None
        assert len(scores) == 1
        assert isinstance(scores[0], float)


# ---------------------------------------------------------------------------
# 2. TestCrossEncoderFallback
# ---------------------------------------------------------------------------

class TestCrossEncoderFallback:
    """Test graceful fallback when model is disabled or unavailable."""

    def setup_method(self):
        reset_reranker_state()

    def teardown_method(self):
        reset_reranker_state()
        # Clean up env vars
        os.environ.pop("OMEGA_CROSS_ENCODER", None)

    def test_fallback_returns_none_when_disabled(self):
        """OMEGA_CROSS_ENCODER=0 disables reranking, returns None."""
        os.environ["OMEGA_CROSS_ENCODER"] = "0"
        reset_reranker_state()
        result = cross_encoder_score("query", ["passage"])
        assert result is None

    def test_fallback_returns_none_when_no_model(self):
        """Nonexistent model dir means model can't load, returns None."""
        os.environ["OMEGA_CROSS_ENCODER"] = "1"
        reset_reranker_state()
        # Point to a nonexistent directory
        with patch.object(reranker, "_RERANKER_DEFAULT_DIR", "/nonexistent/model/dir"):
            reset_reranker_state()
            result = cross_encoder_score("query", ["passage"])
            assert result is None


# ---------------------------------------------------------------------------
# 3. TestCrossEncoderModelLifecycle
# ---------------------------------------------------------------------------

class TestCrossEncoderModelLifecycle:
    """Test model info, preload, and reset."""

    def setup_method(self):
        reset_reranker_state()

    def teardown_method(self):
        reset_reranker_state()

    def test_model_info_returns_dict(self):
        """get_reranker_model_info returns dict with required keys."""
        info = get_reranker_model_info()
        assert isinstance(info, dict)
        assert "model_name" in info
        assert "available" in info
        assert info["model_name"] in ("ms-marco-MiniLM-L-6-v2", "bge-reranker-v2-m3")

    def test_preload_loads_model(self):
        """preload_reranker_model() loads the model and returns True."""
        _ensure_model()
        result = preload_reranker_model()
        assert result is True
        info = get_reranker_model_info()
        assert info["available"] is True

    def test_reset_clears_state(self):
        """reset_reranker_state() clears loaded model."""
        _ensure_model()
        preload_reranker_model()
        reset_reranker_state()
        info = get_reranker_model_info()
        assert info["available"] is False


# ---------------------------------------------------------------------------
# 4. TestModelDownload
# ---------------------------------------------------------------------------

class TestPrecisionResolution:
    """Test OMEGA_RERANKER_PRECISION env var and _resolve_model_config."""

    def teardown_method(self):
        os.environ.pop("OMEGA_RERANKER_PRECISION", None)

    def test_default_precision_is_int8(self):
        """Without env var, bge-reranker defaults to int8 (quantized)."""
        os.environ.pop("OMEGA_RERANKER_PRECISION", None)
        repo_id, dir_path, files = reranker._resolve_model_config("bge-reranker-v2-m3")
        assert "bge-reranker-v2-m3-onnx-int8" in dir_path
        file_names = [remote for remote, _ in files]
        assert "onnx/model_quantized.onnx" in file_names
        assert "onnx/model.onnx_data" not in file_names

    def test_int8_precision(self):
        """OMEGA_RERANKER_PRECISION=int8 selects quantized model."""
        os.environ["OMEGA_RERANKER_PRECISION"] = "int8"
        repo_id, dir_path, files = reranker._resolve_model_config("bge-reranker-v2-m3")
        assert "int8" in dir_path
        file_names = [remote for remote, _ in files]
        assert "onnx/model_quantized.onnx" in file_names
        assert "onnx/model.onnx_data" not in file_names

    def test_invalid_precision_falls_back(self):
        """Unknown precision falls back to default_precision (int8)."""
        os.environ["OMEGA_RERANKER_PRECISION"] = "fp64"
        repo_id, dir_path, files = reranker._resolve_model_config("bge-reranker-v2-m3")
        assert "bge-reranker-v2-m3-onnx-int8" in dir_path

    def test_precision_ignored_for_msmarco(self):
        """ms-marco has no precision variants; env var is ignored."""
        os.environ["OMEGA_RERANKER_PRECISION"] = "int8"
        repo_id, dir_path, files = reranker._resolve_model_config("ms-marco-MiniLM-L-6-v2")
        assert "ms-marco" in dir_path

    def test_explicit_precision_overrides_env(self):
        """Explicit precision param overrides env var."""
        os.environ["OMEGA_RERANKER_PRECISION"] = "fp32"
        _, dir_path, files = reranker._resolve_model_config("bge-reranker-v2-m3", precision="int8")
        assert "int8" in dir_path


class TestModelDownload:
    """Test model download functionality."""

    def test_download_writes_files(self, tmp_path):
        """download_model to tmp_path creates model.onnx and tokenizer.json."""
        target = str(tmp_path / "test-model")
        # Always test with ms-marco (has pre-built ONNX on HuggingFace)
        result = download_model(target, model_name="ms-marco-MiniLM-L-6-v2")
        assert result is not None
        assert Path(result).exists()
        assert (Path(result) / "model.onnx").exists()
        assert (Path(result) / "tokenizer.json").exists()
