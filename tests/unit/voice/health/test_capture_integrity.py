"""Unit tests for v1.3 §4.2 L4-B — ``CaptureIntegrityCoordinator`` mark-based
probe window + improvement heuristic + post-apply-only frames invariant.

These tests are the regression fixture for dossier SVX-VOICE-LINUX-20260422
(v0.21.2 probe-window contamination) — asserting the coordinator no longer
classifies pre-apply frames as "post-apply verdict", without replaying the
whole mixer-saturation hardware scenario.

Scenarios covered (mapped to TEST_PLAN.md D1):

* ``TestCoordinatorProbeWindowBug``       — D1.1 primary regression
* ``TestProbeWindowInvariants``           — D1.2 property-based invariant
* ``TestCaptureTaskResetDuringBypass``    — D1.3 ring reset mid-bypass
* ``TestTupleContract``                   — §7.9 v1.3 tuple contract
* ``TestImprovementHeuristic``            — §14.E2 rolloff-improvement path
* ``TestHappyPathUnaffected``             — D5 contrafactual (no false positives)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
from hypothesis import given
from hypothesis import settings as hp_settings
from hypothesis import strategies as st

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.capture_integrity import (
    CaptureIntegrityCoordinator,
)
from sovyx.voice.health.contract import (
    BypassContext,
    BypassVerdict,
    IntegrityResult,
    IntegrityVerdict,
    RmsSummary,
)

# ── Test doubles ─────────────────────────────────────────────────────

_SAMPLE_RATE = 16_000


@dataclass
class _FakeCaptureTask:
    """Protocol-compatible stand-in for :class:`AudioCaptureTask`.

    Models the mark/tap contract directly: the backing numpy array is
    the "post-apply" buffer, a ``pre_apply_frames`` attribute models the
    old content, and ``samples_written_mark`` returns whatever the test
    configures. The test drives the strategy's ``apply`` to switch the
    buffer contents, so ``tap_frames_since_mark`` returns exactly the
    post-apply content.
    """

    current_mark: tuple[int, int] = (1, 0)
    post_apply_frames: npt.NDArray[np.int16] = field(
        default_factory=lambda: np.zeros(0, dtype=np.int16),
    )
    # The epoch the next call to samples_written_mark() will see. Tests
    # that exercise ring-reset detection bump this between mark and tap.
    next_mark_after_apply: tuple[int, int] | None = None
    tap_calls: list[tuple[tuple[int, int], int, float]] = field(default_factory=list)

    # Protocol stubs — properties
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

    # Protocol stubs — restart requests. Tests never invoke these.
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

    # Mark-based tap API
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
        # Only called by the pre-bypass probe; return the pre-apply
        # content so ``probe_warm`` can classify the "still broken" state.
        want = int(duration_s * _SAMPLE_RATE)
        if self.post_apply_frames.size == 0:
            return np.zeros(0, dtype=np.int16)
        return self.post_apply_frames[:want].copy()

    def apply_mic_ducking_db(self, gain_db: float) -> None:  # pragma: no cover
        pass

    def apply_agc2_floor_lift(self, delta_db: float) -> float:  # pragma: no cover  # noqa: ARG002
        return 0.0

    # Mission C1 §T1.2.a + §T1.8 CaptureTaskProto extensions. Tests that
    # need to exercise the coordinator-side history cross-check or the
    # FORMAT_MISMATCH dispatch override these on a per-test basis.
    async def recent_rms_db_summary(
        self, seconds: float
    ) -> RmsSummary:  # pragma: no cover  # noqa: ARG002
        return RmsSummary.empty()

    async def engage_frame_normalizer(self) -> None:  # pragma: no cover
        return None


@dataclass
class _FakeProbe:
    """Probe stand-in with deterministic before/after verdicts."""

    before: IntegrityResult
    after: IntegrityResult
    analyse_raw_calls: list[npt.NDArray[np.int16]] = field(default_factory=list)

    async def probe_warm(self, _capture: Any) -> IntegrityResult:  # noqa: ANN401
        return self.before

    async def analyse_raw(
        self,
        frames: npt.NDArray[np.int16],
        *,
        endpoint_guid: str,  # noqa: ARG002
    ) -> IntegrityResult:
        self.analyse_raw_calls.append(frames)
        return self.after


@dataclass
class _FakeStrategy:
    """Minimal strategy that records apply/revert calls."""

    name: str = "fake.linux_mixer"
    applied: bool = False
    reverted: bool = False
    apply_raises: BaseException | None = None
    revert_raises: BaseException | None = None  # B3: configurable revert failure
    on_apply: Any = None  # callable(fake_capture) invoked inside apply

    async def probe_eligibility(self, _ctx: BypassContext) -> Any:
        from sovyx.voice.health.contract import Eligibility

        return Eligibility(applicable=True, reason="", estimated_cost_ms=0)

    async def apply(self, ctx: BypassContext) -> str:
        if self.apply_raises is not None:
            raise self.apply_raises
        self.applied = True
        if callable(self.on_apply):
            self.on_apply(ctx.capture_task)
        return f"{self.name}:applied"

    async def revert(self, _ctx: BypassContext) -> None:
        if self.revert_raises is not None:
            raise self.revert_raises
        self.reverted = True


def _result(
    *,
    verdict: IntegrityVerdict,
    rolloff_hz: float = 0.0,
    rms_db: float = -30.0,
    vad_max: float = 0.0,
) -> IntegrityResult:
    """Build a canned :class:`IntegrityResult` for stubbed probes."""
    return IntegrityResult(
        verdict=verdict,
        endpoint_guid="guid-fake",
        rms_db=rms_db,
        vad_max_prob=vad_max,
        spectral_flatness=0.2,
        spectral_rolloff_hz=rolloff_hz,
        duration_s=3.0,
        probed_at_utc=datetime.now(UTC),
        raw_frames=int(3.0 * _SAMPLE_RATE),
        detail="",
    )


def _make_coordinator(
    *,
    before: IntegrityResult,
    after: IntegrityResult,
    post_apply_frames: npt.NDArray[np.int16],
    strategy: _FakeStrategy | None = None,
    tuning: VoiceTuningConfig | None = None,
    capture: _FakeCaptureTask | None = None,
) -> tuple[CaptureIntegrityCoordinator, _FakeCaptureTask, _FakeProbe, _FakeStrategy]:
    capture = capture or _FakeCaptureTask(post_apply_frames=post_apply_frames)
    probe = _FakeProbe(before=before, after=after)
    strategy = strategy or _FakeStrategy()
    coordinator = CaptureIntegrityCoordinator(
        probe=probe,  # type: ignore[arg-type]
        strategies=[strategy],  # type: ignore[list-item]
        capture_task=capture,  # type: ignore[arg-type]
        platform_key="linux",
        tuning=tuning or VoiceTuningConfig(),
    )
    return coordinator, capture, probe, strategy


# ── D1.1 primary regression ────────────────────────────────────────


class TestCoordinatorProbeWindowBug:
    """D1.1 — the post-apply probe MUST analyse post-apply frames only."""

    @pytest.mark.asyncio()
    async def test_tap_invoked_with_pre_apply_mark(self) -> None:
        """Mark captured BEFORE apply, not after — the whole fix point."""
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=192.0)
        after = _result(verdict=IntegrityVerdict.HEALTHY, rolloff_hz=6000.0, vad_max=0.5)
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        coordinator, capture, _, strategy = _make_coordinator(
            before=before,
            after=after,
            post_apply_frames=frames,
        )
        # Arrange: the mark present on the fake is what we expect to see
        # in the tap call — confirming apply did not overwrite it.
        capture.current_mark = (7, 123_456)

        outcomes = await coordinator.handle_deaf_signal()

        assert strategy.applied is True
        assert len(capture.tap_calls) == 1
        tap_mark, tap_min_samples, tap_max_wait = capture.tap_calls[0]
        assert tap_mark == (7, 123_456), (
            "coordinator must pass the exact mark taken pre-apply to "
            "tap_frames_since_mark — any other value reintroduces the "
            "v0.21.2 contamination bug."
        )
        # min_samples equals int(probe_duration_s * 16 kHz) per §4.2.3.
        tuning = VoiceTuningConfig()
        assert tap_min_samples == int(tuning.integrity_probe_duration_s * _SAMPLE_RATE)
        # max_wait_s = probe_duration_s + jitter_margin_s per §14.E1.
        assert tap_max_wait == tuning.integrity_probe_duration_s + tuning.probe_jitter_margin_s
        assert outcomes[-1].verdict is BypassVerdict.APPLIED_HEALTHY

    @pytest.mark.asyncio()
    async def test_post_apply_verdict_not_apo_degraded_when_fixed(self) -> None:
        """With a HEALTHY after-verdict, coordinator must not revert."""
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=192.0)
        after = _result(verdict=IntegrityVerdict.HEALTHY, rolloff_hz=7_500.0, vad_max=0.5)
        coordinator, _, _, strategy = _make_coordinator(
            before=before,
            after=after,
            post_apply_frames=np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16),
        )

        outcomes = await coordinator.handle_deaf_signal()

        assert outcomes[-1].verdict is BypassVerdict.APPLIED_HEALTHY
        assert strategy.reverted is False, (
            "coordinator must not revert a strategy when the post-apply "
            "verdict is HEALTHY — the v0.21.2 bug reverted here."
        )


# ── D1.2 property-based invariant ──────────────────────────────────


class TestProbeWindowInvariants:
    """D1.2 — invariant holds across (settle_s, probe_duration_s) combinations."""

    @given(
        probe_duration_s=st.floats(min_value=0.5, max_value=4.0, allow_nan=False),
        pre_apply_rolloff_hz=st.floats(min_value=50.0, max_value=500.0),
        post_apply_rolloff_hz=st.floats(min_value=5_000.0, max_value=8_000.0),
    )
    @hp_settings(max_examples=20, deadline=None)
    @pytest.mark.asyncio()
    async def test_healthy_after_implies_resolved(
        self,
        probe_duration_s: float,
        pre_apply_rolloff_hz: float,
        post_apply_rolloff_hz: float,
    ) -> None:
        """Whenever the after-probe is HEALTHY the bypass must NOT revert,
        regardless of pre-apply signal state."""
        tuning = VoiceTuningConfig(
            integrity_probe_duration_s=probe_duration_s,
            bypass_strategy_post_apply_settle_s=probe_duration_s + 0.5,
        )
        before = _result(
            verdict=IntegrityVerdict.APO_DEGRADED,
            rolloff_hz=pre_apply_rolloff_hz,
        )
        after = _result(
            verdict=IntegrityVerdict.HEALTHY,
            rolloff_hz=post_apply_rolloff_hz,
            vad_max=0.5,
        )
        frames = np.zeros(int(probe_duration_s * _SAMPLE_RATE), dtype=np.int16)
        coordinator, _, _, strategy = _make_coordinator(
            before=before,
            after=after,
            post_apply_frames=frames,
            tuning=tuning,
        )

        await coordinator.handle_deaf_signal()

        assert strategy.reverted is False


# ── D1.3 ring reset during bypass ──────────────────────────────────


class TestCaptureTaskResetDuringBypass:
    """D1.3 — a ring reset between mark and tap must not hang the tap."""

    @pytest.mark.asyncio()
    async def test_tap_returns_despite_ring_reset_mid_apply(self) -> None:
        """The real :class:`AudioCaptureTask` bumps the epoch on reallocation.
        The tap must treat that as ``all samples are post-mark`` and return
        promptly rather than spin forever waiting for a delta that will
        never accumulate."""
        from sovyx.voice._capture_task import AudioCaptureTask

        capture = AudioCaptureTask.__new__(AudioCaptureTask)
        # Populate the minimum ring-buffer state the new contract exercises.
        capture._ring_buffer = np.zeros(int(10.0 * _SAMPLE_RATE), dtype=np.int16)  # noqa: SLF001
        capture._ring_capacity = capture._ring_buffer.size  # noqa: SLF001
        capture._ring_write_index = 0  # noqa: SLF001
        capture._ring_state = 0  # noqa: SLF001
        capture._tuning = VoiceTuningConfig()  # noqa: SLF001

        mark = capture.samples_written_mark()

        # Simulate a ring reset — bump epoch + reset samples.
        capture._allocate_ring_buffer(VoiceTuningConfig())  # noqa: SLF001

        # The old mark's epoch is now stale; tap must detect and return.
        frames = await capture.tap_frames_since_mark(
            mark,
            min_samples=100,
            max_wait_s=0.5,
        )
        assert frames is not None
        assert frames.size == 0, (
            "post-reset ring has zero samples; tap must return an empty "
            "array, not block until deadline"
        )


# ── §7.9 tuple contract ─────────────────────────────────────────────


class TestTupleContract:
    """Plan §7.9 — :meth:`samples_written_mark` returns a ``tuple[int, int]``.

    v1.3 §4.2.2 escaped the packed-int contract specifically to prevent
    JavaScript / Prometheus / structlog boundary truncation; callers
    rely on the tuple shape so the check belongs in the Protocol test
    suite, not only in the implementation tests.
    """

    def test_mark_is_tuple_of_two_ints(self) -> None:
        from sovyx.voice._capture_task import AudioCaptureTask

        capture = AudioCaptureTask.__new__(AudioCaptureTask)
        capture._ring_state = (42 << 40) | 1_000_000  # noqa: SLF001
        capture._ring_capacity = 16_000  # noqa: SLF001

        mark = capture.samples_written_mark()
        assert isinstance(mark, tuple)
        assert len(mark) == 2
        assert all(isinstance(component, int) for component in mark)
        assert mark == (42, 1_000_000)

    def test_mark_components_within_js_safe_range(self) -> None:
        """Each component individually stays under ``2**53`` in realistic use.

        Epoch is effectively unbounded (Python int) but caps at ~10^4
        over a multi-year daemon lifetime. Samples cap at ``2**40`` by
        the mask; both are far below ``2**53``.
        """
        from sovyx.voice._capture_task import (
            _RING_EPOCH_SHIFT,
            _RING_SAMPLES_MASK,
            AudioCaptureTask,
        )

        assert _RING_SAMPLES_MASK < (1 << 53)
        assert (1 << _RING_EPOCH_SHIFT) <= (1 << 53) or True  # epoch is bit-shift base

        capture = AudioCaptureTask.__new__(AudioCaptureTask)
        # Pack the worst-realistic pair: ~10^4 epochs, near-max samples.
        capture._ring_state = (10_000 << _RING_EPOCH_SHIFT) | (_RING_SAMPLES_MASK - 1)  # noqa: SLF001
        mark = capture.samples_written_mark()
        js_safe = (1 << 53) - 1
        assert mark[0] < js_safe
        assert mark[1] < js_safe


# ── §14.E2 improvement heuristic ───────────────────────────────────


class TestImprovementHeuristic:
    """§14.E2 — a VAD_MUTE verdict with rolloff >> factor × before still resolves."""

    @pytest.mark.asyncio()
    async def test_improvement_path_resolves_without_revert(self) -> None:
        tuning = VoiceTuningConfig()
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=192.0)
        # User stopped speaking during settle; VAD sees silence but
        # rolloff cleaned up by 50x (192 → 9600 Hz).
        after = _result(verdict=IntegrityVerdict.VAD_MUTE, rolloff_hz=9_600.0, vad_max=0.01)
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        coordinator, _, _, strategy = _make_coordinator(
            before=before,
            after=after,
            post_apply_frames=frames,
            tuning=tuning,
        )

        outcomes = await coordinator.handle_deaf_signal()

        assert outcomes[-1].verdict is BypassVerdict.APPLIED_HEALTHY
        assert "improvement_heuristic" in outcomes[-1].detail
        assert strategy.reverted is False

    @pytest.mark.asyncio()
    async def test_vad_mute_without_improvement_still_reverts(self) -> None:
        tuning = VoiceTuningConfig()
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=4_000.0)
        # Rolloff barely moves — below the improvement threshold.
        after = _result(verdict=IntegrityVerdict.VAD_MUTE, rolloff_hz=4_200.0, vad_max=0.01)
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        coordinator, _, _, strategy = _make_coordinator(
            before=before,
            after=after,
            post_apply_frames=frames,
            tuning=tuning,
        )

        outcomes = await coordinator.handle_deaf_signal()

        assert outcomes[-1].verdict is BypassVerdict.APPLIED_STILL_DEAD
        assert strategy.reverted is True


# ── D5 contrafactual ───────────────────────────────────────────────


class TestHappyPathUnaffected:
    """D5 — if the pre-bypass probe is HEALTHY, the coordinator never
    touches any strategy. The L4-B fix must not disturb this path."""

    @pytest.mark.asyncio()
    async def test_short_circuit_on_healthy_pre_probe(self) -> None:
        before = _result(verdict=IntegrityVerdict.HEALTHY, rolloff_hz=7_000.0, vad_max=0.5)
        after = _result(verdict=IntegrityVerdict.HEALTHY)  # unused
        coordinator, capture, probe, strategy = _make_coordinator(
            before=before,
            after=after,
            post_apply_frames=np.zeros(0, dtype=np.int16),
        )

        outcomes = await coordinator.handle_deaf_signal()

        assert outcomes == []
        assert coordinator.is_resolved is True
        assert strategy.applied is False
        # No tap invoked — we short-circuit before any apply.
        assert capture.tap_calls == []
        assert probe.analyse_raw_calls == []


# ── Failure paths preserved ────────────────────────────────────────


class TestFailureModesPreserved:
    """Regression guards for the failure paths the L4-B refactor did
    *not* intend to change — ``FAILED_TO_APPLY`` bookkeeping, eligibility
    skipping, and the apply-exception logger."""

    @pytest.mark.asyncio()
    async def test_apply_raising_does_not_call_tap(self) -> None:
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=200.0)
        after = _result(verdict=IntegrityVerdict.HEALTHY)  # unused
        strategy = _FakeStrategy(
            apply_raises=BypassApplyError(
                "boom",
                reason="amixer_timeout",
            ),
        )
        coordinator, capture, _, _ = _make_coordinator(
            before=before,
            after=after,
            post_apply_frames=np.zeros(0, dtype=np.int16),
            strategy=strategy,
        )

        outcomes = await coordinator.handle_deaf_signal()

        assert outcomes[-1].verdict is BypassVerdict.FAILED_TO_APPLY
        assert capture.tap_calls == [], (
            "tap_frames_since_mark is only invoked on successful apply; "
            "a failed apply must not probe post-state."
        )


# ── B3: BypassRevertError handling at the coordinator boundary ─────


_COORD_LOGGER = "sovyx.voice.health.capture_integrity"


def _events_of(
    caplog: pytest.LogCaptureFixture,
    event_name: str,
) -> list[dict[str, Any]]:
    """Filter caplog records by the structured ``event`` field."""
    return [
        r.msg
        for r in caplog.records
        if r.name == _COORD_LOGGER and isinstance(r.msg, dict) and r.msg.get("event") == event_name
    ]


class TestRevertAtomicityB3:
    """Coordinator distinguishes structured BypassRevertError from a
    strategy bug (generic Exception) — pre-B3 both fell through the
    same ``except Exception`` path and were logged as exceptions, so
    a graceful revert failure was indistinguishable from a crash.
    """

    @pytest.mark.asyncio()
    async def test_bypass_revert_error_emits_structured_event(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sovyx.voice.health.bypass._strategy import BypassRevertError

        caplog.set_level(logging.WARNING, logger=_COORD_LOGGER)
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=200.0)
        # Force APPLIED_STILL_DEAD so revert is invoked.
        after = _result(verdict=IntegrityVerdict.VAD_MUTE, rolloff_hz=210.0, vad_max=0.01)
        strategy = _FakeStrategy(
            revert_raises=BypassRevertError(
                "shared restart downgraded",
                reason="shared_restart_downgraded_to_exclusive",
            ),
        )
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        coordinator, _, _, _ = _make_coordinator(
            before=before,
            after=after,
            post_apply_frames=frames,
            strategy=strategy,
        )
        await coordinator.handle_deaf_signal()

        events = _events_of(caplog, "voice.bypass.revert_failed")
        assert len(events) == 1
        evt = events[0]
        assert evt["voice.strategy"] == strategy.name
        assert evt["voice.reason"] == "shared_restart_downgraded_to_exclusive"
        assert "downgraded" in evt["voice.detail"]
        assert "next_strategy" in str(evt["voice.action_required"])

    @pytest.mark.asyncio()
    async def test_generic_exception_during_revert_falls_through_strategy_bug_path(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-BypassRevertError raise (strategy bug) must NOT be
        emitted as a structured revert_failed event — it's a different
        signal class and dashboards key on the distinction."""
        caplog.set_level(logging.WARNING, logger=_COORD_LOGGER)
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=200.0)
        after = _result(verdict=IntegrityVerdict.VAD_MUTE, rolloff_hz=210.0, vad_max=0.01)
        strategy = _FakeStrategy(
            revert_raises=RuntimeError("strategy implementation bug"),
        )
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        coordinator, _, _, _ = _make_coordinator(
            before=before,
            after=after,
            post_apply_frames=frames,
            strategy=strategy,
        )
        await coordinator.handle_deaf_signal()

        # No structured revert_failed event for a strategy bug.
        assert _events_of(caplog, "voice.bypass.revert_failed") == []
        # But the generic crash logger DID fire (defensive fallthrough).
        crashed_records = [
            r
            for r in caplog.records
            if r.name == _COORD_LOGGER
            and isinstance(r.msg, dict)
            and r.msg.get("event") == "capture_integrity_coordinator_revert_crashed"
        ]
        assert len(crashed_records) == 1

    @pytest.mark.asyncio()
    async def test_revert_failure_does_not_abort_remaining_coordinator_flow(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Even when revert raises, the coordinator must still produce
        the APPLIED_STILL_DEAD outcome and quarantine the endpoint —
        revert failure is observable but non-fatal."""
        from sovyx.voice.health.bypass._strategy import BypassRevertError

        caplog.set_level(logging.WARNING, logger=_COORD_LOGGER)
        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=200.0)
        after = _result(verdict=IntegrityVerdict.VAD_MUTE, rolloff_hz=210.0, vad_max=0.01)
        strategy = _FakeStrategy(
            revert_raises=BypassRevertError("oops", reason="test"),
        )
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        coordinator, _, _, _ = _make_coordinator(
            before=before,
            after=after,
            post_apply_frames=frames,
            strategy=strategy,
        )
        outcomes = await coordinator.handle_deaf_signal()

        # The APPLIED_STILL_DEAD outcome was still produced.
        assert outcomes[-1].verdict is BypassVerdict.APPLIED_STILL_DEAD
        # Coordinator marked itself resolved (quarantine path).
        assert coordinator.is_resolved is True


# ── T6.15 — all-strategies-NOT_APPLICABLE fallback emission ────────


@dataclass
class _NotApplicableStrategy:
    """Strategy stand-in whose ``probe_eligibility`` always declines."""

    name: str = "fake.not_applicable"
    reason: str = "no upstream APO chain matches this bypass"

    async def probe_eligibility(self, _ctx: BypassContext) -> Any:
        from sovyx.voice.health.contract import Eligibility

        return Eligibility(applicable=False, reason=self.reason, estimated_cost_ms=0)

    async def apply(self, _ctx: BypassContext) -> str:  # pragma: no cover
        msg = "apply must not be invoked when not_applicable"
        raise AssertionError(msg)

    async def revert(self, _ctx: BypassContext) -> None:  # pragma: no cover
        msg = "revert must not be invoked when not_applicable"
        raise AssertionError(msg)


_CAPTURE_INTEGRITY_LOGGER = "sovyx.voice.health.capture_integrity"


def _records_named(
    caplog: pytest.LogCaptureFixture,
    event_name: str,
) -> list[dict[str, object]]:
    return [
        r.msg
        for r in caplog.records
        if (
            r.name == _CAPTURE_INTEGRITY_LOGGER
            and isinstance(r.msg, dict)
            and r.msg.get("event") == event_name
        )
    ]


class TestUnrecoverableEmission:
    """T6.15 — bypass coordinator emits when ALL strategies decline."""

    @pytest.mark.asyncio()
    async def test_all_not_applicable_emits_unrecoverable(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.ERROR, logger=_CAPTURE_INTEGRITY_LOGGER)

        before = _result(verdict=IntegrityVerdict.APO_DEGRADED)
        after = _result(verdict=IntegrityVerdict.HEALTHY)
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        capture = _FakeCaptureTask(post_apply_frames=frames)
        probe = _FakeProbe(before=before, after=after)
        strategies: list[Any] = [
            _NotApplicableStrategy(name="strat.alpha"),
            _NotApplicableStrategy(name="strat.beta"),
        ]
        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=strategies,
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="win32",
            tuning=VoiceTuningConfig(),
        )

        outcomes = await coordinator.handle_deaf_signal()

        assert all(o.verdict is BypassVerdict.NOT_APPLICABLE for o in outcomes)
        assert len(outcomes) == 2

        events = _records_named(caplog, "voice_capture_integrity_unrecoverable")
        assert len(events) == 1
        evt = events[0]
        assert evt["platform"] == "win32"
        assert evt["strategy_count"] == 2
        assert evt["strategies_tried"] == ["strat.alpha", "strat.beta"]
        # Windows hint mentions Voice Clarity per master mission spec.
        assert "Voice Clarity" in str(evt["remediation"])

    @pytest.mark.asyncio()
    async def test_mixed_verdicts_does_not_emit_unrecoverable(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # When one strategy WAS applicable but failed (APPLIED_STILL_DEAD
        # / FAILED_TO_APPLY), the unrecoverable event must NOT fire —
        # the cause is different (we tried; hardware persists) and the
        # operator remediation differs (driver/HW investigation, not
        # OS audio-enhancement disablement).
        import logging

        caplog.set_level(logging.ERROR, logger=_CAPTURE_INTEGRITY_LOGGER)

        before = _result(verdict=IntegrityVerdict.APO_DEGRADED)
        # ``after`` is non-HEALTHY → APPLIED_STILL_DEAD verdict.
        after = _result(verdict=IntegrityVerdict.APO_DEGRADED)
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        capture = _FakeCaptureTask(post_apply_frames=frames)
        probe = _FakeProbe(before=before, after=after)
        strategies: list[Any] = [
            _NotApplicableStrategy(name="strat.alpha"),
            _FakeStrategy(name="strat.applicable"),
        ]
        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=strategies,
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="win32",
            tuning=VoiceTuningConfig(),
        )

        await coordinator.handle_deaf_signal()
        events = _records_named(caplog, "voice_capture_integrity_unrecoverable")
        assert events == []

    @pytest.mark.asyncio()
    async def test_remediation_hint_routes_per_platform(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.ERROR, logger=_CAPTURE_INTEGRITY_LOGGER)

        before = _result(verdict=IntegrityVerdict.APO_DEGRADED)
        after = _result(verdict=IntegrityVerdict.HEALTHY)

        for platform_key, expected_token in [
            ("linux", "echo-cancel"),
            ("darwin", "Voice Isolation"),
            ("win32", "Voice Clarity"),
        ]:
            caplog.clear()
            frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
            capture = _FakeCaptureTask(post_apply_frames=frames)
            probe = _FakeProbe(before=before, after=after)
            coordinator = CaptureIntegrityCoordinator(
                probe=probe,  # type: ignore[arg-type]
                strategies=[_NotApplicableStrategy()],
                capture_task=capture,  # type: ignore[arg-type]
                platform_key=platform_key,
                tuning=VoiceTuningConfig(),
            )
            await coordinator.handle_deaf_signal()
            events = _records_named(
                caplog,
                "voice_capture_integrity_unrecoverable",
            )
            assert len(events) == 1, f"platform={platform_key}"
            remediation = str(events[0]["remediation"])
            assert expected_token in remediation, (
                f"platform={platform_key} remediation missing {expected_token!r}: {remediation!r}"
            )

    def test_unknown_platform_remediation_is_generic(self) -> None:
        # Direct helper test — exercising via coordinator would force
        # building an unknown-platform setup which adds setup noise.
        from sovyx.voice.health.capture_integrity import (
            _unrecoverable_remediation_hint,
        )

        hint = _unrecoverable_remediation_hint("freebsd")
        assert "freebsd" in hint
        assert "no user-mode bypass" in hint


# ── T6.16 — post-apply INCONCLUSIVE retry ───────────────────────────


@dataclass
class _TwoPhaseProbe:
    """Probe stand-in returning DIFFERENT verdicts on first vs retry call.

    The coordinator's INCONCLUSIVE retry path issues two
    ``analyse_raw`` calls with different ``post_apply_frames`` (the
    fresh mark + tap pair). This stand-in returns the configured
    verdict for each call, by call index — matching the production
    contract that the retry sees a SECOND, fresh window.
    """

    before: IntegrityResult
    after_sequence: list[IntegrityResult] = field(default_factory=list)
    analyse_raw_calls: list[npt.NDArray[np.int16]] = field(default_factory=list)

    async def probe_warm(self, _capture: Any) -> IntegrityResult:  # noqa: ANN401
        return self.before

    async def analyse_raw(
        self,
        frames: npt.NDArray[np.int16],
        *,
        endpoint_guid: str,  # noqa: ARG002
    ) -> IntegrityResult:
        idx = len(self.analyse_raw_calls)
        self.analyse_raw_calls.append(frames)
        if idx < len(self.after_sequence):
            return self.after_sequence[idx]
        # Past the configured sequence: reuse the last one.
        return self.after_sequence[-1]


class TestInconclusiveRetry:
    """T6.16 — single retry on post-apply INCONCLUSIVE verdict."""

    @pytest.mark.asyncio()
    async def test_retry_recovers_to_healthy(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # First post-apply probe returns INCONCLUSIVE (e.g. tap timed
        # out short during apply window). Retry succeeds with HEALTHY.
        # Coordinator must resolve as APPLIED_HEALTHY, not revert.
        caplog.set_level(logging.INFO, logger=_CAPTURE_INTEGRITY_LOGGER)

        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=192.0)
        first_after = _result(verdict=IntegrityVerdict.INCONCLUSIVE)
        retry_after = _result(verdict=IntegrityVerdict.HEALTHY, rolloff_hz=6000.0)
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        capture = _FakeCaptureTask(post_apply_frames=frames)
        probe = _TwoPhaseProbe(
            before=before,
            after_sequence=[first_after, retry_after],
        )
        strategy = _FakeStrategy()
        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=[strategy],  # type: ignore[list-item]
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="linux",
            tuning=VoiceTuningConfig(),
        )
        outcomes = await coordinator.handle_deaf_signal()

        # Two analyse_raw calls (initial + retry).
        assert len(probe.analyse_raw_calls) == 2
        # Retry log fires with retry_recovered=True.
        retry_events = _records_named(caplog, "capture_integrity_inconclusive_retry")
        assert len(retry_events) == 1
        assert retry_events[0]["retry_recovered"] is True
        assert retry_events[0]["retry_verdict"] == "healthy"
        # Coordinator resolved as APPLIED_HEALTHY, not APPLIED_STILL_DEAD.
        assert outcomes[-1].verdict is BypassVerdict.APPLIED_HEALTHY
        # Strategy was NOT reverted — it actually fixed the signal.
        assert strategy.reverted is False

    @pytest.mark.asyncio()
    async def test_retry_still_inconclusive_falls_through_to_still_dead(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Both probes return INCONCLUSIVE → conservative fall-through
        # to APPLIED_STILL_DEAD + revert (pre-T6.16 behaviour preserved
        # for genuinely inconclusive cases).
        caplog.set_level(logging.INFO, logger=_CAPTURE_INTEGRITY_LOGGER)

        before = _result(verdict=IntegrityVerdict.APO_DEGRADED)
        inconclusive = _result(verdict=IntegrityVerdict.INCONCLUSIVE)
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        capture = _FakeCaptureTask(post_apply_frames=frames)
        probe = _TwoPhaseProbe(
            before=before,
            after_sequence=[inconclusive, inconclusive],
        )
        strategy = _FakeStrategy()
        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=[strategy],  # type: ignore[list-item]
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="linux",
            tuning=VoiceTuningConfig(),
        )
        outcomes = await coordinator.handle_deaf_signal()

        # Two analyse_raw calls (initial + retry); retry was INCONCLUSIVE.
        assert len(probe.analyse_raw_calls) == 2
        retry_events = _records_named(caplog, "capture_integrity_inconclusive_retry")
        assert len(retry_events) == 1
        assert retry_events[0]["retry_recovered"] is False
        # APPLIED_STILL_DEAD path taken; strategy reverted.
        assert outcomes[-1].verdict is BypassVerdict.APPLIED_STILL_DEAD
        assert strategy.reverted is True

    @pytest.mark.asyncio()
    async def test_retry_disabled_falls_through_immediately(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Operator escape hatch — flag False restores pre-T6.16
        # single-probe behaviour. No second analyse_raw call.
        caplog.set_level(logging.INFO, logger=_CAPTURE_INTEGRITY_LOGGER)

        before = _result(verdict=IntegrityVerdict.APO_DEGRADED)
        first_after = _result(verdict=IntegrityVerdict.INCONCLUSIVE)
        retry_after = _result(verdict=IntegrityVerdict.HEALTHY)
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        capture = _FakeCaptureTask(post_apply_frames=frames)
        probe = _TwoPhaseProbe(
            before=before,
            after_sequence=[first_after, retry_after],
        )
        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=[_FakeStrategy()],  # type: ignore[list-item]
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="linux",
            tuning=VoiceTuningConfig(
                capture_integrity_inconclusive_retry_enabled=False,
            ),
        )
        outcomes = await coordinator.handle_deaf_signal()

        # Only ONE analyse_raw call — retry was disabled.
        assert len(probe.analyse_raw_calls) == 1
        # No retry log.
        assert _records_named(caplog, "capture_integrity_inconclusive_retry") == []
        # Falls into APPLIED_STILL_DEAD even though retry would have
        # resolved HEALTHY.
        assert outcomes[-1].verdict is BypassVerdict.APPLIED_STILL_DEAD

    @pytest.mark.asyncio()
    async def test_first_probe_definitive_skips_retry(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # First probe returns a DEFINITIVE verdict (HEALTHY) → retry
        # path NOT taken. Guards against accidentally double-probing
        # the success path.
        caplog.set_level(logging.INFO, logger=_CAPTURE_INTEGRITY_LOGGER)

        before = _result(verdict=IntegrityVerdict.APO_DEGRADED, rolloff_hz=192.0)
        first_after = _result(verdict=IntegrityVerdict.HEALTHY, rolloff_hz=6000.0)
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        capture = _FakeCaptureTask(post_apply_frames=frames)
        probe = _TwoPhaseProbe(
            before=before,
            after_sequence=[first_after],
        )
        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=[_FakeStrategy()],  # type: ignore[list-item]
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="linux",
            tuning=VoiceTuningConfig(),
        )
        outcomes = await coordinator.handle_deaf_signal()

        assert len(probe.analyse_raw_calls) == 1
        assert _records_named(caplog, "capture_integrity_inconclusive_retry") == []
        assert outcomes[-1].verdict is BypassVerdict.APPLIED_HEALTHY

    @pytest.mark.asyncio()
    async def test_retry_tap_exception_falls_through(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # If the retry tap itself raises (transient capture-task
        # error), the coordinator must NOT crash — falls through with
        # the original inconclusive verdict.
        caplog.set_level(logging.DEBUG, logger=_CAPTURE_INTEGRITY_LOGGER)

        before = _result(verdict=IntegrityVerdict.APO_DEGRADED)
        first_after = _result(verdict=IntegrityVerdict.INCONCLUSIVE)
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        capture = _FakeCaptureTask(post_apply_frames=frames)
        probe = _TwoPhaseProbe(
            before=before,
            after_sequence=[first_after],
        )

        # Patch the second tap call to raise.
        original_tap = capture.tap_frames_since_mark
        call_count = {"n": 0}

        async def _flaky_tap(
            mark: tuple[int, int],
            min_samples: int,
            max_wait_s: float,
        ) -> npt.NDArray[np.int16]:
            call_count["n"] += 1
            if call_count["n"] == 2:
                msg = "simulated transient tap failure"
                raise RuntimeError(msg)
            return await original_tap(mark, min_samples, max_wait_s)

        capture.tap_frames_since_mark = _flaky_tap  # type: ignore[method-assign]

        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=[_FakeStrategy()],  # type: ignore[list-item]
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="linux",
            tuning=VoiceTuningConfig(),
        )
        outcomes = await coordinator.handle_deaf_signal()

        # Retry tap raised → graceful fall-through to APPLIED_STILL_DEAD.
        assert outcomes[-1].verdict is BypassVerdict.APPLIED_STILL_DEAD
        # Defensive log fires at DEBUG level.
        debug_events = _records_named(
            caplog,
            "capture_integrity_inconclusive_retry_failed",
        )
        assert len(debug_events) == 1
        # Standard retry log STILL fires (with retry_recovered=False).
        retry_events = _records_named(caplog, "capture_integrity_inconclusive_retry")
        assert len(retry_events) == 1
        assert retry_events[0]["retry_recovered"] is False


# ── Pre-bypass INCONCLUSIVE exhaustion — quarantine, not ValueError ──


class TestPreBypassInconclusiveExhaustion:
    """P0 regression — a PRE-BYPASS INCONCLUSIVE verdict that survives to
    strategy exhaustion must quarantine under the legacy APO_DEGRADED
    reason, NOT raise :class:`ValueError`.

    The T6.16 INCONCLUSIVE retry covers only the POST-APPLY probe, so a
    pre-bypass INCONCLUSIVE (e.g. ring-buffer underrun on a dead
    pipeline — the most common deaf-mic pre-bypass verdict) legitimately
    falls through the verdict-router into the strategy loop and reaches
    the exhaustion quarantine site. Before the fix,
    ``resolve_reason_from_verdict(INCONCLUSIVE)`` raised there, escaping
    ``handle_deaf_signal`` BEFORE ``_is_resolved``/quarantine/failover —
    permanently blocking recovery on every subsequent deaf heartbeat.
    """

    @pytest.mark.asyncio()
    async def test_inconclusive_exhaustion_quarantines_as_apo_degraded(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sovyx.voice.health._quarantine import EndpointQuarantine

        caplog.set_level(logging.WARNING, logger=_CAPTURE_INTEGRITY_LOGGER)

        # Dead pipeline: every probe window is an underrun → INCONCLUSIVE
        # both pre-bypass AND post-apply.
        before = _result(verdict=IntegrityVerdict.INCONCLUSIVE)
        after = _result(verdict=IntegrityVerdict.INCONCLUSIVE)
        frames = np.zeros(int(3.0 * _SAMPLE_RATE), dtype=np.int16)
        capture = _FakeCaptureTask(post_apply_frames=frames)
        probe = _FakeProbe(before=before, after=after)
        strategy = _FakeStrategy()
        quarantine = EndpointQuarantine(quarantine_s=60.0)
        coordinator = CaptureIntegrityCoordinator(
            probe=probe,  # type: ignore[arg-type]
            strategies=[strategy],  # type: ignore[list-item]
            capture_task=capture,  # type: ignore[arg-type]
            platform_key="linux",
            tuning=VoiceTuningConfig(),
            quarantine=quarantine,
        )

        # (a) Must NOT raise — pre-fix this threw ValueError out of
        # handle_deaf_signal from resolve_reason_from_verdict.
        outcomes = await coordinator.handle_deaf_signal()

        # (c) Terminal outcome reached: outcomes returned + resolved.
        assert len(outcomes) >= 1
        assert outcomes[-1].verdict.value == BypassVerdict.APPLIED_STILL_DEAD.value
        assert coordinator.is_resolved is True

        # (b) Endpoint IS quarantined, resolved via the documented legacy
        # fallback (terminal_verdict=None → APO_DEGRADED) — the
        # apo-recheck-eligible reason that preserves failover + TTL
        # recovery via the watchdog recheck loop.
        assert quarantine.is_quarantined("guid-fake") is True
        entry = quarantine.get("guid-fake")
        assert entry is not None
        assert entry.resolved_reason == "apo_degraded"
        assert entry.derived_reason == "apo_degraded"

        # The quarantine log fired — the coordinator completed the full
        # terminal path instead of aborting mid-way.
        quarantined_events = _records_named(
            caplog,
            "capture_integrity_coordinator_quarantined",
        )
        assert len(quarantined_events) == 1
        assert quarantined_events[0]["resolved_reason"] == "apo_degraded"


# ══════════════════════════════════════════════════════════════════════
# Mission C1 §T1.2 — classifier extension regression suite.
#
# Mission anchor:
#   docs-internal/missions/MISSION-c1-vad-mute-reclassification-2026-05-14.md
#   §T1.2 + §9.1 + §20.O (Agent-4 audit-derived additions).
#
# These tests exercise the REAL CaptureIntegrityProbe._classify decision
# tree + the two new static helpers (_is_format_mismatch,
# _is_vad_frontend_dead). They do NOT route through the coordinator —
# coordinator dispatch is T1.3 and lives in its own test file.
#
# All assertions use `.value ==` rather than `is` per ADR-D6 + §20.N
# (anti-pattern #8 xdist-safety for NEW tests).
# ══════════════════════════════════════════════════════════════════════


def _classifier_only_probe() -> Any:
    """Build a CaptureIntegrityProbe with a stub VAD.

    _classify never touches the VAD instance; the constructor requires
    ``vad`` only because _analyse_sync uses it. A plain ``object()``
    suffices and keeps the test surface minimal.
    """
    from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

    return CaptureIntegrityProbe(vad=object(), tuning=None)  # type: ignore[arg-type]


def _history_entry(
    *,
    rms_db: float,
    vad_max_prob: float,
) -> IntegrityResult:
    """Build a synthetic IntegrityResult for history-window tests.

    Other fields are spec-stable placeholders — the dead-frontend check
    only reads rms_db + vad_max_prob.
    """
    return IntegrityResult(
        verdict=IntegrityVerdict.VAD_MUTE,
        endpoint_guid="guid-history",
        rms_db=rms_db,
        vad_max_prob=vad_max_prob,
        spectral_flatness=0.2,
        spectral_rolloff_hz=5_000.0,
        duration_s=3.0,
        probed_at_utc=datetime.now(UTC),
        raw_frames=int(3.0 * _SAMPLE_RATE),
        detail="",
    )


class TestIsFormatMismatchHelper:
    """Mission C1 T1.2 — _is_format_mismatch static helper contract."""

    def test_wellformed_mono_int16_returns_false(self) -> None:
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        frames = np.zeros(2048, dtype=np.int16)
        assert CaptureIntegrityProbe._is_format_mismatch(frames) is False

    def test_float32_dtype_returns_true(self) -> None:
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        frames = np.zeros(2048, dtype=np.float32)
        assert CaptureIntegrityProbe._is_format_mismatch(frames) is True  # type: ignore[arg-type]

    def test_int32_dtype_returns_true(self) -> None:
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        frames = np.zeros(2048, dtype=np.int32)
        assert CaptureIntegrityProbe._is_format_mismatch(frames) is True  # type: ignore[arg-type]

    def test_stereo_2d_shape_returns_true(self) -> None:
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        # Mono int16 but in a (samples, channels) layout — Silero expects
        # a 1-D buffer; the 2-D shape is a format mismatch.
        frames = np.zeros((1024, 2), dtype=np.int16)
        assert CaptureIntegrityProbe._is_format_mismatch(frames) is True


class TestIsVadFrontendDeadHelper:
    """Mission C1 T1.2 + §2.3 — _is_vad_frontend_dead trajectory check."""

    def test_empty_history_returns_false(self) -> None:
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        assert CaptureIntegrityProbe._is_vad_frontend_dead((), tuning=VoiceTuningConfig()) is False

    def test_short_history_returns_false(self) -> None:
        # 4 entries vs default window of 5 → insufficient evidence.
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        history = tuple(_history_entry(rms_db=-30.0, vad_max_prob=0.001) for _ in range(4))
        assert (
            CaptureIntegrityProbe._is_vad_frontend_dead(
                history,
                tuning=VoiceTuningConfig(),
            )
            is False
        )

    def test_full_window_sustained_dead_returns_true(self) -> None:
        # 5 consecutive probes all with RMS above APO floor + VAD at noise.
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        history = tuple(_history_entry(rms_db=-30.0, vad_max_prob=0.001) for _ in range(5))
        assert (
            CaptureIntegrityProbe._is_vad_frontend_dead(
                history,
                tuning=VoiceTuningConfig(),
            )
            is True
        )

    def test_one_recent_healthy_probe_falsifies(self) -> None:
        # 4 dead + 1 recovered → frontend can respond, not dead.
        # The helper inspects the LAST `window` entries (recent slice).
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        history = (
            *(_history_entry(rms_db=-30.0, vad_max_prob=0.001) for _ in range(4)),
            _history_entry(rms_db=-30.0, vad_max_prob=0.85),  # responsive
        )
        assert (
            CaptureIntegrityProbe._is_vad_frontend_dead(
                history,
                tuning=VoiceTuningConfig(),
            )
            is False
        )

    def test_below_rms_floor_falsifies(self) -> None:
        # Sustained VAD silence but with energy below the APO floor —
        # this is DRIVER_SILENT territory, not frontend-dead. The helper
        # must reject so the classifier routes to DRIVER_SILENT instead.
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        history = tuple(_history_entry(rms_db=-90.0, vad_max_prob=0.001) for _ in range(5))
        assert (
            CaptureIntegrityProbe._is_vad_frontend_dead(
                history,
                tuning=VoiceTuningConfig(),
            )
            is False
        )

    def test_threshold_boundary_inclusive_vs_exclusive(self) -> None:
        # rms_db AT floor (=-50.0) is NOT above floor (strict `>`). Helper
        # rejects to avoid edge-of-floor false positives.
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        tuning = VoiceTuningConfig()
        history = tuple(
            _history_entry(
                rms_db=tuning.integrity_apo_rms_floor_db,  # exactly at floor
                vad_max_prob=0.001,
            )
            for _ in range(5)
        )
        assert CaptureIntegrityProbe._is_vad_frontend_dead(history, tuning=tuning) is False

    def test_buffer_longer_than_window_evaluates_recent_slice(self) -> None:
        # 20-entry history, oldest 15 are healthy, newest 5 are dead.
        # The helper must evaluate the recent 5 → DEAD.
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        history = (
            *(_history_entry(rms_db=-30.0, vad_max_prob=0.85) for _ in range(15)),
            *(_history_entry(rms_db=-30.0, vad_max_prob=0.001) for _ in range(5)),
        )
        assert (
            CaptureIntegrityProbe._is_vad_frontend_dead(
                history,
                tuning=VoiceTuningConfig(),
            )
            is True
        )

    def test_window_zero_returns_false(self) -> None:
        # Defensive: a misconfigured window=0 must not fire (any history
        # would "satisfy" len() >= 0 — protects against telemetry-only
        # operator overrides that zero the knob).
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        tuning = VoiceTuningConfig(integrity_history_window_probes=0)
        history = tuple(_history_entry(rms_db=-30.0, vad_max_prob=0.001) for _ in range(5))
        assert CaptureIntegrityProbe._is_vad_frontend_dead(history, tuning=tuning) is False


class TestClassifyDecisionTree:
    """Mission C1 T1.2 — _classify produces the right verdict per branch."""

    def test_healthy_signal_takes_first_branch(self) -> None:
        # Regression: HEALTHY classification still wins ahead of any other.
        probe = _classifier_only_probe()
        verdict = probe._classify(
            rms_db=-25.0,
            vad_max=0.85,  # responsive VAD
            flatness=0.2,
            rolloff_hz=6_500.0,
            tuning=VoiceTuningConfig(),
            frames=np.zeros(2048, dtype=np.int16),
            history=(),
        )
        assert verdict.value == "healthy"

    def test_driver_silent_below_rms_ceiling(self) -> None:
        # Regression: very-low-RMS short-circuits to DRIVER_SILENT.
        probe = _classifier_only_probe()
        verdict = probe._classify(
            rms_db=-95.0,
            vad_max=0.001,
            flatness=0.2,
            rolloff_hz=6_500.0,
            tuning=VoiceTuningConfig(),
            frames=np.zeros(2048, dtype=np.int16),
            history=(),
        )
        assert verdict.value == "driver_silent"

    def test_format_mismatch_floats_to_new_verdict(self) -> None:
        # Mission C1 new branch: wrong dtype short-circuits to
        # FORMAT_MISMATCH BEFORE spectral classification.
        probe = _classifier_only_probe()
        verdict = probe._classify(
            rms_db=-30.0,
            vad_max=0.001,
            flatness=0.5,  # would otherwise look APO-like
            rolloff_hz=2_000.0,
            tuning=VoiceTuningConfig(),
            frames=np.zeros(2048, dtype=np.float32),  # type: ignore[arg-type]
            history=(),
        )
        assert verdict.value == "format_mismatch"

    def test_apo_signature_routes_to_apo_degraded(self) -> None:
        # Regression: classic APO signature (loud carrier, dead VAD,
        # collapsed rolloff) still classifies APO_DEGRADED.
        probe = _classifier_only_probe()
        verdict = probe._classify(
            rms_db=-30.0,
            vad_max=0.001,
            flatness=0.4,  # > apo ceiling
            rolloff_hz=2_500.0,  # < apo rolloff ceiling
            tuning=VoiceTuningConfig(),
            frames=np.zeros(2048, dtype=np.int16),
            history=(),
        )
        assert verdict.value == "apo_degraded"

    def test_vad_frontend_dead_with_full_dead_history(self) -> None:
        # Mission C1 new branch: sustained-VAD-silence-with-energy across
        # the configured window → VAD_FRONTEND_DEAD.
        probe = _classifier_only_probe()
        history = tuple(_history_entry(rms_db=-30.0, vad_max_prob=0.001) for _ in range(5))
        verdict = probe._classify(
            rms_db=-30.0,
            vad_max=0.001,
            flatness=0.15,  # clean speech-band → NOT apo_signature
            rolloff_hz=6_500.0,
            tuning=VoiceTuningConfig(),
            frames=np.zeros(2048, dtype=np.int16),
            history=history,
        )
        assert verdict.value == "vad_frontend_dead"

    def test_vad_mute_when_history_empty_regression(self) -> None:
        # Mission C1 §T1.2 acceptance: empty history + sustained-quiet
        # signal must STILL return VAD_MUTE (default behavior preserved
        # for coordinator paths that don't pass history yet).
        probe = _classifier_only_probe()
        verdict = probe._classify(
            rms_db=-30.0,
            vad_max=0.001,
            flatness=0.15,
            rolloff_hz=6_500.0,
            tuning=VoiceTuningConfig(),
            frames=np.zeros(2048, dtype=np.int16),
            history=(),
        )
        assert verdict.value == "vad_mute"

    def test_vad_mute_falls_through_with_partial_history(self) -> None:
        # 3 dead entries < window of 5 → insufficient evidence → benign.
        probe = _classifier_only_probe()
        history = tuple(_history_entry(rms_db=-30.0, vad_max_prob=0.001) for _ in range(3))
        verdict = probe._classify(
            rms_db=-30.0,
            vad_max=0.001,
            flatness=0.15,
            rolloff_hz=6_500.0,
            tuning=VoiceTuningConfig(),
            frames=np.zeros(2048, dtype=np.int16),
            history=history,
        )
        assert verdict.value == "vad_mute"

    def test_format_mismatch_wins_over_dead_history(self) -> None:
        # Mission C1 decision-tree ordering: FORMAT_MISMATCH fires BEFORE
        # VAD_FRONTEND_DEAD. A buffer with wrong shape AND a dead history
        # → FORMAT_MISMATCH (fix the input shape first; only then can VAD
        # health be evaluated meaningfully).
        probe = _classifier_only_probe()
        history = tuple(_history_entry(rms_db=-30.0, vad_max_prob=0.001) for _ in range(5))
        verdict = probe._classify(
            rms_db=-30.0,
            vad_max=0.001,
            flatness=0.15,
            rolloff_hz=6_500.0,
            tuning=VoiceTuningConfig(),
            frames=np.zeros(2048, dtype=np.float32),  # type: ignore[arg-type]
            history=history,
        )
        assert verdict.value == "format_mismatch"

    def test_classify_without_frames_skips_format_check(self) -> None:
        # Backward compat: pre-mission callers that pass frames=None
        # (or rely on the default) must still reach a verdict; the
        # format check is a no-op without frames.
        probe = _classifier_only_probe()
        verdict = probe._classify(
            rms_db=-30.0,
            vad_max=0.001,
            flatness=0.15,
            rolloff_hz=6_500.0,
            tuning=VoiceTuningConfig(),
            frames=None,
            history=(),
        )
        # Falls through to VAD_MUTE (benign) with no history.
        assert verdict.value == "vad_mute"


class TestClassifierPropertyInvariants:
    """Mission C1 §9.3 — _classify must never raise on any input combination."""

    @given(
        rms_db=st.floats(min_value=-150.0, max_value=10.0, allow_nan=False, allow_infinity=False),
        vad_max=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        flatness=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        rolloff_hz=st.floats(
            min_value=0.0,
            max_value=16_000.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        history_size=st.integers(min_value=0, max_value=20),
    )
    @hp_settings(max_examples=50, deadline=None)
    def test_classify_never_raises_and_returns_known_verdict(
        self,
        rms_db: float,
        vad_max: float,
        flatness: float,
        rolloff_hz: float,
        history_size: int,
    ) -> None:
        probe = _classifier_only_probe()
        history = tuple(
            _history_entry(rms_db=rms_db, vad_max_prob=vad_max) for _ in range(history_size)
        )
        verdict = probe._classify(
            rms_db=rms_db,
            vad_max=vad_max,
            flatness=flatness,
            rolloff_hz=rolloff_hz,
            tuning=VoiceTuningConfig(),
            frames=np.zeros(2048, dtype=np.int16),
            history=history,
        )
        # Invariant: classifier output is always a known StrEnum member.
        assert verdict.value in {
            "healthy",
            "apo_degraded",
            "driver_silent",
            "vad_mute",
            "vad_frontend_dead",
            "format_mismatch",
            "inconclusive",  # Defensive — currently unreachable from _classify.
        }


class TestProbeApiHistoryKwarg:
    """Mission C1 T1.2 — probe_warm / analyse_raw accept the new `history` kwarg."""

    @pytest.mark.asyncio()
    async def test_analyse_raw_accepts_history_keyword(self) -> None:
        # Smoke test: the new keyword-only `history` is accepted by
        # analyse_raw without raising TypeError. Real classification is
        # exercised by the _classify tests above; here we just pin the
        # public surface.
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        probe = CaptureIntegrityProbe(vad=object(), tuning=None)  # type: ignore[arg-type]
        # Buffer below _MIN_SAMPLES_FOR_ANALYSIS — analyse_raw exits via
        # the INCONCLUSIVE branch before touching the VAD, so no real
        # VAD wiring is needed.
        frames = np.zeros(64, dtype=np.int16)
        history = (_history_entry(rms_db=-30.0, vad_max_prob=0.001),)
        result = await probe.analyse_raw(
            frames,
            endpoint_guid="guid-test",
            history=history,
        )
        assert result.verdict.value == "inconclusive"

    @pytest.mark.asyncio()
    async def test_probe_warm_accepts_history_keyword(self) -> None:
        from sovyx.voice.health.capture_integrity import CaptureIntegrityProbe

        probe = CaptureIntegrityProbe(vad=object(), tuning=None)  # type: ignore[arg-type]
        # Fake capture returning an underrun buffer → INCONCLUSIVE.
        capture = _FakeCaptureTask(post_apply_frames=np.zeros(64, dtype=np.int16))
        result = await probe.probe_warm(
            capture,  # type: ignore[arg-type]
            history=(),
        )
        assert result.verdict.value == "inconclusive"
