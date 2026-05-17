"""Unit tests for SupervisorMixin.request_soft_recovery.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§Phase 2 / Option D + §9.1 row "SupervisorMixin.request_clean_restart"
(REVISED to "request_soft_recovery" per the Phase 2 design RESOLVED
section).

Exercises the soft-recovery primitive in isolation: state-reset
chain, voice-axis clear from EngineDegradedStore, telemetry emit,
error-path returns failed RecoveryResult without raising.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass

import pytest

from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    reset_default_degraded_store,
)
from sovyx.voice.pipeline._supervisor_mixin import (
    SoftRecoveryResult,
    SupervisorMixin,
)


@pytest.fixture(autouse=True)
def _reset_store() -> Generator[None, None, None]:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


@dataclass
class _FakeConfig:
    mind_id: str = "default"


class _FakePipelineWithSupervisor(SupervisorMixin):
    """Minimal host that exposes the attributes SupervisorMixin
    reads/writes. Mirrors the VoicePipeline subset that matters for
    soft recovery."""

    def __init__(self) -> None:
        self._config = _FakeConfig(mind_id="test_mind")
        self._coordinator_terminated = True
        self._failover_ladder_exhausted = True
        self._deaf_warnings_consecutive = 3
        self._max_vad_prob_since_heartbeat = 0.001
        self._vad_frames_since_heartbeat = 63
        self._last_terminal_deaf_warn_monotonic = 12345.6
        self.reset_count = 0

    def reset_coordinator_after_failover(self) -> None:
        """Mirror of the real method at _orchestrator.py:668."""
        self.reset_count += 1
        self._coordinator_terminated = False
        self._deaf_warnings_consecutive = 0
        self._max_vad_prob_since_heartbeat = 0.0
        self._vad_frames_since_heartbeat = 0


class _FakePipelineRaising(_FakePipelineWithSupervisor):
    """Host whose reset_coordinator_after_failover raises — exercises
    the supervisor's error-swallowing contract."""

    def reset_coordinator_after_failover(self) -> None:
        raise RuntimeError("test_injected_failure")


class TestSupervisorMixinSoftRecovery:
    @pytest.mark.asyncio()
    async def test_returns_success_result(self) -> None:
        host = _FakePipelineWithSupervisor()
        result = await host.request_soft_recovery(reason="unit_test")
        assert isinstance(result, SoftRecoveryResult)
        assert result.success is True
        assert result.error_class == ""
        assert result.elapsed_ms >= 0

    @pytest.mark.asyncio()
    async def test_calls_reset_coordinator_after_failover(self) -> None:
        host = _FakePipelineWithSupervisor()
        assert host.reset_count == 0
        await host.request_soft_recovery(reason="unit_test")
        assert host.reset_count == 1

    @pytest.mark.asyncio()
    async def test_clears_ladder_exhausted_flag(self) -> None:
        host = _FakePipelineWithSupervisor()
        assert host._failover_ladder_exhausted is True
        await host.request_soft_recovery(reason="unit_test")
        assert host._failover_ladder_exhausted is False

    @pytest.mark.asyncio()
    async def test_clears_terminal_deaf_warn_throttle_clock(self) -> None:
        host = _FakePipelineWithSupervisor()
        assert host._last_terminal_deaf_warn_monotonic == 12345.6
        await host.request_soft_recovery(reason="unit_test")
        assert host._last_terminal_deaf_warn_monotonic == 0.0

    @pytest.mark.asyncio()
    async def test_clears_voice_axis_from_degraded_store(self) -> None:
        import time

        store = get_default_degraded_store()
        store.record(
            DegradedEntry(
                axis="voice",
                reason="failover_ladder_exhausted",
                severity="error",
                title_token="x",
                body_token="y",
                action_chips=(),
                metadata={},
                first_observed_monotonic=time.monotonic(),
                last_observed_monotonic=time.monotonic(),
                occurrence_count=1,
            ),
        )
        assert len(store) == 1

        host = _FakePipelineWithSupervisor()
        await host.request_soft_recovery(reason="unit_test")
        assert len(store) == 0

    @pytest.mark.asyncio()
    async def test_preserves_non_voice_axes(self) -> None:
        """Clear MUST be axis-scoped — LLM + STT axes survive."""
        import time

        store = get_default_degraded_store()
        for ax in ("voice", "llm", "stt"):
            store.record(
                DegradedEntry(
                    axis=ax,
                    reason=f"reason_{ax}",
                    severity="error",
                    title_token="x",
                    body_token="y",
                    action_chips=(),
                    metadata={},
                    first_observed_monotonic=time.monotonic(),
                    last_observed_monotonic=time.monotonic(),
                    occurrence_count=1,
                ),
            )
        host = _FakePipelineWithSupervisor()
        await host.request_soft_recovery(reason="unit_test")
        remaining = {e.axis for e in store.snapshot()}
        assert remaining == {"llm", "stt"}

    @pytest.mark.asyncio()
    async def test_error_path_returns_failed_result_without_raising(self) -> None:
        host = _FakePipelineRaising()
        result = await host.request_soft_recovery(reason="unit_test")
        assert result.success is False
        assert result.error_class == "RuntimeError"

    @pytest.mark.asyncio()
    async def test_idempotent_double_invocation(self) -> None:
        """Calling soft recovery twice in a row is safe — second call
        is a no-op state-wise (already reset)."""
        host = _FakePipelineWithSupervisor()
        await host.request_soft_recovery(reason="first")
        assert host.reset_count == 1
        await host.request_soft_recovery(reason="second")
        assert host.reset_count == 2
        assert host._coordinator_terminated is False
        assert host._failover_ladder_exhausted is False
