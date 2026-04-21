"""Disk-truth status + background download orchestration for voice models.

Decouples the wizard's "what's actually installed" question from the
static :mod:`~sovyx.voice.model_registry` metadata. Every entry here is
backed by a filesystem ``Path.exists()`` check, not a download-URL flag.

The progress tracker drives the setup wizard's download button: the POST
endpoint spawns a task, the GET endpoint returns the current byte count.
We deliberately reuse the registry's :func:`ensure_silero_vad` and
:func:`ensure_kokoro_tts` helpers so the wire protocol (URL, SHA256, retry
policy) stays defined in one place.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn
from sovyx.voice.model_registry import (
    VOICE_MODELS,
    VoiceModelInfo,
    ensure_kokoro_tts,
    ensure_silero_vad,
    get_default_model_dir,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

logger = get_logger(__name__)


# ── Expected on-disk layout ─────────────────────────────────────────
#
# The factory + ensure_* helpers write to these relative paths. Keep
# this table in lockstep with ``ensure_silero_vad`` / ``ensure_kokoro_tts``
# in :mod:`sovyx.voice.model_registry`.
_RELATIVE_PATHS: dict[str, str] = {
    "silero-vad-v5": "silero_vad.onnx",
    "kokoro-v1.0-int8": "kokoro/kokoro-v1.0.int8.onnx",
    "kokoro-voices-v1.0": "kokoro/voices-v1.0.bin",
}


# ── Public types ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VoiceModelDiskStatus:
    """Per-model snapshot of disk presence."""

    name: str
    category: str
    description: str
    installed: bool
    path: str
    size_mb: float
    expected_size_mb: float
    download_available: bool


@dataclass(frozen=True, slots=True)
class VoiceModelsStatus:
    """Aggregate disk-status for the wizard."""

    model_dir: str
    models: list[VoiceModelDiskStatus]
    all_installed: bool
    missing_count: int
    missing_download_mb: float


@dataclass(slots=True)
class ModelDownloadProgress:
    """Mutable progress snapshot shared between the task and GET polling.

    ``error_code`` is a stable, low-cardinality categorical identifier
    (``"cooldown"``, ``"all_mirrors_exhausted"``, ``"checksum_mismatch"``,
    ``"network"``, ``"unknown"``) that the frontend maps to an i18n key.
    The raw ``error`` string remains for diagnostic display.
    ``retry_after_seconds`` is set when ``error_code == "cooldown"`` so
    the UI can render a countdown without parsing the error text.
    """

    task_id: str
    status: str  # "running" | "done" | "error"
    total_models: int
    completed_models: int
    current_model: str | None
    error: str | None
    created_at: float
    finished_at: float | None = None
    error_code: str | None = None
    retry_after_seconds: int | None = None


@dataclass(slots=True)
class _DownloadEntry:
    """Internal tracker entry bound to an ``asyncio.Task``."""

    progress: ModelDownloadProgress
    task: asyncio.Task[None] | None = field(default=None)


# ── Disk status ─────────────────────────────────────────────────────


def check_voice_models_status(
    model_dir: Path | None = None,
) -> VoiceModelsStatus:
    """Stat each downloadable voice model on disk.

    Returns a :class:`VoiceModelsStatus` with one entry per entry in
    :data:`~sovyx.voice.model_registry.VOICE_MODELS` that is
    downloadable. Models marked ``download_available=False`` (e.g.
    moonshine-tiny — managed by the ``moonshine-voice`` package) are
    reported with ``installed=False`` and flagged for the UI to render
    as "managed externally" rather than as a broken dependency.
    """
    base = model_dir or get_default_model_dir()

    entries: list[VoiceModelDiskStatus] = []
    missing_count = 0
    missing_download_mb = 0.0

    for info in VOICE_MODELS.values():
        rel = _RELATIVE_PATHS.get(info.name)
        if rel is None:
            # No disk-path contract for this model (e.g. package-managed).
            entries.append(
                VoiceModelDiskStatus(
                    name=info.name,
                    category=info.category,
                    description=info.description,
                    installed=False,
                    path="",
                    size_mb=0.0,
                    expected_size_mb=info.size_mb,
                    download_available=info.download_available,
                ),
            )
            continue

        path = base / rel
        size_mb = 0.0
        # A mere ``path.exists()`` would misreport a zero-byte partial
        # (e.g. a download that crashed between mkstemp and the first
        # write) as "installed ✓". Load-time the ONNX runtime then fails
        # with an opaque parse error and the wizard offers no recovery,
        # so we require a non-zero size before declaring victory.
        installed = False
        try:
            stat = path.stat()
        except OSError:
            # Missing or vanished mid-race — leave installed=False.
            pass
        else:
            if stat.st_size > 0:
                installed = True
                size_mb = round(stat.st_size / (1024 * 1024), 1)

        if not installed and info.download_available:
            missing_count += 1
            missing_download_mb += info.size_mb

        entries.append(
            VoiceModelDiskStatus(
                name=info.name,
                category=info.category,
                description=info.description,
                installed=installed,
                path=str(path),
                size_mb=size_mb,
                expected_size_mb=info.size_mb,
                download_available=info.download_available,
            ),
        )

    return VoiceModelsStatus(
        model_dir=str(base),
        models=entries,
        all_installed=missing_count == 0,
        missing_count=missing_count,
        missing_download_mb=round(missing_download_mb, 1),
    )


def collect_missing_models(
    model_dir: Path | None = None,
) -> list[VoiceModelInfo]:
    """Return registry entries whose files are missing on disk.

    Used by the Test Speakers endpoint to tell the UI exactly which
    models to offer for download instead of a generic
    ``tts_unavailable`` message.
    """
    status = check_voice_models_status(model_dir)
    installed_names = {m.name for m in status.models if m.installed}
    return [
        info
        for info in VOICE_MODELS.values()
        if info.download_available and info.name not in installed_names
    ]


# ── Download orchestration ──────────────────────────────────────────
#
# Lifecycle: ``start_download()`` creates an entry, ``get_progress()``
# reads the shared snapshot, and the task updates the snapshot in place
# as each ``ensure_*`` helper returns. We never spawn two concurrent
# downloads — the helper is idempotent (skip if exists) but double-writes
# would race on the tempfile path.


_SingletonKey = str  # == task_id


def _new_task_id() -> str:
    """Monotonic, collision-proof task id. Short enough for URLs."""
    import secrets  # noqa: PLC0415

    return secrets.token_hex(8)


async def _run_download(
    entry: _DownloadEntry,
    model_dir: Path,
    missing: list[VoiceModelInfo],
) -> None:
    """Background coroutine — sequence the ``ensure_*`` helpers.

    Both helpers are no-ops when the target file already exists, so if
    the user retries a partially failed download we only re-fetch what's
    missing. Errors are captured on ``entry.progress.error`` so the GET
    endpoint can surface them without losing the partial success count.
    """
    try:
        need_silero = any(m.name == "silero-vad-v5" for m in missing)
        need_kokoro = any(m.name.startswith("kokoro") for m in missing)

        if need_silero:
            entry.progress.current_model = "silero-vad-v5"
            await ensure_silero_vad(model_dir)
            entry.progress.completed_models += 1

        if need_kokoro:
            # ensure_kokoro_tts handles BOTH kokoro files — count them
            # as two completed steps for a smoother progress bar.
            entry.progress.current_model = "kokoro-v1.0-int8"
            await ensure_kokoro_tts(model_dir)
            # Count both kokoro entries at once since ensure_kokoro_tts
            # fetches them together. If only one was missing we still
            # step by the actual missing count.
            kokoro_missing = sum(1 for m in missing if m.name.startswith("kokoro"))
            entry.progress.completed_models += kokoro_missing

        entry.progress.current_model = None
        entry.progress.status = "done"
        entry.progress.finished_at = time.monotonic()
        logger.info(
            "voice_model_download_done",
            task_id=entry.progress.task_id,
            completed=entry.progress.completed_models,
        )
    except Exception as exc:  # noqa: BLE001
        # Dispatch by class name, not isinstance. Under pytest-xdist the
        # test's ``ModelDownloadError`` can resolve to a different class
        # object than the one this module imported — ``except
        # ModelDownloadError`` would miss it and fall through to the
        # generic branch, dropping the structured error_code the UI
        # depends on. See CLAUDE.md anti-pattern #8.
        if type(exc).__name__ == "ModelDownloadError":
            code, retry_after = _classify_download_error(str(exc))
            entry.progress.status = "error"
            entry.progress.error = str(exc)
            entry.progress.error_code = code
            entry.progress.retry_after_seconds = retry_after
            entry.progress.finished_at = time.monotonic()
            logger.warning(
                "voice_model_download_failed",
                task_id=entry.progress.task_id,
                error=str(exc),
                error_code=code,
                retry_after_seconds=retry_after,
            )
        else:
            entry.progress.status = "error"
            entry.progress.error = str(exc)
            entry.progress.error_code = "unknown"
            entry.progress.finished_at = time.monotonic()
            logger.warning(
                "voice_model_download_failed",
                task_id=entry.progress.task_id,
                error=str(exc),
                error_code="unknown",
                exc_info=True,
            )


def _classify_download_error(message: str) -> tuple[str, int | None]:
    """Map a ``ModelDownloadError`` message to a categorical code.

    Returns ``(error_code, retry_after_seconds)`` — ``retry_after_seconds``
    is only populated for the cooldown case. The matching is intentionally
    message-based (string-contains) because the shared downloader formats
    its failure modes as three distinct phrases: "cooldown", "Checksum
    mismatch", and "Failed to download ... across N source(s)". A richer
    carrier would require threading the original exception up through
    ``ensure_*`` helpers — worth doing when we add Retry-After countdown
    per-mirror, but overkill for the three buckets the wizard needs today.
    """
    msg = message.lower()
    if "cooldown" in msg or "retry in" in msg or "next retry allowed" in msg:
        retry_after = _extract_retry_minutes(message) * 60 if "retry" in msg else None
        return ("cooldown", retry_after)
    if "checksum mismatch" in msg:
        return ("checksum_mismatch", None)
    if "across" in msg and "source" in msg:
        return ("all_mirrors_exhausted", None)
    return ("network", None)


def _extract_retry_minutes(message: str) -> int:
    """Parse ``... in 15 minutes`` → 15. Returns 0 on failure."""
    import re  # noqa: PLC0415

    match = re.search(r"(\d+)\s+minute", message)
    return int(match.group(1)) if match else 0


def start_download(
    tracker: dict[_SingletonKey, _DownloadEntry],
    *,
    model_dir: Path | None = None,
    missing: list[VoiceModelInfo] | None = None,
    task_factory: Callable[[Awaitable[None]], asyncio.Task[None]] | None = None,
) -> _DownloadEntry:
    """Kick off a background download and register it in ``tracker``.

    If an earlier task is still running we return it instead of spawning
    a second one. Completed tasks are kept in the tracker so a GET poll
    after the fact still resolves — the caller is expected to prune
    expired entries (see :func:`prune_finished`).
    """
    # Reuse any in-flight task so parallel button clicks don't race.
    for entry in tracker.values():
        if entry.progress.status == "running":
            return entry

    base = model_dir or get_default_model_dir()
    base.mkdir(parents=True, exist_ok=True)
    to_fetch = missing if missing is not None else collect_missing_models(base)

    task_id = _new_task_id()
    progress = ModelDownloadProgress(
        task_id=task_id,
        status="running" if to_fetch else "done",
        total_models=len(to_fetch),
        completed_models=0,
        current_model=None,
        error=None,
        created_at=time.monotonic(),
        finished_at=None if to_fetch else time.monotonic(),
    )
    entry = _DownloadEntry(progress=progress)
    tracker[task_id] = entry

    if not to_fetch:
        logger.info("voice_model_download_noop", task_id=task_id)
        return entry

    coro = _run_download(entry, base, to_fetch)
    entry.task = (
        task_factory(coro) if task_factory else spawn(coro, name=f"voice-model-download:{task_id}")
    )
    logger.info(
        "voice_model_download_start",
        task_id=task_id,
        models=[m.name for m in to_fetch],
    )
    return entry


def prune_finished(
    tracker: dict[_SingletonKey, _DownloadEntry],
    *,
    ttl_s: float = 300.0,
) -> None:
    """Drop finished entries older than ``ttl_s`` seconds."""
    now = time.monotonic()
    stale = [
        task_id
        for task_id, entry in tracker.items()
        if entry.progress.finished_at is not None and now - entry.progress.finished_at > ttl_s
    ]
    for task_id in stale:
        tracker.pop(task_id, None)


__all__ = [
    "ModelDownloadProgress",
    "VoiceModelDiskStatus",
    "VoiceModelsStatus",
    "check_voice_models_status",
    "collect_missing_models",
    "prune_finished",
    "start_download",
]
