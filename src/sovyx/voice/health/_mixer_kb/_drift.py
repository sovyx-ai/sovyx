"""KB profile drift monitoring (F12 foundation).

Once a KB profile is matched + applied to a device, the mixer
reads back at known-good values. Over time, those values can
drift — kernel updates change driver calibration, firmware
patches re-scale the codec's range, hot-plug events leave the
device in a slightly different state. The drift may be silent:
the cascade still classifies the endpoint as HEALTHY, but the
microphone sounds quieter or louder than the user expected.

F12 catches that silent drift. After every successful preset
apply, the orchestrator pushes the post-apply readings into a
:class:`DriftMonitor`. The monitor maintains a rolling window
of readings per ``(profile_id, control_role)`` pair and flags
sustained drift outside the verified baseline range with a
structured ``voice.kb.profile.drift_detected`` event.

Design decisions
================

* **Per-install, not per-deployment** — each install's monitor
  state lives in process memory; the mission's longer-term
  ambition is to roll the per-install signal up to opt-in
  community telemetry (F7 task), but F12 ships only the local
  detector. This keeps F12 self-contained — no network, no
  shared state, no opt-in surface to design.
* **Sustained drift, not transient drift** — a single noisy
  reading can come from a hot-plug glitch or a competing app
  briefly grabbing the mixer. Alerting on a single sample
  produces false positives. The detector requires
  ``min_consecutive_drift_samples`` (default 3) AND a sustained
  proportion ``drift_proportion_threshold`` (default 0.5 — half
  the rolling window) before firing.
* **Hysteresis on alert clear** — the alert latches until a
  sustained recovery (same dual-criterion: N consecutive
  in-baseline samples + proportion threshold). Without
  hysteresis, a sample-rate-jittery reading would oscillate the
  alert state and the dashboard would flap.
* **Closed-set baseline source** — the verified range comes
  from the matched :class:`MixerKBProfile`'s ``factory_signature``
  (the same range the matcher already validates against).
  Drift detection reuses the existing trusted-baseline
  vocabulary; no new ground truth needed.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §4
(drift_monitoring per-install in KB schema v2), §3.10 F12 task;
ChromeOS CRAS per-board overlay (drift monitoring inspiration);
CLAUDE.md anti-patterns #11 (loud-fail bounds), #15 (LRU
eviction for unbounded keys).
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Bound enforcement constants ────────────────────────────────────


_DEFAULT_WINDOW_SIZE = 12
"""Rolling-window depth (samples). Each apply emits ~1 sample
per minute in steady state; 12 samples covers ~12 minutes of
operation, enough for a meaningful proportion check + short
enough that a real drift surfaces within an LLM-conversation
timeframe (the 'I just installed an update and the mic sounds
quiet' user scenario)."""


_MIN_WINDOW_SIZE = 3
_MAX_WINDOW_SIZE = 1024
"""Below 3 samples there's not enough data for a meaningful
proportion threshold. Above 1024 the rolling buffer wastes
memory + extends time-to-detect for slow drift."""


_DEFAULT_MIN_CONSECUTIVE = 3
"""Minimum consecutive-out-of-range samples before considering
drift. Below 3 a single hot-plug glitch fires the alert; above
3 the time-to-detect grows linearly with this value."""


_MIN_CONSECUTIVE_FLOOR = 2
_MAX_CONSECUTIVE_CEILING = 256
"""Bounds — 2 means 'two-in-a-row flush' (one transient OK);
256 caps the time-to-detect at a manageable upper bound for
the largest configured window size."""


_DEFAULT_DRIFT_PROPORTION = 0.5
"""Proportion of the rolling window that must be out-of-range
before drift fires. 0.5 (half) is forgiving enough to absorb
single-sample noise but tight enough that a sustained drift
of even half the readings flags."""


_MIN_DRIFT_PROPORTION = 0.1
_MAX_DRIFT_PROPORTION = 1.0
"""Below 10% the alert fires on transient noise; above 100% is
unreachable (the test would never pass)."""


_DEFAULT_KEY_REGISTRY_MAXSIZE = 512
"""Bounded LRU ceiling for distinct (profile_id, control_role)
keys (anti-pattern #15). 512 is generous — a deployment with
multiple devices each matching multiple profiles still fits.
Eviction is FIFO of least-recently-updated keys."""


# ── State enum ─────────────────────────────────────────────────────


class DriftState(StrEnum):
    """Per-key drift state.

    StrEnum (anti-pattern #9) — value-based comparison is xdist-
    safe and serialises to the structured-log ``state`` field
    verbatim.
    """

    HEALTHY = "healthy"
    """Rolling window dominated by in-baseline readings."""

    DRIFTING = "drifting"
    """Sustained out-of-baseline readings — alert latched."""

    UNKNOWN = "unknown"
    """Not enough samples in the window yet to call either way
    (warm-up state). Initial state for every new key."""


# ── Records ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DriftSample:
    """One observation pushed into the monitor."""

    profile_id: str
    control_role: str
    value: float
    """The reading. Type matches the verified-baseline range
    (raw int / fraction float / dB float). Caller's
    responsibility to convert before pushing — the monitor
    treats the value as opaque and only checks
    ``baseline_min <= value <= baseline_max``."""

    baseline_min: float
    baseline_max: float


@dataclass(slots=True)
class _KeyState:
    """Mutable bookkeeping per (profile_id, control_role)."""

    window: deque[bool]
    """True = in baseline, False = out of baseline. Bounded by
    the monitor's window_size."""

    consecutive_out: int = 0
    consecutive_in: int = 0
    state: DriftState = DriftState.UNKNOWN
    sample_count: int = 0


@dataclass(frozen=True, slots=True)
class DriftMonitorConfig:
    """Tuning knobs for :class:`DriftMonitor`. Loud-fail at
    construction (anti-pattern #11) — every field validated."""

    window_size: int = _DEFAULT_WINDOW_SIZE
    min_consecutive_drift_samples: int = _DEFAULT_MIN_CONSECUTIVE
    min_consecutive_recovery_samples: int = _DEFAULT_MIN_CONSECUTIVE
    drift_proportion_threshold: float = _DEFAULT_DRIFT_PROPORTION
    recovery_proportion_threshold: float = _DEFAULT_DRIFT_PROPORTION
    max_keys: int = _DEFAULT_KEY_REGISTRY_MAXSIZE

    def __post_init__(self) -> None:
        if not (_MIN_WINDOW_SIZE <= self.window_size <= _MAX_WINDOW_SIZE):
            msg = (
                f"window_size must be in "
                f"[{_MIN_WINDOW_SIZE}, {_MAX_WINDOW_SIZE}], "
                f"got {self.window_size}"
            )
            raise ValueError(msg)
        for name, value in (
            ("min_consecutive_drift_samples", self.min_consecutive_drift_samples),
            ("min_consecutive_recovery_samples", self.min_consecutive_recovery_samples),
        ):
            if not (_MIN_CONSECUTIVE_FLOOR <= value <= _MAX_CONSECUTIVE_CEILING):
                msg = (
                    f"{name} must be in "
                    f"[{_MIN_CONSECUTIVE_FLOOR}, {_MAX_CONSECUTIVE_CEILING}], "
                    f"got {value}"
                )
                raise ValueError(msg)
            if value > self.window_size:
                msg = (
                    f"{name} ({value}) must be <= window_size "
                    f"({self.window_size}) — otherwise the threshold is "
                    f"unreachable"
                )
                raise ValueError(msg)
        for name, prop in (
            ("drift_proportion_threshold", self.drift_proportion_threshold),
            ("recovery_proportion_threshold", self.recovery_proportion_threshold),
        ):
            if not (_MIN_DRIFT_PROPORTION <= prop <= _MAX_DRIFT_PROPORTION):
                msg = (
                    f"{name} must be in "
                    f"[{_MIN_DRIFT_PROPORTION}, {_MAX_DRIFT_PROPORTION}], "
                    f"got {prop}"
                )
                raise ValueError(msg)
        if self.max_keys < 1:
            msg = f"max_keys must be >= 1, got {self.max_keys}"
            raise ValueError(msg)


# ── Monitor ────────────────────────────────────────────────────────


class DriftMonitor:
    """Per-install drift detector.

    Construct once at orchestrator init; call :meth:`record` per
    successful preset apply. The monitor maintains rolling
    windows per ``(profile_id, control_role)`` key, classifies
    each key as HEALTHY / DRIFTING / UNKNOWN, and emits
    structured ``voice.kb.profile.drift_detected`` events on
    HEALTHY → DRIFTING transitions and
    ``voice.kb.profile.drift_recovered`` events on DRIFTING →
    HEALTHY transitions.

    Thread-safe: an internal :class:`threading.Lock` serialises
    every mutation. Reads (:meth:`state_for`,
    :meth:`tracked_keys`) are also under the lock — the snapshot
    they return is consistent with respect to one record() call.
    """

    def __init__(self, config: DriftMonitorConfig | None = None) -> None:
        self._config = config or DriftMonitorConfig()
        self._lock = threading.Lock()
        self._states: dict[tuple[str, str], _KeyState] = {}
        # Insertion-order is FIFO for the LRU eviction. We rely on
        # CPython 3.7+ dict ordering guarantee.

    @property
    def config(self) -> DriftMonitorConfig:
        return self._config

    def record(self, sample: DriftSample) -> DriftState:
        """Push one ``sample`` into the monitor; return the
        post-record state for the sample's key.

        Args:
            sample: Reading + verified-baseline range.

        Returns:
            The :class:`DriftState` for this key after the sample
            is recorded. Note: HEALTHY → DRIFTING and DRIFTING →
            HEALTHY transitions also emit structured events.
        """
        if sample.baseline_min > sample.baseline_max:
            msg = (
                f"baseline_min ({sample.baseline_min}) must be <= "
                f"baseline_max ({sample.baseline_max})"
            )
            raise ValueError(msg)
        in_baseline = sample.baseline_min <= sample.value <= sample.baseline_max
        key = (sample.profile_id, sample.control_role)
        with self._lock:
            key_state = self._states.get(key)
            if key_state is None:
                key_state = _KeyState(
                    window=deque(maxlen=self._config.window_size),
                )
                self._states[key] = key_state
                self._evict_lru_if_needed()
            else:
                # Touch — move-to-end via re-insert.
                del self._states[key]
                self._states[key] = key_state

            key_state.window.append(in_baseline)
            key_state.sample_count += 1
            if in_baseline:
                key_state.consecutive_in += 1
                key_state.consecutive_out = 0
            else:
                key_state.consecutive_out += 1
                key_state.consecutive_in = 0

            new_state = self._classify(key_state)
            transitioned_from = key_state.state
            key_state.state = new_state

        if new_state != transitioned_from:
            self._emit_transition_event(
                key=key,
                from_state=transitioned_from,
                to_state=new_state,
                sample=sample,
                key_state_snapshot={
                    "consecutive_out": key_state.consecutive_out,
                    "consecutive_in": key_state.consecutive_in,
                    "sample_count": key_state.sample_count,
                    "window_size": len(key_state.window),
                },
            )
        return new_state

    def state_for(
        self,
        profile_id: str,
        control_role: str,
    ) -> DriftState:
        """Return the current :class:`DriftState` for the key, or
        :attr:`DriftState.UNKNOWN` if the key has never been
        recorded."""
        with self._lock:
            key_state = self._states.get((profile_id, control_role))
            return key_state.state if key_state else DriftState.UNKNOWN

    def tracked_keys(self) -> list[tuple[str, str]]:
        """Snapshot of all tracked keys (insertion order = LRU
        order; oldest first)."""
        with self._lock:
            return list(self._states.keys())

    def reset(self) -> None:
        """Drop all per-key state. Test-only helper."""
        with self._lock:
            self._states.clear()

    # ── Private ────────────────────────────────────────────────

    def _classify(self, key_state: _KeyState) -> DriftState:
        """Apply the classification rules with hysteresis.

        Drift fires when:
          - consecutive_out >= min_consecutive_drift_samples AND
          - out_proportion >= drift_proportion_threshold

        Recovery fires when (state is DRIFTING):
          - consecutive_in >= min_consecutive_recovery_samples AND
          - in_proportion >= recovery_proportion_threshold

        Otherwise: state stays the same (UNKNOWN initially,
        latches DRIFTING/HEALTHY between transitions).
        """
        window = key_state.window
        if not window:
            return key_state.state
        current = key_state.state
        in_count = sum(1 for v in window if v)
        out_count = len(window) - in_count
        in_proportion = in_count / len(window)
        out_proportion = out_count / len(window)

        # Drift trigger.
        if (
            key_state.consecutive_out >= self._config.min_consecutive_drift_samples
            and out_proportion >= self._config.drift_proportion_threshold
        ):
            return DriftState.DRIFTING

        # Recovery trigger (only relevant if currently DRIFTING).
        if current is DriftState.DRIFTING and (
            key_state.consecutive_in >= self._config.min_consecutive_recovery_samples
            and in_proportion >= self._config.recovery_proportion_threshold
        ):
            return DriftState.HEALTHY

        # First-fill: promote UNKNOWN to HEALTHY once we have at
        # least min_consecutive_recovery_samples in-baseline
        # samples in a row at the window's start. This avoids
        # leaving a healthy key in UNKNOWN state forever after
        # warm-up.
        if (
            current is DriftState.UNKNOWN
            and key_state.consecutive_in >= self._config.min_consecutive_recovery_samples
        ):
            return DriftState.HEALTHY

        return current

    def _evict_lru_if_needed(self) -> None:
        """Evict oldest key if registry exceeds max_keys.

        Only one eviction per record() call — avoids cascading
        evictions when the cap is bumped down by config change.
        Caller already holds the lock.
        """
        if len(self._states) > self._config.max_keys:
            oldest_key = next(iter(self._states))
            del self._states[oldest_key]
            logger.info(
                "voice.kb.profile.drift_key_evicted",
                profile_id=oldest_key[0],
                control_role=oldest_key[1],
                tracked_keys_count=len(self._states),
                max_keys=self._config.max_keys,
            )

    def _emit_transition_event(
        self,
        *,
        key: tuple[str, str],
        from_state: DriftState,
        to_state: DriftState,
        sample: DriftSample,
        key_state_snapshot: dict[str, int],
    ) -> None:
        """Emit the appropriate structured event for the transition."""
        # The semantic event names are stable contract — dashboards
        # query on them. ``drift_detected`` for HEALTHY→DRIFTING,
        # ``drift_recovered`` for DRIFTING→HEALTHY. The UNKNOWN→*
        # transitions emit a softer ``drift_warmup_complete`` so
        # the dashboard can distinguish first-fill from real drift.
        if from_state is DriftState.UNKNOWN:
            event = "voice.kb.profile.drift_warmup_complete"
            level_logger = logger.debug
        elif to_state is DriftState.DRIFTING:
            event = "voice.kb.profile.drift_detected"
            level_logger = logger.warning
        elif to_state is DriftState.HEALTHY:
            event = "voice.kb.profile.drift_recovered"
            level_logger = logger.info
        else:
            # Should not happen under the current state machine,
            # but logged-not-raised for forward-compat with future
            # state additions.
            event = "voice.kb.profile.drift_state_changed"
            level_logger = logger.info
        level_logger(
            event,
            profile_id=key[0],
            control_role=key[1],
            from_state=from_state.value,
            to_state=to_state.value,
            sample_value=sample.value,
            baseline_min=sample.baseline_min,
            baseline_max=sample.baseline_max,
            **key_state_snapshot,
        )


__all__ = [
    "DriftMonitor",
    "DriftMonitorConfig",
    "DriftSample",
    "DriftState",
]
