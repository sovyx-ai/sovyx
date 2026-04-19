"""Unit tests for :mod:`sovyx.voice.health._self_feedback` (ADR §4.4.6)."""

from __future__ import annotations

import pytest

from sovyx.voice.health import SelfFeedbackGate, SelfFeedbackMode


class _DuckRecorder:
    """Test double for the ``apply_duck`` callback.

    Records every gain value passed to the callback. Mirrors the
    ``FrameNormalizer.set_ducking_gain_db`` signature so production
    and test wiring stay structurally identical.
    """

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, gain_db: float) -> None:
        self.calls.append(gain_db)


class TestSelfFeedbackMode:
    """Enum surface sanity (StrEnum invariants from CLAUDE.md #9)."""

    def test_values_are_strings(self) -> None:
        assert SelfFeedbackMode.OFF.value == "off"
        assert SelfFeedbackMode.GATE_ONLY.value == "gate-only"
        assert SelfFeedbackMode.GATE_DUCK.value == "gate+duck"

    def test_from_str_accepts_config_values(self) -> None:
        assert SelfFeedbackMode("off") is SelfFeedbackMode.OFF
        assert SelfFeedbackMode("gate-only") is SelfFeedbackMode.GATE_ONLY
        assert SelfFeedbackMode("gate+duck") is SelfFeedbackMode.GATE_DUCK

    def test_roundtrip_through_str(self) -> None:
        for mode in SelfFeedbackMode:
            assert SelfFeedbackMode(mode.value) is mode


class TestConstruction:
    """Construction validation + defaults."""

    def test_defaults_from_tuning(self) -> None:
        gate = SelfFeedbackGate()
        # Tuning default is gate+duck with -18 dB and 50 ms release.
        assert gate.mode is SelfFeedbackMode.GATE_DUCK
        assert gate.duck_gain_db == -18.0
        assert gate.release_ms == 50.0
        assert gate.is_active is False

    def test_accepts_string_mode(self) -> None:
        gate = SelfFeedbackGate(mode="gate-only")
        assert gate.mode is SelfFeedbackMode.GATE_ONLY

    def test_accepts_enum_mode(self) -> None:
        gate = SelfFeedbackGate(mode=SelfFeedbackMode.OFF)
        assert gate.mode is SelfFeedbackMode.OFF

    def test_rejects_positive_duck_gain(self) -> None:
        with pytest.raises(ValueError, match="duck_gain_db must be <= 0"):
            SelfFeedbackGate(duck_gain_db=3.0)

    def test_accepts_neg_inf_duck_gain(self) -> None:
        gate = SelfFeedbackGate(duck_gain_db=float("-inf"))
        assert gate.duck_gain_db == float("-inf")

    def test_custom_release_ms(self) -> None:
        gate = SelfFeedbackGate(release_ms=120.0)
        assert gate.release_ms == 120.0


class TestOffMode:
    """Mode=OFF must be completely silent — no duck, no log transitions."""

    def test_on_tts_start_is_noop(self) -> None:
        duck = _DuckRecorder()
        gate = SelfFeedbackGate(mode=SelfFeedbackMode.OFF, apply_duck=duck)
        gate.on_tts_start()
        assert gate.is_active is False
        assert duck.calls == []

    def test_on_tts_end_is_noop(self) -> None:
        duck = _DuckRecorder()
        gate = SelfFeedbackGate(mode=SelfFeedbackMode.OFF, apply_duck=duck)
        gate.on_tts_end()
        assert gate.is_active is False
        assert duck.calls == []


class TestGateOnlyMode:
    """Mode=GATE_ONLY logs transitions but skips the duck callback."""

    def test_tts_start_flips_active_without_duck(self) -> None:
        duck = _DuckRecorder()
        gate = SelfFeedbackGate(mode=SelfFeedbackMode.GATE_ONLY, apply_duck=duck)
        gate.on_tts_start()
        assert gate.is_active is True
        assert duck.calls == []

    def test_tts_end_releases_without_duck(self) -> None:
        duck = _DuckRecorder()
        gate = SelfFeedbackGate(mode=SelfFeedbackMode.GATE_ONLY, apply_duck=duck)
        gate.on_tts_start()
        gate.on_tts_end()
        assert gate.is_active is False
        assert duck.calls == []


class TestGateDuckMode:
    """Mode=GATE_DUCK engages duck on start and releases to 0 dB on end."""

    def test_start_applies_duck_gain(self) -> None:
        duck = _DuckRecorder()
        gate = SelfFeedbackGate(
            mode=SelfFeedbackMode.GATE_DUCK,
            apply_duck=duck,
            duck_gain_db=-18.0,
        )
        gate.on_tts_start()
        assert gate.is_active is True
        assert duck.calls == [-18.0]

    def test_end_releases_to_zero_db(self) -> None:
        duck = _DuckRecorder()
        gate = SelfFeedbackGate(
            mode=SelfFeedbackMode.GATE_DUCK,
            apply_duck=duck,
            duck_gain_db=-18.0,
        )
        gate.on_tts_start()
        gate.on_tts_end()
        assert gate.is_active is False
        assert duck.calls == [-18.0, 0.0]

    def test_custom_duck_gain_propagates(self) -> None:
        duck = _DuckRecorder()
        gate = SelfFeedbackGate(
            mode=SelfFeedbackMode.GATE_DUCK,
            apply_duck=duck,
            duck_gain_db=-30.0,
        )
        gate.on_tts_start()
        gate.on_tts_end()
        assert duck.calls == [-30.0, 0.0]

    def test_start_is_idempotent(self) -> None:
        """Calling on_tts_start twice only invokes duck on the rising edge."""
        duck = _DuckRecorder()
        gate = SelfFeedbackGate(mode=SelfFeedbackMode.GATE_DUCK, apply_duck=duck)
        gate.on_tts_start()
        gate.on_tts_start()
        gate.on_tts_start()
        assert duck.calls == [-18.0]

    def test_end_is_idempotent(self) -> None:
        """Calling on_tts_end without a prior start is a no-op."""
        duck = _DuckRecorder()
        gate = SelfFeedbackGate(mode=SelfFeedbackMode.GATE_DUCK, apply_duck=duck)
        gate.on_tts_end()
        gate.on_tts_end()
        assert duck.calls == []

    def test_end_after_spurious_starts_single_release(self) -> None:
        """start×N → end should release exactly once."""
        duck = _DuckRecorder()
        gate = SelfFeedbackGate(mode=SelfFeedbackMode.GATE_DUCK, apply_duck=duck)
        gate.on_tts_start()
        gate.on_tts_start()
        gate.on_tts_end()
        gate.on_tts_end()
        assert duck.calls == [-18.0, 0.0]

    def test_cycle_repeats(self) -> None:
        """Multiple TTS utterances in one session each apply + release."""
        duck = _DuckRecorder()
        gate = SelfFeedbackGate(mode=SelfFeedbackMode.GATE_DUCK, apply_duck=duck)
        gate.on_tts_start()
        gate.on_tts_end()
        gate.on_tts_start()
        gate.on_tts_end()
        assert duck.calls == [-18.0, 0.0, -18.0, 0.0]


class TestDuckUnavailable:
    """Gate+duck without apply_duck degrades gracefully."""

    def test_gate_duck_without_callback_activates_without_crash(self) -> None:
        gate = SelfFeedbackGate(mode=SelfFeedbackMode.GATE_DUCK, apply_duck=None)
        gate.on_tts_start()
        gate.on_tts_end()
        assert gate.is_active is False

    def test_warning_logged_once_per_session(self, caplog: pytest.LogCaptureFixture) -> None:
        """The one-shot WARNING must fire once, not on every cycle."""
        gate = SelfFeedbackGate(mode=SelfFeedbackMode.GATE_DUCK, apply_duck=None)
        with caplog.at_level("WARNING", logger="sovyx.voice.health._self_feedback"):
            gate.on_tts_start()
            gate.on_tts_end()
            gate.on_tts_start()
            gate.on_tts_end()
        unavailable_events = [
            r for r in caplog.records if "voice_self_feedback_duck_unavailable" in r.message
        ]
        assert len(unavailable_events) == 1


class TestDuckFailureSwallowed:
    """Exceptions from ``apply_duck`` must not crash the voice loop."""

    def test_start_tolerates_callback_exception(self) -> None:
        def boom(_: float) -> None:
            msg = "normalizer torn down"
            raise RuntimeError(msg)

        gate = SelfFeedbackGate(mode=SelfFeedbackMode.GATE_DUCK, apply_duck=boom)
        gate.on_tts_start()  # must not raise
        assert gate.is_active is True

    def test_end_tolerates_callback_exception(self) -> None:
        calls: list[float] = []

        def partial_fail(gain_db: float) -> None:
            calls.append(gain_db)
            if gain_db == 0.0:
                msg = "release failed"
                raise RuntimeError(msg)

        gate = SelfFeedbackGate(mode=SelfFeedbackMode.GATE_DUCK, apply_duck=partial_fail)
        gate.on_tts_start()
        gate.on_tts_end()  # must not raise despite the release crash
        assert gate.is_active is False
        assert calls == [-18.0, 0.0]
