"""Tests for ``voice/_wake_word_router.py`` — Phase 8 / T8.6-T8.9.

Pin the multi-mind wake-word router contract:
- T8.6: WakeWordRouter class semantics (lazy registration, fan-out,
  first-match-wins, failure isolation)
- T8.7: Lazy registration — detectors are constructed only via
  register_mind, not eagerly
- T8.8: Per-mind cooldown is independent — cooldown for mind A
  doesn't suppress mind B
- T8.9: Per-mind false-fire forwarding — note_false_fire(mind_id)
  routes to the matched detector
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from sovyx.engine.types import MindId
from sovyx.voice._wake_word_router import WakeWordRouter, WakeWordRouterEvent
from sovyx.voice.wake_word import (
    VerificationResult,
    WakeWordConfig,
    WakeWordEvent,
    WakeWordState,
)

_FRAME = 1280  # OpenWakeWord input size


def _mock_onnx_session(scores: list[float]) -> MagicMock:
    """ONNX session that returns the queued scores in order."""
    session = MagicMock()
    inputs_meta = MagicMock()
    inputs_meta.name = "input"
    session.get_inputs.return_value = [inputs_meta]
    score_iter = iter(scores)
    session.run.side_effect = lambda *_a, **_kw: [np.array([[next(score_iter)]], dtype=np.float32)]
    return session


def _make_router_with_mind(
    mind_id: str,
    scores: list[float],
    *,
    config: WakeWordConfig | None = None,
    verifier: object = None,
) -> WakeWordRouter:
    """Construct a router with one mind registered + mocked ONNX."""
    router = WakeWordRouter()
    mock_ort = MagicMock()
    mock_ort.SessionOptions.return_value = MagicMock()
    mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
    mock_ort.InferenceSession.return_value = _mock_onnx_session(scores)
    with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
        router.register_mind(
            MindId(mind_id),
            model_path=Path(f"/fake/{mind_id}.onnx"),
            config=config,
            verifier=verifier,  # type: ignore[arg-type]
        )
    return router


def _frame() -> np.ndarray:
    return np.zeros(_FRAME, dtype=np.float32)


def _verifier_true(_audio: np.ndarray) -> VerificationResult:
    return VerificationResult(verified=True, transcription="confirmed")


# ── T8.6 — Router class semantics ─────────────────────────────────────


class TestRouterConstruction:
    def test_empty_router_starts_with_no_minds(self) -> None:
        router = WakeWordRouter()
        assert router.is_empty
        assert len(router) == 0
        assert router.registered_minds == ()

    def test_register_mind_increments_count(self) -> None:
        router = _make_router_with_mind("aria", [0.1])
        assert len(router) == 1
        assert not router.is_empty
        assert MindId("aria") in router

    def test_register_empty_mind_id_rejected(self) -> None:
        """Empty mind_id would match every empty-id record at
        unregister/notify time — reject at construction."""
        router = WakeWordRouter()
        with pytest.raises(ValueError, match="non-empty"):
            router.register_mind(
                MindId(""),
                model_path=Path("/fake/m.onnx"),
            )

    def test_re_register_replaces_detector(self) -> None:
        """T7.15-style hot-reload: re-registering an existing mind
        replaces the prior detector (the prior ONNX session is
        garbage-collected normally)."""
        router = _make_router_with_mind("aria", [0.1])
        first_detector = router._detectors[MindId("aria")]  # noqa: SLF001
        # Re-register with a different score sequence.
        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = _mock_onnx_session([0.99])
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            router.register_mind(
                MindId("aria"),
                model_path=Path("/fake/aria-v2.onnx"),
            )
        second_detector = router._detectors[MindId("aria")]  # noqa: SLF001
        assert second_detector is not first_detector
        assert len(router) == 1

    def test_unregister_is_idempotent(self) -> None:
        router = _make_router_with_mind("aria", [0.1])
        router.unregister_mind(MindId("aria"))
        assert len(router) == 0
        # Second unregister is a no-op.
        router.unregister_mind(MindId("aria"))
        assert len(router) == 0
        # Unregistering an unknown mind is also a no-op.
        router.unregister_mind(MindId("never-existed"))
        assert len(router) == 0


class TestProcessFrameNoMatch:
    def test_no_minds_returns_none(self) -> None:
        router = WakeWordRouter()
        result = router.process_frame(_frame())
        assert result is None

    def test_low_score_returns_none(self) -> None:
        router = _make_router_with_mind("aria", [0.1])
        result = router.process_frame(_frame())
        assert result is None


class TestProcessFrameSingleMindMatch:
    def test_high_score_returns_router_event(self) -> None:
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=_FRAME / 16000,
        )
        router = _make_router_with_mind(
            "aria",
            [0.9],
            config=config,
            verifier=_verifier_true,
        )
        result = router.process_frame(_frame())
        assert result is not None
        assert isinstance(result, WakeWordRouterEvent)
        assert result.mind_id == MindId("aria")
        assert isinstance(result.event, WakeWordEvent)
        assert result.event.detected is True


class TestProcessFrameMultiMindFanOut:
    def test_first_match_wins_in_registration_order(self) -> None:
        """Two minds both at high score on same frame: the
        first-registered wins. Determinism is part of the contract."""
        # Register aria first (would match 0.9), then luna (also 0.9).
        router = _make_router_with_mind(
            "aria",
            [0.9],
            config=WakeWordConfig(
                stage1_threshold=0.5,
                stage2_threshold=0.5,
                stage2_window_seconds=_FRAME / 16000,
            ),
            verifier=_verifier_true,
        )
        # Add luna on top of the existing router.
        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = _mock_onnx_session([0.95])
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            router.register_mind(
                MindId("luna"),
                model_path=Path("/fake/luna.onnx"),
                config=WakeWordConfig(
                    stage1_threshold=0.5,
                    stage2_threshold=0.5,
                    stage2_window_seconds=_FRAME / 16000,
                ),
                verifier=_verifier_true,
            )

        result = router.process_frame(_frame())
        assert result is not None
        # Aria registered first — wins despite luna's higher score.
        assert result.mind_id == MindId("aria")

    def test_only_matching_mind_wins(self) -> None:
        """Mind A scores low, mind B scores high → B wins."""
        # aria scores low; luna scores high.
        router = WakeWordRouter()
        # Build mocks for both detectors.
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=_FRAME / 16000,
        )

        for mind_id, score in [("aria", 0.1), ("luna", 0.92)]:
            mock_ort = MagicMock()
            mock_ort.SessionOptions.return_value = MagicMock()
            mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
            mock_ort.InferenceSession.return_value = _mock_onnx_session([score])
            with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
                router.register_mind(
                    MindId(mind_id),
                    model_path=Path(f"/fake/{mind_id}.onnx"),
                    config=config,
                    verifier=_verifier_true,
                )

        result = router.process_frame(_frame())
        assert result is not None
        assert result.mind_id == MindId("luna")


class TestProcessFrameFailureIsolation:
    def test_raising_detector_logged_and_skipped(self) -> None:
        """A detector that raises during process_frame is logged +
        skipped. Other detectors continue to fire."""
        router = WakeWordRouter()
        # First detector raises when process_frame is called.
        broken_detector = MagicMock()
        broken_detector.process_frame.side_effect = RuntimeError("ONNX exploded")
        router._detectors[MindId("broken")] = broken_detector  # noqa: SLF001
        # Second detector fires normally.
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=_FRAME / 16000,
        )
        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = _mock_onnx_session([0.95])
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            router.register_mind(
                MindId("healthy"),
                model_path=Path("/fake/healthy.onnx"),
                config=config,
                verifier=_verifier_true,
            )
        # process_frame skips the broken detector, healthy fires.
        result = router.process_frame(_frame())
        assert result is not None
        assert result.mind_id == MindId("healthy")


# ── T8.7 — Lazy registration ─────────────────────────────────────────


class TestLazyRegistration:
    def test_router_does_not_construct_until_register_mind(self) -> None:
        """Constructing a router without registering any minds does
        NOT touch onnxruntime — operators with one active mind don't
        pay the construction cost for inactive minds."""
        # Patch the onnxruntime import as a tripwire — if WakeWordRouter
        # tries to construct a session at __init__ time, it'll fail
        # (sys.modules has no onnxruntime).
        with patch.dict("sys.modules", {"onnxruntime": MagicMock()}) as mocked:
            router = WakeWordRouter()
            assert router.is_empty
            # InferenceSession was NOT called at router construction.
            ort = mocked.get("onnxruntime")
            assert ort is not None
            # No .InferenceSession invocation yet.
            assert not ort.InferenceSession.called  # type: ignore[attr-defined]


# ── T8.8 — Per-mind independent cooldown ──────────────────────────────


class TestPerMindIndependentCooldown:
    def test_mind_a_cooldown_does_not_block_mind_b(self) -> None:
        """After mind A confirms (enters COOLDOWN), mind B can still
        confirm on a subsequent frame.

        Important — the router short-circuits after the first match
        in a single frame, so mind B does NOT see frame 1 audio
        (avoids spurious cross-mind detections on the same wake
        event). The independence we pin here is across DIFFERENT
        frames: aria's COOLDOWN on frame 2 does not suppress luna's
        IDLE→detection on frame 2.
        """
        router = WakeWordRouter()
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=_FRAME / 16000,
            cooldown_seconds=10 * _FRAME / 16000,  # 10-frame cooldown
        )
        # aria fires on frame 1, then enters COOLDOWN.
        # luna doesn't see frame 1 (short-circuit). luna's first
        # processed frame is frame 2 — its queued score 0.95 fires.
        for mind_id, scores in [
            ("aria", [0.95]),
            ("luna", [0.95]),
        ]:
            mock_ort = MagicMock()
            mock_ort.SessionOptions.return_value = MagicMock()
            mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
            mock_ort.InferenceSession.return_value = _mock_onnx_session(scores)
            with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
                router.register_mind(
                    MindId(mind_id),
                    model_path=Path(f"/fake/{mind_id}.onnx"),
                    config=config,
                    verifier=_verifier_true,
                )

        # Frame 1: aria fires (router short-circuits before luna).
        e1 = router.process_frame(_frame())
        assert e1 is not None
        assert e1.mind_id == MindId("aria")
        assert router.state_for(MindId("aria")) == WakeWordState.COOLDOWN
        # Luna never saw frame 1 (short-circuit) — still IDLE.
        assert router.state_for(MindId("luna")) == WakeWordState.IDLE

        # Frame 2: aria is in COOLDOWN (no detection), luna gets the
        # frame for the first time and fires.
        # Critical: aria's COOLDOWN does NOT prevent the router from
        # iterating to luna.
        e2 = router.process_frame(_frame())
        assert e2 is not None
        assert e2.mind_id == MindId("luna")


# ── T8.9 — Per-mind false-fire forwarding ─────────────────────────────


class TestPerMindFalseFireForwarding:
    def test_note_false_fire_routes_to_named_mind(self) -> None:
        """note_false_fire(mind_id) only touches that mind's detector."""
        router = WakeWordRouter()
        config = WakeWordConfig(cooldown_adaptive_enabled=True)
        for mind_id in ["aria", "luna"]:
            mock_ort = MagicMock()
            mock_ort.SessionOptions.return_value = MagicMock()
            mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
            mock_ort.InferenceSession.return_value = _mock_onnx_session([0.1])
            with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
                router.register_mind(
                    MindId(mind_id),
                    model_path=Path(f"/fake/{mind_id}.onnx"),
                    config=config,
                )

        # Forward 3 false-fires to aria only.
        for _ in range(3):
            router.note_false_fire(MindId("aria"))

        # aria has 3 false-fires; luna has 0.
        aria = router._detectors[MindId("aria")]  # noqa: SLF001
        luna = router._detectors[MindId("luna")]  # noqa: SLF001
        assert len(aria._false_fire_monotonics) == 3  # noqa: PLR2004, SLF001
        assert len(luna._false_fire_monotonics) == 0  # noqa: SLF001

    def test_note_false_fire_unknown_mind_silent(self) -> None:
        """Stale signal targeting an unregistered mind silently no-ops."""
        router = _make_router_with_mind("aria", [0.1])
        # Should not raise.
        router.note_false_fire(MindId("ghost-mind"))


class TestStateAccessor:
    def test_state_for_unregistered_returns_none(self) -> None:
        router = WakeWordRouter()
        assert router.state_for(MindId("aria")) is None

    def test_state_for_registered_returns_idle(self) -> None:
        router = _make_router_with_mind("aria", [0.1])
        assert router.state_for(MindId("aria")) == WakeWordState.IDLE


class TestOrchestratorIntegrationT810:
    """Pin the T8.10 orchestrator dispatch path.

    When the router is wired into ``VoicePipeline``, wake-word
    detections route through the router; the matched ``mind_id``
    flows into ``WakeWordDetectedEvent`` (instead of the static
    config mind_id); ``_current_mind_id`` is reset between turns
    via ``_clear_utterance_id``.
    """

    @pytest.mark.asyncio
    async def test_router_wired_pipeline_starts_clean(self) -> None:
        """A pipeline with a router but no minds registered behaves
        as a no-wake pipeline (router.is_empty → no detection)."""
        from unittest.mock import AsyncMock

        from sovyx.voice.pipeline._config import VoicePipelineConfig
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        empty_router = WakeWordRouter()
        config = VoicePipelineConfig(mind_id="default-mind")
        pipeline = VoicePipeline(
            config=config,
            vad=MagicMock(),
            wake_word=MagicMock(),
            stt=AsyncMock(),
            tts=AsyncMock(),
            wake_word_router=empty_router,
        )
        # Construction succeeds + per-turn mind_id starts at config.
        assert pipeline._current_mind_id == "default-mind"  # noqa: SLF001
        assert pipeline._wake_word_router is empty_router  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_router_match_overrides_mind_id(self) -> None:
        """When router matches, ``_current_mind_id`` is set to the
        matched mind for the duration of the turn."""
        from unittest.mock import AsyncMock

        import numpy as np

        from sovyx.voice.pipeline._config import VoicePipelineConfig
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        # Build router with one mind that fires immediately.
        config_ww = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=_FRAME / 16000,
        )
        router = _make_router_with_mind(
            "matched-mind",
            [0.95],
            config=config_ww,
            verifier=_verifier_true,
        )

        config = VoicePipelineConfig(mind_id="default-mind", wake_word_enabled=True)
        pipeline = VoicePipeline(
            config=config,
            vad=MagicMock(),
            wake_word=MagicMock(),
            stt=AsyncMock(),
            tts=AsyncMock(),
            wake_word_router=router,
        )
        await pipeline.start()
        try:
            # Feed a frame at IDLE with router → detection fires for matched-mind.
            from sovyx.voice.vad import VADEvent, VADState

            pipeline._vad.process_frame.return_value = VADEvent(  # noqa: SLF001
                is_speech=True,
                probability=0.95,
                state=VADState.SPEECH,
            )

            # WakeWordDetector requires 1280-sample frames. The
            # production pipeline feeds 512-sample frames but the
            # MagicMock detectors in the legacy integration tests
            # accept any shape. Our real router-wrapped detector
            # validates shape strictly.
            frame_int16 = np.zeros(1280, dtype=np.int16)
            await pipeline.feed_frame(frame_int16)

            # Router-driven mind_id override took effect.
            assert pipeline._current_mind_id == "matched-mind"  # noqa: SLF001
        finally:
            await pipeline.stop()

    @pytest.mark.asyncio
    async def test_clear_utterance_id_resets_current_mind_id(self) -> None:
        """After a turn ends (back to IDLE), ``_current_mind_id`` is
        reset to ``config.mind_id`` so the next turn's matched
        router event re-resolves cleanly."""
        from unittest.mock import AsyncMock

        from sovyx.voice.pipeline._config import VoicePipelineConfig
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        config = VoicePipelineConfig(mind_id="default-mind")
        pipeline = VoicePipeline(
            config=config,
            vad=MagicMock(),
            wake_word=MagicMock(),
            stt=AsyncMock(),
            tts=AsyncMock(),
        )
        # Manually flip current_mind_id (simulates a router match).
        pipeline._current_mind_id = "previous-turn-mind"  # noqa: SLF001
        # Reset path.
        pipeline._clear_utterance_id()  # noqa: SLF001
        assert pipeline._current_mind_id == "default-mind"  # noqa: SLF001


class TestResetAll:
    def test_reset_all_resets_every_detector(self) -> None:
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=_FRAME / 16000,
        )
        router = _make_router_with_mind(
            "aria",
            [0.95],
            config=config,
            verifier=_verifier_true,
        )
        # Trigger detection → COOLDOWN.
        result = router.process_frame(_frame())
        assert result is not None
        assert router.state_for(MindId("aria")) == WakeWordState.COOLDOWN

        router.reset_all()
        assert router.state_for(MindId("aria")) == WakeWordState.IDLE
