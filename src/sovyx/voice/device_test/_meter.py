"""PeakHoldMeter — professional-grade VU computation for the setup-wizard.

What a "good" meter does:

* **RMS** — slow integration value matching perceived loudness.
* **Peak** — instantaneous max-abs of the current frame.
* **Peak-hold** — the peak marker latches at a peak for ``hold_ms``, then
  decays at a fixed ``decay_db_per_sec``. This is the classic analogue-VU
  ballistic that makes transient peaks visible despite the 30 Hz frame rate.
* **Clipping** — any sample at or above ``clipping_db`` (default -0.3 dBFS,
  which catches 32_678/32_768 saturation in int16). Sticky for one frame.
* **VAD trigger** — RMS crossing ``vad_trigger_db`` signals "voice detected"
  so the UI can show its threshold marker.

Numerical choices
-----------------

* All math is float32 in NumPy — no Python loops over samples.
* int16 → normalised float via division by 32_768.0 (full-scale = ±1.0).
* ``_FLOOR_DB = -120.0`` caps silence so log10(0) is bounded, matching
  the Pydantic field constraints on :class:`LevelFrame`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import numpy.typing as npt


_FLOOR_DB: float = -120.0
_INT16_FULLSCALE: float = 32_768.0


@dataclass(frozen=True, slots=True)
class MeterReading:
    """One snapshot of meter state — always safe to publish over the wire."""

    rms_db: float
    peak_db: float
    hold_db: float
    clipping: bool
    vad_trigger: bool


class PeakHoldMeter:
    """Stateful meter that ingests int16 frames and emits :class:`MeterReading`.

    The meter is designed for ~30 Hz frames from the device test session;
    it does not assume any particular sample rate for the audio itself —
    only that ``clock_s`` monotonically advances between reads.
    """

    def __init__(
        self,
        *,
        hold_ms: int = 1_500,
        decay_db_per_sec: float = 20.0,
        vad_trigger_db: float = -30.0,
        clipping_db: float = -0.3,
    ) -> None:
        if hold_ms < 0:
            msg = "hold_ms must be >= 0"
            raise ValueError(msg)
        if decay_db_per_sec <= 0:
            msg = "decay_db_per_sec must be > 0"
            raise ValueError(msg)
        if clipping_db > 0 or clipping_db < _FLOOR_DB:
            msg = f"clipping_db must be in [{_FLOOR_DB}, 0]"
            raise ValueError(msg)
        self._hold_ms = hold_ms
        self._decay = decay_db_per_sec
        self._vad_trigger_db = vad_trigger_db
        self._clipping_db = clipping_db

        self._hold_db: float = _FLOOR_DB
        self._hold_latched_at_s: float | None = None
        self._last_clock_s: float | None = None

    def reset(self) -> None:
        """Discard all accumulated state — used on device change."""
        self._hold_db = _FLOOR_DB
        self._hold_latched_at_s = None
        self._last_clock_s = None

    def process(
        self,
        frame: npt.NDArray[np.int16],
        *,
        clock_s: float,
    ) -> MeterReading:
        """Integrate one frame and return the current :class:`MeterReading`.

        ``clock_s`` is a monotonically increasing timestamp in seconds (the
        caller typically passes :func:`asyncio.loop.time`). Decay and hold
        expiry are computed from the delta between successive calls — the
        meter does not touch the system clock.
        """
        if frame.size == 0:
            return self._current_reading(clock_s)

        samples_f32 = frame.astype(np.float32, copy=False) / _INT16_FULLSCALE
        rms_lin = float(np.sqrt(np.mean(samples_f32 * samples_f32, dtype=np.float32)))
        peak_lin = float(np.max(np.abs(samples_f32)))

        rms_db = _lin_to_db(rms_lin)
        peak_db = _lin_to_db(peak_lin)

        self._advance_hold(peak_db, clock_s)

        return MeterReading(
            rms_db=rms_db,
            peak_db=peak_db,
            hold_db=self._hold_db,
            clipping=peak_db >= self._clipping_db,
            vad_trigger=rms_db >= self._vad_trigger_db,
        )

    def _advance_hold(self, peak_db: float, clock_s: float) -> None:
        prev_clock = self._last_clock_s
        self._last_clock_s = clock_s

        if peak_db > self._hold_db:
            self._hold_db = peak_db
            self._hold_latched_at_s = clock_s
            return

        if self._hold_latched_at_s is None:
            # Falling from a never-latched state — just track peak.
            self._hold_db = peak_db
            return

        held_ms = (clock_s - self._hold_latched_at_s) * 1000.0
        if held_ms < self._hold_ms:
            # Still within hold window — don't decay.
            return

        # Decay.
        delta_s = 0.0 if prev_clock is None else max(0.0, clock_s - prev_clock)
        self._hold_db = max(peak_db, self._hold_db - self._decay * delta_s)

    def _current_reading(self, clock_s: float) -> MeterReading:
        # Called when a zero-sized frame arrives (e.g. keepalive tick).
        self._advance_hold(_FLOOR_DB, clock_s)
        return MeterReading(
            rms_db=_FLOOR_DB,
            peak_db=_FLOOR_DB,
            hold_db=self._hold_db,
            clipping=False,
            vad_trigger=False,
        )


def _lin_to_db(x: float) -> float:
    """Convert a linear amplitude in [0, 1+eps] to dBFS, clamped at floor."""
    if x <= 0.0 or math.isnan(x):
        return _FLOOR_DB
    db = 20.0 * math.log10(x)
    if db < _FLOOR_DB:
        return _FLOOR_DB
    return db
