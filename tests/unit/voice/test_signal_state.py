"""W1.1 / G-P0-1 — honest capture signal-state classification.

Pins the dead-mic-vs-warming disambiguation: a dead/absent mic must never
masquerade as "warming up". The classifier is the single source of truth
consumed by every dashboard surface (VU-meter, SNR verdict, noise-floor,
frames-delivered).
"""

from __future__ import annotations

from sovyx.voice.capture._constants import _HEARTBEAT_INTERVAL_S
from sovyx.voice.capture._signal_state import (
    _SILENCE_FLOOR_DB,
    _STALE_FRAME_CEILING_S,
    _WARMING_FRAME_THRESHOLD,
    SignalState,
    classify_signal_state,
)


class TestClassifySignalState:
    def test_not_running_is_no_device(self) -> None:
        assert (
            classify_signal_state(running=False, frames_delivered=999, last_rms_db=-20.0)
            is SignalState.NO_DEVICE
        )

    def test_running_but_zero_frames_is_no_device(self) -> None:
        # Stream open but no PCM delivered — read-failed / never-started /
        # unplugged. NOT "warming".
        assert (
            classify_signal_state(running=True, frames_delivered=0, last_rms_db=None)
            is SignalState.NO_DEVICE
        )

    def test_few_frames_is_warming(self) -> None:
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=_WARMING_FRAME_THRESHOLD - 1,
                last_rms_db=-30.0,
            )
            is SignalState.WARMING
        )

    def test_frames_flowing_no_rms_reading_is_warming(self) -> None:
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=_WARMING_FRAME_THRESHOLD + 100,
                last_rms_db=None,
            )
            is SignalState.WARMING
        )

    def test_established_at_floor_is_live_silent(self) -> None:
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=_WARMING_FRAME_THRESHOLD,
                last_rms_db=_SILENCE_FLOOR_DB - 5.0,
            )
            is SignalState.LIVE_SILENT
        )

    def test_established_with_energy_is_live_signal(self) -> None:
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=_WARMING_FRAME_THRESHOLD,
                last_rms_db=-25.0,
            )
            is SignalState.LIVE_SIGNAL
        )

    def test_dead_mic_does_not_masquerade_as_warming(self) -> None:
        """THE bug (LIVE2-P2-1): a mic that has been delivering floor-level
        PCM for a long time is LIVE_SILENT ("check your mic"), never stuck
        in WARMING regardless of how many frames have flowed."""
        state = classify_signal_state(
            running=True,
            frames_delivered=1_000_000,
            last_rms_db=-80.0,
        )
        assert state is SignalState.LIVE_SILENT
        assert state is not SignalState.WARMING

    def test_floor_boundary_is_inclusive_silent(self) -> None:
        # Exactly at the floor counts as silent (>= would flip; we use <=).
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=_WARMING_FRAME_THRESHOLD,
                last_rms_db=_SILENCE_FLOOR_DB,
            )
            is SignalState.LIVE_SILENT
        )


class TestStaleFrameRecencyD6:
    """D6 — stale last-good telemetry must never classify LIVE_SIGNAL.

    ``frames_delivered`` / ``last_rms_db`` freeze at their last-good
    values when frames stop arriving (consumer parked, callback dead);
    pre-D6 the classifier returned LIVE_SIGNAL forever off that cache,
    defeating the honest dead-mic purpose (W1.1 / G-P0-1). A frame older
    than ``_STALE_FRAME_CEILING_S`` now demotes to NO_DEVICE — the
    existing state whose contract is "stream open but no PCM arriving".
    """

    def test_stale_while_running_is_not_live_signal(self) -> None:
        """THE bug: healthy-looking cache + stopped frames ≠ LIVE_SIGNAL."""
        state = classify_signal_state(
            running=True,
            frames_delivered=1_000_000,
            last_rms_db=-20.0,
            seconds_since_last_frame=_STALE_FRAME_CEILING_S * 10.0,
        )
        assert state is SignalState.NO_DEVICE
        assert state is not SignalState.LIVE_SIGNAL

    def test_stale_silent_cache_is_no_device_not_live_silent(self) -> None:
        # LIVE_SILENT asserts frames ARE flowing — a stalled feed may not
        # claim it, however silent the cached RMS looks.
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=1_000_000,
                last_rms_db=-80.0,
                seconds_since_last_frame=_STALE_FRAME_CEILING_S + 1.0,
            )
            is SignalState.NO_DEVICE
        )

    def test_stale_during_warming_is_no_device_not_warming(self) -> None:
        # A feed that died mid-warmup must not report "warming up" forever
        # (the same masquerade class as LIVE2-P2-1).
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=_WARMING_FRAME_THRESHOLD - 1,
                last_rms_db=-30.0,
                seconds_since_last_frame=_STALE_FRAME_CEILING_S + 1.0,
            )
            is SignalState.NO_DEVICE
        )

    def test_ceiling_boundary_is_inclusive_stale(self) -> None:
        # ``>=`` per anti-pattern #24 — inclusive + coarse-clock safe.
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=1_000_000,
                last_rms_db=-20.0,
                seconds_since_last_frame=_STALE_FRAME_CEILING_S,
            )
            is SignalState.NO_DEVICE
        )

    def test_fresh_frames_keep_live_signal(self) -> None:
        """Regression: fresh recency changes NO verdict."""
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=1_000_000,
                last_rms_db=-20.0,
                seconds_since_last_frame=0.01,
            )
            is SignalState.LIVE_SIGNAL
        )

    def test_fresh_frames_keep_live_silent(self) -> None:
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=1_000_000,
                last_rms_db=-80.0,
                seconds_since_last_frame=0.01,
            )
            is SignalState.LIVE_SILENT
        )

    def test_fresh_frames_keep_warming(self) -> None:
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=_WARMING_FRAME_THRESHOLD - 1,
                last_rms_db=-30.0,
                seconds_since_last_frame=0.01,
            )
            is SignalState.WARMING
        )

    def test_none_recency_preserves_legacy_verdicts(self) -> None:
        # Callers without a recency signal (default None) get the exact
        # pre-D6 behaviour.
        assert (
            classify_signal_state(
                running=True,
                frames_delivered=1_000_000,
                last_rms_db=-20.0,
                seconds_since_last_frame=None,
            )
            is SignalState.LIVE_SIGNAL
        )

    def test_not_running_still_wins_over_recency(self) -> None:
        assert (
            classify_signal_state(
                running=False,
                frames_delivered=999,
                last_rms_db=-20.0,
                seconds_since_last_frame=0.01,
            )
            is SignalState.NO_DEVICE
        )

    def test_ceiling_tied_to_heartbeat_interval(self) -> None:
        # The ceiling is 3× the capture heartbeat cadence — one missed
        # heartbeat is jitter, three means frames genuinely stopped.
        assert _STALE_FRAME_CEILING_S == 3.0 * _HEARTBEAT_INTERVAL_S


class TestProducerAndModelContract:
    def test_status_snapshot_default_matches_enum(self) -> None:
        # The dashboard model default is a literal kept in sync with the
        # SSoT enum (it avoids a top-level voice.capture import); pin them.
        from sovyx.dashboard.routes.voice import VoiceStatusCapture

        assert VoiceStatusCapture().signal_state == SignalState.NO_DEVICE.value

    def test_status_snapshot_carries_signal_state(self) -> None:
        # The producer must emit signal_state so the boundary round-trips it.
        from sovyx.dashboard.routes.voice import VoiceStatusCapture

        snapshot = {
            "running": True,
            "frames_delivered": 1_000_000,
            "last_rms_db": -80.0,
            "signal_state": SignalState.LIVE_SILENT.value,
        }
        model = VoiceStatusCapture.model_validate(snapshot)
        assert model.signal_state == SignalState.LIVE_SILENT.value
