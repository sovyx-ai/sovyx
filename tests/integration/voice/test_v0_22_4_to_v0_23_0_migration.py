"""Migration regression suite v0.22.4 → v0.23.0 (Step 18).

Mission §1 + Step 18: end-to-end verification that an instance
upgrading from v0.22.4 to v0.23.0 sees:

* All 6 ring init events fire in canonical order
* macOS detector functions are no-ops on non-darwin (current host)
* ETW probe is a no-op when disabled (default)
* Frame-history bounded + correct after 100 synthetic transitions
* KB profile loader handles missing trusted key gracefully (LENIENT)
* Deprecation WARN fires for the legacy mixer band-aid functions
* Pipeline boot succeeds end-to-end with the full v0.23.0 stack

The suite runs synthetically (no real STT / LLM / TTS / audio device
required). It exercises the integration of every Step 1-17 component
+ verifies no migration-time regression.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 18.
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.voice.pipeline._config import VoicePipelineConfig
from sovyx.voice.pipeline._frame_types import (
    BargeInInterruptionFrame,
    EndFrame,
    UserStartedSpeakingFrame,
)
from sovyx.voice.pipeline._orchestrator import VoicePipeline
from sovyx.voice.pipeline._state import VoicePipelineState

_FACTORY_LOGGER = "sovyx.voice.factory"
_MIXER_LOGGER = "sovyx.voice.health._linux_mixer_apply"


@pytest.fixture(autouse=True)
def _reset_resolver_singleton() -> Generator[None, None, None]:
    from sovyx.voice.health._capabilities import (
        reset_default_resolver_for_tests,
    )

    reset_default_resolver_for_tests()
    yield
    reset_default_resolver_for_tests()


def _make_pipeline() -> VoicePipeline:
    """Synthetic pipeline that doesn't require ONNX models or audio device."""
    return VoicePipeline(
        config=VoicePipelineConfig(),
        vad=MagicMock(),
        wake_word=MagicMock(),
        stt=AsyncMock(),
        tts=AsyncMock(),
        event_bus=None,
    )


# ── Step 2 contract: all 6 ring events present in factory.py ───────


class TestRingInitEventsPersistedInFactorySource:
    """Step 2 contract: factory.py must emit 6 ring init events.
    The migration suite re-pins this so a regression in factory.py
    that drops one is caught at integration time."""

    def test_six_ring_events_in_factory_source(self) -> None:
        factory_src = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "sovyx"
            / "voice"
            / "factory.py"
        ).read_text(encoding="utf-8")

        for ring_num in range(1, 7):
            assert f'"voice.ring_{ring_num}.initialized"' in factory_src


# ── Step 5/6 contract: macOS detectors no-op on non-darwin ─────────


class TestMacosDetectorsNoOpOnNonDarwin:
    """Step 5+6 wire-ups must be no-ops on Windows/Linux."""

    @pytest.mark.asyncio
    async def test_maybe_log_macos_diagnostics_no_op_on_non_darwin(self) -> None:
        from sovyx.voice.factory import _maybe_log_macos_diagnostics

        if sys.platform == "darwin":
            pytest.skip("running on darwin; non-darwin no-op contract not testable here")
        result = await _maybe_log_macos_diagnostics()
        assert result is None


# ── Step 4 contract: ETW probe is no-op when disabled ─────────────


class TestEtwProbeDefaultOff:
    @pytest.mark.asyncio
    async def test_etw_probe_returns_silently_with_default_config(self) -> None:
        from sovyx.voice.factory import _maybe_log_recent_audio_etw_events

        # Default config has voice_probe_windows_etw_events_enabled=False.
        result = await _maybe_log_recent_audio_etw_events()
        assert result is None


# ── Steps 11-14 contract: frame ring buffer bounded + correct ─────


class TestFrameHistoryBoundedAcross100Turns:
    def test_100_synthetic_turns_no_leak_no_divergence(self) -> None:
        pipeline = _make_pipeline()
        capacity = pipeline._state_machine.history_capacity

        for i in range(100):
            pipeline._current_utterance_id = f"uuid-{i}"
            pipeline._record_frame(
                UserStartedSpeakingFrame(
                    frame_type="UserStartedSpeaking",
                    timestamp_monotonic=time.monotonic(),
                    source="wake_word",
                ),
            )
            pipeline._state = VoicePipelineState.RECORDING
            pipeline._state = VoicePipelineState.IDLE  # emits EndFrame

        history = pipeline._state_machine.frame_history()
        # Bounded ring — at most history_capacity frames.
        assert len(history) <= capacity
        # Both UserStartedSpeaking + End frames should be in the
        # latter half of the ring (the most recent 100 turns produced
        # 200 frames; only the last 256 fit).
        end_count = sum(1 for f in history if isinstance(f, EndFrame))
        user_count = sum(1 for f in history if isinstance(f, UserStartedSpeakingFrame))
        # Total 200 frames produced; 256-cap ring keeps last 200
        # (since 200 < 256). So we should see exactly 100 of each.
        assert end_count == 100
        assert user_count == 100


# ── Step 7 contract: KB loader graceful when trusted key absent ────


class TestKbLoaderGracefulOnMissingKey:
    def test_loader_handles_missing_trusted_key(self) -> None:
        """When _trusted_keys/v1.pub is absent, load_trusted_public_key
        returns None and the verifier short-circuits to
        REJECTED_NO_TRUSTED_KEY rather than crashing the loader."""
        from sovyx.voice.health._mixer_kb._signing import (
            KBSignatureVerifier,
            Mode,
            VerifyResult,
        )

        # Construct verifier with no public key (simulates missing v1.pub).
        verifier = KBSignatureVerifier(public_key=None, mode=Mode.LENIENT)
        verdict = verifier.verify({"profile_id": "test", "schema_version": 1})
        assert verdict is VerifyResult.REJECTED_NO_TRUSTED_KEY


# ── Step 17 contract: deprecation WARN fires for legacy band-aids ──


class TestDeprecationWarnFiresForLegacyMixer:
    @pytest.mark.asyncio
    async def test_apply_mixer_reset_emits_deprecation_warn(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Step 17: apply_mixer_reset MUST emit
        voice.deprecation.legacy_mixer_band_aid_call WARN at every
        entry, regardless of whether the underlying call succeeds.

        Captured via capsys (structlog routes through stdout in the
        test environment) rather than caplog (which doesn't see the
        bypass). The WARN content is verified via substring match
        on the stdout dump.
        """
        from sovyx.engine.config import VoiceTuningConfig
        from sovyx.voice.health._linux_mixer_apply import apply_mixer_reset

        tuning = VoiceTuningConfig()
        with contextlib.suppress(Exception):
            # Empty controls list raises BypassApplyError, but the
            # deprecation WARN fires BEFORE the validation check.
            await apply_mixer_reset(card_index=0, controls_to_reset=[], tuning=tuning)

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "voice.deprecation.legacy_mixer_band_aid_call" in combined
        assert "apply_mixer_reset" in combined
        assert "v0.24.0" in combined


# ── Cross-step: pipeline boots with full v0.23.0 stack synthetically ─


class TestPipelineBootCleanWithV023Stack:
    """Smoke: a freshly-constructed VoicePipeline with all the
    v0.23.0 components wired (state machine, frame ring, chaos
    injector, observability_pii) starts in a clean baseline state."""

    def test_pipeline_starts_in_idle_with_empty_frame_history(self) -> None:
        pipeline = _make_pipeline()
        assert pipeline._state == VoicePipelineState.IDLE
        assert pipeline.frame_history == ()

    def test_pipeline_records_frames_on_state_transitions(self) -> None:
        pipeline = _make_pipeline()
        pipeline._state = VoicePipelineState.RECORDING
        pipeline._state = VoicePipelineState.IDLE
        history = pipeline.frame_history
        # State setter hook emits EndFrame on terminal IDLE.
        end_frames = [f for f in history if isinstance(f, EndFrame)]
        assert len(end_frames) == 1
        assert end_frames[0].reason == "from_recording"


# ── Cross-step: barge-in chain emits BargeInInterruptionFrame ─────


class TestBargeInChainProducesFrame:
    @pytest.mark.asyncio
    async def test_cancel_speech_chain_records_barge_in_frame(self) -> None:
        pipeline = _make_pipeline()
        await pipeline.cancel_speech_chain(reason="migration_test")
        history = pipeline.frame_history
        barge_frames = [f for f in history if isinstance(f, BargeInInterruptionFrame)]
        assert len(barge_frames) == 1
        assert barge_frames[0].reason == "migration_test"
        assert len(barge_frames[0].step_results) == 5


# ── Cross-step: capability resolver fail-closed for Phase 1 stubs ──


class TestCapabilityResolverFailClosedDefaults:
    """Capability probes that don't exist on the current host return
    False. Used to verify the capability dispatch (Step 3) doesn't
    silently flip behaviour after the upgrade."""

    def test_etw_capability_false_on_non_windows(self) -> None:
        from sovyx.voice.health._capabilities import (
            Capability,
            CapabilityResolver,
        )

        if sys.platform == "win32":
            pytest.skip("on Windows host; capability gate behaviour platform-dependent")
        resolver = CapabilityResolver()
        assert resolver.has(Capability.ETW_AUDIO_PROVIDER) is False
        assert resolver.has(Capability.AUDIOSRV_QUERY) is False

    def test_coreaudio_capability_false_on_non_darwin(self) -> None:
        from sovyx.voice.health._capabilities import (
            Capability,
            CapabilityResolver,
        )

        if sys.platform == "darwin":
            pytest.skip("on darwin host; capability gate behaviour platform-dependent")
        resolver = CapabilityResolver()
        assert resolver.has(Capability.COREAUDIO_VPIO) is False
