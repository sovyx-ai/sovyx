"""Voice Capture Health — anonymous opt-in telemetry rollup (ADR §4.9, L9).

Sovyx is local-first: nothing leaves the user's machine without an
explicit ``telemetry.enabled = true`` in the engine config. When that
bit *is* set, this module accumulates a tiny anonymized rollup of
cascade outcomes — *which audio configurations actually work in the
field, broken down by platform + host API* — and persists it to a
single JSON file under ``data_dir``.

What this records
=================

For every cascade attempt the orchestrator runs, we keep a counter
keyed by the immutable shape of the attempt:

* ``platform`` (``"win32"`` / ``"linux"`` / ``"darwin"``)
* ``host_api`` (``"WASAPI"`` / ``"WDM-KS"`` / ``"ALSA"`` / ``"CoreAudio"`` / ...)
* whether it succeeded (``HEALTHY``)

The bucket value is a ``(success, failure)`` integer pair.

What this does **not** record
=============================

* No device names — ``"Razer BlackShark V2 Pro"`` is identifying.
* No Bluetooth addresses, USB VID/PID, friendly names, or paths.
* No audio fingerprints (those are designed to identify a specific user
  setup so we can detect drift).
* No timestamps beyond a single ``last_updated`` ISO-8601 string.
* No user IDs, mind IDs, machine IDs, or hostnames.
* No raw probe RMS / VAD samples.

The rollup is meant to answer one question: *"on this platform + host
API, what fraction of cascade attempts succeed?"* That's the only
upstream-useful signal here, and it requires no PII.

Lifecycle
=========

* The recorder is a process-wide singleton (``get_telemetry()``) backed
  by a :class:`threading.Lock`. It is safe to call from sync helpers
  invoked off the event loop (the cascade does this).
* Writes are debounced — every ``_FLUSH_INTERVAL_S`` of activity, plus
  unconditionally on graceful shutdown via :func:`flush`.
* The recorder is a no-op when ``telemetry.enabled`` is ``False`` —
  zero allocations, zero file I/O. Calling :func:`record_cascade_outcome`
  on a disabled recorder is fast enough to live on the cascade hot path.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from sovyx.engine.config import EngineConfig

logger = get_logger(__name__)


_TELEMETRY_FILENAME = "voice_health_telemetry.json"
"""Sibling JSON under ``data_dir`` — never under ``data_dir/logs``
(operators ``rm -rf`` the log directory routinely)."""


_TELEMETRY_SCHEMA_VERSION = 1
"""Bump on shape changes. Readers must tolerate older versions and
gracefully discard newer ones (forward-compatibility is intentional)."""


_FLUSH_INTERVAL_S = 30.0
"""Minimum gap between disk flushes. The rollup is so small that
flushing more often is harmless — this just keeps the cascade hot path
free of fsync stalls."""


@dataclass(frozen=True, slots=True)
class CascadeOutcomeKey:
    """Immutable bucket key — the only dimensions we keep.

    Attributes:
        platform: ``"win32"`` | ``"linux"`` | ``"darwin"`` |
            ``"unknown"``. Mirrors :func:`sys.platform`.
        host_api: PortAudio host API string, normalised to
            ``"unknown"`` when missing.
    """

    platform: str
    host_api: str


@dataclass(slots=True)
class CascadeOutcomeBucket:
    """Mutable counter pair for one ``CascadeOutcomeKey``."""

    success: int = 0
    failure: int = 0

    def total(self) -> int:
        return self.success + self.failure

    def success_rate(self) -> float:
        total = self.total()
        return self.success / total if total else 0.0


@dataclass(slots=True)
class _TelemetryState:
    """In-memory aggregate. Persisted by :meth:`VoiceHealthTelemetry.flush`."""

    buckets: dict[CascadeOutcomeKey, CascadeOutcomeBucket] = field(default_factory=dict)
    last_flushed_monotonic: float = 0.0
    last_updated_iso: str = ""


class VoiceHealthTelemetry:
    """Process-wide anonymous rollup of cascade outcomes.

    Construction is cheap: when ``enabled=False`` every public method
    short-circuits. The orchestrator constructs one instance at boot and
    passes it to :func:`set_telemetry` so that all subsystems see the
    same accumulator.
    """

    def __init__(self, *, enabled: bool, output_path: Path) -> None:
        """Create a recorder bound to ``output_path``.

        Args:
            enabled: When ``False`` the recorder is a no-op. Set from
                :attr:`EngineConfig.telemetry.enabled`.
            output_path: Absolute path to the JSON rollup file. Parent
                directory is created on first flush.
        """
        self._enabled = enabled
        self._output_path = output_path
        self._lock = threading.Lock()
        self._state = _TelemetryState()
        self._monotonic: Callable[[], float] = time.monotonic

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def output_path(self) -> Path:
        return self._output_path

    def record_cascade_outcome(
        self,
        *,
        platform: str,
        host_api: str | None,
        success: bool,
    ) -> None:
        """Bump the bucket for ``(platform, host_api)`` by one.

        Safe to call from any thread / sync context. Triggers an
        opportunistic disk flush every :data:`_FLUSH_INTERVAL_S` seconds
        of activity. The flush is best-effort and never raises into the
        caller — failures are logged at debug level and the in-memory
        rollup remains intact.
        """
        if not self._enabled:
            return
        key = CascadeOutcomeKey(
            platform=platform or "unknown",
            host_api=host_api or "unknown",
        )
        now_monotonic = self._monotonic()
        flush_due = False
        with self._lock:
            bucket = self._state.buckets.get(key)
            if bucket is None:
                bucket = CascadeOutcomeBucket()
                self._state.buckets[key] = bucket
            if success:
                bucket.success += 1
            else:
                bucket.failure += 1
            self._state.last_updated_iso = _utcnow_iso()
            if now_monotonic - self._state.last_flushed_monotonic >= _FLUSH_INTERVAL_S:
                self._state.last_flushed_monotonic = now_monotonic
                flush_due = True
        if flush_due:
            self.flush()

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serialisable rollup of every bucket.

        The output schema mirrors what :func:`flush` writes to disk so
        callers (e.g. ``sovyx doctor voice --export-telemetry``) can
        share the result without re-reading the file.
        """
        with self._lock:
            buckets = [
                {
                    "platform": key.platform,
                    "host_api": key.host_api,
                    "success": bucket.success,
                    "failure": bucket.failure,
                    "total": bucket.total(),
                    "success_rate": round(bucket.success_rate(), 4),
                }
                for key, bucket in self._state.buckets.items()
            ]
            last_updated = self._state.last_updated_iso
        buckets.sort(key=lambda row: (row["platform"], row["host_api"]))
        return {
            "schema_version": _TELEMETRY_SCHEMA_VERSION,
            "last_updated": last_updated,
            "buckets": buckets,
        }

    def flush(self) -> bool:
        """Persist the rollup to :attr:`output_path` atomically.

        Returns ``True`` on success, ``False`` if disabled or on any I/O
        failure. Uses ``tempfile`` + ``os.replace`` so a crash mid-write
        leaves the previous rollup intact.
        """
        if not self._enabled:
            return False
        payload = self.snapshot()
        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                delete=False,
                dir=self._output_path.parent,
                prefix=".voice_health_telemetry-",
                suffix=".json",
                encoding="utf-8",
            ) as tmp:
                json.dump(payload, tmp, indent=2, sort_keys=True)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = Path(tmp.name)
        except OSError as exc:
            logger.debug("voice_health_telemetry_tempfile_failed", detail=str(exc))
            return False
        try:
            os.replace(tmp_path, self._output_path)
        except OSError as exc:
            logger.debug("voice_health_telemetry_replace_failed", detail=str(exc))
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return False
        return True

    def reset(self) -> None:
        """Clear the in-memory rollup. Mainly used by tests + ``--reset``."""
        with self._lock:
            self._state = _TelemetryState()


# ── Module-level access ──────────────────────────────────────────────────


_recorder: VoiceHealthTelemetry | None = None
_recorder_lock = threading.Lock()


def get_telemetry() -> VoiceHealthTelemetry | None:
    """Return the process-wide recorder, or ``None`` if uninitialised."""
    with _recorder_lock:
        return _recorder


def set_telemetry(recorder: VoiceHealthTelemetry | None) -> None:
    """Install (or clear) the process-wide recorder. Tests use this."""
    global _recorder  # noqa: PLW0603 — module-level singleton, by design
    with _recorder_lock:
        _recorder = recorder


def build_telemetry_from_config(config: EngineConfig) -> VoiceHealthTelemetry:
    """Construct a recorder bound to ``EngineConfig`` settings.

    Caller is responsible for installing the result via
    :func:`set_telemetry`. The output path defaults to
    ``data_dir/voice_health_telemetry.json``.
    """
    output_path = config.database.data_dir / _TELEMETRY_FILENAME
    return VoiceHealthTelemetry(
        enabled=config.telemetry.enabled,
        output_path=output_path,
    )


def record_cascade_outcome(*, platform: str, host_api: str | None, success: bool) -> None:
    """Convenience wrapper — forwards to the installed recorder if any.

    Safe to call when telemetry is uninitialised; the call is a no-op.
    The metrics facade calls this from :func:`record_cascade_attempt`
    so cascade authors don't need a second instrumentation call.
    """
    recorder = get_telemetry()
    if recorder is None:
        return
    recorder.record_cascade_outcome(
        platform=platform,
        host_api=host_api,
        success=success,
    )


# ── Internal helpers (overridable from tests) ────────────────────────────


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp, second precision (no microseconds)."""
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


__all__ = [
    "CascadeOutcomeBucket",
    "CascadeOutcomeKey",
    "VoiceHealthTelemetry",
    "build_telemetry_from_config",
    "get_telemetry",
    "record_cascade_outcome",
    "set_telemetry",
]
