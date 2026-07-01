"""Honest capture signal-state classification (W1.1 / G-P0-1).

The dashboard historically could not tell a **dead / absent microphone**
from a **microphone that is merely warming up or sitting in a quiet
room**: an empty SNR window rendered ``no_signal`` and an un-ready
noise-floor rendered "warming up" for BOTH cases, and — because the
noise-floor baseline only accrues during non-silent capture — a dead mic
stayed "warming up" forever (LIVE2-P2-1/4/5/6).

This module is the single source of truth that disambiguates the four
states purely from real capture-task telemetry (``running`` +
``frames_delivered`` + ``last_rms_db``), so every dashboard surface
(VU-meter, SNR verdict, noise-floor panel, frames-delivered) can render
the SAME honest verdict instead of each inventing its own "warming"
heuristic:

* ``NO_DEVICE``   — the capture task is not running, or it is running but
  has delivered ZERO frames (stream open but no PCM — a read-failed /
  never-started / unplugged device), or frames DID flow once but the most
  recent one is older than ``_STALE_FRAME_CEILING_S`` (consumer parked /
  callback dead — see :func:`classify_signal_state`). NOT "warming".
* ``WARMING``     — frames ARE flowing but too few have arrived to judge
  signal yet (the genuinely-transient just-started window). This is the
  ONLY state that legitimately reads as "warming up".
* ``LIVE_SILENT`` — frames have been flowing long enough to judge AND the
  recent RMS is at the noise floor. The mic substrate works but no sound
  is reaching it (muted, APO-destroyed, or a truly silent room) — an
  operator-actionable "check your mic", NOT "warming".
* ``LIVE_SIGNAL`` — frames flowing with real signal energy above the floor.

The classifier is a pure function so it is trivially testable and so the
producer (``AudioCaptureTask.status_snapshot``) and any consumer share one
implementation rather than independent thresholds (anti-pattern #53).
"""

from __future__ import annotations

from enum import StrEnum

from sovyx.voice.capture._constants import _HEARTBEAT_INTERVAL_S


class SignalState(StrEnum):
    """Honest capture signal state — see module docstring."""

    NO_DEVICE = "no_device"
    WARMING = "warming"
    LIVE_SILENT = "live_silent"
    LIVE_SIGNAL = "live_signal"


_WARMING_FRAME_THRESHOLD = 50
"""Frames that must arrive before we stop reporting ``WARMING`` and start
judging silence-vs-signal. At the 16 kHz / 512-sample capture frame that is
~1.6 s — long enough that a transient cold-start isn't mislabelled silent,
short enough that a genuinely dead mic exits "warming" quickly instead of
masquerading as warming forever (the LIVE2-P2-1 bug)."""

_SILENCE_FLOOR_DB = -65.0
"""Recent-RMS ceiling (dBFS) below which a frame-delivering stream is
classified ``LIVE_SILENT`` rather than ``LIVE_SIGNAL``. The capture floor
sits near -80 dBFS; -65 leaves headroom for a quiet-but-working room to
still register as silent (correctly — there is no speech) while real
speech (typically >-40 dBFS) reads as ``LIVE_SIGNAL``. Tunable; the
NO_DEVICE / WARMING disambiguation that closes G-P0-1 does not depend on
its exact value."""

_STALE_FRAME_CEILING_S = 3.0 * _HEARTBEAT_INTERVAL_S
"""Recency ceiling (seconds) beyond which cached ``last_rms_db`` /
``frames_delivered`` telemetry is too stale to claim a live verdict (D6).
Tied to the capture heartbeat cadence
(``VoiceTuningConfig.capture_heartbeat_interval_seconds``, default 2 s):
one missed heartbeat is scheduler jitter, three consecutive misses means
frames have genuinely stopped arriving. Without this ceiling the
classifier kept returning ``LIVE_SIGNAL`` forever off the LAST-good RMS
after the consumer parked or the callback died — exactly the dishonest
verdict W1.1 / G-P0-1 exists to prevent."""


def classify_signal_state(
    *,
    running: bool,
    frames_delivered: int,
    last_rms_db: float | None,
    seconds_since_last_frame: float | None = None,
    warming_frame_threshold: int = _WARMING_FRAME_THRESHOLD,
    silence_floor_db: float = _SILENCE_FLOOR_DB,
    stale_frame_ceiling_s: float = _STALE_FRAME_CEILING_S,
) -> SignalState:
    """Classify the capture signal state from real capture telemetry.

    Staleness guard (D6): ``frames_delivered`` and ``last_rms_db`` are
    CACHED last-good values — when frames stop arriving (consumer parked,
    callback dead) they freeze at whatever they last read, and the naive
    verdict stays ``LIVE_SIGNAL`` forever. When
    ``seconds_since_last_frame`` exceeds ``stale_frame_ceiling_s`` the
    classifier returns :attr:`SignalState.NO_DEVICE` — the EXISTING state
    whose contract already reads "stream open but no PCM arriving", which
    is literally what a stalled feed is. ``LIVE_SILENT`` would lie (it
    asserts frames ARE flowing), and no new enum member is added because
    the value set has typed dashboard consumers. The comparison is ``>=``
    per anti-pattern #24 (coarse-clock safe). ``None`` recency skips the
    guard so legacy callers keep their exact pre-D6 verdicts.

    Args:
        running: Whether the capture task's stream loop is active.
        frames_delivered: Cumulative PCM frames the loop has delivered.
        last_rms_db: Most recent per-frame RMS in dBFS, or ``None`` when no
            frame has produced a reading yet.
        seconds_since_last_frame: Monotonic age of the most recent
            delivered frame, or ``None`` when the caller has no recency
            signal (skips the staleness guard).
        warming_frame_threshold: Frames required before judging silence.
        silence_floor_db: RMS ceiling below which a live stream is silent.
        stale_frame_ceiling_s: Frame age at or beyond which the cached
            telemetry no longer supports a live verdict.

    Returns:
        The :class:`SignalState` describing the capture substrate truth.
    """
    if not running or frames_delivered <= 0:
        return SignalState.NO_DEVICE
    if seconds_since_last_frame is not None and seconds_since_last_frame >= stale_frame_ceiling_s:
        return SignalState.NO_DEVICE
    if frames_delivered < warming_frame_threshold or last_rms_db is None:
        return SignalState.WARMING
    if last_rms_db <= silence_floor_db:
        return SignalState.LIVE_SILENT
    return SignalState.LIVE_SIGNAL


__all__ = ["SignalState", "classify_signal_state"]
