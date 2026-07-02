"""Audio thread + consume loop — :class:`LoopMixin`.

Extracted from ``voice/_capture_task.py`` per master mission Phase 1
/ T1.4 step 9b. Owns the five tightly-coupled methods that move
samples from the PortAudio callback thread into the
:class:`VoicePipeline`:

* :meth:`_audio_callback` — runs in the PortAudio thread; counts
  xruns, copies the block, and dispatches to the asyncio loop via
  ``loop.call_soon_threadsafe``. T1.30-wrapped in
  ``try/except BaseException`` so no raise can ever propagate out
  of the audio thread (would otherwise drop into PortAudio's
  ``CallbackAbort`` and stall the entire capture chain). Each
  dispatch is stamped with the stream epoch observed at callback
  time (PIPELINE-9, 2026-07-02).
* :meth:`_enqueue` — runs on the asyncio loop; non-blocking
  enqueue with oldest-frame eviction on overflow. Drops blocks
  whose stream-epoch stamp predates the current epoch — an
  ``_enqueue`` already scheduled via ``call_soon_threadsafe`` when
  the old stream closed can still execute DURING the restart
  path's subsequent ``await open_input_stream(...)``, i.e. AFTER
  the queue drain, and would otherwise push an old-stream raw
  block through the NEW FrameNormalizer built for the new
  stream's rate/channels (PIPELINE-9; see
  ``RestartMixin._drain_stale_frames``).
* :meth:`_check_sustained_underrun_rate` — band-aid #9
  replacement; rolling-window xrun-fraction WARN with rate
  limiting.
* :meth:`_consume_loop` — main consumer task: pulls frames,
  normalises, feeds the pipeline, handles ``sd.PortAudioError``
  with exponential-backoff reconnect (band-aid #10 replacement).
* :meth:`_maybe_emit_heartbeat` — periodic RMS / frame-count log
  for operator confirmation that the pipeline is alive.

Contract — same hybrid-Option-C pattern as :class:`RestartMixin`
/ :class:`LifecycleMixin`. Host-class state attributes are
declared on the mixin for mypy strict but initialised in the
host's ``__init__``. Method-via-MRO references resolve through
the composed instance:

* ``self._ring_write`` lives on :class:`RingMixin`.
* ``self._close_stream`` /
  ``self._reopen_stream_after_device_error`` live on
  :class:`LifecycleMixin` / :class:`RestartMixin` respectively.

Test-patch path migration (CLAUDE.md anti-pattern #20): the
methods reference ``time.monotonic()`` via the module-level
``time`` import in this file, so test patches that previously
targeted ``sovyx.voice._capture_task.time.monotonic`` MUST
migrate to ``sovyx.voice.capture._loop_mixin.time.monotonic``.
13 such patch sites were migrated in the same commit.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from sovyx.engine._backoff import BackoffPolicy, BackoffSchedule, JitterStrategy
from sovyx.observability.logging import get_logger
from sovyx.voice._stream_opener import _import_sounddevice
from sovyx.voice.capture._constants import (
    _CAPTURE_UNDERRUN_MIN_CALLBACKS,
    _CAPTURE_UNDERRUN_WARN_FRACTION,
    _CAPTURE_UNDERRUN_WARN_INTERVAL_S,
    _CAPTURE_UNDERRUN_WINDOW_S,
    _HEARTBEAT_INTERVAL_S,
    _QUEUE_EVICTION_WARN_INTERVAL_S,
    _RECONNECT_DELAY_S,
    _VALIDATION_MIN_RMS_DB,
)
from sovyx.voice.capture._helpers import _rms_db_int16

if TYPE_CHECKING:
    from types import ModuleType

    import numpy as np
    import numpy.typing as npt

    from sovyx.voice._chaos import ChaosInjector
    from sovyx.voice._frame_normalizer import FrameNormalizer
    from sovyx.voice.pipeline import VoicePipeline


logger = get_logger(__name__)


__all__ = ["LoopMixin"]


class LoopMixin:
    """Audio-callback + consume-loop methods sharing AudioCaptureTask state.

    The mixin owns the producer-consumer machinery between the
    PortAudio thread and the asyncio loop. Methods on this mixin
    do NOT run on the PortAudio thread except for
    :meth:`_audio_callback` itself; everything else (including
    :meth:`_enqueue` which is dispatched via
    ``loop.call_soon_threadsafe``) runs on the asyncio loop.
    """

    # Host-class state declarations for mypy strict. The host class
    # (``AudioCaptureTask``) sets these in ``__init__``; the mixin
    # only reads / writes via ``self``.
    _running: bool
    _pipeline: VoicePipeline
    _queue: asyncio.Queue[npt.NDArray[np.int16]]
    _loop: asyncio.AbstractEventLoop | None
    _normalizer: FrameNormalizer | None
    _sd_module: ModuleType | None
    _input_device: int | str | None
    _host_api_name: str | None
    _resolved_device_name: str | None
    _stream_id: str
    _stream_underruns: int
    _stream_overflows: int
    _stream_callback_frames: int
    _underrun_window_started_at: float | None
    _underrun_window_callbacks_at_start: int
    _underrun_window_underruns_at_start: int
    _last_underrun_warning_monotonic: float | None
    _last_rms_db: float
    _last_heartbeat_monotonic: float
    _frames_delivered: int
    _frames_since_heartbeat: int
    _silent_frames_since_heartbeat: int
    _last_window_monotonic: float | None
    _queue_evictions_total: int
    _last_eviction_warning_monotonic: float | None
    _reconnect_backoff: BackoffSchedule | None
    _chaos: ChaosInjector

    # PIPELINE-9 (2026-07-02) — monotonic stream-generation counter.
    # Bumped by ``RestartMixin._drain_stale_frames`` after every
    # ``_close_stream`` on a restart path; ``_audio_callback`` stamps
    # the value it observes onto each dispatched block and
    # ``_enqueue`` drops stamps that predate the current epoch.
    # Unlike the annotations above this is a REAL class-level default
    # (not host-``__init__``-initialised): the epoch is owned entirely
    # by the two capture mixins, ``0`` is the correct pre-first-
    # restart value, and instance writes (`+=`) shadow the class
    # attribute per normal Python semantics.
    _stream_epoch: int = 0

    # Method-via-MRO declarations — these live on AudioCaptureTask
    # or other mixins (RingMixin / LifecycleMixin / RestartMixin)
    # and resolve through the composed instance.
    #
    # ``_ring_write`` and ``_close_stream`` are declared as ``def``
    # stubs because their owning mixins (RingMixin / LifecycleMixin)
    # appear BEFORE LoopMixin in the AudioCaptureTask MRO, so the
    # stubs are always shadowed at runtime.
    #
    # ``_reopen_stream_after_device_error`` lives on RestartMixin
    # which appears AFTER LoopMixin — a ``def`` stub here would WIN
    # MRO resolution and shadow the real method (the stub returns
    # ``None``, which makes the consume-loop reconnect path silently
    # succeed without ever calling the unified opener). Declared
    # inside ``if TYPE_CHECKING:`` so the body is type-check-only
    # (the ``if`` guard is evaluated at import time and ``False`` at
    # runtime, so no class attribute is created and MRO falls
    # through to RestartMixin's real implementation).
    def _ring_write(self, frame: npt.NDArray[np.int16]) -> None: ...
    def _close_stream(self, reason: str = "unknown") -> None: ...

    if TYPE_CHECKING:

        async def _reopen_stream_after_device_error(self) -> None: ...

    def _audio_callback(
        self,
        indata: npt.NDArray[np.int16],
        frames: int,  # noqa: ARG002
        time_info: object,  # noqa: ARG002
        status: object,
    ) -> None:
        """PortAudio callback — runs in the audio thread.

        Hands the raw block (any shape, any sample rate that the opener
        negotiated) to the asyncio loop. Downmix + resample + rewindow
        happen on the consumer side via :class:`FrameNormalizer`, which
        is not thread-safe and therefore cannot be touched here. Drops
        frames when the queue is saturated rather than blocking the
        audio thread, which would cause device underruns.

        T1.30 — the ENTIRE body is wrapped in ``try/except BaseException``
        so any raise (``MemoryError`` on ``indata.copy()``, ``TypeError``
        on a malformed status object, ``AttributeError`` on a transient
        attribute glitch, etc.) is caught instead of propagating into
        sounddevice's ``CallbackAbort`` path. Pre-T1.30 a stray exception
        here either killed the audio thread silently or left
        sounddevice in CallbackAbort state with the entire capture chain
        stalled and no structured signal upstream. Post-T1.30 the
        exception is logged and an empty marker frame is queued so the
        consumer's ``await self._queue.get()`` unblocks. The empty
        marker is a no-op for :class:`FrameNormalizer.push` (see line
        579 of ``_frame_normalizer.py``: PortAudio occasionally
        delivers zero-sized callbacks at stream open / close
        boundaries, so the contract is already established).
        ``BaseException`` rather than ``Exception`` because
        ``KeyboardInterrupt`` / ``SystemExit`` are equally fatal to the
        audio thread; both are delivered to the main thread by Python's
        signal handler so catching them here is safe.
        """
        try:
            if status:
                # CallbackFlags: input overflow/underflow. Track for the
                # per-stream ``audio.stream.closed`` event so operators
                # can correlate xruns with kernel-mixer / USB-bus
                # pressure.
                if getattr(status, "input_overflow", False):
                    self._stream_overflows += 1
                if getattr(status, "input_underflow", False):
                    self._stream_underruns += 1
                logger.debug("audio_callback_status", status=str(status))
            self._stream_callback_frames += 1
            block = indata.copy()
            loop = self._loop
            if loop is None:
                return
            # PIPELINE-9 — stamp the stream epoch observed NOW (audio
            # thread, GIL-atomic int read). Restart paths bump the
            # epoch after _close_stream returns, so a callback from
            # the dying stream carries the OLD stamp and _enqueue can
            # drop it even when the dispatch executes after the
            # restart's queue drain.
            stream_epoch = self._stream_epoch
            # Loop may be closed mid-shutdown — swallow that and move on.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._enqueue, block, stream_epoch)
        except BaseException as exc:  # noqa: BLE001 — must NEVER raise out of PortAudio thread (T1.30)
            with contextlib.suppress(Exception):
                logger.error(
                    "voice.audio_callback.uncaught_raise",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    stream_id=self._stream_id,
                )
            # Queue an empty marker frame so the consumer unblocks.
            # FrameNormalizer.push handles size==0 as a no-op
            # (`_frame_normalizer.py:579`), so this doesn't crash
            # anything downstream — it just nudges the queue out of a
            # potentially-stalled await.
            with contextlib.suppress(Exception):
                loop = self._loop
                if loop is not None:
                    import numpy as np

                    empty_marker = np.zeros(0, dtype=np.int16)
                    loop.call_soon_threadsafe(self._enqueue, empty_marker)

    def _enqueue(
        self,
        frame: npt.NDArray[np.int16],
        stream_epoch: int | None = None,
    ) -> None:
        """Enqueue a frame; drop the oldest on overflow.

        D4 — evictions are no longer silent. Every dropped frame bumps
        the lifetime counter ``_queue_evictions_total`` (plain ``int +=``
        — this method runs on the asyncio loop via
        ``call_soon_threadsafe``, never the audio thread, so the cheap
        bookkeeping is anti-pattern #14 safe) and emits a
        ``voice.capture.queue_eviction`` WARN throttled to one per
        ``_QUEUE_EVICTION_WARN_INTERVAL_S`` (same monotonic-gated
        pattern as ``voice.vad.inference_timeout``). The common
        (non-full) path is untouched: one ``full()`` check, no
        allocation, no logging.

        PIPELINE-9 (2026-07-02): ``stream_epoch`` is the stamp the
        audio callback observed at capture time. A stamp older than
        the current :attr:`_stream_epoch` means the block came from a
        stream that a restart path has since closed — the dispatch was
        already scheduled when the restart drained the queue, so
        accepting it would feed old-stream raw audio through the NEW
        FrameNormalizer (wrong rate/channels → tempo-shifted samples
        into VAD/ring once per restart). Dropped with a DEBUG log
        (bounded to the handful of in-flight callbacks per restart).
        ``None`` (direct callers, the T1.30 empty-marker path) skips
        the check.
        """
        if stream_epoch is not None and stream_epoch != self._stream_epoch:
            logger.debug(
                "voice.capture.stale_epoch_frame_dropped",
                **{
                    "voice.block_epoch": stream_epoch,
                    "voice.current_epoch": self._stream_epoch,
                    "voice.block_samples": int(frame.size),
                },
            )
            return
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue_evictions_total += 1
                now = time.monotonic()
                if (
                    self._last_eviction_warning_monotonic is None
                    or now - self._last_eviction_warning_monotonic
                    >= _QUEUE_EVICTION_WARN_INTERVAL_S
                ):
                    self._last_eviction_warning_monotonic = now
                    logger.warning(
                        "voice.capture.queue_eviction",
                        **{
                            "voice.queue_evictions_lifetime": self._queue_evictions_total,
                            "voice.queue_maxsize": self._queue.maxsize,
                            "voice.action_required": (
                                "Capture queue overflowed — the consumer is "
                                "falling behind the audio callback and the "
                                "OLDEST frames are being dropped, so speech "
                                "may be silently lost. Likely causes: host "
                                "CPU pressure starving the event loop, or a "
                                "slow pipeline.feed_frame stage (VAD / "
                                "normalizer). Check CPU load; if sustained, "
                                "reduce co-resident load or raise "
                                "SOVYX_TUNING__VOICE__CAPTURE_QUEUE_MAXSIZE."
                            ),
                        },
                    )
        self._queue.put_nowait(frame)

    def _check_sustained_underrun_rate(self) -> None:
        """Fire ``voice.audio.capture_sustained_underrun`` WARN when
        the rolling-window underrun fraction exceeds the threshold
        (band-aid #9 replacement).

        Runs in ``_consume_loop`` between awaits — never in the audio
        callback (anti-pattern #14). Pure increment counters in the
        callback; this method computes the per-window rate from
        snapshots taken at window roll, then compares to the warn
        threshold and rate-limits the WARN per stream.

        Side-effect-free when:
        * No stream is open (``_stream_id`` is empty).
        * The window has not elapsed yet.
        * The window has < ``_CAPTURE_UNDERRUN_MIN_CALLBACKS`` samples.
        * The fraction is below ``_CAPTURE_UNDERRUN_WARN_FRACTION``.
        * A prior WARN fired within ``_CAPTURE_UNDERRUN_WARN_INTERVAL_S``
          seconds (rate-limited).

        On WARN, the structured event includes ``action_required`` so
        the operator gets concrete remediation steps (USB hub
        bandwidth, host CPU pressure, WASAPI mode swap, etc.) directly
        in the log feed without needing a separate runbook lookup.
        """
        if not self._stream_id:
            return
        # Per CLAUDE.md anti-pattern #24, use ``>=`` against monotonic
        # deadlines so coarse-clock systems don't silently never fire.
        now = time.monotonic()
        if self._underrun_window_started_at is None:
            self._underrun_window_started_at = now
            self._underrun_window_callbacks_at_start = self._stream_callback_frames
            self._underrun_window_underruns_at_start = self._stream_underruns
            return
        elapsed = now - self._underrun_window_started_at
        if elapsed < _CAPTURE_UNDERRUN_WINDOW_S:
            return
        callbacks_in_window = (
            self._stream_callback_frames - self._underrun_window_callbacks_at_start
        )
        underruns_in_window = self._stream_underruns - self._underrun_window_underruns_at_start
        # Roll the window before any early-return path so the next
        # iteration starts from a fresh snapshot regardless of whether
        # this window fired the WARN.
        self._underrun_window_started_at = now
        self._underrun_window_callbacks_at_start = self._stream_callback_frames
        self._underrun_window_underruns_at_start = self._stream_underruns
        if callbacks_in_window < _CAPTURE_UNDERRUN_MIN_CALLBACKS:
            return
        fraction = underruns_in_window / callbacks_in_window
        if fraction < _CAPTURE_UNDERRUN_WARN_FRACTION:
            return
        if (
            self._last_underrun_warning_monotonic is not None
            and now - self._last_underrun_warning_monotonic < _CAPTURE_UNDERRUN_WARN_INTERVAL_S
        ):
            return
        self._last_underrun_warning_monotonic = now
        logger.warning(
            "voice.audio.capture_sustained_underrun",
            **{
                "voice.stream_id": self._stream_id,
                "voice.device_id": self._resolved_device_name or "default",
                "voice.window_seconds": round(elapsed, 2),
                "voice.underruns_in_window": underruns_in_window,
                "voice.callbacks_in_window": callbacks_in_window,
                "voice.underrun_fraction": round(fraction, 4),
                "voice.threshold_fraction": _CAPTURE_UNDERRUN_WARN_FRACTION,
                "voice.action_required": (
                    "Capture stream is xrunning under sustained pressure. "
                    "Likely causes: USB-bus bandwidth contention (try a "
                    "different port, avoid hubs), host CPU saturation "
                    "starving the audio thread, or driver-side glitch. "
                    "On Windows consider WASAPI exclusive via the dashboard. "
                    "On Linux check `pactl list short sources` for "
                    "competing clients."
                ),
            },
        )

    async def _consume_loop(self) -> None:
        """Pull frames off the queue and feed them to the pipeline.

        On ``sd.PortAudioError`` (device unplugged, driver reset) we
        close the stream, sleep briefly, and reopen through the unified
        opener — so a user yanking a USB headset does not wedge the
        pipeline.

        Emits an ``audio_capture_heartbeat`` log every
        ``capture_heartbeat_interval_seconds`` so operators can confirm
        (a) frames are arriving, (b) the mic is not stuck at silence.
        """
        sd = self._sd_module if self._sd_module is not None else _import_sounddevice()

        while self._running:
            try:
                block = await self._queue.get()
                windows = self._normalizer.push(block) if self._normalizer is not None else [block]
                for window in windows:
                    rms_db = _rms_db_int16(window)
                    self._last_rms_db = rms_db
                    self._frames_delivered += 1
                    # D6 — frame-recency stamp so status_snapshot can
                    # detect that frames STOPPED arriving (stale
                    # LIVE_SIGNAL guard in classify_signal_state).
                    self._last_window_monotonic = time.monotonic()
                    self._frames_since_heartbeat += 1
                    if rms_db < _VALIDATION_MIN_RMS_DB:
                        self._silent_frames_since_heartbeat += 1
                    # Record the post-normalization frame into the ring
                    # buffer BEFORE feeding the pipeline so the bypass
                    # coordinator's integrity probe sees the exact
                    # samples that VAD sees — not an upstream raw block
                    # that the normalizer would later resample / downmix.
                    self._ring_write(window)
                    # TS3 chaos: opt-in frame-drop at the
                    # CAPTURE_UNDERRUN site. When chaos fires, skip
                    # pipeline.feed_frame — observationally identical
                    # to a PortAudio kernel-side underrun from the
                    # consumer's perspective. Validates the O2 deaf
                    # coordinator + watchdog promotion paths fire
                    # correctly under realistic underrun rates.
                    if self._chaos.should_inject():
                        continue
                    await self._pipeline.feed_frame(window)
                self._maybe_emit_heartbeat()
                # Band-aid #9 — sustained-underrun rolling-window check.
                # Runs in the consumer (not the audio callback) where
                # logging is safe; the callback only does counter
                # increments per anti-pattern #14.
                self._check_sustained_underrun_rate()
            except asyncio.CancelledError:
                raise
            except sd.PortAudioError as exc:
                logger.warning(
                    "audio_capture_device_error",
                    error=str(exc),
                    device=self._input_device,
                    host_api=self._host_api_name,
                )
                await asyncio.to_thread(self._close_stream, "device_error")
                # Band-aid #10 replacement: exponential backoff with
                # FULL jitter. Constant ``_RECONNECT_DELAY_S`` was the
                # legacy band-aid that hammered a degraded driver
                # every 5 s regardless of how long the outage was.
                # The schedule is lazy-initialised so the (overwhelmingly
                # common) zero-error case has zero backoff overhead.
                # Reset on successful reconnect; advance on each failure.
                if self._reconnect_backoff is None:
                    # Clamp base delay to the BackoffPolicy minimum
                    # (1 ms) so a test-time _RECONNECT_DELAY_S=0
                    # override + future config that lets operators
                    # set 0 doesn't violate the loud-fail bound.
                    # The clamp preserves the operator intent of
                    # "fast retries" while keeping the policy's
                    # busy-loop guard rail.
                    base = max(_RECONNECT_DELAY_S, 0.001)
                    self._reconnect_backoff = BackoffSchedule(
                        BackoffPolicy(
                            base_delay_s=base,
                            max_delay_s=max(base * 12.0, 60.0),
                            multiplier=2.0,
                            max_attempts=1_000_000,  # effectively unbounded
                            jitter=JitterStrategy.FULL,
                        )
                    )
                try:
                    delay_s = self._reconnect_backoff.next()
                except StopIteration:
                    # Should not occur with max_attempts=1M, but the
                    # schedule contract requires handling.
                    delay_s = _RECONNECT_DELAY_S
                logger.info(
                    "audio_capture_reconnect_backoff",
                    delay_s=round(delay_s, 3),
                    attempt=self._reconnect_backoff.attempt_count,
                    base_s=_RECONNECT_DELAY_S,
                )
                await asyncio.sleep(delay_s)
                if not self._running:
                    return
                try:
                    await self._reopen_stream_after_device_error()
                    logger.info("audio_capture_device_reconnected")
                    # Successful reconnect — reset the backoff so the
                    # next outage starts from base_delay_s, not
                    # wherever the previous outage's escalation left
                    # us. Without reset, a transient outage 30 min
                    # ago would still penalise today's reconnect.
                    self._reconnect_backoff.reset()
                except Exception as reopen_exc:  # noqa: BLE001
                    logger.error(
                        "audio_capture_reconnect_failed",
                        error=str(reopen_exc),
                        next_delay_attempt=self._reconnect_backoff.attempt_count,
                    )
            except Exception:  # noqa: BLE001
                # A single bad frame must not kill the loop. Log with
                # traceback so persistent upstream errors surface.
                logger.exception("audio_capture_feed_failed")

    def _maybe_emit_heartbeat(self) -> None:
        """Log a periodic RMS/frame-count heartbeat.

        Only fires when ``_HEARTBEAT_INTERVAL_S`` has elapsed since the
        last one, so log volume stays constant regardless of sample
        rate. Resets per-interval counters after each emit.
        """
        now = time.monotonic()
        if now - self._last_heartbeat_monotonic < _HEARTBEAT_INTERVAL_S:
            return
        normalizer = self._normalizer
        logger.info(
            "audio_capture_heartbeat",
            device=self._input_device,
            host_api=self._host_api_name,
            frames_delivered=self._frames_delivered,
            frames_since_last=self._frames_since_heartbeat,
            silent_frames=self._silent_frames_since_heartbeat,
            last_rms_db=round(self._last_rms_db, 1),
            source_rate=normalizer.source_rate if normalizer is not None else None,
            source_channels=normalizer.source_channels if normalizer is not None else None,
            normalizer_active=(not normalizer.is_passthrough if normalizer is not None else False),
        )
        self._last_heartbeat_monotonic = now
        self._frames_since_heartbeat = 0
        self._silent_frames_since_heartbeat = 0
