"""Module-level constants for the capture subsystem.

Extracted from ``voice/_capture_task.py`` (lines 164-255 pre-split)
per master mission Phase 1 / T1.4 step 5. Pure data constants —
some bound to ``VoiceTuningConfig`` fields at module import time so
operator overrides via ``SOVYX_TUNING__VOICE__*`` env vars apply
through the legacy import path uniformly.

Public surface organised by concern:

* **Frame format** — ``_SAMPLE_RATE``, ``_FRAME_SAMPLES``: physical
  contract with the pipeline (16 kHz / 512-sample int16 blocks);
  changing them invalidates the Silero VAD model.
* **Stream lifecycle** — ``_RECONNECT_DELAY_S``, ``_QUEUE_MAXSIZE``,
  ``_VALIDATION_S``, ``_VALIDATION_MIN_RMS_DB``,
  ``_HEARTBEAT_INTERVAL_S``: bound to ``VoiceTuningConfig`` so
  operators tune via env vars.
* **Ring-buffer state packing** (v1.3 §4.2 L4-B) —
  ``_RING_EPOCH_SHIFT``, ``_RING_SAMPLES_MASK``: encode
  ``(epoch, samples_written)`` into a single ``int`` attribute so a
  single ``LOAD_ATTR`` observes both components atomically.
* **Sustained-underrun detector** (band-aid #9 replacement) —
  ``_CAPTURE_UNDERRUN_*``: rolling-window thresholds with
  documented operator-meaningful rationales.

Legacy import surface preserved: ``voice/_capture_task.py``
re-exports every name in ``__all__`` so ``test_capture_task.py``'s
direct imports of ``_RING_EPOCH_SHIFT`` / ``_HEARTBEAT_INTERVAL_S``
and the monkeypatch sites at
``sovyx.voice._capture_task._RECONNECT_DELAY_S`` keep working
without an import-path migration.
"""

from __future__ import annotations

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning

__all__ = [
    "_CAPTURE_UNDERRUN_MIN_CALLBACKS",
    "_CAPTURE_UNDERRUN_WARN_FRACTION",
    "_CAPTURE_UNDERRUN_WARN_INTERVAL_S",
    "_CAPTURE_UNDERRUN_WINDOW_S",
    "_FRAME_SAMPLES",
    "_HEARTBEAT_INTERVAL_S",
    "_QUEUE_MAXSIZE",
    "_RECONNECT_DELAY_S",
    "_RING_EPOCH_SHIFT",
    "_RING_SAMPLES_MASK",
    "_SAMPLE_RATE",
    "_VALIDATION_MIN_RMS_DB",
    "_VALIDATION_S",
]


_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 512  # must match VoicePipeline._FRAME_SAMPLES
_RECONNECT_DELAY_S = _VoiceTuning().capture_reconnect_delay_seconds
_QUEUE_MAXSIZE = _VoiceTuning().capture_queue_maxsize
_VALIDATION_S = _VoiceTuning().capture_validation_seconds
_VALIDATION_MIN_RMS_DB = _VoiceTuning().capture_validation_min_rms_db
_HEARTBEAT_INTERVAL_S = _VoiceTuning().capture_heartbeat_interval_seconds


# ── v1.3 §4.2 L4-B — ring buffer state packing ──────────────────────
#
# The capture task packs ``(epoch, samples_written)`` into a single
# ``int`` attribute so a single ``LOAD_ATTR`` by an external consumer
# (:meth:`samples_written_mark`) observes both components atomically
# — without the packing, a reader could race the writer between the
# "bump samples" and "bump epoch" assignments and see an epoch that
# does not match the samples count.
#
# Layout: ``state = (epoch << _RING_EPOCH_SHIFT) | (samples & _RING_SAMPLES_MASK)``
#
# * Samples occupy the low 40 bits. At 16 kHz that is 2**40 / 16000 / 86400 / 365
#   ≈ 2179 years of continuous capture before wrapping — practically unreachable
#   within a single process lifetime.
# * Epoch occupies the remaining (high) bits of a Python ``int`` (arbitrary
#   precision). In realistic use the epoch increments once per stream reopen,
#   so ~10^4 is the practical ceiling across a multi-year daemon lifetime.
#
# External consumers (:class:`CaptureIntegrityCoordinator`) receive the
# pair as ``tuple[int, int]`` — neither component individually can
# exceed ``2**53`` in realistic deployments, so the tuple survives JSON
# / Prometheus / structlog serialization boundaries without truncation.
_RING_EPOCH_SHIFT = 40
_RING_SAMPLES_MASK = (1 << _RING_EPOCH_SHIFT) - 1


# ── Band-aid #9 replacement — sustained-underrun detection ───────────
#
# Pre-band-aid #9: ``_audio_callback`` incremented ``_stream_underruns``
# on every ``input_underflow`` callback flag (≈ once per kernel xrun).
# The counter was emitted only at ``audio.stream.closed`` (post-mortem)
# and once per heartbeat — but with no threshold, no rate, and no
# operator-actionable WARN. A USB driver in distress could throw 5 000
# underruns in 30 seconds and the operator would see nothing until the
# stream closed.
#
# Spec (F1 #9): "PortAudio Stream.latency query per callback".
# Stream.latency is a static configured value (not a per-callback
# instantaneous reading), so the spec's letter does not match
# PortAudio's API. The spec's INTENT is "detect sustained xruns
# during a stream's life and surface them as actionable signal".
#
# Fix: rolling-window underrun-fraction monitor. Each ``window_seconds``
# of capture, the consumer loop computes ``underruns / callbacks`` over
# the last window; if the fraction exceeds ``warn_fraction`` AND the
# window has at least ``min_callbacks`` samples, emit a structured
# ``voice.audio.capture_sustained_underrun`` WARN. Rate-limited to
# at most one WARN per ``warn_interval_seconds`` per stream so a
# multi-minute outage produces an actionable trickle, not a flood.
#
# Why the consumer (not the audio thread): the PortAudio callback runs
# in PortAudio's audio thread which MUST NOT block (no logging, no
# allocation). Counters increment in the callback (anti-pattern #14
# safe — pure int += int); the rate check + WARN happen in
# ``_consume_loop`` between awaits, where logging is safe.
_CAPTURE_UNDERRUN_WINDOW_S = 10.0
"""Rolling-window length over which the underrun fraction is computed.
10 s is long enough that a 1-callback xrun (e.g. CPU spike that
resolved) doesn't trip the warn — but short enough that a sustained
USB-bus pressure surfaces within an utterance, not several utterances
later. Matches the order of magnitude of the saturation feedback
window in :mod:`sovyx.voice._frame_normalizer` for operator mental-
model parity."""

_CAPTURE_UNDERRUN_WARN_FRACTION = 0.05
"""Underrun-to-callback fraction above which the WARN fires. 5%
sustained underruns is the canonical "device under stress" threshold:
below that, transient kernel-side glitches (USB scheduling jitter,
host CPU spikes) are perceptually transparent; above 5% the dropouts
are audible to a human and start affecting VAD/STT accuracy."""

_CAPTURE_UNDERRUN_MIN_CALLBACKS = 50
"""Minimum callback count in the window before the WARN can fire.
At a typical 32 ms block size, 50 callbacks ≈ 1.6 s of capture
— enough sample size that the ratio is statistically meaningful
without being so large that an early-stream burst is missed."""

_CAPTURE_UNDERRUN_WARN_INTERVAL_S = 30.0
"""Minimum gap between two sustained-underrun WARN logs from the same
stream. Without rate-limiting, a sustained-underrun condition would
fire one WARN per consumer-loop iteration, drowning the dashboard.
30 s matches the typical operator response cadence — long enough
that a recovering condition self-suppresses, short enough that an
unattended outage produces a regular drumbeat in the log feed."""
