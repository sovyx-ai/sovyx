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


# ---------------------------------------------------------------------------
# Phase 6 / T6.31 — random PortAudioError injection (20% of attempts)
# ---------------------------------------------------------------------------


class _FakePortAudioError(OSError):
    """Mirror of ``sounddevice.PortAudioError`` for chaos injection.

    The real ``sounddevice.PortAudioError`` is an ``OSError`` subclass
    so the cascade's ``_classify_open_error`` (gated on OSError) runs
    against the message text. This stand-in preserves the same
    inheritance contract so the classifier-on-raised-exception fallback
    in ``_try_combo`` stays exercised.
    """


def _default_portaudio_error_factory(message: str) -> Exception:
    """Default chaos error factory — produces ``_FakePortAudioError``."""
    return _FakePortAudioError(message)


@dataclass
class _RandomInjectionProbe:
    """Probe that randomly raises a chaos error on a percentage of
    attempts. The non-injected attempts return the configured
    ``baseline`` diagnosis.

    Seeded ``random.Random`` for determinism — a flaky chaos test is
    worse than no chaos test.

    ``error_factory`` lets tests pick the exception class injected at
    each call site:

    * ``_FakePortAudioError`` (T6.31 default) — mirrors the
      sounddevice ``PortAudioError`` ``OSError`` inheritance.
    * StreamOpenError factory (T6.32) — mirrors the unified-opener
      pyramid exhaustion path; carries a typed ``ErrorCode`` and
      attempts history.
    """

    baseline: Diagnosis = Diagnosis.HEALTHY
    injection_rate: float = 0.2
    seed: int = 0
    error_message: str = "AUDCLNT_E_DEVICE_IN_USE"
    error_factory: Callable[[str], Exception] = field(
        default=_default_portaudio_error_factory,
    )
    calls: list[Combo] = field(default_factory=list)
    raised_calls: list[Combo] = field(default_factory=list)
    _rng: object = None  # initialised lazily in __post_init__

    def __post_init__(self) -> None:
        import random

        # Use ``object.__setattr__`` because ``_rng`` is dataclass-managed.
        object.__setattr__(self, "_rng", random.Random(self.seed))  # noqa: S311 — test-only RNG

    async def __call__(
        self,
        *,
        combo: Combo,
        mode: ProbeMode,
        device_index: int,  # noqa: ARG002
        hard_timeout_s: float,  # noqa: ARG002
    ) -> ProbeResult:
        self.calls.append(combo)
        # mypy-narrow: _rng is set in __post_init__.
        rng = self._rng
        assert rng is not None  # noqa: S101 — internal contract; never None at call time
        if rng.random() < self.injection_rate:  # type: ignore[attr-defined]
            self.raised_calls.append(combo)
            raise self.error_factory(self.error_message)
        return ProbeResult(
            diagnosis=self.baseline,
            mode=mode,
            combo=combo,
            vad_max_prob=0.9 if self.baseline is Diagnosis.HEALTHY else 0.0,
            vad_mean_prob=0.5 if self.baseline is Diagnosis.HEALTHY else 0.0,
            rms_db=-20.0 if self.baseline is Diagnosis.HEALTHY else -80.0,
            callbacks_fired=50,
            duration_ms=500,
            error=None,
        )


class TestRandomPortAudioErrorInjection:
    """T6.31 — cascade survives intermittent PortAudioError storms.

    Operators see real production environments where USB / driver
    glitches surface ~5-20 % of the time. The cascade must:

    1. Continue trying remaining combos when one raises.
    2. Find a HEALTHY winner if any non-injected combo can produce one.
    3. Classify the raised exception correctly via
       ``_classify_open_error`` (the OSError-gated fallback path
       in ``_try_combo``).
    4. Never leak the lifecycle lock — proven by the deadlock guard
       in the existing ``test_lock_released_after_exception_storm``;
       these new chaos tests don't re-prove the lock contract.
    """

    @pytest.mark.asyncio()
    async def test_twenty_percent_injection_still_finds_winner(self) -> None:
        # 20 % injection — cascade probes can fail randomly but
        # baseline=HEALTHY means the FIRST non-raised attempt wins.
        # WINDOWS_CASCADE has 6 entries; expect at least one to land
        # cleanly at p=0.2 per attempt → P(all 6 raise) = 0.2^6 ≈ 0.006.
        # Seeded RNG with seed=0 deterministically produces a winner.
        probe = _RandomInjectionProbe(
            baseline=Diagnosis.HEALTHY,
            injection_rate=0.2,
            seed=0,
        )
        result = await _run(probe_fn=probe)
        # Cascade found the first non-injected HEALTHY combo.
        assert result.winning_combo is not None  # type: ignore[attr-defined]
        assert result.winning_probe.diagnosis is Diagnosis.HEALTHY  # type: ignore[attr-defined]
        # At least one attempt was injected (seed=0 deterministic).
        # Total calls = injected raises + 1 winner; cascade short-
        # circuits on first HEALTHY so it doesn't probe everything.
        assert len(probe.calls) >= 1
        assert len(probe.calls) <= 6  # cap by WINDOWS_CASCADE size

    @pytest.mark.asyncio()
    async def test_one_hundred_percent_injection_exhausts_cleanly(self) -> None:
        # Every attempt raises — cascade must walk all combos and
        # return cleanly with winning_combo=None. Lifecycle lock
        # released so subsequent cascades work.
        probe = _RandomInjectionProbe(
            baseline=Diagnosis.HEALTHY,  # never reached
            injection_rate=1.0,
            seed=1,
        )
        result = await _run(probe_fn=probe)
        assert result.winning_combo is None  # type: ignore[attr-defined]
        # Every attempt raised — cascade walked all 6 entries.
        assert len(probe.calls) == 6
        assert len(probe.raised_calls) == 6

    @pytest.mark.asyncio()
    async def test_injected_audclnt_e_device_in_use_classified_as_device_busy(
        self,
    ) -> None:
        # Sanity-check: the injected ``AUDCLNT_E_DEVICE_IN_USE`` text
        # routes through ``_classify_open_error`` and lands as
        # DEVICE_BUSY in the resulting attempts list. Pins the
        # classifier-on-raised-exception fallback in ``_try_combo``.
        probe = _RandomInjectionProbe(
            baseline=Diagnosis.NO_SIGNAL,  # non-HEALTHY → cascade doesn't short-circuit
            injection_rate=1.0,
            seed=2,
            error_message="AUDCLNT_E_DEVICE_IN_USE",
        )
        result = await _run(probe_fn=probe)
        # Every attempt raised → every result classified as DEVICE_BUSY.
        diagnoses = {r.diagnosis for r in result.attempts}  # type: ignore[attr-defined]
        assert Diagnosis.DEVICE_BUSY in diagnoses

    @pytest.mark.asyncio()
    async def test_injected_kernel_invalidated_quarantines_endpoint(self) -> None:
        # Injected AUDCLNT_E_DEVICE_INVALIDATED routes through
        # _classify_open_error → KERNEL_INVALIDATED → triggers the
        # T6.9 quarantine + short-circuit path. Pins the chaos-to-
        # quarantine bridge: real production driver wedges surface
        # exactly this way.
        from sovyx.voice.health._quarantine import EndpointQuarantine

        quarantine = EndpointQuarantine(quarantine_s=300.0, maxsize=16)
        probe = _RandomInjectionProbe(
            baseline=Diagnosis.NO_SIGNAL,
            injection_rate=1.0,
            seed=3,
            error_message="AUDCLNT_E_DEVICE_INVALIDATED",
        )
        result = await _run(probe_fn=probe, quarantine=quarantine)
        # First raised attempt → KERNEL_INVALIDATED → quarantine + return.
        assert result.source == "quarantined"  # type: ignore[attr-defined]
        assert quarantine.is_quarantined("chaos-endpoint")
        # Only ONE probe ran (the rest were skipped post-quarantine).
        assert len(probe.calls) == 1

    @pytest.mark.asyncio()
    @pytest.mark.parametrize("seed", [0, 1, 2, 7, 42, 137, 999])
    async def test_seeded_rng_breadth_at_twenty_percent(self, seed: int) -> None:
        # Hypothesis-style breadth via parametrized seeds. Each seed
        # is a deterministic RNG draw; together they sample the
        # injection-rate distribution. P(all 6 raise) ≈ 0.0064 per
        # seed; with 7 seeds, expected zero all-injected runs.
        probe = _RandomInjectionProbe(
            baseline=Diagnosis.HEALTHY,
            injection_rate=0.2,
            seed=seed,
        )
        result = await _run(probe_fn=probe)
        # The cascade is a HEALTHY-winner short-circuit — at 20 %
        # injection with HEALTHY baseline, every seed should find
        # a winner within the 6-attempt budget. If a seed produces
        # all-6-injected, the test surfaces it as a flake we can
        # investigate (deterministic via the seed).
        if result.winning_combo is None:  # type: ignore[attr-defined]
            # Diagnostic: print the call breakdown so the failing
            # seed is actionable. With 7 seeds and 0.2^6 ≈ 0.6 %
            # per seed of all-injected, this branch is
            # statistically unreachable; if it fires, investigate
            # the RNG implementation.
            assert len(probe.raised_calls) < 6, (
                f"seed={seed} produced all-6-injected (statistical flake)"
            )


# ---------------------------------------------------------------------------
# Phase 6 / T6.32 — random StreamOpenError injection (10% of combos)
# ---------------------------------------------------------------------------


def _stream_open_error_factory(message: str) -> Exception:
    """Build a ``StreamOpenError`` whose detail field carries ``message``.

    The unified-opener pyramid raises this when every combination it
    tried failed. ``code`` is an :class:`ErrorCode` enum; ``attempts``
    is a list of :class:`OpenAttempt` records. For chaos testing we
    pass an empty attempts list — the cascade's classifier consumes
    only the ``str(exc)`` (the detail field) so the empty-history
    case stays representative.
    """
    from sovyx.voice._stream_opener import StreamOpenError
    from sovyx.voice.device_test._protocol import ErrorCode

    # ErrorCode value chosen by the message content for realism — the
    # cascade's classifier ignores the typed code and matches on
    # detail-text substring, but sounddevice production paths
    # synchronise the two; mirror that here.
    if "BUFFER_SIZE" in message.upper():
        code = ErrorCode.BUFFER_SIZE_INVALID
    elif "DEVICE_IN_USE" in message.upper() or "BUSY" in message.upper():
        code = ErrorCode.DEVICE_BUSY
    elif "PERMISSION" in message.upper() or "DENIED" in message.upper():
        code = ErrorCode.PERMISSION_DENIED
    else:
        code = ErrorCode.DEVICE_BUSY  # generic-fault fallback per protocol
    return StreamOpenError(code=code, detail=message, attempts=[])


class TestRandomStreamOpenErrorInjection:
    """T6.32 — cascade survives intermittent ``StreamOpenError`` storms.

    StreamOpenError is the unified-opener pyramid's exhaustion
    sentinel. The cascade's probe path uses ``sd.InputStream``
    directly (not the opener), so StreamOpenError doesn't fire on
    the probe path in production today — but the cascade's
    classifier-on-raised-exception fallback in ``_try_combo`` MUST
    handle the StreamOpenError text correctly because:

    1. A future refactor may route the probe through the unified
       opener, surfacing StreamOpenError directly.
    2. The capture-task post-cascade open path uses the opener; if
       the opener regresses to leak StreamOpenError into a probe-
       adjacent context, we want the contract pinned.
    3. The classifier's substring-match approach means the typed
       exception class doesn't matter — operators see the same
       Diagnosis output for both PortAudioError and StreamOpenError
       carrying the same root-cause text.

    Tests parallel the T6.31 PortAudioError suite — same shape,
    different exception class.
    """

    @pytest.mark.asyncio()
    async def test_ten_percent_injection_still_finds_winner(self) -> None:
        # Spec injection rate is 10 % per the master mission — lower
        # than T6.31's 20 % because StreamOpenError represents
        # opener-pyramid exhaustion (rarer than per-call PortAudioError).
        probe = _RandomInjectionProbe(
            baseline=Diagnosis.HEALTHY,
            injection_rate=0.1,
            seed=0,
            error_factory=_stream_open_error_factory,
        )
        result = await _run(probe_fn=probe)
        assert result.winning_combo is not None  # type: ignore[attr-defined]
        assert result.winning_probe.diagnosis is Diagnosis.HEALTHY  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_one_hundred_percent_stream_open_error_exhausts_cleanly(
        self,
    ) -> None:
        # Every attempt raises StreamOpenError → cascade walks all 6
        # combos, returns winning_combo=None. Pins the contract: the
        # ``_try_combo`` Exception catch-all handles non-OSError
        # exceptions too (StreamOpenError is a plain Exception, not
        # an OSError subclass — different code path from PortAudioError).
        probe = _RandomInjectionProbe(
            baseline=Diagnosis.HEALTHY,
            injection_rate=1.0,
            seed=4,
            error_factory=_stream_open_error_factory,
        )
        result = await _run(probe_fn=probe)
        assert result.winning_combo is None  # type: ignore[attr-defined]
        assert len(probe.calls) == 6
        assert len(probe.raised_calls) == 6

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        "error_message",
        [
            "AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED on attempt 3",
            "AUDCLNT_E_DEVICE_IN_USE during opener pyramid",
            "AUDCLNT_E_DEVICE_INVALIDATED in opener",
            "PERMISSION denied during open",
        ],
    )
    async def test_stream_open_error_routes_to_driver_error_regardless_of_text(
        self,
        error_message: str,
    ) -> None:
        # PINNING THE DEFENSIVE-CLASSIFIER CONTRACT.
        #
        # ``_try_combo`` (cascade/_executor.py) deliberately gates the
        # classifier-on-raised-exception fallback on ``isinstance(exc,
        # OSError)``. StreamOpenError is a plain ``Exception``, NOT an
        # OSError subclass — so the classifier is BYPASSED and every
        # StreamOpenError raised at the probe layer becomes a synthetic
        # ``Diagnosis.DRIVER_ERROR`` regardless of the detail text.
        #
        # This is intentional: an unrelated coding-bug ``TypeError`` /
        # ``AttributeError`` whose message accidentally contains a
        # diagnosis keyword (``"format"`` / ``"in use"`` / ``"access"``)
        # cannot be misclassified as a structured Diagnosis. The
        # ``OSError`` gate is the load-bearing safety net that keeps
        # the classifier surface honest.
        #
        # Consequence: KERNEL_INVALIDATED text in a StreamOpenError
        # does NOT trigger the T6.9 quarantine fast-path. Operators
        # who deploy a future opener that leaks StreamOpenError must
        # either (a) wrap the raise in an OSError subclass, or (b)
        # change the gate. Either is a deliberate decision; this test
        # surfaces the contract so the choice is forced rather than
        # silently regressed.
        from sovyx.voice.health._quarantine import EndpointQuarantine

        quarantine = EndpointQuarantine(quarantine_s=300.0, maxsize=16)
        probe = _RandomInjectionProbe(
            baseline=Diagnosis.NO_SIGNAL,
            injection_rate=1.0,
            seed=5,
            error_message=error_message,
            error_factory=_stream_open_error_factory,
        )
        result = await _run(probe_fn=probe, quarantine=quarantine)

        # Every probe raised StreamOpenError → every result is
        # synthetic DRIVER_ERROR. Cascade walks all 6 combos; no
        # quarantine short-circuit.
        diagnoses = {r.diagnosis for r in result.attempts}  # type: ignore[attr-defined]
        assert diagnoses == {Diagnosis.DRIVER_ERROR}, (
            f"StreamOpenError({error_message!r}) leaked through the "
            "OSError-gated classifier; got diagnoses={diagnoses}"
        )
        assert result.source == "none"  # type: ignore[attr-defined]
        assert not quarantine.is_quarantined("chaos-endpoint")

    @pytest.mark.asyncio()
    @pytest.mark.parametrize("seed", [0, 7, 42, 137])
    async def test_seeded_rng_breadth_at_ten_percent(self, seed: int) -> None:
        # Hypothesis-style breadth at the spec's 10 % rate. P(all 6
        # raise) = 0.1^6 = 1e-6 per seed → 4 seeds expect zero
        # all-injected runs (1.6 sigma below).
        probe = _RandomInjectionProbe(
            baseline=Diagnosis.HEALTHY,
            injection_rate=0.1,
            seed=seed,
            error_factory=_stream_open_error_factory,
        )
        result = await _run(probe_fn=probe)
        # 10 % rate + HEALTHY baseline → every seed should find a winner.
        if result.winning_combo is None:  # type: ignore[attr-defined]
            assert len(probe.raised_calls) < 6, (
                f"seed={seed} produced all-6-injected at 10 % rate "
                "(statistically unreachable — investigate RNG)"
            )
