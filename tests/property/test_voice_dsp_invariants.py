"""Property-based tests for voice DSP invariants (TS4).

Mission §6 acceptance gate #15 demands ≥10 properties covering
DSP invariants, mixer convergence, AGC2 stability, idempotency.
This module covers the in-process DSP layer (AGC2 + state machine
+ HystrixGuard) — Linux mixer convergence properties land in F8
when the KB profile system ships.

Each property runs with ``max_examples`` tuned to keep the suite
fast while still exploring meaningful corners — Hypothesis's
shrinker concentrates failure cases far more efficiently than
random brute force, so 50–200 examples per property is enough to
catch the classes of bug example-based tests miss (asymmetric
attack/release pumping, slew-rate violations, gain-bound escapes,
state-machine history overflow, bucket overflow under bursty
input, …).

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §6
(test strategy), §3.10 TS4. Hypothesis docs:
hypothesis.readthedocs.io/en/latest/data.html
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from sovyx.voice._agc2 import AGC2, AGC2Config
from sovyx.voice._hystrix_guard import (
    GuardRegistry,
    HystrixGuard,
    HystrixGuardConfig,
)
from sovyx.voice._observability_pii import BoundedCardinalityBucket
from sovyx.voice._stage_metrics import VoiceStage
from sovyx.voice.pipeline._state import VoicePipelineState
from sovyx.voice.pipeline._state_machine import (
    PipelineStateMachine,
    is_transition_allowed,
)

# ── Helpers ────────────────────────────────────────────────────────


_INT16_MIN = -32768
_INT16_MAX = 32767


def _mk_int16_samples(amp_norm: float, n: int) -> np.ndarray:
    """Build an int16 mono frame at constant amplitude ``amp_norm`` ∈ [0, 1]."""
    value = int(round(amp_norm * _INT16_MAX))
    return np.full(n, value, dtype=np.int16)


def _safe_amp_strategy() -> st.SearchStrategy[float]:
    """Hypothesis strategy for normalised amplitudes that don't trip
    ``log10(0)`` in dBFS conversion. Lower bound 1e-4 keeps the
    speech-level estimator above the silence floor at default config."""
    return st.floats(
        min_value=1e-4,
        max_value=1.0,
        allow_nan=False,
        allow_infinity=False,
    )


# ── AGC2 invariants ────────────────────────────────────────────────


class TestAGC2Invariants:
    """AGC2 must satisfy: gain confined, no overflow, slew rate bounded."""

    @settings(max_examples=100, deadline=None)
    @given(
        amp=_safe_amp_strategy(),
        n=st.integers(min_value=1, max_value=2048),
    )
    def test_output_always_in_int16_range(self, amp: float, n: int) -> None:
        agc = AGC2()
        out = agc.process(_mk_int16_samples(amp, n))
        assert out.dtype == np.int16
        # int16 dtype enforces this at the OS level, but assert on the
        # values directly to catch a future refactor that returns
        # un-clipped float and casts later.
        assert int(np.min(out)) >= _INT16_MIN
        assert int(np.max(out)) <= _INT16_MAX

    @settings(max_examples=100, deadline=None)
    @given(
        amp=_safe_amp_strategy(),
        n=st.integers(min_value=1, max_value=2048),
    )
    def test_gain_within_configured_bounds(self, amp: float, n: int) -> None:
        cfg = AGC2Config()
        agc = AGC2(cfg)
        agc.process(_mk_int16_samples(amp, n))
        assert cfg.min_gain_db <= agc.current_gain_db <= cfg.max_gain_db

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        amp=_safe_amp_strategy(),
        n=st.integers(min_value=160, max_value=2048),  # >= 10ms @ 16k
        n_iters=st.integers(min_value=2, max_value=10),
    )
    def test_slew_rate_never_exceeds_configured_max(
        self,
        amp: float,
        n: int,
        n_iters: int,
    ) -> None:
        """Per-call gain change must respect the slew-rate ceiling."""
        cfg = AGC2Config()
        agc = AGC2(cfg)
        # Frame duration in seconds.
        frame_s = n / cfg.sample_rate
        # Slew limit DB per FRAME is the per-second ceiling × frame
        # duration. We allow a small epsilon for IEEE-754 round-off
        # propagating through the dB → linear → dB chain.
        max_per_frame_db = cfg.max_gain_change_db_per_second * frame_s + 1e-6
        prev_gain = agc.current_gain_db
        for _ in range(n_iters):
            agc.process(_mk_int16_samples(amp, n))
            delta = abs(agc.current_gain_db - prev_gain)
            assert delta <= max_per_frame_db, (
                f"slew violation: delta={delta} dB, max_per_frame_db={max_per_frame_db}"
            )
            prev_gain = agc.current_gain_db

    @settings(max_examples=80, deadline=None)
    @given(amp=_safe_amp_strategy())
    def test_silent_input_does_not_drag_speech_estimate(
        self,
        amp: float,
    ) -> None:
        """Silence frames must NOT update the speech-level estimator —
        the silence-floor gate is the key invariant that keeps AGC2
        from pumping up the noise floor."""
        cfg = AGC2Config()
        agc = AGC2(cfg)
        # Establish a non-trivial speech level with one loud frame.
        agc.process(_mk_int16_samples(amp, 512))
        level_after_speech = agc.speech_level_dbfs
        # Now feed many true-silence frames.
        silence = np.zeros(512, dtype=np.int16)
        for _ in range(20):
            agc.process(silence)
        # Estimator should be unchanged — silent frames are gated.
        assert agc.speech_level_dbfs == level_after_speech

    @settings(max_examples=50, deadline=None)
    @given(n=st.integers(min_value=1, max_value=2048))
    def test_empty_array_returns_unchanged(self, n: int) -> None:  # noqa: ARG002
        """Zero-length frame is a no-op (counted as silence)."""
        agc = AGC2()
        empty = np.zeros(0, dtype=np.int16)
        out = agc.process(empty)
        assert out.size == 0
        assert agc.frames_silenced == 1

    @settings(max_examples=80, deadline=None)
    @given(
        amp=_safe_amp_strategy(),
        n=st.integers(min_value=1, max_value=2048),
    )
    def test_reset_returns_to_initial_state(self, amp: float, n: int) -> None:
        cfg = AGC2Config()
        agc = AGC2(cfg)
        agc.process(_mk_int16_samples(amp, n))
        agc.reset()
        assert agc.current_gain_db == 0.0
        assert agc.speech_level_dbfs == cfg.target_dbfs
        assert agc.frames_processed == 0
        assert agc.frames_silenced == 0
        assert agc.frames_clipped == 0

    @settings(max_examples=50, deadline=None)
    @given(amp=st.floats(min_value=0.7, max_value=1.0, allow_nan=False))
    def test_high_amplitude_input_does_not_overflow(self, amp: float) -> None:
        """Saturation protector must keep peak post-gain ≤ int16 rail
        even for hot input that would otherwise clip."""
        agc = AGC2()
        out = agc.process(_mk_int16_samples(amp, 512))
        assert int(np.max(np.abs(out))) <= _INT16_MAX


# ── BoundedCardinalityBucket invariants ────────────────────────────


class TestBoundedCardinalityBucketInvariants:
    @settings(max_examples=100, deadline=None)
    @given(
        values=st.lists(
            st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126)),
            min_size=0,
            max_size=200,
        ),
        maxsize=st.integers(min_value=1, max_value=50),
    )
    def test_preserved_count_never_exceeds_maxsize(
        self,
        values: list[str],
        maxsize: int,
    ) -> None:
        bucket = BoundedCardinalityBucket(maxsize=maxsize)
        for v in values:
            bucket.bucket(v)
        assert bucket.preserved_count <= maxsize

    @settings(max_examples=100, deadline=None)
    @given(
        values=st.lists(
            st.text(min_size=1, max_size=20),
            min_size=1,
            max_size=200,
        ),
        maxsize=st.integers(min_value=1, max_value=20),
    )
    def test_other_count_monotonic(
        self,
        values: list[str],
        maxsize: int,
    ) -> None:
        """Once a value buckets to 'other' the counter only grows."""
        bucket = BoundedCardinalityBucket(maxsize=maxsize)
        prev = 0
        for v in values:
            bucket.bucket(v)
            assert bucket.other_count >= prev
            prev = bucket.other_count

    @settings(max_examples=80, deadline=None)
    @given(
        values=st.lists(st.text(min_size=1, max_size=20), min_size=0, max_size=200),
        maxsize=st.integers(min_value=1, max_value=10),
    )
    def test_bucket_preserves_first_n_distinct(
        self,
        values: list[str],
        maxsize: int,
    ) -> None:
        """Insertion-order preservation invariant."""
        bucket = BoundedCardinalityBucket(maxsize=maxsize)
        first_n_distinct: list[str] = []
        seen: set[str] = set()
        for v in values:
            if v and v not in seen:
                seen.add(v)
                if len(first_n_distinct) < maxsize:
                    first_n_distinct.append(v)
            bucket.bucket(v)
        # Each preserved value should round-trip verbatim.
        for v in first_n_distinct:
            assert bucket.bucket(v) == v


# ── PipelineStateMachine invariants ────────────────────────────────


_PIPELINE_STATES = list(VoicePipelineState)


def _state_strategy() -> st.SearchStrategy[VoicePipelineState]:
    return st.sampled_from(_PIPELINE_STATES)


class TestPipelineStateMachineInvariants:
    @settings(max_examples=100, deadline=None)
    @given(
        transitions=st.lists(
            st.tuples(_state_strategy(), _state_strategy()),
            min_size=0,
            max_size=200,
        ),
        capacity=st.integers(min_value=1, max_value=64),
    )
    def test_history_bounded_by_capacity(
        self,
        transitions: list[tuple[VoicePipelineState, VoicePipelineState]],
        capacity: int,
    ) -> None:
        m = PipelineStateMachine(history_capacity=capacity)
        for f, t in transitions:
            m.record_transition(f, t)
        assert len(m.history()) <= capacity

    @settings(max_examples=100, deadline=None)
    @given(
        transitions=st.lists(
            st.tuples(_state_strategy(), _state_strategy()),
            min_size=1,
            max_size=100,
        ),
    )
    def test_current_state_matches_last_to_state(
        self,
        transitions: list[tuple[VoicePipelineState, VoicePipelineState]],
    ) -> None:
        m = PipelineStateMachine()
        for f, t in transitions:
            m.record_transition(f, t)
        assert m.current_state is transitions[-1][1]

    @settings(max_examples=100, deadline=None)
    @given(
        transitions=st.lists(
            st.tuples(_state_strategy(), _state_strategy()),
            min_size=0,
            max_size=200,
        ),
    )
    def test_transition_count_matches_call_count(
        self,
        transitions: list[tuple[VoicePipelineState, VoicePipelineState]],
    ) -> None:
        m = PipelineStateMachine()
        for f, t in transitions:
            m.record_transition(f, t)
        assert m.transition_count == len(transitions)

    @settings(max_examples=100, deadline=None)
    @given(
        transitions=st.lists(
            st.tuples(_state_strategy(), _state_strategy()),
            min_size=0,
            max_size=200,
        ),
    )
    def test_invalid_count_matches_table_disagreements(
        self,
        transitions: list[tuple[VoicePipelineState, VoicePipelineState]],
    ) -> None:
        m = PipelineStateMachine()
        expected_invalid = sum(1 for f, t in transitions if not is_transition_allowed(f, t))
        for f, t in transitions:
            m.record_transition(f, t)
        assert m.invalid_transition_count == expected_invalid


# ── HystrixGuard invariants ────────────────────────────────────────


class TestHystrixGuardInvariants:
    @settings(max_examples=50, deadline=None)
    @given(
        n_failures=st.integers(min_value=0, max_value=20),
        threshold=st.integers(min_value=1, max_value=10),
    )
    @pytest.mark.asyncio()
    async def test_failure_count_never_exceeds_actual_failures(
        self,
        n_failures: int,
        threshold: int,
    ) -> None:
        cfg = HystrixGuardConfig(
            failure_threshold=threshold,
            recovery_timeout_s=600.0,  # never auto-recover during test
            watchdog_timeout_s=None,
        )
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        for _ in range(n_failures):
            try:
                async with guard.run():
                    msg = "boom"
                    raise RuntimeError(msg)
            except RuntimeError:
                pass
            except Exception:  # noqa: BLE001 — CircuitOpenError once OPEN
                # After OPEN, subsequent calls raise CircuitOpenError
                # WITHOUT entering the body — they don't count as
                # failures (no record_failure call from the rejected
                # path). So the failure_count should remain == n_failures
                # capped at the point we tripped + 0 for rejected calls.
                pass
        assert guard.failure_count <= n_failures

    @settings(max_examples=30, deadline=None)
    @given(
        n_keys=st.integers(min_value=1, max_value=30),
        maxsize=st.integers(min_value=1, max_value=10),
    )
    def test_registry_size_bounded_by_maxsize(
        self,
        n_keys: int,
        maxsize: int,
    ) -> None:
        reg = GuardRegistry(owner=VoiceStage.STT, maxsize=maxsize)
        for i in range(n_keys):
            reg.guard_for(f"dev-{i}")
        assert len(reg) <= maxsize


# ── Generic dB sanity ──────────────────────────────────────────────


class TestDBSanity:
    @settings(max_examples=100, deadline=None)
    @given(
        amp=st.floats(min_value=1e-6, max_value=1.0, allow_nan=False),
    )
    def test_dbfs_of_constant_amp_is_negative_or_zero(self, amp: float) -> None:
        """Pure-math sanity for the dB conversion AGC2 relies on —
        any normalised amp <= 1 yields dBFS <= 0."""
        # AGC2's internal computation is RMS of int16 / 32768; for a
        # constant amp, RMS = amp; assert dBFS sign.
        assume(amp > 0)
        dbfs = 20.0 * math.log10(amp)
        assert dbfs <= 0.0

    @settings(max_examples=100, deadline=None)
    @given(
        gain_db=st.floats(min_value=-30.0, max_value=30.0, allow_nan=False),
    )
    def test_db_to_linear_to_db_roundtrip(self, gain_db: float) -> None:
        linear = 10.0 ** (gain_db / 20.0)
        # Guard against shrinker handing us numerical edge cases that
        # collapse to 0 or inf.
        assume(linear > 0 and math.isfinite(linear))
        dbfs_back = 20.0 * math.log10(linear)
        assert math.isclose(dbfs_back, gain_db, abs_tol=1e-9)
