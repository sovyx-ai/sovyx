"""Sovyx embedding engine.

Generates 384-dimensional embeddings using E5-small-v2 via ONNX Runtime.
Includes model download with retry, checksum verification, and lazy loading.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
import numpy as np

from sovyx.engine.errors import EmbeddingError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = get_logger(__name__)

# ── Model constants ─────────────────────────────────────────────────────────

MODEL_URL = "https://huggingface.co/intfloat/e5-small-v2/resolve/main/model_quantized.onnx"
MODEL_FILENAME = "e5-small-v2-q8.onnx"
TOKENIZER_FILENAME = "tokenizer.json"
TOKENIZER_URL = "https://huggingface.co/intfloat/e5-small-v2/resolve/main/tokenizer.json"
MODEL_DIMENSIONS = 384
MAX_TOKENS = 512


# ── Model Downloader ────────────────────────────────────────────────────────


class ModelDownloader:
    """Download and verify ONNX models.

    Features:
        - Download with retry (3 attempts, exponential backoff)
        - SHA-256 checksum verification post-download
        - Atomic write (download to .tmp, rename after checksum ok)
        - Auto-creates models directory
    """

    MAX_RETRIES: ClassVar[int] = 3
    BACKOFF_BASE: ClassVar[float] = 1.0

    def __init__(self, models_dir: Path | None = None) -> None:
        self.models_dir = models_dir or Path.home() / ".sovyx" / "models"

    async def ensure_model(
        self,
        filename: str,
        url: str,
        expected_sha256: str = "",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download model if not present. Returns path to the file.

        Args:
            filename: Target filename in models_dir.
            url: Download URL.
            expected_sha256: Expected SHA-256 hex digest (skip check if empty).
            progress_callback: Optional (downloaded_bytes, total_bytes) callback.

        Returns:
            Path to the model file.

        Raises:
            EmbeddingError: If download fails after retries or checksum mismatch.
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        target = self.models_dir / filename

        if target.exists():
            if expected_sha256 and not self._verify_checksum(target, expected_sha256):
                logger.warning(
                    "model_checksum_mismatch_redownloading",
                    filename=filename,
                )
                target.unlink()
            else:
                return target

        tmp_path = target.with_suffix(".tmp")
        last_error: Exception | None = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                await self._download(url, tmp_path, progress_callback)

                if expected_sha256 and not self._verify_checksum(tmp_path, expected_sha256):
                    tmp_path.unlink(missing_ok=True)
                    msg = f"Checksum mismatch for {filename} (expected {expected_sha256[:16]}...)"
                    raise EmbeddingError(msg)

                tmp_path.rename(target)
                logger.info("model_downloaded", filename=filename)
                return target

            except EmbeddingError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "model_download_retry",
                    filename=filename,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.BACKOFF_BASE * (2 ** (attempt - 1)))

        tmp_path.unlink(missing_ok=True)
        msg = f"Failed to download {filename} after {self.MAX_RETRIES} attempts"
        raise EmbeddingError(msg) from last_error

    @staticmethod
    async def _download(
        url: str,
        dest: Path,
        callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """Download via httpx with streaming."""
        async with (
            httpx.AsyncClient(follow_redirects=True) as client,
            client.stream("GET", url, timeout=300) as resp,
        ):
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if callback:
                        callback(downloaded, total)

    @staticmethod
    def _verify_checksum(path: Path, expected: str) -> bool:
        """Verify SHA-256 checksum of a file."""
        sha256 = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
        return sha256.hexdigest() == expected


# ── Embedding Engine ────────────────────────────────────────────────────────


class EmbeddingEngine:
    """Generate text embeddings using E5-small-v2 via ONNX Runtime.

    Model: intfloat/e5-small-v2 quantized int8 (~34MB)
    Dimensions: 384
    E5 prefix: "query: " for queries, "passage: " for documents.

    Lazy loading: model loaded on first use, not on instantiation.
    """

    def __init__(self, model_dir: Path | None = None) -> None:
        self._model_dir = model_dir or Path.home() / ".sovyx" / "models"
        # onnxruntime.InferenceSession and tokenizers.Tokenizer
        # are untyped libraries — using Any
        self._session: Any = None
        self._tokenizer: Any = None
        self._has_embeddings = False
        self._loaded = False
        self._init_lock = asyncio.Lock()

    async def ensure_loaded(self) -> None:
        """Ensure model is loaded. Downloads if necessary.

        Uses double-checked locking to prevent concurrent downloads:
        fast path (no lock) for already-loaded case, lock for first init.

        If the model is unavailable (no internet, ONNX fails),
        sets has_embeddings=False for FTS5 fallback. Does NOT raise.
        """
        # Fast path: already loaded
        if self._loaded:
            return

        async with self._init_lock:
            # Double-check after acquiring lock
            if self._loaded:
                return

            try:
                downloader = ModelDownloader(self._model_dir)

                model_path = await downloader.ensure_model(MODEL_FILENAME, MODEL_URL)
                tokenizer_path = await downloader.ensure_model(TOKENIZER_FILENAME, TOKENIZER_URL)

                self._load_model(model_path, tokenizer_path)
                self._has_embeddings = True
                self._loaded = True
                logger.info("embedding_engine_loaded", model_dir=str(self._model_dir))

            except Exception:
                logger.warning(
                    "embedding_model_unavailable_fts5_fallback",
                    exc_info=True,
                )
                self._has_embeddings = False
                self._loaded = True

    def _load_model(self, model_path: Path, tokenizer_path: Path) -> None:
        """Load ONNX session and tokenizer."""
        import onnxruntime as ort  # type: ignore[import-untyped]
        from tokenizers import Tokenizer  # type: ignore[import-not-found]

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.log_severity_level = logging.WARNING

        self._session = ort.InferenceSession(
            str(model_path),
            sess_options,
            providers=["CPUExecutionProvider"],
        )
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self._tokenizer.enable_padding(length=MAX_TOKENS, pad_id=0, pad_token="[PAD]")

    async def encode(self, text: str, *, is_query: bool = False) -> list[float]:
        """Generate embedding for a single text.

        Args:
            text: Text to encode (truncated to 512 tokens).
            is_query: If True, prepends "query: "; else "passage: ".

        Returns:
            List of 384 L2-normalized floats.

        Raises:
            EmbeddingError: If model unavailable or input invalid.
        """
        if not text or not text.strip():
            msg = "Cannot encode empty text"
            raise EmbeddingError(msg)

        await self.ensure_loaded()

        if not self._has_embeddings:
            msg = "Embedding model not available"
            raise EmbeddingError(msg)

        prefix = "query: " if is_query else "passage: "
        prefixed = prefix + text

        result = await asyncio.to_thread(self._encode_sync, [prefixed])
        return result[0]

    async def encode_batch(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: Texts to encode.
            is_query: If True, prepends "query: " to each text.

        Returns:
            List of embedding vectors (384 floats each).

        Raises:
            EmbeddingError: If model unavailable.
        """
        if not texts:
            return []

        await self.ensure_loaded()

        if not self._has_embeddings:
            msg = "Embedding model not available"
            raise EmbeddingError(msg)

        prefix = "query: " if is_query else "passage: "
        prefixed = [prefix + t for t in texts]

        return await asyncio.to_thread(self._encode_sync, prefixed)

    def _encode_sync(self, texts: list[str]) -> list[list[float]]:
        """Synchronous encoding (runs in thread pool)."""
        if self._tokenizer is None or self._session is None:
            msg = "EmbeddingEngine not loaded — call ensure_loaded() first"
            raise RuntimeError(msg)

        encoded = self._tokenizer.encode_batch(texts)

        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)

        outputs = self._session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )

        # Mean pooling with attention mask
        token_embeddings = outputs[0]  # (batch, seq_len, hidden)
        mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
        sum_embeddings = np.sum(token_embeddings * mask_expanded, axis=1)
        sum_mask = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        mean_pooled = sum_embeddings / sum_mask

        # L2 normalize
        norms = np.linalg.norm(mean_pooled, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-9, a_max=None)
        normalized = mean_pooled / norms

        return [row.tolist() for row in normalized]

    @property
    def dimensions(self) -> int:
        """Embedding dimensionality (384 for E5-small-v2)."""
        return MODEL_DIMENSIONS

    @property
    def is_loaded(self) -> bool:
        """True if the model has been loaded (or load attempted)."""
        return self._loaded

    @property
    def has_embeddings(self) -> bool:
        """True if model loaded successfully. False → FTS5 fallback."""
        return self._has_embeddings
