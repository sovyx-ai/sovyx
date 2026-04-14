"""Tests for sovyx.brain.embedding — embedding engine and model downloader.

Covers: download with retries, Retry-After parsing, mirror fallback,
cooldown markers, HF_TOKEN auth, checksum verification, and embedding
generation (unit + integration with real ONNX model when available).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sovyx.brain.embedding import (
    MODEL_DIMENSIONS,
    EmbeddingEngine,
    ModelDownloader,
    _clear_cooldown,
    _cooldown_path,
    _is_in_cooldown,
    _is_permanent,
    _is_transient,
    _write_cooldown,
)
from sovyx.engine.errors import EmbeddingError

# ── Check if ONNX models are available ──────────────────────────────────────

_MODELS_DIR = Path.home() / ".sovyx" / "models"
_HAS_MODEL = (_MODELS_DIR / "e5-small-v2.onnx").exists() and (
    _MODELS_DIR / "tokenizer.json"
).exists()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return dot / (na * nb)


# ── Error classification tests ──────────────────────────────────────────────


class TestErrorClassification:
    """Transient vs permanent HTTP error classification."""

    @pytest.mark.parametrize("code", [408, 429, 500, 502, 503, 504, 520, 522, 524])
    def test_transient_codes(self, code: int) -> None:
        assert _is_transient(code) is True

    @pytest.mark.parametrize("code", [200, 301, 400, 404])
    def test_non_transient_codes(self, code: int) -> None:
        assert _is_transient(code) is False

    @pytest.mark.parametrize("code", [401, 403, 404, 410, 451])
    def test_permanent_codes(self, code: int) -> None:
        assert _is_permanent(code) is True

    @pytest.mark.parametrize("code", [200, 429, 500, 503])
    def test_non_permanent_codes(self, code: int) -> None:
        assert _is_permanent(code) is False


# ── Cooldown tests ──────────────────────────────────────────────────────────


class TestDownloadCooldown:
    """Cooldown marker lifecycle."""

    def test_no_cooldown_initially(self, tmp_path: Path) -> None:
        assert _is_in_cooldown(tmp_path, "test.onnx") is False

    def test_write_creates_marker(self, tmp_path: Path) -> None:
        _write_cooldown(tmp_path, "test.onnx", "429 error", 5)
        marker = _cooldown_path(tmp_path, "test.onnx")
        assert marker.exists()
        data = json.loads(marker.read_text())
        assert data["attempts"] == 5
        assert "429 error" in data["error"]

    def test_in_cooldown_after_write(self, tmp_path: Path) -> None:
        _write_cooldown(tmp_path, "test.onnx", "error", 3)
        assert _is_in_cooldown(tmp_path, "test.onnx") is True

    def test_cooldown_expires(self, tmp_path: Path) -> None:
        """Expired marker → not in cooldown."""
        marker = _cooldown_path(tmp_path, "test.onnx")
        data = {"failed_at": time.time() - 9999, "error": "old", "attempts": 1}
        marker.write_text(json.dumps(data))
        assert _is_in_cooldown(tmp_path, "test.onnx") is False

    def test_clear_cooldown_removes_marker(self, tmp_path: Path) -> None:
        _write_cooldown(tmp_path, "test.onnx", "error", 1)
        _clear_cooldown(tmp_path, "test.onnx")
        assert _is_in_cooldown(tmp_path, "test.onnx") is False

    def test_clear_cooldown_missing_is_noop(self, tmp_path: Path) -> None:
        """Clearing non-existent marker doesn't raise."""
        _clear_cooldown(tmp_path, "test.onnx")

    def test_corrupt_marker_removed(self, tmp_path: Path) -> None:
        """Corrupt JSON marker is cleaned up."""
        marker = _cooldown_path(tmp_path, "test.onnx")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("NOT JSON")
        assert _is_in_cooldown(tmp_path, "test.onnx") is False
        assert not marker.exists()


# ── ModelDownloader tests ───────────────────────────────────────────────────


class TestModelDownloader:
    """Model download and verification."""

    def test_creates_models_dir(self, tmp_path: Path) -> None:
        dl = ModelDownloader(tmp_path / "models")
        assert dl.models_dir == tmp_path / "models"

    def test_env_var_model_dir(self, tmp_path: Path) -> None:
        """SOVYX_MODEL_DIR env var overrides default."""
        with patch.dict("os.environ", {"SOVYX_MODEL_DIR": str(tmp_path / "env")}):
            dl = ModelDownloader()
            assert dl.models_dir == tmp_path / "env"

    def test_explicit_dir_takes_precedence(self, tmp_path: Path) -> None:
        """Explicit models_dir beats env var."""
        with patch.dict("os.environ", {"SOVYX_MODEL_DIR": "/ignored"}):
            dl = ModelDownloader(tmp_path / "explicit")
            assert dl.models_dir == tmp_path / "explicit"

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
        """Download fails all retries → EmbeddingError + cooldown marker."""
        dl = ModelDownloader(tmp_path / "models")
        dl.BACKOFF_BASE = 0.001
        dl.BACKOFF_MAX = 0.01

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

        # Cooldown marker should exist
        assert _is_in_cooldown(tmp_path / "models", "test.onnx")

    async def test_cooldown_blocks_retry(self, tmp_path: Path) -> None:
        """If in cooldown, ensure_model raises immediately without HTTP."""
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _write_cooldown(models_dir, "test.onnx", "previous failure", 5)

        dl = ModelDownloader(models_dir)

        with pytest.raises(EmbeddingError, match="cooldown"):
            await dl.ensure_model("test.onnx", "http://example.com/model")

    async def test_ensure_model_retry_success(self, tmp_path: Path) -> None:
        """Download fails 1x then succeeds."""
        dl = ModelDownloader(tmp_path / "models")
        dl.BACKOFF_BASE = 0.001
        dl.BACKOFF_MAX = 0.01

        call_count = 0

        async def mock_download(
            url: str,
            dest: Path,
            callback: object = None,
            headers: object = None,
        ) -> None:
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
        """Checksum mismatch → EmbeddingError (permanent, no retry)."""
        dl = ModelDownloader(tmp_path / "models")

        async def mock_download(
            url: str,
            dest: Path,
            callback: object = None,
            headers: object = None,
        ) -> None:
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

        async def mock_download(
            url: str,
            dest: Path,
            callback: object = None,
            headers: object = None,
        ) -> None:
            dest.write_bytes(b"model")

        with patch.object(dl, "_download", side_effect=mock_download):
            result = await dl.ensure_model("test.onnx", "http://example.com")
            assert result.exists()

    def test_verify_checksum_correct(self, tmp_path: Path) -> None:
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
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        model_file = models_dir / "test.onnx"
        model_file.write_bytes(b"old bad data")

        correct_data = b"correct model data"
        correct_hash = hashlib.sha256(correct_data).hexdigest()

        dl = ModelDownloader(models_dir)

        async def mock_download(
            url: str,
            dest: Path,
            callback: object = None,
            headers: object = None,
        ) -> None:
            dest.write_bytes(correct_data)

        with patch.object(dl, "_download", side_effect=mock_download):
            result = await dl.ensure_model(
                "test.onnx",
                "http://example.com",
                expected_sha256=correct_hash,
            )
            assert result.read_bytes() == correct_data


# ── Mirror fallback tests ──────────────────────────────────────────────────


class TestMirrorFallback:
    """Multi-URL fallback behavior."""

    async def test_mirror_used_after_primary_fails(self, tmp_path: Path) -> None:
        """If primary URL fails all retries, mirror is tried."""
        dl = ModelDownloader(tmp_path / "models")
        dl.BACKOFF_BASE = 0.001
        dl.BACKOFF_MAX = 0.01

        urls_attempted: list[str] = []

        async def mock_download(
            url: str,
            dest: Path,
            callback: object = None,
            headers: object = None,
        ) -> None:
            urls_attempted.append(url)
            if "primary" in url:
                raise ConnectionError("primary down")
            dest.write_bytes(b"from mirror")

        with patch.object(dl, "_download", side_effect=mock_download):
            result = await dl.ensure_model(
                "test.onnx",
                "http://primary/model",
                mirror_urls=["http://mirror/model"],
            )
            assert result.exists()
            assert result.read_bytes() == b"from mirror"
            # Primary was tried MAX_RETRIES times, then mirror once
            primary_attempts = sum(1 for u in urls_attempted if "primary" in u)
            mirror_attempts = sum(1 for u in urls_attempted if "mirror" in u)
            assert primary_attempts == dl.MAX_RETRIES
            assert mirror_attempts >= 1

    async def test_all_mirrors_fail(self, tmp_path: Path) -> None:
        """All URLs fail → cooldown + EmbeddingError."""
        dl = ModelDownloader(tmp_path / "models")
        dl.BACKOFF_BASE = 0.001
        dl.BACKOFF_MAX = 0.01

        with (
            patch.object(
                dl,
                "_download",
                new_callable=AsyncMock,
                side_effect=ConnectionError("all down"),
            ),
            pytest.raises(EmbeddingError, match="Failed to download"),
        ):
            await dl.ensure_model(
                "test.onnx",
                "http://primary/model",
                mirror_urls=["http://mirror1/m", "http://mirror2/m"],
            )

    async def test_primary_success_no_mirror(self, tmp_path: Path) -> None:
        """Primary succeeds → mirrors not tried."""
        dl = ModelDownloader(tmp_path / "models")
        urls_attempted: list[str] = []

        async def mock_download(
            url: str,
            dest: Path,
            callback: object = None,
            headers: object = None,
        ) -> None:
            urls_attempted.append(url)
            dest.write_bytes(b"ok")

        with patch.object(dl, "_download", side_effect=mock_download):
            await dl.ensure_model(
                "test.onnx",
                "http://primary/model",
                mirror_urls=["http://mirror/model"],
            )
            assert all("primary" in u for u in urls_attempted)


# ── Retry-After header tests ───────────────────────────────────────────────


class TestRetryAfterParsing:
    """RFC 7231 §7.1.3 Retry-After header parsing."""

    def test_retry_after_seconds(self) -> None:
        resp = httpx.Response(429, headers={"retry-after": "30"})
        result = ModelDownloader._parse_retry_after(resp)
        assert result == 30.0

    def test_retry_after_float(self) -> None:
        resp = httpx.Response(429, headers={"retry-after": "1.5"})
        result = ModelDownloader._parse_retry_after(resp)
        assert result == 1.5

    def test_retry_after_missing(self) -> None:
        resp = httpx.Response(429)
        result = ModelDownloader._parse_retry_after(resp)
        assert result is None

    def test_x_ratelimit_reset_epoch(self) -> None:
        future = time.time() + 45
        resp = httpx.Response(429, headers={"x-ratelimit-reset": str(future)})
        result = ModelDownloader._parse_retry_after(resp)
        assert result is not None
        assert 40 < result < 50  # noqa: PLR2004

    def test_x_ratelimit_reset_past(self) -> None:
        """Past epoch → returns None (delta <= 0)."""
        past = time.time() - 100
        resp = httpx.Response(429, headers={"x-ratelimit-reset": str(past)})
        result = ModelDownloader._parse_retry_after(resp)
        assert result is None

    def test_retry_after_takes_precedence(self) -> None:
        """Standard header beats non-standard."""
        resp = httpx.Response(
            429,
            headers={
                "retry-after": "10",
                "x-ratelimit-reset": str(time.time() + 999),
            },
        )
        result = ModelDownloader._parse_retry_after(resp)
        assert result == 10.0

    def test_invalid_header_value(self) -> None:
        resp = httpx.Response(429, headers={"retry-after": "not-a-number"})
        result = ModelDownloader._parse_retry_after(resp)
        assert result is None


# ── HTTP status handling tests ──────────────────────────────────────────────


class TestHTTPStatusHandling:
    """Transient vs permanent error behavior in download loop."""

    async def test_permanent_error_stops_retries(self, tmp_path: Path) -> None:
        """404 on primary → immediately moves to mirror (no retries)."""
        dl = ModelDownloader(tmp_path / "models")
        dl.BACKOFF_BASE = 0.001
        dl.BACKOFF_MAX = 0.01

        call_count = 0

        async def mock_download(
            url: str,
            dest: Path,
            callback: object = None,
            headers: object = None,
        ) -> None:
            nonlocal call_count
            call_count += 1
            if "primary" in url:
                resp = httpx.Response(404, request=httpx.Request("GET", url))
                raise httpx.HTTPStatusError("Not Found", request=resp.request, response=resp)
            dest.write_bytes(b"from mirror")

        with patch.object(dl, "_download", side_effect=mock_download):
            result = await dl.ensure_model(
                "test.onnx",
                "http://primary/model",
                mirror_urls=["http://mirror/model"],
            )
            assert result.exists()
            # Primary should have been tried only ONCE (permanent = no retry)
            # Then mirror succeeds on first try
            assert call_count == 2  # noqa: PLR2004

    async def test_429_retries_with_backoff(self, tmp_path: Path) -> None:
        """429 triggers retries (not permanent)."""
        dl = ModelDownloader(tmp_path / "models")
        dl.BACKOFF_BASE = 0.001
        dl.BACKOFF_MAX = 0.01

        call_count = 0

        async def mock_download(
            url: str,
            dest: Path,
            callback: object = None,
            headers: object = None,
        ) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:  # noqa: PLR2004
                resp = httpx.Response(
                    429,
                    request=httpx.Request("GET", url),
                    headers={"retry-after": "0.001"},
                )
                raise httpx.HTTPStatusError("Rate Limited", request=resp.request, response=resp)
            dest.write_bytes(b"success after rate limit")

        with patch.object(dl, "_download", side_effect=mock_download):
            result = await dl.ensure_model("test.onnx", "http://example.com")
            assert result.exists()
            assert call_count == 3  # noqa: PLR2004


# ── Auth header tests ──────────────────────────────────────────────────────


class TestAuthHeaders:
    """HuggingFace token authentication."""

    def test_no_token_empty_headers(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            headers = ModelDownloader._auth_headers()
            assert headers == {}

    def test_hf_token_env(self) -> None:
        with patch.dict("os.environ", {"HF_TOKEN": "hf_test123"}):
            headers = ModelDownloader._auth_headers()
            assert headers == {"Authorization": "Bearer hf_test123"}

    def test_hugging_face_hub_token_env(self) -> None:
        with patch.dict("os.environ", {"HUGGING_FACE_HUB_TOKEN": "hf_hub456"}):
            headers = ModelDownloader._auth_headers()
            assert headers == {"Authorization": "Bearer hf_hub456"}

    def test_hf_token_takes_precedence(self) -> None:
        """HF_TOKEN beats HUGGING_FACE_HUB_TOKEN."""
        with patch.dict(
            "os.environ",
            {"HF_TOKEN": "primary", "HUGGING_FACE_HUB_TOKEN": "fallback"},
        ):
            headers = ModelDownloader._auth_headers()
            assert headers == {"Authorization": "Bearer primary"}


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

    def test_env_var_model_dir(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"SOVYX_MODEL_DIR": str(tmp_path / "env")}):
            engine = EmbeddingEngine()
            assert engine._model_dir == tmp_path / "env"

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

        # Mock ONNX session
        fake_output = np.random.randn(1, 512, 384).astype(np.float32)
        mock_session = MagicMock()
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

        mock_enc = MagicMock()
        mock_enc.ids = list(range(10)) + [0] * 502
        mock_enc.attention_mask = [1] * 10 + [0] * 502
        mock_engine._tokenizer.encode_batch.return_value = [
            mock_enc,
            mock_enc,
            mock_enc,
        ]
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
        dest = tmp_path / "model.bin"
        data = b"fake model binary"

        async def mock_handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(200, content=data)

        transport = httpx.MockTransport(mock_handler)

        with patch(
            "sovyx.brain._model_downloader.httpx.AsyncClient",
            return_value=httpx.AsyncClient(transport=transport),
        ):
            await ModelDownloader._download("http://example.com/model.onnx", dest)

        assert dest.exists()
        assert dest.read_bytes() == data

    async def test_download_with_callback(self, tmp_path: Path) -> None:
        """Progress callback is invoked."""
        dest = tmp_path / "model.bin"
        data = b"chunk"
        progress: list[tuple[int, int]] = []

        async def mock_handler(
            request: httpx.Request,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                content=data,
                headers={"content-length": str(len(data))},
            )

        transport = httpx.MockTransport(mock_handler)

        with patch(
            "sovyx.brain._model_downloader.httpx.AsyncClient",
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

        # `ort` is bound at module import in embedding.py, so a
        # sys.modules patch can't intercept it — patch the module
        # attribute directly. `tokenizers` is imported locally inside
        # _load_model, so sys.modules still works for it.
        with (
            patch("sovyx.brain.embedding.ort", mock_ort_mod),
            patch.dict("sys.modules", {"tokenizers": mock_tok_mod}),
        ):
            engine._load_model(tmp_path / "model.onnx", tmp_path / "tok.json")

        assert engine._session is mock_sess
        assert engine._tokenizer is mock_tok


class TestCategoryCentroid:
    """Category centroid computation."""

    async def test_centroid_of_identical_vectors(self) -> None:
        engine = EmbeddingEngine()
        v = [1.0] + [0.0] * (MODEL_DIMENSIONS - 1)
        centroid = await engine.compute_category_centroid([v, v, v])
        assert len(centroid) == MODEL_DIMENSIONS
        assert centroid[0] == pytest.approx(1.0, abs=0.001)

    async def test_centroid_of_opposite_vectors(self) -> None:
        engine = EmbeddingEngine()
        v1 = [1.0] + [0.0] * (MODEL_DIMENSIONS - 1)
        v2 = [-1.0] + [0.0] * (MODEL_DIMENSIONS - 1)
        centroid = await engine.compute_category_centroid([v1, v2])
        assert len(centroid) == MODEL_DIMENSIONS

    async def test_centroid_empty_raises(self) -> None:
        engine = EmbeddingEngine()
        with pytest.raises(ValueError, match="empty"):
            await engine.compute_category_centroid([])

    async def test_centroid_is_l2_normalized(self) -> None:
        engine = EmbeddingEngine()
        v1 = [0.6] + [0.8] + [0.0] * (MODEL_DIMENSIONS - 2)
        v2 = [0.8] + [0.6] + [0.0] * (MODEL_DIMENSIONS - 2)
        centroid = await engine.compute_category_centroid([v1, v2])
        norm = math.sqrt(sum(x * x for x in centroid))
        assert abs(norm - 1.0) < 0.01


class TestCosineSimilarity:
    """Cosine similarity utility."""

    def test_identical_vectors(self) -> None:
        v = [0.5] * 10
        assert EmbeddingEngine.cosine_similarity(v, v) == pytest.approx(1.0, abs=0.01)

    def test_orthogonal_vectors(self) -> None:
        v1 = [1.0, 0.0]
        v2 = [0.0, 1.0]
        assert EmbeddingEngine.cosine_similarity(v1, v2) == pytest.approx(0.0, abs=0.01)

    def test_opposite_vectors(self) -> None:
        v1 = [1.0, 0.0]
        v2 = [-1.0, 0.0]
        assert EmbeddingEngine.cosine_similarity(v1, v2) == pytest.approx(-1.0, abs=0.01)

    def test_clamped_to_valid_range(self) -> None:
        v1 = [100.0] * 10
        v2 = [100.0] * 10
        result = EmbeddingEngine.cosine_similarity(v1, v2)
        assert -1.0 <= result <= 1.0


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
        q = await engine.encode("machine learning", is_query=True)
        p = await engine.encode("machine learning", is_query=False)
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
        similar = await engine.encode("quantum mechanics formulas")
        sim_different = _cosine_similarity(a, b)
        sim_similar = _cosine_similarity(a, similar)
        # Different topics should be less similar than related topics
        assert sim_different < sim_similar

    async def test_encode_batch(self, engine: EmbeddingEngine) -> None:
        texts = ["hello", "world", "test"]
        results = await engine.encode_batch(texts)
        assert len(results) == 3  # noqa: PLR2004
        assert all(len(r) == MODEL_DIMENSIONS for r in results)

    async def test_is_loaded_after_encode(self, engine: EmbeddingEngine) -> None:
        assert engine.is_loaded is True
        assert engine.has_embeddings is True

    async def test_lazy_loading(self) -> None:
        engine = EmbeddingEngine()
        assert engine.is_loaded is False
        await engine.encode("trigger lazy load")
        assert engine.is_loaded is True
