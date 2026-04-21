"""Sovyx EmbeddingEngine - ONNX-Runtime embedding service.

ModelDownloader extracted to ``brain/_model_downloader.py``.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import onnxruntime as ort

from sovyx.brain._model_downloader import (
    _COOLDOWN_SECONDS,
    MAX_TOKENS,
    MODEL_DIMENSIONS,
    MODEL_FILENAME,
    MODEL_SHA256,
    MODEL_URL,
    MODEL_URLS,
    TOKENIZER_FILENAME,
    TOKENIZER_SHA256,
    TOKENIZER_URL,
    TOKENIZER_URLS,
    ModelDownloader,
    _clear_cooldown,  # noqa: F401  (re-exported for tests / back-compat)
    _cooldown_path,  # noqa: F401  (re-exported for tests / back-compat)
    _is_in_cooldown,  # noqa: F401  (re-exported for tests / back-compat)
    _is_permanent,  # noqa: F401  (re-exported for tests / back-compat)
    _is_transient,  # noqa: F401  (re-exported for tests / back-compat)
    _write_cooldown,  # noqa: F401  (re-exported for tests / back-compat)
)
from sovyx.engine._model_downloader import ModelDownloadError
from sovyx.engine.errors import EmbeddingError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.engine._model_downloader import DownloadAttempt


__all__ = [
    "MAX_TOKENS",
    "MODEL_DIMENSIONS",
    "MODEL_FILENAME",
    "MODEL_SHA256",
    "MODEL_URL",
    "MODEL_URLS",
    "TOKENIZER_FILENAME",
    "TOKENIZER_SHA256",
    "TOKENIZER_URL",
    "TOKENIZER_URLS",
    "EmbeddingEngine",
    "EmbeddingError",
    "ModelDownloadError",
    "ModelDownloader",
    "_COOLDOWN_SECONDS",
    "_clear_cooldown",
    "_cooldown_path",
    "_is_in_cooldown",
    "_is_permanent",
    "_is_transient",
    "_write_cooldown",
]


logger = get_logger(__name__)


def _record_download_attempt(attempt: DownloadAttempt) -> None:
    """Feed one mirror attempt into ``sovyx.model.download.attempts``.

    Same label contract as the voice-tier hook — keeps the counter
    homogeneous across brain + voice models so a single PromQL query
    (``rate by (source, result)``) answers "how often do mirrors save us."
    """
    from sovyx.observability.metrics import get_metrics  # noqa: PLC0415

    metrics = get_metrics()
    metrics.model_download_attempts.add(
        1,
        {
            "model": attempt.filename,
            "source": attempt.source,
            "result": attempt.result,
            "error_type": attempt.error_type or "",
        },
    )


class EmbeddingEngine:
    """Generate text embeddings using E5-small-v2 via ONNX Runtime.

    Model: intfloat/e5-small-v2 quantized int8 (~34MB)
    Dimensions: 384
    E5 prefix: "query: " for queries, "passage: " for documents.

    Lazy loading: model loaded on first use, not on instantiation.

    Configuration via environment variables:
        SOVYX_MODEL_DIR: Custom directory for model files.
        HF_TOKEN: HuggingFace authentication token (avoids rate limits).
    """

    def __init__(self, model_dir: Path | None = None) -> None:
        env_dir = os.environ.get("SOVYX_MODEL_DIR")
        if model_dir is not None:
            self._model_dir = model_dir
        elif env_dir:
            self._model_dir = Path(env_dir)
        else:
            self._model_dir = Path.home() / ".sovyx" / "models"

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
                downloader = ModelDownloader(
                    self._model_dir,
                    cooldown_seconds=_COOLDOWN_SECONDS,
                    on_attempt=_record_download_attempt,
                )

                model_path = await downloader.ensure_model(
                    MODEL_FILENAME,
                    MODEL_URL,
                    expected_sha256=MODEL_SHA256,
                    mirror_urls=MODEL_URLS[1:],
                )
                tokenizer_path = await downloader.ensure_model(
                    TOKENIZER_FILENAME,
                    TOKENIZER_URL,
                    expected_sha256=TOKENIZER_SHA256,
                    mirror_urls=TOKENIZER_URLS[1:],
                )

                self._load_model(model_path, tokenizer_path)
                self._has_embeddings = True
                self._loaded = True
                logger.info(
                    "embedding_engine_loaded",
                    model_dir=str(self._model_dir),
                )

            except (EmbeddingError, ModelDownloadError, OSError, RuntimeError, ImportError) as exc:
                # ModelDownloadError: download/checksum failure from the
                # shared downloader. EmbeddingError: historical alias,
                # still caught for back-compat. OSError: missing/unreadable
                # model file. RuntimeError: ONNX session construction
                # failure (invalid graph, EP unavailable). ImportError:
                # onnxruntime or tokenizers not installable on this
                # platform. This is the graceful-fallback path — search
                # still works via FTS5, so log the reason structured but
                # don't dump a traceback.
                logger.warning(
                    "embedding_model_unavailable_fts5_fallback",
                    reason=str(exc),
                    reason_type=type(exc).__name__,
                )
                self._has_embeddings = False
                self._loaded = True

    def _load_model(self, model_path: Path, tokenizer_path: Path) -> None:
        """Load ONNX session and tokenizer."""
        from tokenizers import Tokenizer

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # ORT severity: 0=VERBOSE, 1=INFO, 2=WARNING, 3=ERROR, 4=FATAL
        sess_options.log_severity_level = 2  # WARNING

        self._session = ort.InferenceSession(
            str(model_path),
            sess_options,
            providers=["CPUExecutionProvider"],
        )
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self._tokenizer.enable_padding(  # nosec B106
            length=MAX_TOKENS,
            pad_id=0,
            pad_token="[PAD]",
        )

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

        started_at = time.monotonic()
        result = await asyncio.to_thread(self._encode_sync, [prefixed])
        embed_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "brain.embedding.encoded",
            **{
                "brain.text_len": len(text),
                "brain.embed_ms": embed_ms,
                "brain.cache_hit": False,
                "brain.is_query": is_query,
                "brain.batch_size": 1,
            },
        )
        return result[0]

    async def encode_batch(
        self,
        texts: Sequence[str],
        *,
        is_query: bool = False,
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

        started_at = time.monotonic()
        result = await asyncio.to_thread(self._encode_sync, prefixed)
        embed_ms = int((time.monotonic() - started_at) * 1000)
        total_chars = sum(len(t) for t in texts)
        logger.info(
            "brain.embedding.encoded",
            **{
                "brain.text_len": total_chars,
                "brain.embed_ms": embed_ms,
                "brain.cache_hit": False,
                "brain.is_query": is_query,
                "brain.batch_size": len(texts),
            },
        )
        return result

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

    # ── Category Centroid Cache ───────────────────────────────────────

    async def compute_category_centroid(
        self,
        embeddings: list[list[float]],
    ) -> list[float]:
        """Compute the mean centroid of a set of embeddings.

        Used to represent the "center of mass" of a concept category.
        The centroid is L2-normalized for consistent cosine similarity
        comparisons.

        Args:
            embeddings: List of L2-normalized embedding vectors.
                Must all have the same dimensionality (384).

        Returns:
            L2-normalized centroid vector.

        Raises:
            ValueError: If embeddings list is empty.
        """
        if not embeddings:
            msg = "Cannot compute centroid of empty embedding list"
            raise ValueError(msg)

        arr = np.array(embeddings, dtype=np.float32)
        mean = arr.mean(axis=0)
        norm = float(np.linalg.norm(mean))
        if norm < 1e-9:
            result: list[float] = mean.tolist()
            return result
        normalized: list[float] = (mean / norm).tolist()
        return normalized

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two L2-normalized vectors.

        Since both vectors are L2-normalized (from encode/centroid),
        cosine similarity equals their dot product.

        Args:
            a: First L2-normalized embedding vector.
            b: Second L2-normalized embedding vector.

        Returns:
            Cosine similarity in [-1.0, 1.0].
        """
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        return max(-1.0, min(1.0, dot))

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
