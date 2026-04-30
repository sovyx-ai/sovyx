"""VAD feedback channel for AGC2 silence-period gating (Phase 4 / T4.52).

The orchestrator's VAD runs DOWNSTREAM of the FrameNormalizer's
AGC2 stage in the capture chain, so AGC2 can't read the VAD
verdict for the SAME frame. This module bridges the producer
(orchestrator after each VAD inference) and the consumer (AGC2
on the next frame's process call) via a thread-safe last-verdict
slot.

The 1-frame lag (~32 ms at 16 kHz / 512-sample windows) is
absorbed by AGC2's slow-attack / slower-release time constants
(typical 100-500 ms). The lag is small enough that "VAD said
speech 32 ms ago" is a reliable proxy for "this frame is part
of the same utterance".

Freshness: every write carries the producer's monotonic time;
:func:`get_last_verdict` returns ``None`` when the verdict is
older than the configured horizon (default 0.5 s = ~16 frames).
Stale verdicts fall back to AGC2's pre-T4.52 RMS-only gate.

Architecture matches :mod:`._snr_heartbeat` and
:mod:`._noise_floor_trending`: module-level singleton state for
the single-mind production deployment, graduates to per-mind
instances when v0.31.0 ships multi-mind voice.

Concurrency: producer (asyncio orchestrator loop) and consumer
(capture audio thread inside FrameNormalizer.push → AGC2) both
touch the slot under :class:`threading.Lock`. The slot is a
single tuple, not a buffer — no FIFO required, only the latest
verdict matters.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

_DEFAULT_FRESHNESS_S = 0.5
"""Maximum age (seconds) of a verdict before :func:`get_last_verdict`
falls back to ``None``. 0.5 s ≈ 16 frames at the 16 kHz / 512-sample
rate; comfortably longer than any reasonable orchestrator → AGC2
plumbing latency, short enough that a stalled orchestrator (e.g.
asyncio loop blocked) doesn't keep AGC2 acting on a multi-second-old
verdict."""


@dataclass(frozen=True, slots=True)
class _Verdict:
    is_speech: bool
    monotonic: float


_lock = threading.Lock()
_last: _Verdict | None = None


def set_last_verdict(*, is_speech: bool, monotonic: float | None = None) -> None:
    """Publish the most recent VAD verdict.

    Called from the orchestrator's ``feed_frame`` after every
    successful VAD inference. The ``monotonic`` argument is
    optional — when omitted the current ``time.monotonic()``
    value is captured. Production code should let this default
    so the freshness check uses the same clock as the consumer;
    tests that need deterministic timing pass an explicit value.

    Args:
        is_speech: The :class:`VADEvent.is_speech` flag the FSM
            settled on for the latest emitted window.
        monotonic: Optional explicit timestamp (seconds, monotonic
            clock). Defaults to ``time.monotonic()`` at call time.
    """
    global _last
    ts = monotonic if monotonic is not None else time.monotonic()
    with _lock:
        _last = _Verdict(is_speech=bool(is_speech), monotonic=float(ts))


def get_last_verdict(
    *,
    now_monotonic: float | None = None,
    max_age_seconds: float = _DEFAULT_FRESHNESS_S,
) -> bool | None:
    """Return the freshest VAD verdict, or ``None`` if absent / stale.

    Called from inside :meth:`AGC2.process` when
    ``vad_feedback_enabled`` is True. The freshness window
    guards against the orchestrator stalling — without it, a
    blocked asyncio loop would keep AGC2 acting on a multi-
    second-old verdict.

    Args:
        now_monotonic: Optional explicit "now" timestamp; defaults
            to ``time.monotonic()`` at call time. Tests pass a
            deterministic value alongside :func:`set_last_verdict`.
        max_age_seconds: Maximum age before the verdict is
            considered stale. Default 0.5 s.

    Returns:
        The cached ``is_speech`` value when fresh; ``None`` when
        no verdict has been published yet OR the cached verdict
        is older than ``max_age_seconds``. AGC2 treats ``None``
        as "no feedback available, fall back to RMS gate".
    """
    with _lock:
        cached = _last
    if cached is None:
        return None
    now = now_monotonic if now_monotonic is not None else time.monotonic()
    if now - cached.monotonic > max_age_seconds:
        return None
    return cached.is_speech


def reset_for_tests() -> None:
    """Clear the cached verdict.

    Test-only helper. Production code does NOT clear the slot —
    the next ``set_last_verdict`` overwrites in place.
    """
    global _last
    with _lock:
        _last = None
