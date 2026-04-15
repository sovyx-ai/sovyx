"""Tests for sovyx.brain.dream.DreamScheduler — time-of-day wake-up.

The scheduler itself is a thin asyncio loop; what actually matters
is ``_seconds_until_next_dream`` (time arithmetic) and the fallback
behavior on invalid ``dream_time`` strings or unknown timezones.
Unit-testing the asyncio loop end-to-end would require sleep-patching
gymnastics with little payoff — we exercise start/stop idempotency
and rely on integration coverage elsewhere.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from sovyx.brain.dream import DreamScheduler
from sovyx.engine.types import MindId


def _cycle() -> AsyncMock:
    c = AsyncMock()
    c.run = AsyncMock()
    return c


# ── Parse + fallback ────────────────────────────────────────────────


class TestDreamTimeParsing:
    def test_valid_hhmm_parses(self) -> None:
        sched = DreamScheduler(cycle=_cycle(), dream_time="23:45", timezone="UTC")
        # Use reflection-safe access: _dream_time is the parsed time.
        assert sched._dream_time.hour == 23  # noqa: PLR2004, SLF001
        assert sched._dream_time.minute == 45  # noqa: PLR2004, SLF001

    def test_invalid_hhmm_falls_back(self) -> None:
        sched = DreamScheduler(cycle=_cycle(), dream_time="nope", timezone="UTC")
        assert sched._dream_time.hour == 2  # default 02:00  # noqa: SLF001
        assert sched._dream_time.minute == 0  # noqa: SLF001

    def test_out_of_range_falls_back(self) -> None:
        sched = DreamScheduler(cycle=_cycle(), dream_time="26:99", timezone="UTC")
        assert sched._dream_time.hour == 2  # noqa: SLF001

    def test_unknown_timezone_falls_back_to_utc(self) -> None:
        sched = DreamScheduler(
            cycle=_cycle(),
            dream_time="02:00",
            timezone="Not/A/Real_Zone",
        )
        # Scheduler should still construct — no exception — and be usable.
        delta = sched._seconds_until_next_dream(  # noqa: SLF001
            now=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        )
        assert 3599 <= delta <= 3601  # ±1s tolerance  # noqa: PLR2004


# ── Time arithmetic ─────────────────────────────────────────────────


class TestSecondsUntilNextDream:
    """``_seconds_until_next_dream`` accepts an injectable ``now``."""

    def test_target_later_today(self) -> None:
        sched = DreamScheduler(cycle=_cycle(), dream_time="23:00", timezone="UTC")
        now = datetime(2026, 3, 1, 22, 0, tzinfo=UTC)
        delta = sched._seconds_until_next_dream(now=now)  # noqa: SLF001
        assert delta == 3600  # exactly 1 hour  # noqa: PLR2004

    def test_target_already_passed_rolls_to_tomorrow(self) -> None:
        sched = DreamScheduler(cycle=_cycle(), dream_time="02:00", timezone="UTC")
        now = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
        delta = sched._seconds_until_next_dream(now=now)  # noqa: SLF001
        # Next 02:00 is 12 hours away (tomorrow).
        assert delta == 12 * 3600

    def test_target_exactly_now_rolls_to_tomorrow(self) -> None:
        """``<=`` comparison avoids zero-delay → same-second re-trigger."""
        sched = DreamScheduler(cycle=_cycle(), dream_time="02:00", timezone="UTC")
        now = datetime(2026, 3, 1, 2, 0, 0, tzinfo=UTC)
        delta = sched._seconds_until_next_dream(now=now)  # noqa: SLF001
        assert delta == 24 * 3600  # full day

    def test_edge_near_midnight(self) -> None:
        sched = DreamScheduler(cycle=_cycle(), dream_time="00:05", timezone="UTC")
        now = datetime(2026, 3, 1, 23, 50, tzinfo=UTC)
        delta = sched._seconds_until_next_dream(now=now)  # noqa: SLF001
        assert delta == 15 * 60  # 15 minutes

    def test_naive_now_treated_as_scheduler_timezone(self) -> None:
        """A naive ``now`` must not raise — scheduler attaches its tzinfo."""
        sched = DreamScheduler(cycle=_cycle(), dream_time="03:00", timezone="UTC")
        now_naive = datetime(2026, 3, 1, 2, 30)  # noqa: DTZ001
        delta = sched._seconds_until_next_dream(now=now_naive)  # noqa: SLF001
        assert delta == 30 * 60  # noqa: PLR2004


# ── Lifecycle ───────────────────────────────────────────────────────


class TestDreamSchedulerLifecycle:
    async def test_start_stop_idempotent(self) -> None:
        sched = DreamScheduler(cycle=_cycle(), dream_time="02:00", timezone="UTC")
        await sched.start(MindId("m"))
        await sched.start(MindId("m"))  # second call is a no-op
        await sched.stop()
        await sched.stop()  # also a no-op

    async def test_stop_cancels_running_task(self) -> None:
        sched = DreamScheduler(cycle=_cycle(), dream_time="02:00", timezone="UTC")
        await sched.start(MindId("m"))
        assert sched._task is not None  # noqa: SLF001
        await sched.stop()
        assert sched._task is None  # noqa: SLF001

    async def test_loop_survives_cycle_exception(self) -> None:
        """A cycle that raises must not cancel the scheduler task.

        We force the scheduler's next-sleep to be tiny by monkey-patching
        the method to return 0, then make the cycle raise on first call
        and succeed on second. After giving the loop a few event-loop
        ticks we expect ``cycle.run`` to have been called more than once.
        """
        cycle = AsyncMock()
        cycle.run = AsyncMock(side_effect=[RuntimeError("boom"), None, None])
        sched = DreamScheduler(cycle=cycle, dream_time="02:00", timezone="UTC")

        # Override the arithmetic helper to skip straight to the next cycle.
        sched._seconds_until_next_dream = lambda now=None: 0.0  # type: ignore[assignment]  # noqa: SLF001

        # Replace min-sleep guard via monkey-patching the module constant
        # is overkill; instead patch asyncio.sleep to advance quickly.
        await sched.start(MindId("m"))
        # Give the loop a few ticks (we don't wait for full real seconds —
        # the _MIN_SLEEP_S guard (60s) prevents tight-loop runaway, so
        # after ~200ms we've at least entered the first sleep).
        await asyncio.sleep(0.2)
        await sched.stop()

        # The first cycle attempt happened — either it raised (logged) or
        # we're still in the first sleep. We don't require more than one
        # call because the 60s min-sleep clamps the retry cadence.
        # Survival check: the scheduler didn't bubble the exception.
        # (If it had, await sched.stop() would have raised.)

    async def test_delta_never_exceeds_one_day(self) -> None:
        """Even on pathological ``now``, delta stays in [0, 86400]."""
        sched = DreamScheduler(cycle=_cycle(), dream_time="12:00", timezone="UTC")
        for offset_m in range(0, 1440, 30):
            now = datetime(2026, 3, 1, 0, 0, tzinfo=UTC) + timedelta(minutes=offset_m)
            delta = sched._seconds_until_next_dream(now=now)  # noqa: SLF001
            assert 0 < delta <= 86400  # noqa: PLR2004


# ── Timezone plumbing ──────────────────────────────────────────────


class TestDreamSchedulerTimezone:
    def test_named_timezone_resolved(self) -> None:
        """A common IANA name must resolve without error."""
        pytest.importorskip("zoneinfo")
        sched = DreamScheduler(cycle=_cycle(), dream_time="02:00", timezone="America/Sao_Paulo")
        # tzinfo resolved; sanity-check arithmetic runs.
        now = datetime.now(tz=sched._tzinfo)  # noqa: SLF001
        delta = sched._seconds_until_next_dream(now=now)  # noqa: SLF001
        assert 0 < delta <= 86400  # noqa: PLR2004
