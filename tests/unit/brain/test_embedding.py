"""Tests for sovyx.brain.embedding — embedding engine and model downloader."""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.brain.embedding import (
    MODEL_DIMENSIONS,
    EmbeddingEngine,
    ModelDownloader,
)
from sovyx.engine.errors import EmbeddingError

# ── Check if ONNX models are available ──────────────────────────────────────

_MODELS_DIR = Path.home() / ".sovyx" / "models"
_HAS_MODEL = (_MODELS_DIR / "e5-small-v2-q8.onnx").exists() and (
    _MODELS_DIR / "tokenizer.json"
).exists()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return dot / (na * nb)


# ── ModelDownloader tests ───────────────────────────────────────────────────


class TestModelDownloader:
    """Model download and verification."""

    def test_creates_models_dir(self, tmp_path: Path) -> None:
        dl = ModelDownloader(tmp_path / "models")
        assert dl.models_dir == tmp_path / "models"

    async def test_ensure_model_already_exists(self, tmp_path: Path) -> None:
        """Model already present → returns path without download."""
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        model_file = models_dir / "test.onnx"
        model_file.write_bytes(b"fake model data")

        dl = ModelDownloader(models_dir)
        result = await dl.ensure_model("test.onnx", "http://example.com/model")
        assert result == model_file

    async def test_ensure_model_download_fails(self, tmp_path: Path) -> None:
        """Download fails 3x → EmbeddingError."""
        dl = ModelDownloader(tmp_path / "models")
        dl.BACKOFF_BASE = 0.01  # Speed up retries

        with (
            patch.object(
                dl,
                "_download",
                new_callable=AsyncMock,
                side_effect=ConnectionError("no internet"),
            ),
            pytest.raises(EmbeddingError, match="Failed to download"),
        ):
            await dl.ensure_model("test.onnx", "http://example.com/model")

    async def test_ensure_model_retry_success(self, tmp_path: Path) -> None:
        """Download fails 1x then succeeds."""
        dl = ModelDownloader(tmp_path / "models")
        dl.BACKOFF_BASE = 0.01

        call_count = 0

        async def mock_download(url: str, dest: Path, callback: object = None) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                msg = "temporary failure"
                raise ConnectionError(msg)
            dest.write_bytes(b"model data")

        with patch.object(dl, "_download", side_effect=mock_download):
            result = await dl.ensure_model("test.onnx", "http://example.com")
            assert result.exists()
            assert call_count == 2  # noqa: PLR2004

    async def test_ensure_model_checksum_mismatch(self, tmp_path: Path) -> None:
        """Checksum mismatch → EmbeddingError + file removed."""
        dl = ModelDownloader(tmp_path / "models")

        async def mock_download(url: str, dest: Path, callback: object = None) -> None:
            dest.write_bytes(b"wrong data")

        with (
            patch.object(dl, "_download", side_effect=mock_download),
            pytest.raises(EmbeddingError, match="Checksum mismatch"),
        ):
            await dl.ensure_model(
                "test.onnx",
                "http://example.com",
                expected_sha256="deadbeef" * 8,
            )

    async def test_ensure_model_creates_dir(self, tmp_path: Path) -> None:
        """models_dir created automatically."""
        dl = ModelDownloader(tmp_path / "deep" / "models")

        async def mock_download(url: str, dest: Path, callback: object = None) -> None:
            dest.write_bytes(b"model")

        with patch.object(dl, "_download", side_effect=mock_download):
            result = await dl.ensure_model("test.onnx", "http://example.com")
            assert result.exists()

    def test_verify_checksum_correct(self, tmp_path: Path) -> None:
        import hashlib

        data = b"test data for checksum"
        path = tmp_path / "test.bin"
        path.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert ModelDownloader._verify_checksum(path, expected) is True

    def test_verify_checksum_wrong(self, tmp_path: Path) -> None:
        path = tmp_path / "test.bin"
        path.write_bytes(b"data")
        assert ModelDownloader._verify_checksum(path, "wrong") is False

    async def test_existing_model_bad_checksum_redownloads(self, tmp_path: Path) -> None:
        """Existing model with bad checksum triggers redownload."""
        import hashlib

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        model_file = models_dir / "test.onnx"
        model_file.write_bytes(b"old bad data")

        correct_data = b"correct model data"
        correct_hash = hashlib.sha256(correct_data).hexdigest()

        dl = ModelDownloader(models_dir)

        async def mock_download(url: str, dest: Path, callback: object = None) -> None:
            dest.write_bytes(correct_data)

        with patch.object(dl, "_download", side_effect=mock_download):
            result = await dl.ensure_model(
                "test.onnx", "http://example.com", expected_sha256=correct_hash
            )
            assert result.read_bytes() == correct_data


# ── EmbeddingEngine tests ───────────────────────────────────────────────────


class TestEmbeddingEngineUnit:
    """Unit tests (no ONNX required)."""

    def test_dimensions(self) -> None:
        engine = EmbeddingEngine()
        assert engine.dimensions == MODEL_DIMENSIONS

    def test_initial_state(self) -> None:
        engine = EmbeddingEngine()
        assert engine.is_loaded is False
        assert engine.has_embeddings is False

    async def test_encode_empty_text_raises(self) -> None:
        engine = EmbeddingEngine()
        with pytest.raises(EmbeddingError, match="empty"):
            await engine.encode("")

    async def test_encode_whitespace_raises(self) -> None:
        engine = EmbeddingEngine()
        with pytest.raises(EmbeddingError, match="empty"):
            await engine.encode("   ")

    async def test_encode_batch_empty_list(self) -> None:
        engine = EmbeddingEngine()
        result = await engine.encode_batch([])
        assert result == []

    async def test_model_unavailable_fallback(self, tmp_path: Path) -> None:
        """If model can't load, has_embeddings=False."""
        engine = EmbeddingEngine(model_dir=tmp_path / "empty")
        with patch.object(
            ModelDownloader,
            "ensure_model",
            new_callable=AsyncMock,
            side_effect=EmbeddingError("no model"),
        ):
            await engine.ensure_loaded()
            assert engine.is_loaded is True
            assert engine.has_embeddings is False

    async def test_encode_after_failed_load(self, tmp_path: Path) -> None:
        """Encode raises when model not available."""
        engine = EmbeddingEngine(model_dir=tmp_path / "empty")
        with (
            patch.object(
                ModelDownloader,
                "ensure_model",
                new_callable=AsyncMock,
                side_effect=EmbeddingError("no model"),
            ),
            pytest.raises(EmbeddingError, match="not available"),
        ):
            await engine.encode("test")


class TestEmbeddingEngineMocked:
    """Tests with mocked ONNX runtime."""

    @pytest.fixture
    def mock_engine(self, tmp_path: Path) -> EmbeddingEngine:
        """Engine with mocked internals."""
        import numpy as np

        engine = EmbeddingEngine(model_dir=tmp_path)
        engine._loaded = True
        engine._has_embeddings = True

        # Mock tokenizer
        mock_encoding = MagicMock()
        mock_encoding.ids = list(range(10)) + [0] * 502
        mock_encoding.attention_mask = [1] * 10 + [0] * 502

        mock_tokenizer = MagicMock()
        mock_tokenizer.encode_batch.return_value = [mock_encoding]
        engine._tokenizer = mock_tokenizer

        # Mock ONNX session — return (batch, seq, hidden) tensor
        mock_session = MagicMock()
        fake_output = np.random.randn(1, 512, 384).astype(np.float32)
        mock_session.run.return_value = [fake_output]
        engine._session = mock_session

        return engine

    async def test_encode_calls_tokenizer_with_prefix(self, mock_engine: EmbeddingEngine) -> None:
        result = await mock_engine.encode("hello", is_query=True)
        assert len(result) == MODEL_DIMENSIONS
        mock_engine._tokenizer.encode_batch.assert_called_with(["query: hello"])

    async def test_encode_passage_prefix(self, mock_engine: EmbeddingEngine) -> None:
        await mock_engine.encode("test", is_query=False)
        mock_engine._tokenizer.encode_batch.assert_called_with(["passage: test"])

    async def test_encode_l2_normalized(self, mock_engine: EmbeddingEngine) -> None:
        result = await mock_engine.encode("test normalization")
        norm = math.sqrt(sum(v * v for v in result))
        assert abs(norm - 1.0) < 0.01

    async def test_encode_batch_multiple(self, mock_engine: EmbeddingEngine) -> None:
        import numpy as np

        # Setup for batch of 3
        mock_enc = MagicMock()
        mock_enc.ids = list(range(10)) + [0] * 502
        mock_enc.attention_mask = [1] * 10 + [0] * 502
        mock_engine._tokenizer.encode_batch.return_value = [mock_enc, mock_enc, mock_enc]
        mock_engine._session.run.return_value = [np.random.randn(3, 512, 384).astype(np.float32)]

        results = await mock_engine.encode_batch(["a", "b", "c"])
        assert len(results) == 3  # noqa: PLR2004
        assert all(len(r) == MODEL_DIMENSIONS for r in results)

    async def test_encode_batch_unavailable_raises(self, tmp_path: Path) -> None:
        engine = EmbeddingEngine(model_dir=tmp_path)
        engine._loaded = True
        engine._has_embeddings = False
        with pytest.raises(EmbeddingError, match="not available"):
            await engine.encode_batch(["test"])

    async def test_ensure_loaded_called_once(self, mock_engine: EmbeddingEngine) -> None:
        """Second ensure_loaded is a no-op."""
        await mock_engine.ensure_loaded()
        assert mock_engine.is_loaded is True

    async def test_ensure_loaded_with_download(self, tmp_path: Path) -> None:
        """ensure_loaded triggers download and model load."""
        engine = EmbeddingEngine(model_dir=tmp_path / "models")

        async def fake_ensure(filename: str, url: str, **kwargs: object) -> Path:
            p = tmp_path / "models" / filename
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"fake")
            return p

        with (
            patch.object(
                ModelDownloader,
                "ensure_model",
                side_effect=fake_ensure,
            ),
            patch.object(engine, "_load_model") as mock_load,
        ):
            await engine.ensure_loaded()
            assert mock_load.called
            assert engine._has_embeddings is True


class TestDownloadAndLoad:
    """Tests for _download and _load_model methods."""

    async def test_download_writes_file(self, tmp_path: Path) -> None:
        """_download writes streamed data to dest."""
        import httpx

        dest = tmp_path / "model.bin"
        data = b"fake model binary"

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=data)

        transport = httpx.MockTransport(mock_handler)

        with patch(
            "sovyx.brain.embedding.httpx.AsyncClient",
            return_value=httpx.AsyncClient(transport=transport),
        ):
            await ModelDownloader._download("http://example.com/model.onnx", dest)

        assert dest.exists()
        assert dest.read_bytes() == data

    async def test_download_with_callback(self, tmp_path: Path) -> None:
        """Progress callback is invoked."""
        import httpx

        dest = tmp_path / "model.bin"
        data = b"chunk"
        progress: list[tuple[int, int]] = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=data,
                headers={"content-length": str(len(data))},
            )

        transport = httpx.MockTransport(mock_handler)

        with patch(
            "sovyx.brain.embedding.httpx.AsyncClient",
            return_value=httpx.AsyncClient(transport=transport),
        ):
            await ModelDownloader._download(
                "http://example.com/model.onnx",
                dest,
                lambda d, t: progress.append((d, t)),
            )

        assert len(progress) > 0

    def test_load_model_calls_onnx(self, tmp_path: Path) -> None:
        """_load_model instantiates ONNX session and tokenizer."""
        engine = EmbeddingEngine(model_dir=tmp_path)

        mock_sess = MagicMock()
        mock_tok = MagicMock()

        mock_ort_mod = MagicMock()
        mock_ort_mod.SessionOptions.return_value = MagicMock()
        mock_ort_mod.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort_mod.InferenceSession.return_value = mock_sess

        mock_tok_mod = MagicMock()
        mock_tok_mod.Tokenizer.from_file.return_value = mock_tok

        with (
            patch.dict(
                "sys.modules",
                {"onnxruntime": mock_ort_mod, "tokenizers": mock_tok_mod},
            ),
        ):
            # Need to reimport since _load_model does `import onnxruntime`
            engine._load_model(tmp_path / "model.onnx", tmp_path / "tok.json")

        assert engine._session is mock_sess
        assert engine._tokenizer is mock_tok


@pytest.mark.skipif(not _HAS_MODEL, reason="ONNX model not available")
class TestEmbeddingEngineIntegration:
    """Integration tests with real ONNX model."""

    @pytest.fixture
    async def engine(self) -> EmbeddingEngine:
        e = EmbeddingEngine()
        await e.ensure_loaded()
        return e

    async def test_encode_returns_384_floats(self, engine: EmbeddingEngine) -> None:
        result = await engine.encode("hello world")
        assert len(result) == MODEL_DIMENSIONS
        assert all(isinstance(v, float) for v in result)

    async def test_encode_l2_normalized(self, engine: EmbeddingEngine) -> None:
        result = await engine.encode("test normalization")
        norm = math.sqrt(sum(v * v for v in result))
        assert abs(norm - 1.0) < 0.01

    async def test_encode_query_prefix(self, engine: EmbeddingEngine) -> None:
        """query vs passage prefix produces different embeddings."""
        q = await engine.encode("machine learning", is_query=True)
        p = await engine.encode("machine learning", is_query=False)
        # Same text, different prefix → different (but similar) embeddings
        sim = _cosine_similarity(q, p)
        assert sim < 1.0
        assert sim > 0.5

    async def test_similar_texts_high_similarity(self, engine: EmbeddingEngine) -> None:
        a = await engine.encode("the cat sat on the mat")
        b = await engine.encode("a cat was sitting on a mat")
        assert _cosine_similarity(a, b) > 0.8

    async def test_different_texts_lower_similarity(self, engine: EmbeddingEngine) -> None:
        a = await engine.encode("quantum physics equations")
        b = await engine.encode("chocolate cake recipe")
        sim = _cosine_similarity(a, b)
        assert sim < 0.5

    async def test_encode_batch(self, engine: EmbeddingEngine) -> None:
        texts = ["hello", "world", "test"]
        results = await engine.encode_batch(texts)
        assert len(results) == 3  # noqa: PLR2004
        assert all(len(r) == MODEL_DIMENSIONS for r in results)

    async def test_is_loaded_after_encode(self, engine: EmbeddingEngine) -> None:
        assert engine.is_loaded is True
        assert engine.has_embeddings is True

    async def test_lazy_loading(self) -> None:
        """Model not loaded until first encode."""
        engine = EmbeddingEngine()
        assert engine.is_loaded is False
        await engine.encode("trigger lazy load")
        assert engine.is_loaded is True
