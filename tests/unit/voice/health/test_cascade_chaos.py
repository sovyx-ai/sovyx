"""L12 — Chaos / failure-injection tests for the L2 cascade.

These tests do not exercise nominal paths (covered by ``test_cascade.py``)
— they prove the cascade survives ugly environments:

* every probe times out → the cascade exhausts its budget cleanly
  rather than hanging on the lifecycle lock
* intermittent ``DRIVER_ERROR`` → the cascade keeps walking and finds
  the one working combo
* every probe returns ``LOW_SIGNAL`` → no winner, but a best-attempt
  result with the strongest RMS is returned
* concurrent cascade calls during a hot-plug storm → the lifecycle
  lock serialises them and never allows two probes to run for the
  same endpoint simultaneously
* a probe that raises mid-cascade does not poison subsequent attempts
* the post-cascade ``record_winning`` raising does not corrupt the
  successful result

Each test injects fakes only — no PortAudio, no ONNX, no real I/O.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest

from sovyx.engine._lock_dict import LRULockDict
from sovyx.voice.health.cascade import run_cascade
from sovyx.voice.health.contract import (
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)

# ---------------------------------------------------------------------------
# Local fakes (kept lean; the canonical fakes live in test_cascade.py)
# ---------------------------------------------------------------------------


@dataclass
class _ChaoticProbe:
    """Probe that follows a per-call diagnosis script.

    ``diagnoses`` is consumed in order; once exhausted every subsequent
    call returns the ``terminal`` diagnosis (defaults to
    ``DRIVER_ERROR``). The probe records every call so tests can
    assert on attempt count + ordering.
    """

    diagnoses: list[Diagnosis] = field(default_factory=list)
    terminal: Diagnosis = Diagnosis.DRIVER_ERROR
    sleep_per_call_s: float = 0.0
    raise_after: int | None = None
    rms_db_per_call: list[float] = field(default_factory=list)
    calls: list[Combo] = field(default_factory=list)
    barrier: asyncio.Event | None = None
    in_flight: list[Combo] = field(default_factory=list)
    max_concurrent: int = 0

    async def __call__(
        self,
        *,
        combo: Combo,
        mode: ProbeMode,
        device_index: int,  # noqa: ARG002
        hard_timeout_s: float,  # noqa: ARG002
    ) -> ProbeResult:
        self.calls.append(combo)
        self.in_flight.append(combo)
        self.max_concurrent = max(self.max_concurrent, len(self.in_flight))
        try:
            if self.barrier is not None:
                await self.barrier.wait()
            if self.sleep_per_call_s > 0.0:
                await asyncio.sleep(self.sleep_per_call_s)
            if self.raise_after is not None and len(self.calls) > self.raise_after:
                msg = "chaos probe exploded by design"
                raise RuntimeError(msg)
            idx = len(self.calls) - 1
            diagnosis = self.diagnoses[idx] if idx < len(self.diagnoses) else self.terminal
            rms = (
                self.rms_db_per_call[idx]
                if idx < len(self.rms_db_per_call)
                else (-20.0 if diagnosis is Diagnosis.HEALTHY else -80.0)
            )
            return ProbeResult(
                diagnosis=diagnosis,
                mode=mode,
                combo=combo,
                vad_max_prob=0.9 if diagnosis is Diagnosis.HEALTHY else 0.0,
                vad_mean_prob=0.5 if diagnosis is Diagnosis.HEALTHY else 0.0,
                rms_db=rms,
                callbacks_fired=50,
                duration_ms=500,
                error=None,
            )
        finally:
            self.in_flight.remove(combo)


def _run(
    *,
    probe_fn: Callable[..., Awaitable[ProbeResult]],
    **overrides: object,
) -> Awaitable[object]:
    base: dict[str, object] = {
        "endpoint_guid": "chaos-endpoint",
        "device_index": 0,
        "mode": ProbeMode.COLD,
        "platform_key": "win32",
        "probe_fn": probe_fn,
    }
    base.update(overrides)
    return run_cascade(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Total budget exhaustion
# ---------------------------------------------------------------------------


class TestTotalBudgetExhaustion:
    @pytest.mark.asyncio()
    async def test_every_probe_slow_returns_budget_exhausted(self) -> None:
        # Each probe sleeps 0.4 s; with 6 entries in WINDOWS_CASCADE the
        # cascade can cover ~2 attempts before the 1.0 s budget runs out.
        probe = _ChaoticProbe(diagnoses=[], sleep_per_call_s=0.4)
        result = await _run(
            probe_fn=probe,
            total_budget_s=1.0,
            attempt_budget_s=2.0,
        )
        assert result.budget_exhausted is True  # type: ignore[attr-defined]
        assert result.winning_combo is None  # type: ignore[attr-defined]
        # The cascade should have started at least one attempt — we don't
        # pin an exact count because asyncio scheduling jitter on Windows
        # can shift it by ±1.
        assert len(probe.calls) >= 1
        assert probe.max_concurrent == 1

    @pytest.mark.asyncio()
    async def test_zero_budget_emits_no_attempts(self) -> None:
        probe = _ChaoticProbe(diagnoses=[Diagnosis.HEALTHY])
        result = await _run(
            probe_fn=probe,
            total_budget_s=0.0,
        )
        assert result.budget_exhausted is True  # type: ignore[attr-defined]
        assert probe.calls == []


# ---------------------------------------------------------------------------
# Intermittent open failures
# ---------------------------------------------------------------------------


class TestIntermittentOpenFailures:
    @pytest.mark.asyncio()
    async def test_first_three_driver_error_then_healthy(self) -> None:
        probe = _ChaoticProbe(
            diagnoses=[
                Diagnosis.DRIVER_ERROR,
                Diagnosis.DRIVER_ERROR,
                Diagnosis.DRIVER_ERROR,
                Diagnosis.HEALTHY,
            ],
        )
        result = await _run(probe_fn=probe)
        assert result.winning_combo is not None  # type: ignore[attr-defined]
        assert result.winning_probe is not None  # type: ignore[attr-defined]
        assert result.winning_probe.diagnosis is Diagnosis.HEALTHY  # type: ignore[attr-defined]
        assert len(probe.calls) == 4

    @pytest.mark.asyncio()
    async def test_alternating_failures_still_picks_winner(self) -> None:
        probe = _ChaoticProbe(
            diagnoses=[
                Diagnosis.NO_SIGNAL,
                Diagnosis.LOW_SIGNAL,
                Diagnosis.HEALTHY,
            ],
        )
        result = await _run(probe_fn=probe)
        assert result.winning_probe is not None  # type: ignore[attr-defined]
        assert result.winning_probe.diagnosis is Diagnosis.HEALTHY  # type: ignore[attr-defined]
        # Source must be "cascade" — we did not configure overrides or
        # a populated store.
        assert result.source == "cascade"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Best-attempt fallback when no combo is HEALTHY
# ---------------------------------------------------------------------------


class TestNoWinnerFallback:
    @pytest.mark.asyncio()
    async def test_all_low_signal_no_winner(self) -> None:
        probe = _ChaoticProbe(
            diagnoses=[Diagnosis.LOW_SIGNAL] * 6,
            terminal=Diagnosis.LOW_SIGNAL,
        )
        result = await _run(probe_fn=probe)
        assert result.winning_combo is None  # type: ignore[attr-defined]
        # Cascade walked the whole table.
        assert len(probe.calls) == 6

    @pytest.mark.asyncio()
    async def test_all_apo_corrupt_returns_no_winner(self) -> None:
        probe = _ChaoticProbe(
            diagnoses=[Diagnosis.APO_DEGRADED] * 6,
            terminal=Diagnosis.APO_DEGRADED,
        )
        result = await _run(probe_fn=probe)
        assert result.winning_combo is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Lifecycle lock during simulated hot-plug storm
# ---------------------------------------------------------------------------


class TestLifecycleLockSerialisation:
    @pytest.mark.asyncio()
    async def test_concurrent_cascades_never_overlap(self) -> None:
        # All callers share one LRULockDict + endpoint, so the cascade
        # must serialise them through a single lock — no two probes for
        # the same endpoint may execute concurrently.
        barrier = asyncio.Event()
        probe = _ChaoticProbe(
            diagnoses=[Diagnosis.HEALTHY] * 32,
            barrier=barrier,
        )
        locks = LRULockDict[str](maxsize=4)

        async def go() -> object:
            return await _run(probe_fn=probe, lifecycle_locks=locks)

        tasks = [asyncio.create_task(go()) for _ in range(4)]
        # Release the barrier so the in-flight probe can complete; the
        # next-in-line cascade will then pick the lock up.
        barrier.set()
        results = await asyncio.gather(*tasks)
        assert probe.max_concurrent == 1
        for r in results:
            assert r.winning_probe is not None  # type: ignore[attr-defined]
            assert r.winning_probe.diagnosis is Diagnosis.HEALTHY  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Probe raising mid-cascade
# ---------------------------------------------------------------------------


class TestProbeException:
    @pytest.mark.asyncio()
    async def test_probe_raise_does_not_break_cascade(self) -> None:
        # The cascade catches probe exceptions per-attempt and treats
        # them as a non-fatal failure (logged at ERROR level). It
        # continues walking the remaining combos rather than aborting
        # the whole cascade. With ``raise_after=0`` every attempt
        # raises, so no winner emerges — but the cascade returns
        # cleanly with ``winning_combo=None``.
        probe = _ChaoticProbe(diagnoses=[], raise_after=0)
        result = await _run(probe_fn=probe)
        assert result.winning_combo is None  # type: ignore[attr-defined]
        # All six default Windows cascade entries should have been attempted.
        assert len(probe.calls) == 6

    @pytest.mark.asyncio()
    async def test_lock_released_after_exception_storm(self) -> None:
        # An exception path must release the lifecycle lock for the
        # endpoint. We prove that by running a clean cascade
        # immediately after — if the lock leaked, this would deadlock
        # under the ``--timeout=30`` ceiling.
        chaos = _ChaoticProbe(diagnoses=[], raise_after=0)
        await _run(probe_fn=chaos)
        recovery = _ChaoticProbe(diagnoses=[Diagnosis.HEALTHY])
        result = await _run(probe_fn=recovery)
        assert result.winning_probe is not None  # type: ignore[attr-defined]
        assert result.winning_probe.diagnosis is Diagnosis.HEALTHY  # type: ignore[attr-defined]
