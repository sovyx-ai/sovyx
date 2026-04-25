"""Tests for :mod:`sovyx.engine._backoff` (band-aid #10 foundation).

Covers BackoffPolicy bound enforcement, BackoffSchedule
deterministic schedule (NONE jitter), per-strategy jitter
ranges (FULL / EQUAL), exhausted iteration, thread safety,
and schedule reset.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25
band-aid #10; F1 inventory entry #10.
"""

from __future__ import annotations

import threading

import pytest

from sovyx.engine._backoff import (
    BackoffPolicy,
    BackoffSchedule,
    JitterStrategy,
)

# ── BackoffPolicy bound enforcement ───────────────────────────────


class TestBackoffPolicyBounds:
    def test_canonical_defaults(self) -> None:
        p = BackoffPolicy()
        assert p.base_delay_s == 0.5  # noqa: PLR2004
        assert p.max_delay_s == 60.0  # noqa: PLR2004
        assert p.multiplier == 2.0  # noqa: PLR2004
        assert p.max_attempts == 10  # noqa: PLR2004
        assert p.jitter is JitterStrategy.FULL

    def test_base_below_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="base_delay_s"):
            BackoffPolicy(base_delay_s=0.0001)

    def test_base_above_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="base_delay_s"):
            BackoffPolicy(base_delay_s=601.0)

    def test_max_below_base_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_delay_s.*must be >= base"):
            BackoffPolicy(base_delay_s=10.0, max_delay_s=5.0)

    def test_multiplier_below_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="multiplier"):
            BackoffPolicy(multiplier=0.5)

    def test_multiplier_above_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="multiplier"):
            BackoffPolicy(multiplier=11.0)

    def test_max_attempts_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            BackoffPolicy(max_attempts=0)

    def test_frozen_dataclass(self) -> None:
        p = BackoffPolicy()
        with pytest.raises((AttributeError, TypeError)):
            p.base_delay_s = 1.0  # type: ignore[misc]


# ── JitterStrategy enum ───────────────────────────────────────────


class TestJitterStrategy:
    def test_three_strategies(self) -> None:
        values = {s.value for s in JitterStrategy}
        assert values == {"none", "full", "equal"}

    def test_str_enum_value_comparison(self) -> None:
        assert JitterStrategy.FULL == "full"


# ── BackoffSchedule — NONE jitter (deterministic) ─────────────────


class TestBackoffScheduleDeterministic:
    def test_none_jitter_yields_exact_exponential(self) -> None:
        policy = BackoffPolicy(
            base_delay_s=1.0,
            max_delay_s=100.0,
            multiplier=2.0,
            max_attempts=5,
            jitter=JitterStrategy.NONE,
        )
        schedule = BackoffSchedule(policy)
        delays = list(schedule)
        assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]

    def test_none_jitter_caps_at_max_delay(self) -> None:
        policy = BackoffPolicy(
            base_delay_s=1.0,
            max_delay_s=10.0,
            multiplier=2.0,
            max_attempts=10,
            jitter=JitterStrategy.NONE,
        )
        delays = list(BackoffSchedule(policy))
        # Attempts: 1, 2, 4, 8, 10, 10, 10, 10, 10, 10
        assert delays[:4] == [1.0, 2.0, 4.0, 8.0]
        assert all(d == 10.0 for d in delays[4:])  # noqa: PLR2004

    def test_constant_multiplier_degenerates_to_linear(self) -> None:
        policy = BackoffPolicy(
            base_delay_s=2.0,
            max_delay_s=100.0,
            multiplier=1.0,  # legacy band-aid behaviour
            max_attempts=5,
            jitter=JitterStrategy.NONE,
        )
        delays = list(BackoffSchedule(policy))
        assert delays == [2.0, 2.0, 2.0, 2.0, 2.0]


# ── BackoffSchedule — jitter strategies ────────────────────────────


class TestBackoffScheduleJitter:
    def test_full_jitter_within_zero_to_computed(self) -> None:
        policy = BackoffPolicy(
            base_delay_s=10.0,
            max_delay_s=100.0,
            multiplier=2.0,
            max_attempts=20,
            jitter=JitterStrategy.FULL,
        )
        schedule = BackoffSchedule(policy, seed=42)
        delays = list(schedule)
        # Computed sequence: 10, 20, 40, 80, 100, 100, 100, 100, ...
        # FULL jitter draws each from [0, computed], so every
        # delay must be <= the corresponding computed value.
        computed = [10.0, 20.0, 40.0, 80.0] + [100.0] * 16
        for d, c in zip(delays, computed, strict=True):
            assert 0.0 <= d <= c

    def test_equal_jitter_within_half_to_computed(self) -> None:
        policy = BackoffPolicy(
            base_delay_s=10.0,
            max_delay_s=100.0,
            multiplier=2.0,
            max_attempts=20,
            jitter=JitterStrategy.EQUAL,
        )
        delays = list(BackoffSchedule(policy, seed=42))
        computed = [10.0, 20.0, 40.0, 80.0] + [100.0] * 16
        for d, c in zip(delays, computed, strict=True):
            assert c / 2.0 <= d <= c

    def test_seed_makes_jitter_deterministic(self) -> None:
        """Two schedules with the same seed produce the same
        jittered sequence."""
        policy = BackoffPolicy(jitter=JitterStrategy.FULL, max_attempts=20)
        a = BackoffSchedule(policy, seed=99)
        b = BackoffSchedule(policy, seed=99)
        assert list(a) == list(b)


# ── Exhaustion + iteration semantics ──────────────────────────────


class TestScheduleExhaustion:
    def test_max_attempts_count_yields(self) -> None:
        policy = BackoffPolicy(
            base_delay_s=0.5,
            max_attempts=3,
            jitter=JitterStrategy.NONE,
        )
        schedule = BackoffSchedule(policy)
        delays = list(schedule)
        assert len(delays) == 3

    def test_exhausted_property_flips(self) -> None:
        policy = BackoffPolicy(max_attempts=2, jitter=JitterStrategy.NONE)
        schedule = BackoffSchedule(policy)
        assert schedule.exhausted is False
        schedule.next()
        assert schedule.exhausted is False
        schedule.next()
        assert schedule.exhausted is True

    def test_next_after_exhaustion_raises(self) -> None:
        policy = BackoffPolicy(max_attempts=1, jitter=JitterStrategy.NONE)
        schedule = BackoffSchedule(policy)
        schedule.next()
        with pytest.raises(StopIteration):
            schedule.next()

    def test_iteration_protocol(self) -> None:
        policy = BackoffPolicy(max_attempts=4, jitter=JitterStrategy.NONE)
        schedule = BackoffSchedule(policy)
        count = 0
        for _ in schedule:
            count += 1
        assert count == 4

    def test_attempt_count_tracks_calls(self) -> None:
        policy = BackoffPolicy(max_attempts=10, jitter=JitterStrategy.NONE)
        schedule = BackoffSchedule(policy)
        schedule.next()
        schedule.next()
        assert schedule.attempt_count == 2  # noqa: PLR2004


# ── Reset ─────────────────────────────────────────────────────────


class TestReset:
    def test_reset_zeros_attempt_count(self) -> None:
        policy = BackoffPolicy(max_attempts=10, jitter=JitterStrategy.NONE)
        schedule = BackoffSchedule(policy)
        for _ in range(3):
            schedule.next()
        assert schedule.attempt_count == 3  # noqa: PLR2004
        schedule.reset()
        assert schedule.attempt_count == 0
        assert schedule.exhausted is False

    def test_reset_resumes_from_attempt_zero(self) -> None:
        """After reset, the next delay matches attempt 0
        (base_delay_s, before any exponentiation)."""
        policy = BackoffPolicy(
            base_delay_s=1.0,
            multiplier=2.0,
            max_attempts=10,
            jitter=JitterStrategy.NONE,
        )
        schedule = BackoffSchedule(policy)
        for _ in range(5):
            schedule.next()
        schedule.reset()
        assert schedule.next() == 1.0


# ── Thread safety ─────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_next_calls_consistent(self) -> None:
        policy = BackoffPolicy(max_attempts=200, jitter=JitterStrategy.NONE)
        schedule = BackoffSchedule(policy)
        n_threads = 8
        per_thread = 25
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                barrier.wait()
                for _ in range(per_thread):
                    schedule.next()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        # No counter loss — each thread incremented exactly per_thread
        # attempts; total must equal n_threads * per_thread.
        assert schedule.attempt_count == n_threads * per_thread


# ── Module exports ────────────────────────────────────────────────


class TestPublicSurface:
    def test_all_exports(self) -> None:
        from sovyx.engine import _backoff

        assert set(_backoff.__all__) == {
            "BackoffPolicy",
            "BackoffSchedule",
            "JitterStrategy",
        }
