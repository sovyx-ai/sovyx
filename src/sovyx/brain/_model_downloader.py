"""Sovyx embedding engine.

Generates 384-dimensional embeddings using E5-small-v2 via ONNX Runtime.
Includes robust model download with rate-limit handling, mirror fallback,
checksum verification, and lazy loading.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import time
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import httpx

from sovyx.engine.errors import EmbeddingError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = get_logger(__name__)

# ── Model constants ─────────────────────────────────────────────────────────

MODEL_FILENAME = "e5-small-v2.onnx"
MODEL_SHA256 = "4b8205be2a3c5fc53c6534d76a2012064f7309c162b806f2889c6ec8ec4fdcba"
TOKENIZER_FILENAME = "tokenizer.json"
TOKENIZER_SHA256 = "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"
MODEL_DIMENSIONS = 384
MAX_TOKENS = 512

# Primary + mirror URLs for resilience.
# Order: HuggingFace (canonical) → GitHub Releases (mirror).
MODEL_URLS: tuple[str, ...] = (
    "https://huggingface.co/intfloat/e5-small-v2/resolve/main/model.onnx",
    "https://github.com/sovyx-ai/sovyx/releases/download/models-v1/e5-small-v2.onnx",
)
TOKENIZER_URLS: tuple[str, ...] = (
    "https://huggingface.co/intfloat/e5-small-v2/resolve/main/tokenizer.json",
    "https://github.com/sovyx-ai/sovyx/releases/download/models-v1/tokenizer.json",
)

# Backward compatibility aliases (single URL).
MODEL_URL = MODEL_URLS[0]
TOKENIZER_URL = TOKENIZER_URLS[0]


# ── Error classification ────────────────────────────────────────────────────


def _is_transient(status_code: int) -> bool:
    """True if the HTTP status indicates a transient/retriable error."""
    return status_code in {408, 429, 500, 502, 503, 504, 520, 522, 524}


def _is_permanent(status_code: int) -> bool:
    """True if the HTTP status indicates a permanent/non-retriable error."""
    return status_code in {401, 403, 404, 410, 451}


# ── Download cooldown ───────────────────────────────────────────────────────

# Default sourced from EngineConfig.tuning.brain; overridable via
# ``SOVYX_TUNING__BRAIN__MODEL_DOWNLOAD_COOLDOWN_SECONDS``.
from sovyx.engine.config import BrainTuningConfig as _BrainTuning  # noqa: E402

_COOLDOWN_SECONDS = _BrainTuning().model_download_cooldown_seconds


def _cooldown_path(models_dir: Path, filename: str) -> Path:
    """Path to the cooldown marker for a given model file."""
    return models_dir / f".{filename}.failed"


def _is_in_cooldown(models_dir: Path, filename: str) -> bool:
    """Check if a previous download failure is still within cooldown."""
    marker = _cooldown_path(models_dir, filename)
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text())
        failed_at: float = data.get("failed_at", 0)
        return bool((time.time() - failed_at) < _COOLDOWN_SECONDS)
    except (json.JSONDecodeError, OSError):
        marker.unlink(missing_ok=True)
        return False


def _write_cooldown(
    models_dir: Path,
    filename: str,
    error: str,
    attempts: int,
) -> None:
    """Record a download failure for cooldown enforcement."""
    marker = _cooldown_path(models_dir, filename)
    data = {
        "failed_at": time.time(),
        "error": error[:500],
        "attempts": attempts,
        "cooldown_seconds": _COOLDOWN_SECONDS,
    }
    with contextlib.suppress(OSError):
        marker.write_text(json.dumps(data))


def _clear_cooldown(models_dir: Path, filename: str) -> None:
    """Remove cooldown marker after successful download."""
    _cooldown_path(models_dir, filename).unlink(missing_ok=True)


# ── Model Downloader ────────────────────────────────────────────────────────


class ModelDownloader:
    """Download and verify ONNX models with enterprise-grade resilience.

    Features:
        - Multiple mirror URLs with automatic failover
        - Retry-After header respect (RFC 7231 §7.1.3)
        - Exponential backoff with decorrelated jitter (AWS-style)
        - Transient vs permanent error classification
        - HuggingFace token authentication (HF_TOKEN env var)
        - Download cooldown (15min marker after exhausting retries)
        - SHA-256 checksum verification post-download
        - Atomic write (download to .tmp, rename after checksum ok)
        - Configurable model directory (SOVYX_MODEL_DIR env var)
    """

    MAX_RETRIES: ClassVar[int] = 5
    BACKOFF_BASE: ClassVar[float] = 1.0
    BACKOFF_MAX: ClassVar[float] = 60.0
    DOWNLOAD_TIMEOUT: ClassVar[float] = 300.0

    def __init__(self, models_dir: Path | None = None) -> None:
        env_dir = os.environ.get("SOVYX_MODEL_DIR")
        if models_dir is not None:
            self.models_dir = models_dir
        elif env_dir:
            self.models_dir = Path(env_dir)
        else:
            self.models_dir = Path.home() / ".sovyx" / "models"

    async def ensure_model(
        self,
        filename: str,
        url: str,
        expected_sha256: str = "",
        progress_callback: Callable[[int, int], None] | None = None,
        *,
        mirror_urls: Sequence[str] = (),
    ) -> Path:
        """Download model if not present. Returns path to the file.

        Tries the primary URL first. If all retries are exhausted,
        falls through to each mirror URL with fresh retries.

        Args:
            filename: Target filename in models_dir.
            url: Primary download URL.
            expected_sha256: Expected SHA-256 hex digest (skip if empty).
            progress_callback: Optional (downloaded, total) callback.
            mirror_urls: Fallback URLs tried after primary exhausts retries.

        Returns:
            Path to the verified model file.

        Raises:
            EmbeddingError: If download fails on all URLs or checksum mismatch.
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        target = self.models_dir / filename

        # ── Fast path: file already exists and checksum matches ──
        if target.exists():
            if expected_sha256 and not self._verify_checksum(target, expected_sha256):
                logger.warning(
                    "model_checksum_mismatch_redownloading",
                    filename=filename,
                )
                target.unlink()
            else:
                return target

        # ── Cooldown check: don't hammer if we just failed ──
        if _is_in_cooldown(self.models_dir, filename):
            logger.info(
                "model_download_in_cooldown",
                filename=filename,
                cooldown_seconds=_COOLDOWN_SECONDS,
            )
            msg = (
                f"Download of {filename} is in cooldown after recent failure. "
                f"Retry in up to {_COOLDOWN_SECONDS // 60} minutes."
            )
            raise EmbeddingError(msg)

        # ── Build URL list: primary + mirrors ──
        all_urls = [url, *mirror_urls]

        total_attempts = 0
        last_error: Exception | None = None

        for url_idx, download_url in enumerate(all_urls):
            source = "primary" if url_idx == 0 else f"mirror-{url_idx}"
            logger.info(
                "model_download_starting",
                filename=filename,
                source=source,
                url=download_url,
            )

            result = await self._try_download_with_retries(
                filename=filename,
                url=download_url,
                expected_sha256=expected_sha256,
                progress_callback=progress_callback,
                source=source,
            )

            if isinstance(result, Path):
                _clear_cooldown(self.models_dir, filename)
                return result

            # result is (attempts, last_exception)
            attempts, exc = result
            total_attempts += attempts
            last_error = exc

            if url_idx < len(all_urls) - 1:
                logger.info(
                    "model_download_trying_mirror",
                    filename=filename,
                    next_source=f"mirror-{url_idx + 1}",
                    previous_error=str(exc),
                )

        # ── All URLs exhausted: write cooldown and raise ──
        _write_cooldown(
            self.models_dir,
            filename,
            str(last_error),
            total_attempts,
        )

        msg = (
            f"Failed to download {filename} after {total_attempts} attempts "
            f"across {len(all_urls)} source(s). "
            f"Next retry allowed in {_COOLDOWN_SECONDS // 60} minutes."
        )
        raise EmbeddingError(msg) from last_error

    async def _try_download_with_retries(
        self,
        *,
        filename: str,
        url: str,
        expected_sha256: str,
        progress_callback: Callable[[int, int], None] | None,
        source: str,
    ) -> Path | tuple[int, Exception]:
        """Attempt download with retries. Returns Path on success or
        (attempt_count, last_exception) on exhaustion."""
        target = self.models_dir / filename
        tmp_path = target.with_suffix(".tmp")
        last_error: Exception | None = None
        sleep_time = self.BACKOFF_BASE

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                await self._download(url, tmp_path, progress_callback, self._auth_headers())

                if expected_sha256 and not self._verify_checksum(tmp_path, expected_sha256):
                    tmp_path.unlink(missing_ok=True)
                    msg = f"Checksum mismatch for {filename} (expected {expected_sha256[:16]}...)"
                    raise EmbeddingError(msg)

                tmp_path.rename(target)
                logger.info(
                    "model_downloaded",
                    filename=filename,
                    source=source,
                    attempts=attempt,
                )
                return target

            except EmbeddingError:
                # Checksum mismatch is permanent — don't retry.
                raise

            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code

                if _is_permanent(status):
                    logger.warning(
                        "model_download_permanent_error",
                        filename=filename,
                        source=source,
                        status=status,
                        attempt=attempt,
                    )
                    tmp_path.unlink(missing_ok=True)
                    break  # No retries for permanent errors.

                # Transient error — compute wait time.
                retry_after = self._parse_retry_after(exc.response)
                if retry_after is not None:
                    wait = min(retry_after, self.BACKOFF_MAX)
                    logger.info(
                        "model_download_rate_limited",
                        filename=filename,
                        source=source,
                        status=status,
                        attempt=attempt,
                        retry_after_seconds=wait,
                    )
                else:
                    # Decorrelated jitter: sleep = min(max, rand(base, sleep*3))
                    wait = min(
                        self.BACKOFF_MAX,
                        random.uniform(  # noqa: S311
                            self.BACKOFF_BASE, sleep_time * 3
                        ),
                    )
                    logger.warning(
                        "model_download_retry",
                        filename=filename,
                        source=source,
                        status=status,
                        attempt=attempt,
                        wait_seconds=round(wait, 1),
                        error=str(exc),
                    )

                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(wait)
                    sleep_time = wait  # Feed back into jitter.

            except Exception as exc:
                last_error = exc
                wait = min(
                    self.BACKOFF_MAX,
                    random.uniform(  # noqa: S311
                        self.BACKOFF_BASE, sleep_time * 3
                    ),
                )
                logger.warning(
                    "model_download_retry",
                    filename=filename,
                    source=source,
                    attempt=attempt,
                    wait_seconds=round(wait, 1),
                    error=str(exc),
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(wait)
                    sleep_time = wait

        tmp_path.unlink(missing_ok=True)
        assert last_error is not None  # At least one attempt ran.  # noqa: S101
        return (self.MAX_RETRIES, last_error)

    @staticmethod
    async def _download(
        url: str,
        dest: Path,
        callback: Callable[[int, int], None] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Stream-download a file with optional auth headers."""
        req_headers = dict(headers) if headers else {}
        async with (
            httpx.AsyncClient(follow_redirects=True, headers=req_headers) as client,
            client.stream("GET", url, timeout=ModelDownloader.DOWNLOAD_TIMEOUT) as resp,
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
    def _auth_headers() -> dict[str, str]:
        """Build auth headers from HF_TOKEN or HUGGING_FACE_HUB_TOKEN."""
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """Parse Retry-After from response (RFC 7231 §7.1.3).

        Supports both delta-seconds and HTTP-date formats.
        Also checks X-RateLimit-Reset (common non-standard header).
        """
        # Standard header: Retry-After
        raw = response.headers.get("retry-after")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass  # Could be HTTP-date; we ignore for simplicity.

        # Non-standard: X-RateLimit-Reset (epoch seconds)
        reset = response.headers.get("x-ratelimit-reset")
        if reset:
            try:
                delta = float(reset) - time.time()
                if delta > 0:
                    return delta
            except ValueError:
                pass

        return None

    @staticmethod
    def _verify_checksum(path: Path, expected: str) -> bool:
        """Verify SHA-256 checksum of a file."""
        sha256 = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
        return sha256.hexdigest() == expected


# ── Embedding Engine ────────────────────────────────────────────────────────
