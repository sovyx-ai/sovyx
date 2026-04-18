"""Generic resilient model downloader — shared by brain + voice.

Feature set:

- Multiple mirror URLs with automatic failover (primary → mirror-1 → …).
- ``Retry-After`` / ``X-RateLimit-Reset`` header respect (RFC 7231 §7.1.3).
- Exponential backoff with **decorrelated jitter** (AWS-style).
- Transient vs permanent HTTP error classification — 4xx permanents short
  the retry loop, 5xx/429 keep retrying.
- Cooldown marker (``.{filename}.failed``) blocks hammer-retry after
  exhausting all URLs.
- SHA-256 checksum verification — mismatch is permanent (no retry).
- Atomic write via ``.tmp`` + rename.
- HuggingFace Bearer auth via ``HF_TOKEN`` / ``HUGGING_FACE_HUB_TOKEN``.
- Optional OTel counter hook (``on_attempt``) for observability without
  creating a hard dependency on the observability package.

This module deliberately has NO brain-specific or voice-specific
knowledge. It was extracted from :mod:`sovyx.brain._model_downloader`
so :mod:`sovyx.voice.model_registry` could stop carrying a strictly
inferior sibling implementation (single URL, fixed backoff, no Retry-
After, no cooldown, no classification, sync-inside-async).
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

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = get_logger(__name__)


# ── Error classification ────────────────────────────────────────────────────


def _is_transient(status_code: int) -> bool:
    """True if the HTTP status indicates a transient/retriable error."""
    return status_code in {408, 429, 500, 502, 503, 504, 520, 522, 524}


def _is_permanent(status_code: int) -> bool:
    """True if the HTTP status indicates a permanent/non-retriable error."""
    return status_code in {401, 403, 404, 410, 451}


# ── Cooldown marker (filesystem) ─────────────────────────────────────────────


_DEFAULT_COOLDOWN_SECONDS: float = 900.0  # 15 minutes — overridable per-instance


def _cooldown_path(models_dir: Path, filename: str) -> Path:
    """Path to the cooldown marker for a given model file."""
    return models_dir / f".{filename}.failed"


def _is_in_cooldown(
    models_dir: Path,
    filename: str,
    cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
) -> bool:
    """Check if a previous download failure is still within cooldown."""
    marker = _cooldown_path(models_dir, filename)
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text())
        failed_at: float = data.get("failed_at", 0)
        return bool((time.time() - failed_at) < cooldown_seconds)
    except (json.JSONDecodeError, OSError):
        marker.unlink(missing_ok=True)
        return False


def _write_cooldown(
    models_dir: Path,
    filename: str,
    error: str,
    attempts: int,
    cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
) -> None:
    """Record a download failure for cooldown enforcement."""
    marker = _cooldown_path(models_dir, filename)
    data = {
        "failed_at": time.time(),
        "error": error[:500],
        "attempts": attempts,
        "cooldown_seconds": cooldown_seconds,
    }
    with contextlib.suppress(OSError):
        marker.write_text(json.dumps(data))


def _clear_cooldown(models_dir: Path, filename: str) -> None:
    """Remove cooldown marker after successful download."""
    _cooldown_path(models_dir, filename).unlink(missing_ok=True)


# ── Typed download error ─────────────────────────────────────────────────────


class ModelDownloadError(RuntimeError):
    """Raised when all mirror URLs exhaust retries or checksum fails.

    Typed class so callers can distinguish a genuine download failure
    from generic runtime errors without string-matching.
    """


# ── Attempt record (used by the OTel hook) ───────────────────────────────────


class DownloadAttempt:
    """Immutable record of a single mirror attempt.

    Exposed to the optional ``on_attempt`` callback so callers can feed
    it into OTel counters without having to parse log messages.
    """

    __slots__ = ("filename", "source", "url", "result", "status_code", "error_type")

    def __init__(
        self,
        *,
        filename: str,
        source: str,
        url: str,
        result: str,
        status_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        self.filename = filename
        self.source = source
        self.url = url
        self.result = result  # "ok" | "transient" | "permanent" | "error"
        self.status_code = status_code
        self.error_type = error_type


# ── Model Downloader ────────────────────────────────────────────────────────


class ModelDownloader:
    """Download and verify model files with enterprise-grade resilience.

    See module docstring for the full feature list. Instantiate once per
    cache directory and reuse across multiple ``ensure_model`` calls.

    Args:
        models_dir: Target directory. Falls back to ``SOVYX_MODEL_DIR``
            env var, then ``~/.sovyx/models``.
        cooldown_seconds: Duration of the post-failure cooldown marker.
        on_attempt: Optional callable invoked for each mirror attempt
            (success or failure). Used for OTel instrumentation without
            a hard dependency on the observability package.
    """

    MAX_RETRIES: ClassVar[int] = 5
    BACKOFF_BASE: ClassVar[float] = 1.0
    BACKOFF_MAX: ClassVar[float] = 60.0
    DOWNLOAD_TIMEOUT: ClassVar[float] = 300.0
    DEFAULT_COOLDOWN_SECONDS: ClassVar[float] = 900.0  # 15 min

    def __init__(
        self,
        models_dir: Path | None = None,
        *,
        cooldown_seconds: float | None = None,
        on_attempt: Callable[[DownloadAttempt], None] | None = None,
    ) -> None:
        env_dir = os.environ.get("SOVYX_MODEL_DIR")
        if models_dir is not None:
            self.models_dir = models_dir
        elif env_dir:
            self.models_dir = Path(env_dir)
        else:
            self.models_dir = Path.home() / ".sovyx" / "models"

        self.cooldown_seconds = (
            cooldown_seconds if cooldown_seconds is not None else self.DEFAULT_COOLDOWN_SECONDS
        )
        self._on_attempt = on_attempt

    async def ensure_model(
        self,
        filename: str,
        url: str,
        expected_sha256: str = "",
        progress_callback: Callable[[int, int], None] | None = None,
        *,
        mirror_urls: Sequence[str] = (),
    ) -> Path:
        """Download ``filename`` if not present, verify checksum, return path.

        Tries ``url`` first. On exhaustion, falls through each
        ``mirror_urls`` entry with fresh retries.

        Raises:
            ModelDownloadError: If all URLs fail or checksum mismatches.
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        target = self.models_dir / filename

        # Fast path — already on disk, checksum OK.
        if target.exists():
            if expected_sha256 and not self._verify_checksum(target, expected_sha256):
                logger.warning(
                    "model_checksum_mismatch_redownloading",
                    filename=filename,
                )
                target.unlink()
            else:
                return target

        # Cooldown — prevent hammer-retry after recent exhaustion.
        if _is_in_cooldown(self.models_dir, filename, self.cooldown_seconds):
            logger.info(
                "model_download_in_cooldown",
                filename=filename,
                cooldown_seconds=self.cooldown_seconds,
            )
            msg = (
                f"Download of {filename} is in cooldown after recent failure. "
                f"Retry in up to {int(self.cooldown_seconds // 60)} minutes."
            )
            raise ModelDownloadError(msg)

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

        _write_cooldown(
            self.models_dir,
            filename,
            str(last_error),
            total_attempts,
            self.cooldown_seconds,
        )

        msg = (
            f"Failed to download {filename} after {total_attempts} attempts "
            f"across {len(all_urls)} source(s). "
            f"Next retry allowed in {int(self.cooldown_seconds // 60)} minutes."
        )
        raise ModelDownloadError(msg) from last_error

    async def _try_download_with_retries(
        self,
        *,
        filename: str,
        url: str,
        expected_sha256: str,
        progress_callback: Callable[[int, int], None] | None,
        source: str,
    ) -> Path | tuple[int, Exception]:
        """Retry loop for a single URL. Returns path on success, else
        ``(attempts, last_exception)`` on exhaustion."""
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
                    raise ModelDownloadError(msg)

                tmp_path.rename(target)
                logger.info(
                    "model_downloaded",
                    filename=filename,
                    source=source,
                    attempts=attempt,
                )
                self._emit_attempt(filename, source, url, result="ok")
                return target

            except ModelDownloadError:
                self._emit_attempt(
                    filename, source, url, result="permanent", error_type="ChecksumMismatch"
                )
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
                    self._emit_attempt(
                        filename,
                        source,
                        url,
                        result="permanent",
                        status_code=status,
                        error_type=type(exc).__name__,
                    )
                    break

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

                self._emit_attempt(
                    filename,
                    source,
                    url,
                    result="transient",
                    status_code=status,
                    error_type=type(exc).__name__,
                )

                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(wait)
                    sleep_time = wait

            except (httpx.HTTPError, OSError, TimeoutError) as exc:
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
                    error_type=type(exc).__name__,
                )
                self._emit_attempt(
                    filename,
                    source,
                    url,
                    result="transient",
                    error_type=type(exc).__name__,
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(wait)
                    sleep_time = wait

        tmp_path.unlink(missing_ok=True)
        assert last_error is not None  # noqa: S101
        return (self.MAX_RETRIES, last_error)

    def _emit_attempt(
        self,
        filename: str,
        source: str,
        url: str,
        *,
        result: str,
        status_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        """Invoke the optional OTel hook without letting it break the download."""
        if self._on_attempt is None:
            return
        try:
            self._on_attempt(
                DownloadAttempt(
                    filename=filename,
                    source=source,
                    url=url,
                    result=result,
                    status_code=status_code,
                    error_type=error_type,
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("model_download_attempt_hook_failed", exc_info=True)

    @staticmethod
    async def _download(
        url: str,
        dest: Path,
        callback: Callable[[int, int], None] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Stream-download a file to ``dest`` with optional auth headers."""
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
        """Build auth headers from ``HF_TOKEN`` / ``HUGGING_FACE_HUB_TOKEN``."""
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """Parse ``Retry-After`` (delta-seconds) or ``X-RateLimit-Reset`` (epoch)."""
        raw = response.headers.get("retry-after")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass

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


__all__ = [
    "DownloadAttempt",
    "ModelDownloadError",
    "ModelDownloader",
    "_clear_cooldown",
    "_cooldown_path",
    "_is_in_cooldown",
    "_is_permanent",
    "_is_transient",
    "_write_cooldown",
]
