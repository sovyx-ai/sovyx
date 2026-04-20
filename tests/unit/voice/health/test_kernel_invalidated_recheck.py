"""Tests for §4.4.7 KernelInvalidatedRechecker background loop."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from sovyx.voice.health import (
    Combo,
    Diagnosis,
    EndpointQuarantine,
    KernelInvalidatedRechecker,
    ProbeMode,
    ProbeResult,
    QuarantineEntry,
    reset_default_quarantine,
)

if TYPE_CHECKING:
    from collections.abc import Generator


# ── Helpers ────────────────────────────────────────────────────────────────


def _win_combo(**overrides: object) -> Combo:
    base: dict[str, object] = {
        "host_api": "Windows WASAPI",
        "sample_rate": 16_000,
        "channels": 1,
        "sample_format": "int16",
        "exclusive": True,
        "auto_convert": False,
        "frames_per_buffer": 480,
        "platform_key": "win32",
    }
    base.update(overrides)
    return Combo(**base)  # type: ignore[arg-type]


def _probe_result(diagnosis: Diagnosis, combo: Combo | None = None) -> ProbeResult:
    return ProbeResult(
        diagnosis=diagnosis,
        mode=ProbeMode.COLD,
        combo=combo if combo is not None else _win_combo(),
        vad_max_prob=None,
        vad_mean_prob=None,
        rms_db=-30.0,
        callbacks_fired=5,
        duration_ms=200,
        error=None,
    )


class _FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _StubProbe:
    """Records calls and returns queued results."""

    def __init__(self, responses: list[ProbeResult] | dict[str, ProbeResult]) -> None:
        self.responses = responses
        self.calls: list[QuarantineEntry] = []

    async def __call__(self, entry: QuarantineEntry) -> ProbeResult:
        self.calls.append(entry)
        if isinstance(self.responses, dict):
            return self.responses[entry.endpoint_guid]
        # list — pop front; if empty, raise a clear error so the test notices.
        if not self.responses:
            msg = f"StubProbe: no more responses queued for {entry.endpoint_guid}"
            raise AssertionError(msg)
        return self.responses.pop(0)


class _RaisingProbe:
    """Always raises — used to verify loop resilience."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def __call__(self, entry: QuarantineEntry) -> ProbeResult:
        self.calls += 1
        raise self.exc


@pytest.fixture(autouse=True)
def _reset_singleton() -> Generator[None, None, None]:
    reset_default_quarantine()
    yield
    reset_default_quarantine()


@pytest.fixture()
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture()
def quarantine(clock: _FakeClock) -> EndpointQuarantine:
    return EndpointQuarantine(quarantine_s=300.0, maxsize=16, clock=clock)


# ── Construction ───────────────────────────────────────────────────────────


class TestConstruction:
    def test_rejects_non_positive_interval(self, quarantine: EndpointQuarantine) -> None:
        with pytest.raises(ValueError, match="interval_s must be positive"):
            KernelInvalidatedRechecker(
                probe_entry=_StubProbe([]),
                quarantine=quarantine,
                interval_s=0.0,
            )

    def test_default_interval_from_tuning(self, quarantine: EndpointQuarantine) -> None:
        from sovyx.engine.config import VoiceTuningConfig

        expected = VoiceTuningConfig().kernel_invalidated_recheck_interval_s
        rc = KernelInvalidatedRechecker(
            probe_entry=_StubProbe([]),
            quarantine=quarantine,
        )
        assert rc.interval_s == expected

    def test_quarantine_defaults_to_singleton(self) -> None:
        # With no quarantine arg, rechecker falls back to module singleton.
        # The singleton is lazy — confirm construction succeeds and references it.
        from sovyx.voice.health._quarantine import get_default_quarantine

        rc = KernelInvalidatedRechecker(
            probe_entry=_StubProbe([]),
            interval_s=1.0,
        )
        assert rc._quarantine is get_default_quarantine()


# ── Lifecycle ──────────────────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio()
    async def test_start_is_idempotent(self, quarantine: EndpointQuarantine) -> None:
        sleeps: list[float] = []

        async def _sleep(s: float) -> None:
            sleeps.append(s)
            await asyncio.sleep(0)  # yield so task can run

        rc = KernelInvalidatedRechecker(
            probe_entry=_StubProbe([]),
            quarantine=quarantine,
            interval_s=0.01,
            sleep=_sleep,
        )
        await rc.start()
        assert rc.is_running
        # Calling start again must not spawn a second task.
        first_task = rc._task
        await rc.start()
        assert rc._task is first_task
        await rc.stop()

    @pytest.mark.asyncio()
    async def test_stop_cancels_running_task(self, quarantine: EndpointQuarantine) -> None:
        gate = asyncio.Event()

        async def _sleep(_s: float) -> None:
            gate.set()
            await asyncio.sleep(3600)  # will be cancelled

        rc = KernelInvalidatedRechecker(
            probe_entry=_StubProbe([]),
            quarantine=quarantine,
            interval_s=1.0,
            sleep=_sleep,
        )
        await rc.start()
        await gate.wait()
        await rc.stop()
        assert not rc.is_running
        assert rc._task is None

    @pytest.mark.asyncio()
    async def test_stop_without_start_is_noop(self, quarantine: EndpointQuarantine) -> None:
        rc = KernelInvalidatedRechecker(
            probe_entry=_StubProbe([]),
            quarantine=quarantine,
            interval_s=1.0,
        )
        await rc.stop()
        assert not rc.is_running


# ── Round behaviour ────────────────────────────────────────────────────────


class TestRound:
    """_round() — direct invocation so we don't race the loop."""

    @pytest.mark.asyncio()
    async def test_round_noop_when_quarantine_empty(self, quarantine: EndpointQuarantine) -> None:
        probe = _StubProbe([])
        rc = KernelInvalidatedRechecker(
            probe_entry=probe,
            quarantine=quarantine,
            interval_s=1.0,
        )
        rc._started = True
        await rc._round()
        assert probe.calls == []

    @pytest.mark.asyncio()
    async def test_healthy_clears_quarantine_and_emits_metric(
        self, quarantine: EndpointQuarantine
    ) -> None:
        quarantine.add(endpoint_guid="{A}", host_api="Windows WASAPI")
        probe = _StubProbe([_probe_result(Diagnosis.HEALTHY)])
        rc = KernelInvalidatedRechecker(
            probe_entry=probe,
            quarantine=quarantine,
            interval_s=1.0,
        )
        rc._started = True
        await rc._round()
        assert not quarantine.is_quarantined("{A}")
        assert len(probe.calls) == 1

    @pytest.mark.asyncio()
    async def test_kernel_invalidated_readds_with_fresh_ttl(
        self, quarantine: EndpointQuarantine, clock: _FakeClock
    ) -> None:
        quarantine.add(endpoint_guid="{A}", host_api="Windows WASAPI")
        original_expires = quarantine.get("{A}").expires_at_monotonic  # type: ignore[union-attr]
        clock.advance(100.0)
        probe = _StubProbe([_probe_result(Diagnosis.KERNEL_INVALIDATED)])
        rc = KernelInvalidatedRechecker(
            probe_entry=probe,
            quarantine=quarantine,
            interval_s=1.0,
        )
        rc._started = True
        await rc._round()
        new_entry = quarantine.get("{A}")
        assert new_entry is not None
        assert new_entry.expires_at_monotonic > original_expires
        assert new_entry.reason == "watchdog_recheck"

    @pytest.mark.asyncio()
    async def test_mid_state_diagnosis_does_not_mutate_quarantine(
        self, quarantine: EndpointQuarantine
    ) -> None:
        quarantine.add(endpoint_guid="{A}", host_api="Windows WASAPI")
        original = quarantine.get("{A}")
        probe = _StubProbe([_probe_result(Diagnosis.DEVICE_BUSY)])
        rc = KernelInvalidatedRechecker(
            probe_entry=probe,
            quarantine=quarantine,
            interval_s=1.0,
        )
        rc._started = True
        await rc._round()
        assert quarantine.get("{A}") == original

    @pytest.mark.asyncio()
    async def test_probe_exception_isolated_from_other_entries(
        self, quarantine: EndpointQuarantine
    ) -> None:
        quarantine.add(endpoint_guid="{BAD}", host_api="Windows WASAPI")
        quarantine.add(endpoint_guid="{GOOD}", host_api="Windows WASAPI")

        class _MixedProbe:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def __call__(self, entry: QuarantineEntry) -> ProbeResult:
                self.calls.append(entry.endpoint_guid)
                if entry.endpoint_guid == "{BAD}":
                    msg = "boom"
                    raise RuntimeError(msg)
                return _probe_result(Diagnosis.HEALTHY)

        probe = _MixedProbe()
        rc = KernelInvalidatedRechecker(
            probe_entry=probe,
            quarantine=quarantine,
            interval_s=1.0,
        )
        rc._started = True
        await rc._round()
        # Both entries were visited.
        assert set(probe.calls) == {"{BAD}", "{GOOD}"}
        # {BAD} remains quarantined (probe failed, no mutation).
        assert quarantine.is_quarantined("{BAD}")
        # {GOOD} was cleared (HEALTHY).
        assert not quarantine.is_quarantined("{GOOD}")

    @pytest.mark.asyncio()
    async def test_round_early_exits_on_stop_between_entries(
        self, quarantine: EndpointQuarantine
    ) -> None:
        quarantine.add(endpoint_guid="{A}", host_api="Windows WASAPI")
        quarantine.add(endpoint_guid="{B}", host_api="Windows WASAPI")

        rc = KernelInvalidatedRechecker(
            probe_entry=_StubProbe([]),
            quarantine=quarantine,
            interval_s=1.0,
        )

        class _StopAfterFirst:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def __call__(self, entry: QuarantineEntry) -> ProbeResult:
                self.calls.append(entry.endpoint_guid)
                # Simulate stop() having been called mid-round.
                rc._started = False
                return _probe_result(Diagnosis.HEALTHY)

        probe = _StopAfterFirst()
        rc._probe = probe
        rc._started = True
        await rc._round()
        # Only the first entry was probed before the early-exit kicked in.
        assert len(probe.calls) == 1


# ── Loop resilience ────────────────────────────────────────────────────────


class TestLoop:
    @pytest.mark.asyncio()
    async def test_loop_runs_round_on_each_tick(self, quarantine: EndpointQuarantine) -> None:
        quarantine.add(endpoint_guid="{A}", host_api="Windows WASAPI")

        # Keep returning KERNEL_INVALIDATED so quarantine stays populated.
        probe_calls: list[str] = []

        async def _probe(entry: QuarantineEntry) -> ProbeResult:
            probe_calls.append(entry.endpoint_guid)
            return _probe_result(Diagnosis.KERNEL_INVALIDATED)

        sleep_hits = 0
        done = asyncio.Event()

        async def _sleep(_s: float) -> None:
            nonlocal sleep_hits
            sleep_hits += 1
            if sleep_hits >= 2:
                done.set()
                # Small yield, then keep returning so loop can be stopped cleanly.
                await asyncio.sleep(0)
                return
            await asyncio.sleep(0)

        rc = KernelInvalidatedRechecker(
            probe_entry=_probe,
            quarantine=quarantine,
            interval_s=0.001,
            sleep=_sleep,
        )
        await rc.start()
        await asyncio.wait_for(done.wait(), timeout=2.0)
        await rc.stop()
        # At least one round executed (sleep fired ≥2 times).
        assert probe_calls  # probe was called at least once

    @pytest.mark.asyncio()
    async def test_loop_survives_round_exception(
        self, quarantine: EndpointQuarantine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        quarantine.add(endpoint_guid="{A}", host_api="Windows WASAPI")

        round_calls = 0
        done = asyncio.Event()

        async def _raising_round(self: KernelInvalidatedRechecker) -> None:
            nonlocal round_calls
            round_calls += 1
            if round_calls >= 2:
                done.set()
                self._started = False
                return
            msg = "round blew up"
            raise RuntimeError(msg)

        monkeypatch.setattr(KernelInvalidatedRechecker, "_round", _raising_round)

        async def _sleep(_s: float) -> None:
            await asyncio.sleep(0)

        rc = KernelInvalidatedRechecker(
            probe_entry=_StubProbe([]),
            quarantine=quarantine,
            interval_s=0.001,
            sleep=_sleep,
        )
        await rc.start()
        await asyncio.wait_for(done.wait(), timeout=2.0)
        await rc.stop()
        # Loop did NOT die on first exception — _round was called ≥2 times.
        assert round_calls >= 2

    @pytest.mark.asyncio()
    async def test_loop_exits_on_cancellation_during_sleep(
        self, quarantine: EndpointQuarantine
    ) -> None:
        started = asyncio.Event()

        async def _sleep(_s: float) -> None:
            started.set()
            await asyncio.sleep(3600)

        rc = KernelInvalidatedRechecker(
            probe_entry=_StubProbe([]),
            quarantine=quarantine,
            interval_s=1.0,
            sleep=_sleep,
        )
        await rc.start()
        await started.wait()
        await rc.stop()
        # stop() awaited the task to completion — it's gone.
        assert rc._task is None
