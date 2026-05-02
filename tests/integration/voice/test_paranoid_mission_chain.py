"""Voice Windows Paranoid Mission — T35 integration scenarios.

Mission spec: ``docs-internal/missions/MISSION-voice-windows-paranoid-2026-04-26.md``
Part 7 — Integration test scenarios.

Status of each scenario as of 2026-05-02:

* **Scenario 1 — Happy path (Tier 1 RAW)**: BLOCKED on T27
  (operator-deferred per ADR
  ``docs-internal/ADR-voice-tier1-raw-architectural-gap-2026-04-29.md``).
  ``WindowsRawCommunicationsBypass.apply`` is a flag-gated stub; the
  COM bindings + production logic land in a future mini-mission once
  the operator unblocks.

* **Scenario 2 — Tier 1 fails → Tier 2 host_api_rotate succeeds**:
  ✅ shipped here. T28 (`3e837ad`) shipped the Tier 2 production
  apply. This test exercises the coordinator's strategy iteration:
  Tier 1 raises ``BypassApplyError(reason="iaudioclient3_unsupported")``;
  the coordinator records ``FAILED_TO_APPLY`` and falls through to
  Tier 2 which applies cleanly. Coordinator-level fakes per the same
  pattern as ``tests/unit/voice/health/test_capture_integrity.py`` —
  no real COM bindings + no real PortAudio device list.

* **Scenario 3 — Mid-session device change**: BLOCKED on
  ``request_device_change_restart`` wire-up (out of scope per runtime
  listener mission Phase 2 — the IMM listener Phase 1b emits the
  structured event ``voice.default_capture_changed`` only; turning
  that into a CaptureRestartFrame requires the future restart wire-up).
  The event-emit path IS covered by
  ``tests/unit/voice/pipeline/test_orchestrator_listener_wireup.py``.

* **Scenario 4 — Cold probe rejects silent combo**: ✅ shipped at
  ``tests/unit/voice/health/test_probe.py::TestFuroW1UserReplay``
  (commit ``c888c2b``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.capture_integrity import CaptureIntegrityCoordinator
from sovyx.voice.health.contract import (
    BypassVerdict,
    Eligibility,
    IntegrityResult,
    IntegrityVerdict,
)

if TYPE_CHECKING:
    import numpy.typing as npt

    from sovyx.voice.health.contract import BypassContext


_SAMPLE_RATE = 16_000


# ── Fakes (mirror tests/unit/voice/health/test_capture_integrity.py shapes) ─


@dataclass
class _FakeCaptureTask:
    current_mark: tuple[int, int] = (1, 0)
    post_apply_frames: npt.NDArray[np.int16] = field(
        default_factory=lambda: np.zeros(0, dtype=np.int16),
    )
    tap_calls: list[tuple[tuple[int, int], int, float]] = field(default_factory=list)
    epoch_increments: int = 0

    @property
    def active_device_guid(self) -> str:
        return "guid-fake-paranoid-mission"

    @property
    def active_device_name(self) -> str:
        return "Realtek HD Audio (faked)"

    @property
    def active_device_index(self) -> int:
        return 1

    @property
    def active_device_kind(self) -> str:
        return "input"

    @property
    def host_api_name(self) -> str | None:
        # MME is the realistic source host_api for Tier 2 wire-up
        # (Voice Clarity APO bound to MME endpoints — the Razer + Win11
        # repro from sovyx.log uses DirectSound which Tier 2 rotates
        # away from too).
        return "MME"

    async def request_exclusive_restart(self) -> Any:  # pragma: no cover
        raise AssertionError("not exercised by Scenario 2")

    async def request_shared_restart(self) -> Any:  # pragma: no cover
        raise AssertionError("not exercised by Scenario 2")

    async def request_alsa_hw_direct_restart(self) -> Any:  # pragma: no cover
        raise AssertionError("Linux-only, not exercised here")

    async def request_session_manager_restart(
        self,
        target_device: Any | None = None,  # noqa: ARG002
    ) -> Any:  # pragma: no cover
        raise AssertionError("Linux-only, not exercised here")

    def samples_written_mark(self) -> tuple[int, int]:
        return self.current_mark

    async def tap_frames_since_mark(
        self,
        mark: tuple[int, int],
        min_samples: int,
        max_wait_s: float,
    ) -> npt.NDArray[np.int16]:
        self.tap_calls.append((mark, min_samples, max_wait_s))
        return self.post_apply_frames

    async def tap_recent_frames(self, duration_s: float) -> npt.NDArray[np.int16]:
        want = int(duration_s * _SAMPLE_RATE)
        if self.post_apply_frames.size == 0:
            return np.zeros(0, dtype=np.int16)
        return self.post_apply_frames[:want].copy()

    def apply_mic_ducking_db(self, gain_db: float) -> None:  # pragma: no cover
        pass

    # Helper used by the fake Tier 2 strategy below to simulate the
    # ring-buffer epoch increment that production rotation triggers.
    def simulate_epoch_increment(self) -> None:
        epoch, samples = self.current_mark
        self.current_mark = (epoch + 1, samples)
        self.epoch_increments += 1


@dataclass
class _FakeProbe:
    before: IntegrityResult
    after: IntegrityResult
    analyse_raw_calls: list[npt.NDArray[np.int16]] = field(default_factory=list)

    async def probe_warm(self, _capture: Any) -> IntegrityResult:
        return self.before

    async def analyse_raw(
        self,
        frames: npt.NDArray[np.int16],
        *,
        endpoint_guid: str,
    ) -> IntegrityResult:
        del endpoint_guid
        self.analyse_raw_calls.append(frames)
        return self.after


@dataclass
class _Tier1RawFakeStrategy:
    """Fake of the Tier 1 RAW bypass — apply always raises
    ``BypassApplyError(reason="iaudioclient3_unsupported")``.

    Mirrors what the production stub does in v0.24.0 + what the
    eventual production strategy will emit on hardware that doesn't
    support IAudioClient3 RAW mode (i.e. most consumer audio cards).
    """

    name: str = "win.raw_communications"

    async def probe_eligibility(self, _ctx: BypassContext) -> Eligibility:
        return Eligibility(applicable=True, reason="", estimated_cost_ms=0)

    async def apply(self, _ctx: BypassContext) -> str:
        raise BypassApplyError(
            "IAudioClient3 unsupported on this endpoint",
            reason="iaudioclient3_unsupported",
        )

    async def revert(self, _ctx: BypassContext) -> None:  # pragma: no cover
        # Never invoked — apply raised, no revert needed.
        pass


@dataclass
class _Tier2HostApiRotateFakeStrategy:
    """Fake of the Tier 2 host-API rotate-then-exclusive bypass —
    apply succeeds + bumps the fake capture task's ring-buffer epoch
    once (matching the production single-rotation contract per
    anti-pattern #29 + mission Risk #3)."""

    name: str = "win.host_api_rotate_then_exclusive"
    applied: bool = False
    reverted: bool = False

    async def probe_eligibility(self, _ctx: BypassContext) -> Eligibility:
        return Eligibility(applicable=True, reason="", estimated_cost_ms=0)

    async def apply(self, ctx: BypassContext) -> str:
        # Simulate the production contract: the rotation increments
        # the ring-buffer epoch exactly once (anti-pattern #29 emit-
        # before-increment ordering — the CaptureRestartFrame is
        # already recorded by the production strategy before this
        # method increments the epoch in the real code path).
        capture = ctx.capture_task
        if hasattr(capture, "simulate_epoch_increment"):
            capture.simulate_epoch_increment()
        self.applied = True
        return "rotated_then_exclusive_engaged"

    async def revert(self, _ctx: BypassContext) -> None:
        self.reverted = True


def _result(
    *,
    verdict: IntegrityVerdict,
    rolloff_hz: float = 0.0,
    rms_db: float = -30.0,
    vad_max: float = 0.0,
) -> IntegrityResult:
    return IntegrityResult(
        verdict=verdict,
        endpoint_guid="guid-fake-paranoid-mission",
        rms_db=rms_db,
        vad_max_prob=vad_max,
        spectral_flatness=0.2,
        spectral_rolloff_hz=rolloff_hz,
        duration_s=3.0,
        probed_at_utc=datetime.now(UTC),
        raw_frames=int(3.0 * _SAMPLE_RATE),
        detail="",
    )


# ── Scenario 2 — Tier 1 fails → Tier 2 host_api_rotate succeeds ─────


class TestScenario2Tier1FailsTier2Succeeds:
    """Mission Part 7 Scenario 2 — coordinator iterates strategies in
    order; Tier 1 RAW raises BypassApplyError, Tier 2 host_api_rotate
    applies cleanly + fixes the deaf signal.

    Coordinator-level integration: real ``CaptureIntegrityCoordinator``,
    real metric counters, fake strategies + fake capture task + fake
    probe. Validates the cross-strategy state machine end-to-end —
    this is where the v0.21.2 contamination bug used to live and where
    the mission's Tier 2 wire-up needs to be tested.
    """

    @pytest.mark.asyncio
    async def test_tier1_fails_falls_through_to_tier2(self) -> None:
        """Happy path of the fall-through: Tier 1 raises, Tier 2 applies,
        deaf signal resolves to HEALTHY."""
        # Pre-bypass probe says APO_DEGRADED (Voice Clarity sweep
        # signature: low rolloff + speech-band dead). Post-Tier-2
        # apply tap returns HEALTHY frames.
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=192.0)
        after = _result(verdict=IntegrityVerdict.HEALTHY, rolloff_hz=6_500.0, vad_max=0.55)
        post_apply_frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)

        capture = _FakeCaptureTask(post_apply_frames=post_apply_frames)
        probe = _FakeProbe(before=before, after=after)
        tier1 = _Tier1RawFakeStrategy()
        tier2 = _Tier2HostApiRotateFakeStrategy()

        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=[tier1, tier2],
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="windows",
            tuning=VoiceTuningConfig(),
        )

        outcomes = await coordinator.handle_deaf_signal()

        # Two outcomes recorded in order: Tier 1 failed-to-apply,
        # Tier 2 applied-healthy.
        assert len(outcomes) == 2, (
            f"expected 2 outcomes (tier1 fail + tier2 success), "
            f"got {len(outcomes)}: "
            f"{[(o.strategy_name, o.verdict.value) for o in outcomes]}"
        )

        first, second = outcomes
        assert first.strategy_name == "win.raw_communications"
        assert first.verdict is BypassVerdict.FAILED_TO_APPLY
        assert "iaudioclient3_unsupported" in first.detail, (
            "FAILED_TO_APPLY detail must carry the BypassApplyError reason "
            "verbatim — operator dashboards key on this string."
        )
        assert first.integrity_after is None, (
            "Failed apply must not record a post-state probe — the tap is "
            "skipped on the raise path (see capture_integrity.py:586)."
        )

        assert second.strategy_name == "win.host_api_rotate_then_exclusive"
        assert second.verdict is BypassVerdict.APPLIED_HEALTHY
        assert second.integrity_after is not None
        assert second.integrity_after.verdict is IntegrityVerdict.HEALTHY

        # Coordinator is resolved after a successful HEALTHY apply —
        # second deaf heartbeat must be a no-op (idempotency contract).
        assert coordinator.is_resolved is True
        second_call = await coordinator.handle_deaf_signal()
        assert second_call == [], (
            "post-resolution deaf signal must be a no-op; coordinator "
            "must NOT re-iterate strategies after a HEALTHY outcome."
        )

    @pytest.mark.asyncio
    async def test_ring_buffer_epoch_incremented_exactly_once(self) -> None:
        """Mission Risk #3: ring-buffer epoch++ during rotation must
        not produce a second increment + the consumer task tap must
        observe the post-apply mark exactly once. The fake Tier 2
        strategy increments the epoch inside ``apply``; we assert
        the count is 1, never 2."""
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=192.0)
        after = _result(verdict=IntegrityVerdict.HEALTHY, rolloff_hz=6_500.0, vad_max=0.55)
        capture = _FakeCaptureTask(
            post_apply_frames=np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16),
        )
        probe = _FakeProbe(before=before, after=after)
        tier1 = _Tier1RawFakeStrategy()
        tier2 = _Tier2HostApiRotateFakeStrategy()

        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=[tier1, tier2],
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="windows",
            tuning=VoiceTuningConfig(),
        )

        await coordinator.handle_deaf_signal()

        assert capture.epoch_increments == 1, (
            f"epoch must increment exactly once during the Tier 2 "
            f"rotation, got {capture.epoch_increments}. A second "
            f"increment indicates a redundant ring-buffer reset that "
            f"would surface NaN frames to the VAD consumer "
            f"(mission Risk #3)."
        )

    @pytest.mark.asyncio
    async def test_tap_only_called_for_successful_strategy(self) -> None:
        """The tap is invoked for the post-apply probe of EACH strategy
        that succeeded. Tier 1 raised (no tap); Tier 2 applied (tap
        called once). Validates capture_integrity.py:586 (continue
        skips tap on raise) + the Tier 2 success path's tap invocation.
        """
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=192.0)
        after = _result(verdict=IntegrityVerdict.HEALTHY, rolloff_hz=6_500.0, vad_max=0.55)
        capture = _FakeCaptureTask(
            post_apply_frames=np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16),
        )
        probe = _FakeProbe(before=before, after=after)
        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=[_Tier1RawFakeStrategy(), _Tier2HostApiRotateFakeStrategy()],
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="windows",
            tuning=VoiceTuningConfig(),
        )

        await coordinator.handle_deaf_signal()

        assert len(capture.tap_calls) == 1, (
            f"expected exactly 1 tap call (for Tier 2 post-apply probe), "
            f"got {len(capture.tap_calls)}. Tier 1 must not invoke the "
            f"tap because its apply raised before reaching the post-apply "
            f"probe stage (capture_integrity.py:586 continue)."
        )

    @pytest.mark.asyncio
    async def test_revert_not_called_on_either_strategy(self) -> None:
        """Tier 1 raised before applying — revert is unnecessary.
        Tier 2 applied + resolved HEALTHY — no revert needed
        (the v1.3 §4.2 L4-B revert path only fires when
        APPLIED_STILL_DEAD or improvement-heuristic fails).
        """
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=192.0)
        after = _result(verdict=IntegrityVerdict.HEALTHY, rolloff_hz=6_500.0, vad_max=0.55)
        capture = _FakeCaptureTask(
            post_apply_frames=np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16),
        )
        probe = _FakeProbe(before=before, after=after)
        tier1 = _Tier1RawFakeStrategy()
        tier2 = _Tier2HostApiRotateFakeStrategy()

        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=[tier1, tier2],
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="windows",
            tuning=VoiceTuningConfig(),
        )

        await coordinator.handle_deaf_signal()

        assert tier2.applied is True
        assert tier2.reverted is False, (
            "Tier 2 applied + resolved HEALTHY — revert must NOT fire. "
            "A spurious revert would undo the working bypass."
        )
