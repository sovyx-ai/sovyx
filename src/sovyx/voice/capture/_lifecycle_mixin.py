"""Stream-lifecycle helpers — :class:`LifecycleMixin`.

Extracted from ``voice/_capture_task.py`` per master mission Phase 1
/ T1.4 step 9a. Owns the three small methods that bracket every
stream's lifetime — open-event emission, close-event emission, and
the consumer-shutdown signal used by the terminal failure branches
of :class:`RestartMixin`.

Methods on this mixin run on the asyncio loop thread (NOT the
PortAudio callback thread); they read/write the per-stream
counters owned by :class:`AudioCaptureTask`.

Contract — same hybrid-Option-C pattern as
:class:`RestartMixin`: host-class state attributes are declared
on the mixin for mypy strict, but initialised in the host's
``__init__``. Method-via-MRO references resolve through the
composed instance (none required for this mixin — all three
methods read state directly).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    import asyncio

logger = get_logger(__name__)


__all__ = ["LifecycleMixin"]


class LifecycleMixin:
    """Stream open/close/shutdown helpers shared with AudioCaptureTask."""

    # Host-class state declarations for mypy strict. The host class
    # (``AudioCaptureTask``) sets these in ``__init__``; the mixin
    # only reads / writes via ``self``.
    _running: bool
    _consumer: asyncio.Task[None] | None
    _stream: Any
    _stream_id: str
    _stream_underruns: int
    _stream_overflows: int
    _stream_callback_frames: int
    _underrun_window_started_at: float | None
    _underrun_window_callbacks_at_start: int
    _underrun_window_underruns_at_start: int
    _last_underrun_warning_monotonic: float | None
    _blocksize: int
    _resolved_device_name: str | None

    def _signal_consumer_shutdown(self) -> None:
        """Mark the task dead and wake the consumer so it can exit.

        Used by the terminal ``OPEN_FAILED_NO_STREAM`` branches of
        :meth:`request_exclusive_restart` and
        :meth:`request_shared_restart` — after both open paths have
        failed, the stream is ``None`` and the consume loop would
        otherwise stay parked on ``queue.get()`` forever (nothing can
        enqueue, and the ``sd.PortAudioError`` reconnect branch cannot
        fire without a live stream). Flipping ``_running`` + cancelling
        the consumer task unblocks it and lets upstream supervisors
        detect the dead state by observing the task's completion and
        the returned verdict.

        Safe to call from outside the consumer task (e.g. the
        coordinator's bypass ``apply``/``revert`` path). Idempotent —
        a second invocation after the consumer is already done is a
        no-op.
        """
        self._running = False
        consumer = self._consumer
        if consumer is not None and not consumer.done():
            consumer.cancel()

    def _emit_stream_opened(
        self,
        info: Any,  # noqa: ANN401 — StreamInfo dataclass, typed lazily
        *,
        apo_bypass_attempted: bool,
    ) -> None:
        """Generate a fresh stream_id and emit ``audio.stream.opened``.

        Resets the per-stream lifecycle counters
        (``_stream_underruns`` / ``_stream_overflows`` /
        ``_stream_callback_frames``) so the matching
        ``audio.stream.closed`` event reflects *this* stream only — not
        cumulative activity from prior reopens.

        ``apo_bypass_attempted`` is ``True`` only when the open was
        triggered by :meth:`request_exclusive_restart` (the explicit
        APO-bypass path); reverts and reconnects pass ``False``.
        """
        self._stream_id = uuid4().hex[:16]
        self._stream_underruns = 0
        self._stream_overflows = 0
        self._stream_callback_frames = 0
        # Band-aid #9 — reset sustained-underrun state per stream so
        # the warn fires on the new stream's xrun rate, not a leftover
        # snapshot from a prior reopened stream's state.
        self._underrun_window_started_at = None
        self._underrun_window_callbacks_at_start = 0
        self._underrun_window_underruns_at_start = 0
        self._last_underrun_warning_monotonic = None
        sample_rate = int(getattr(info, "sample_rate", 0) or 0)
        mode = "exclusive" if getattr(info, "exclusive_used", False) else "shared"
        buffer_size_ms = int(self._blocksize * 1000 / sample_rate) if sample_rate else 0
        logger.info(
            "audio.stream.opened",
            **{
                "voice.stream_id": self._stream_id,
                "voice.device_id": self._resolved_device_name or "default",
                "voice.host_api": getattr(info, "host_api", None),
                "voice.mode": mode,
                "voice.sample_rate": sample_rate,
                "voice.channels": int(getattr(info, "channels", 0) or 0),
                "voice.buffer_size_ms": buffer_size_ms,
                "voice.apo_bypass_attempted": apo_bypass_attempted,
                "voice.fallback_depth": int(getattr(info, "fallback_depth", 0) or 0),
                "voice.auto_convert_used": bool(getattr(info, "auto_convert_used", False)),
            },
        )

    def _close_stream(self, reason: str = "unknown") -> None:
        """Stop and close the stream — tolerant of already-closed streams.

        Emits ``audio.stream.closed`` with the cumulative xrun counts
        and frame total observed by the PortAudio callback for this
        stream BEFORE tearing it down. ``reason`` is a stable tag
        (``"shutdown"`` / ``"exclusive_restart"`` / ``"shared_restart"``
        / ``"device_error"`` / ``"unknown"``) the dashboard uses to
        distinguish operator-initiated tear-downs from device errors.
        """
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        if self._stream_id:
            logger.info(
                "audio.stream.closed",
                **{
                    "voice.stream_id": self._stream_id,
                    "voice.device_id": self._resolved_device_name or "default",
                    "voice.reason": reason,
                    "voice.underruns": self._stream_underruns,
                    "voice.overflows": self._stream_overflows,
                    "voice.frames_processed": self._stream_callback_frames,
                },
            )
            self._stream_id = ""
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001 — stream may already be dead
            logger.debug("audio_capture_close_failed", exc_info=True)
