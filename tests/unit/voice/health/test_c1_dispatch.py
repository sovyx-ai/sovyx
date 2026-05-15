"""Mission C1 commit 4b — coordinator verdict-router dispatch + VAD-
frontend reset ladder + verdict-derived quarantine reason tests.

Covers §9.1 + §20.O test inventory from
``docs-internal/missions/MISSION-c1-vad-mute-reclassification-2026-05-14.md``:

* :class:`TestCoordinatorDispatch` — T1.3 verdict-router per-branch
  coverage + §20.A benign-skip resubmission + §20.B exhaustiveness.
* :class:`TestVADFrontendRecovery` — T1.4 ladder L1 + L3 success +
  exhaustion paths + §10 rollback knob + §20.D live-pipeline-VAD target.
* :class:`TestQuarantineReason` — T1.7 verdict→reason map + LENIENT
  dual-emit + None-verdict legacy fallback.
* :class:`TestMixinTerminalLatch` — §20.M T1.6.b verdict-classified
  latch predicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import numpy.typing as npt
import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.health._quarantine import EndpointQuarantine
from sovyx.voice.health._vad_frontend_recovery import VADFrontendRecovery
from sovyx.voice.health.capture_integrity import (
    _DEFAULT_QUARANTINE_REASON,
    _VERDICT_TO_QUARANTINE_REASON,
    CaptureIntegrityCoordinator,
)
from sovyx.voice.health.contract import (
    BypassContext,
    BypassOutcome,
    BypassVerdict,
    IntegrityResult,
    IntegrityVerdict,
    RmsSummary,
)
from sovyx.voice.pipeline._bypass_coordinator_mixin import (
    _is_terminal_outcome_set,
)

_SAMPLE_RATE = 16_000


@dataclass
class _FakeCaptureTask:
    """Stand-in for :class:`AudioCaptureTask` — minimal Protocol surface."""

    engage_calls: int = 0

    @property
    def active_device_guid(self) -> str:
        return "guid-fake"

    @property
    def active_device_name(self) -> str:
        return "Fake Mic"

    @property
    def active_device_index(self) -> int:
        return 1

    @property
    def active_device_kind(self) -> str:
        return "input"

    @property
    def host_api_name(self) -> str | None:
        return "ALSA"

    async def request_exclusive_restart(self) -> Any:  # pragma: no cover
        raise AssertionError("unused")

    async def request_shared_restart(self) -> Any:  # pragma: no cover
        raise AssertionError("unused")

    async def request_alsa_hw_direct_restart(self) -> Any:  # pragma: no cover
        raise AssertionError("unused")

    async def request_session_manager_restart(
        self,
        target_device: Any | None = None,  # noqa: ARG002
    ) -> Any:  # pragma: no cover
        raise AssertionError("unused")

    def samples_written_mark(self) -> tuple[int, int]:
        return (1, 0)

    async def tap_frames_since_mark(
        self,
        mark: tuple[int, int],  # noqa: ARG002
        min_samples: int,  # noqa: ARG002
        max_wait_s: float,  # noqa: ARG002
    ) -> npt.NDArray[np.int16]:
        return np.zeros(0, dtype=np.int16)

    async def tap_recent_frames(self, duration_s: float) -> npt.NDArray[np.int16]:  # noqa: ARG002
        return np.zeros(0, dtype=np.int16)

    def apply_mic_ducking_db(self, gain_db: float) -> None:  # pragma: no cover  # noqa: ARG002
        pass

    async def recent_rms_db_summary(
        self,
        seconds: float,  # noqa: ARG002
    ) -> RmsSummary:
        return RmsSummary.empty()

    async def engage_frame_normalizer(self) -> None:
        self.engage_calls += 1


@dataclass
class _ScriptedProbe:
    """Probe stub with a configurable sequence of probe_warm verdicts."""

    verdicts: list[IntegrityVerdict] = field(default_factory=list)
    calls: int = 0

    async def probe_warm(self, _capture: Any) -> IntegrityResult:  # noqa: ANN401
        idx = min(self.calls, len(self.verdicts) - 1)
        verdict = self.verdicts[idx]
        self.calls += 1
        return _make_result(verdict=verdict)

    async def analyse_raw(  # pragma: no cover — only legacy strategy path
        self,
        frames: npt.NDArray[np.int16],  # noqa: ARG002
        *,
        endpoint_guid: str,  # noqa: ARG002
    ) -> IntegrityResult:
        return _make_result(verdict=IntegrityVerdict.INCONCLUSIVE)


@dataclass
class _FakePipeline:
    """Stand-in for :class:`VoicePipeline` — only the ladder's surface."""

    reset_vad_calls: int = 0
    swap_vad_calls: int = 0

    async def reset_vad(self) -> None:
        self.reset_vad_calls += 1

    async def swap_vad(self, new_vad: Any) -> None:  # pragma: no cover  # noqa: ARG002, ANN401
        self.swap_vad_calls += 1


def _make_result(
    *,
    verdict: IntegrityVerdict,
    rms_db: float = -40.0,
    vad_max: float = 0.0,
) -> IntegrityResult:
    return IntegrityResult(
        verdict=verdict,
        endpoint_guid="guid-fake",
        rms_db=rms_db,
        vad_max_prob=vad_max,
        spectral_flatness=0.2,
        spectral_rolloff_hz=4000.0,
        duration_s=3.0,
        probed_at_utc=datetime.now(UTC),
        raw_frames=int(3.0 * _SAMPLE_RATE),
        detail="",
    )


def _make_coordinator(
    *,
    probe: _ScriptedProbe,
    capture: _FakeCaptureTask | None = None,
    pipeline: _FakePipeline | None = None,
    quarantine: EndpointQuarantine | None = None,
    tuning: VoiceTuningConfig | None = None,
) -> CaptureIntegrityCoordinator:
    return CaptureIntegrityCoordinator(
        probe=probe,  # type: ignore[arg-type]
        strategies=[],
        capture_task=capture or _FakeCaptureTask(),  # type: ignore[arg-type]
        platform_key="linux",
        tuning=tuning or VoiceTuningConfig(),
        pipeline_ref=pipeline,
        quarantine=quarantine,
    )


# ─────────────────────────────────────────────────────────────────────
# §9.1 / §20.O — Coordinator verdict-router dispatch
# ─────────────────────────────────────────────────────────────────────


class TestCoordinatorDispatch:
    """T1.3 verdict-router per-branch coverage."""

    @pytest.mark.asyncio()
    async def test_healthy_false_alarm_returns_empty_and_resolves(self) -> None:
        """HEALTHY pre-bypass probe → false_alarm path + terminal latch."""
        probe = _ScriptedProbe(verdicts=[IntegrityVerdict.HEALTHY])
        coordinator = _make_coordinator(probe=probe)

        outcomes = await coordinator.handle_deaf_signal()

        assert outcomes == []
        assert coordinator.is_resolved is True

    @pytest.mark.asyncio()
    async def test_vad_mute_benign_skip_does_not_resolve(self) -> None:
        """VAD_MUTE → benign-skip return; §20.A — _is_resolved must remain False."""
        probe = _ScriptedProbe(verdicts=[IntegrityVerdict.VAD_MUTE])
        coordinator = _make_coordinator(probe=probe)

        outcomes = await coordinator.handle_deaf_signal()

        assert outcomes == []
        assert coordinator.is_resolved is False

    @pytest.mark.asyncio()
    async def test_benign_skip_allows_resubmission(self) -> None:
        """§20.A — second handle_deaf_signal call after benign-skip still classifies.

        Pre-fix, ``_is_resolved=True`` on benign branches would
        permanently lock out subsequent legitimate deaf signals; the
        coordinator's one-shot contract is for TERMINAL verdicts only.
        """
        probe = _ScriptedProbe(
            verdicts=[IntegrityVerdict.VAD_MUTE, IntegrityVerdict.VAD_MUTE],
        )
        coordinator = _make_coordinator(probe=probe)

        first = await coordinator.handle_deaf_signal()
        second = await coordinator.handle_deaf_signal()

        assert first == []
        assert second == []
        assert probe.calls == 2  # not short-circuited by _is_resolved

    @pytest.mark.asyncio()
    async def test_driver_silent_dispatches_cascade_reevaluation(self) -> None:
        """DRIVER_SILENT → single CASCADE_REEVALUATION_REQUESTED outcome."""
        probe = _ScriptedProbe(verdicts=[IntegrityVerdict.DRIVER_SILENT])
        coordinator = _make_coordinator(probe=probe)

        outcomes = await coordinator.handle_deaf_signal()

        assert len(outcomes) == 1
        assert outcomes[0].verdict.value == "cascade_reevaluation_requested"
        assert outcomes[0].strategy_name == "coordinator_dispatch"
        assert coordinator.is_resolved is False

    @pytest.mark.asyncio()
    async def test_format_mismatch_dispatches_normalizer_engagement(self) -> None:
        """FORMAT_MISMATCH → single NORMALIZER_ENGAGEMENT_REQUESTED outcome."""
        probe = _ScriptedProbe(verdicts=[IntegrityVerdict.FORMAT_MISMATCH])
        coordinator = _make_coordinator(probe=probe)

        outcomes = await coordinator.handle_deaf_signal()

        assert len(outcomes) == 1
        assert outcomes[0].verdict.value == "normalizer_engagement_requested"
        assert coordinator.is_resolved is False

    @pytest.mark.asyncio()
    async def test_vad_frontend_dead_runs_ladder_and_recovers(self) -> None:
        """VAD_FRONTEND_DEAD + ladder L1 success → ladder healthy outcome + no quarantine."""
        # Sequence: pre-bypass (VAD_FRONTEND_DEAD), post-L1 reset (HEALTHY).
        probe = _ScriptedProbe(
            verdicts=[IntegrityVerdict.VAD_FRONTEND_DEAD, IntegrityVerdict.HEALTHY],
        )
        capture = _FakeCaptureTask()
        pipeline = _FakePipeline()
        quarantine = EndpointQuarantine(quarantine_s=60.0)
        coordinator = _make_coordinator(
            probe=probe,
            capture=capture,
            pipeline=pipeline,
            quarantine=quarantine,
        )

        outcomes = await coordinator.handle_deaf_signal()

        assert len(outcomes) == 1
        assert outcomes[0].verdict.value == "vad_frontend_reset_applied_healthy"
        assert pipeline.reset_vad_calls == 1
        # Ladder success is NON-terminal per §20.M T1.6.b — pipeline is
        # healthy again, future heartbeats welcome.
        assert coordinator.is_resolved is False
        # No quarantine on recovery.
        assert quarantine.snapshot() == ()

    @pytest.mark.asyncio()
    async def test_vad_frontend_dead_ladder_exhausts_quarantines(self) -> None:
        """Ladder all-fail → coordinator quarantines with derived_reason."""
        # Sequence: pre-bypass (VAD_FRONTEND_DEAD), post-L1 (VAD_MUTE
        # still dead), post-L3 (VAD_MUTE still dead).
        probe = _ScriptedProbe(
            verdicts=[
                IntegrityVerdict.VAD_FRONTEND_DEAD,
                IntegrityVerdict.VAD_MUTE,
                IntegrityVerdict.VAD_MUTE,
            ],
        )
        capture = _FakeCaptureTask()
        pipeline = _FakePipeline()
        quarantine = EndpointQuarantine(quarantine_s=60.0)
        coordinator = _make_coordinator(
            probe=probe,
            capture=capture,
            pipeline=pipeline,
            quarantine=quarantine,
        )

        outcomes = await coordinator.handle_deaf_signal()

        # Two ladder steps attempted, both STILL_DEAD.
        assert len(outcomes) == 2
        assert all(o.verdict.value == "vad_frontend_reset_applied_still_dead" for o in outcomes)
        # Exhaustion is terminal.
        assert coordinator.is_resolved is True
        # Quarantined with derived reason.
        snap = quarantine.snapshot()
        assert len(snap) == 1
        assert snap[0].derived_reason == "vad_frontend_dead"

    @pytest.mark.asyncio()
    async def test_apo_degraded_falls_through_to_strategy_iteration(self) -> None:
        """APO_DEGRADED → existing strategy iteration path runs (no early dispatch)."""
        # No strategies registered → loop exits immediately → quarantine
        # fires with terminal_verdict=APO_DEGRADED.
        probe = _ScriptedProbe(verdicts=[IntegrityVerdict.APO_DEGRADED])
        quarantine = EndpointQuarantine(quarantine_s=60.0)
        coordinator = _make_coordinator(probe=probe, quarantine=quarantine)

        outcomes = await coordinator.handle_deaf_signal()

        assert outcomes == []
        assert coordinator.is_resolved is True
        snap = quarantine.snapshot()
        assert len(snap) == 1
        assert snap[0].derived_reason == "apo_degraded"


# ─────────────────────────────────────────────────────────────────────
# T1.4 — VAD-frontend recovery ladder
# ─────────────────────────────────────────────────────────────────────


class TestVADFrontendRecovery:
    """T1.4 ladder behaviour + §20 audit closures."""

    def _make_context(self, pipeline: _FakePipeline | None) -> BypassContext:
        capture = _FakeCaptureTask()

        async def _probe_fn() -> IntegrityResult:
            return _make_result(verdict=IntegrityVerdict.HEALTHY)

        return BypassContext(
            endpoint_guid="guid-fake",
            endpoint_friendly_name="Fake Mic",
            host_api_name="ALSA",
            platform_key="linux",
            capture_task=capture,  # type: ignore[arg-type]
            probe_fn=_probe_fn,
            current_device_index=1,
            current_device_kind="input",
            pipeline_ref=pipeline,
        )

    @pytest.mark.asyncio()
    async def test_l1_silero_reset_success(self) -> None:
        """L1 reset_vad → post-step HEALTHY → ladder terminates with success."""
        probe = _ScriptedProbe(verdicts=[IntegrityVerdict.HEALTHY])
        capture = _FakeCaptureTask()
        pipeline = _FakePipeline()
        ladder = VADFrontendRecovery(
            probe=probe,  # type: ignore[arg-type]
            capture_task=capture,  # type: ignore[arg-type]
            tuning=VoiceTuningConfig(),
        )

        outcomes = await ladder.run(
            self._make_context(pipeline),
            _make_result(verdict=IntegrityVerdict.VAD_FRONTEND_DEAD),
        )

        assert len(outcomes) == 1
        assert outcomes[0].verdict.value == "vad_frontend_reset_applied_healthy"
        assert outcomes[0].strategy_name == "vad_frontend_reset:silero_reset"
        assert pipeline.reset_vad_calls == 1

    @pytest.mark.asyncio()
    async def test_l1_fail_l3_succeeds(self) -> None:
        """L1 reset_vad still dead → L3 normalizer_engage → HEALTHY → terminates."""
        probe = _ScriptedProbe(
            verdicts=[IntegrityVerdict.VAD_FRONTEND_DEAD, IntegrityVerdict.HEALTHY],
        )
        capture = _FakeCaptureTask()
        pipeline = _FakePipeline()
        ladder = VADFrontendRecovery(
            probe=probe,  # type: ignore[arg-type]
            capture_task=capture,  # type: ignore[arg-type]
            tuning=VoiceTuningConfig(),
        )

        outcomes = await ladder.run(
            self._make_context(pipeline),
            _make_result(verdict=IntegrityVerdict.VAD_FRONTEND_DEAD),
        )

        assert len(outcomes) == 2
        assert outcomes[0].verdict.value == "vad_frontend_reset_applied_still_dead"
        assert outcomes[1].verdict.value == "vad_frontend_reset_applied_healthy"
        assert outcomes[1].strategy_name == "vad_frontend_reset:normalizer_engage"
        assert pipeline.reset_vad_calls == 1
        assert capture.engage_calls == 1

    @pytest.mark.asyncio()
    async def test_exhaustion_returns_all_still_dead(self) -> None:
        """Every step fails → outcomes are all STILL_DEAD."""
        probe = _ScriptedProbe(
            verdicts=[
                IntegrityVerdict.VAD_FRONTEND_DEAD,
                IntegrityVerdict.VAD_FRONTEND_DEAD,
            ],
        )
        capture = _FakeCaptureTask()
        pipeline = _FakePipeline()
        ladder = VADFrontendRecovery(
            probe=probe,  # type: ignore[arg-type]
            capture_task=capture,  # type: ignore[arg-type]
            tuning=VoiceTuningConfig(),
        )

        outcomes = await ladder.run(
            self._make_context(pipeline),
            _make_result(verdict=IntegrityVerdict.VAD_FRONTEND_DEAD),
        )

        assert len(outcomes) == 2
        assert all(o.verdict.value == "vad_frontend_reset_applied_still_dead" for o in outcomes)

    @pytest.mark.asyncio()
    async def test_rollback_knob_disables_ladder(self) -> None:
        """§10 rollback — vad_frontend_reset_enabled=False → empty outcomes."""
        tuning = VoiceTuningConfig(vad_frontend_reset_enabled=False)
        probe = _ScriptedProbe(verdicts=[IntegrityVerdict.HEALTHY])
        capture = _FakeCaptureTask()
        pipeline = _FakePipeline()
        ladder = VADFrontendRecovery(
            probe=probe,  # type: ignore[arg-type]
            capture_task=capture,  # type: ignore[arg-type]
            tuning=tuning,
        )

        outcomes = await ladder.run(
            self._make_context(pipeline),
            _make_result(verdict=IntegrityVerdict.VAD_FRONTEND_DEAD),
        )

        assert outcomes == []
        assert pipeline.reset_vad_calls == 0

    @pytest.mark.asyncio()
    async def test_missing_pipeline_ref_returns_empty(self) -> None:
        """§20.D — ladder without pipeline_ref emits warning and returns []."""
        probe = _ScriptedProbe(verdicts=[IntegrityVerdict.HEALTHY])
        capture = _FakeCaptureTask()
        ladder = VADFrontendRecovery(
            probe=probe,  # type: ignore[arg-type]
            capture_task=capture,  # type: ignore[arg-type]
            tuning=VoiceTuningConfig(),
        )

        outcomes = await ladder.run(
            self._make_context(pipeline=None),
            _make_result(verdict=IntegrityVerdict.VAD_FRONTEND_DEAD),
        )

        assert outcomes == []

    @pytest.mark.asyncio()
    async def test_resets_live_pipeline_vad_not_probe(self) -> None:
        """§20.D — ladder L1 calls pipeline.reset_vad, NOT probe's VAD.

        The pipeline ref captured in :class:`BypassContext` MUST be the
        LIVE :class:`VoicePipeline` so the recovery acts on the
        actual inference instance, not the probe's separate VAD.
        """
        probe = _ScriptedProbe(verdicts=[IntegrityVerdict.HEALTHY])
        probe_reset_calls = 0  # noqa: F841 — sentinel; probe has no reset_vad

        capture = _FakeCaptureTask()
        pipeline = _FakePipeline()
        ladder = VADFrontendRecovery(
            probe=probe,  # type: ignore[arg-type]
            capture_task=capture,  # type: ignore[arg-type]
            tuning=VoiceTuningConfig(),
        )

        await ladder.run(
            self._make_context(pipeline),
            _make_result(verdict=IntegrityVerdict.VAD_FRONTEND_DEAD),
        )

        # The PIPELINE's reset_vad was called.
        assert pipeline.reset_vad_calls == 1
        # The probe's VAD instance is independent (we don't poke it
        # from the ladder); the test asserts the call surface only
        # touches pipeline_ref.

    @pytest.mark.asyncio()
    async def test_step_crash_records_still_dead_outcome(self) -> None:
        """A crashing step yields a STILL_DEAD outcome with detail tag."""
        probe = _ScriptedProbe(verdicts=[IntegrityVerdict.HEALTHY])
        capture = _FakeCaptureTask()
        pipeline = _FakePipeline()
        # Force reset_vad to raise.
        pipeline.reset_vad = AsyncMock(side_effect=RuntimeError("session_crashed"))  # type: ignore[method-assign]
        ladder = VADFrontendRecovery(
            probe=probe,  # type: ignore[arg-type]
            capture_task=capture,  # type: ignore[arg-type]
            tuning=VoiceTuningConfig(),
        )

        outcomes = await ladder.run(
            self._make_context(pipeline),
            _make_result(verdict=IntegrityVerdict.VAD_FRONTEND_DEAD),
        )

        # L1 crashed → STILL_DEAD; L3 then attempted (HEALTHY now).
        assert len(outcomes) == 2
        assert outcomes[0].verdict.value == "vad_frontend_reset_applied_still_dead"
        assert "RuntimeError" in outcomes[0].detail


# ─────────────────────────────────────────────────────────────────────
# T1.7 — Quarantine reason verdict-driven
# ─────────────────────────────────────────────────────────────────────


class TestQuarantineReason:
    """T1.7 verdict→reason map + LENIENT dual-emit semantics."""

    def test_verdict_to_reason_map_covers_terminal_verdicts(self) -> None:
        """Every terminal :class:`IntegrityVerdict` has a derived reason."""
        # Verdicts that may reach :meth:`_quarantine_endpoint` as a
        # terminal verdict. HEALTHY/VAD_MUTE/INCONCLUSIVE never reach
        # quarantine and are intentionally absent.
        expected_keys = {
            IntegrityVerdict.APO_DEGRADED,
            IntegrityVerdict.DRIVER_SILENT,
            IntegrityVerdict.VAD_FRONTEND_DEAD,
            IntegrityVerdict.FORMAT_MISMATCH,
        }
        assert set(_VERDICT_TO_QUARANTINE_REASON.keys()) == expected_keys

    def test_reason_values_are_low_cardinality_strings(self) -> None:
        """Derived reasons are low-cardinality snake_case strings."""
        for verdict, reason in _VERDICT_TO_QUARANTINE_REASON.items():
            assert reason == verdict.value, f"map drift: {verdict.value} → {reason!r}"

    def test_default_reason_is_legacy_apo_degraded(self) -> None:
        """Unknown / None verdict falls back to the legacy default."""
        assert _DEFAULT_QUARANTINE_REASON == "apo_degraded"

    @pytest.mark.asyncio()
    async def test_vad_frontend_dead_quarantine_uses_derived_reason(self) -> None:
        """Terminal VAD_FRONTEND_DEAD verdict → derived_reason on entry."""
        probe = _ScriptedProbe(
            verdicts=[
                IntegrityVerdict.VAD_FRONTEND_DEAD,
                IntegrityVerdict.VAD_FRONTEND_DEAD,
                IntegrityVerdict.VAD_FRONTEND_DEAD,
            ],
        )
        capture = _FakeCaptureTask()
        pipeline = _FakePipeline()
        quarantine = EndpointQuarantine(quarantine_s=60.0)
        coordinator = _make_coordinator(
            probe=probe,
            capture=capture,
            pipeline=pipeline,
            quarantine=quarantine,
        )

        await coordinator.handle_deaf_signal()

        snap = quarantine.snapshot()
        assert len(snap) == 1
        # Mission C1 LENIENT — legacy reason preserved.
        assert snap[0].reason == "apo_degraded"
        # Verdict-derived reason is the new authority.
        assert snap[0].derived_reason == "vad_frontend_dead"

    @pytest.mark.asyncio()
    async def test_apo_degraded_quarantine_keeps_legacy_reason(self) -> None:
        """APO_DEGRADED falls through to strategy iteration; derived = legacy."""
        probe = _ScriptedProbe(verdicts=[IntegrityVerdict.APO_DEGRADED])
        quarantine = EndpointQuarantine(quarantine_s=60.0)
        coordinator = _make_coordinator(probe=probe, quarantine=quarantine)

        await coordinator.handle_deaf_signal()

        snap = quarantine.snapshot()
        assert len(snap) == 1
        assert snap[0].reason == "apo_degraded"
        assert snap[0].derived_reason == "apo_degraded"


# ─────────────────────────────────────────────────────────────────────
# §20.M T1.6.b — Mixin terminal-latch verdict classification
# ─────────────────────────────────────────────────────────────────────


def _outcome(verdict: BypassVerdict) -> BypassOutcome:
    return BypassOutcome(
        strategy_name="x",
        attempt_index=0,
        verdict=verdict,
        integrity_before=_make_result(verdict=IntegrityVerdict.VAD_FRONTEND_DEAD),
        integrity_after=None,
        elapsed_ms=0.0,
        detail="",
    )


class TestMixinTerminalLatch:
    """§20.M T1.6.b — verdict-classified terminal-latch predicate."""

    def test_empty_outcomes_not_terminal(self) -> None:
        """Empty outcomes never latch — coordinator short-circuited."""
        assert _is_terminal_outcome_set([]) is False

    def test_applied_healthy_is_terminal(self) -> None:
        """Legacy APPLIED_HEALTHY latches terminal."""
        assert _is_terminal_outcome_set([_outcome(BypassVerdict.APPLIED_HEALTHY)]) is True

    def test_ladder_healthy_is_not_terminal(self) -> None:
        """VAD_FRONTEND_RESET_APPLIED_HEALTHY does NOT latch — pipeline
        is healthy again, future heartbeats welcome."""
        assert (
            _is_terminal_outcome_set(
                [_outcome(BypassVerdict.VAD_FRONTEND_RESET_APPLIED_HEALTHY)],
            )
            is False
        )

    def test_dispatch_outcomes_not_terminal(self) -> None:
        """§20.E — CASCADE/NORMALIZER request outcomes do NOT latch.

        Pre-fix these benign dispatch requests latched and emitted
        ``voice_apo_bypass_ineffective`` — the wrong dashboard signal.
        """
        assert (
            _is_terminal_outcome_set(
                [_outcome(BypassVerdict.CASCADE_REEVALUATION_REQUESTED)],
            )
            is False
        )
        assert (
            _is_terminal_outcome_set(
                [_outcome(BypassVerdict.NORMALIZER_ENGAGEMENT_REQUESTED)],
            )
            is False
        )

    def test_all_not_applicable_is_terminal(self) -> None:
        """T6.15 — all strategies NOT_APPLICABLE latches terminal."""
        outcomes = [_outcome(BypassVerdict.NOT_APPLICABLE)] * 3
        assert _is_terminal_outcome_set(outcomes) is True

    def test_ladder_exhausted_is_terminal(self) -> None:
        """Every outcome STILL_DEAD or RESET_STILL_DEAD → terminal."""
        outcomes = [
            _outcome(BypassVerdict.VAD_FRONTEND_RESET_APPLIED_STILL_DEAD),
            _outcome(BypassVerdict.VAD_FRONTEND_RESET_APPLIED_STILL_DEAD),
        ]
        assert _is_terminal_outcome_set(outcomes) is True

    def test_mixed_strategy_still_dead_is_terminal(self) -> None:
        """Strategy iteration exhausted (all APPLIED_STILL_DEAD) → terminal."""
        outcomes = [
            _outcome(BypassVerdict.APPLIED_STILL_DEAD),
            _outcome(BypassVerdict.APPLIED_STILL_DEAD),
        ]
        assert _is_terminal_outcome_set(outcomes) is True

    def test_ladder_healthy_overrides_other_still_dead(self) -> None:
        """ANY ladder-healthy outcome wins the non-terminal classification."""
        outcomes = [
            _outcome(BypassVerdict.VAD_FRONTEND_RESET_APPLIED_STILL_DEAD),
            _outcome(BypassVerdict.VAD_FRONTEND_RESET_APPLIED_HEALTHY),
        ]
        assert _is_terminal_outcome_set(outcomes) is False
