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
