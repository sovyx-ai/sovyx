"""Tests for :mod:`sovyx.voice._hystrix_guard`.

Covers R1's three-defence primitive:

* Circuit breaker — CLOSED → OPEN on N failures, HALF_OPEN after
  recovery_timeout_s, CLOSED again on probe success, OPEN again on
  probe failure.
* Bulkhead — fail-fast :class:`BulkheadFullError` once
  ``max_concurrent`` slots are taken (no blocking).
* Watchdog — :func:`asyncio.timeout`-based deadline raising
  :class:`WatchdogFiredError` (counted as a CB failure).

Plus the :class:`GuardRegistry` LRU eviction discipline.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.10
R1, Netflix Hystrix wiki.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sovyx.voice._hystrix_guard import (
    BulkheadFullError,
    CircuitOpenError,
    CircuitState,
    GuardRegistry,
    HystrixGuard,
    HystrixGuardConfig,
    HystrixGuardError,
    WatchdogFiredError,
)
from sovyx.voice._stage_metrics import VoiceStage

# ── HystrixGuardConfig — bound enforcement ──────────────────────────


class TestHystrixGuardConfig:
    def test_canonical_defaults(self) -> None:
        cfg = HystrixGuardConfig()
        assert cfg.failure_threshold == 3
        assert cfg.recovery_timeout_s == 30.0
        assert cfg.max_concurrent == 4
        assert cfg.watchdog_timeout_s == 10.0

    def test_failure_threshold_below_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="failure_threshold must be in"):
            HystrixGuardConfig(failure_threshold=0)

    def test_failure_threshold_above_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="failure_threshold must be in"):
            HystrixGuardConfig(failure_threshold=101)

    def test_recovery_timeout_below_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="recovery_timeout_s must be in"):
            HystrixGuardConfig(recovery_timeout_s=0.5)

    def test_recovery_timeout_above_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="recovery_timeout_s must be in"):
            HystrixGuardConfig(recovery_timeout_s=601.0)

    def test_max_concurrent_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_concurrent must be in"):
            HystrixGuardConfig(max_concurrent=0)

    def test_max_concurrent_above_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_concurrent must be in"):
            HystrixGuardConfig(max_concurrent=257)

    def test_watchdog_none_allowed(self) -> None:
        cfg = HystrixGuardConfig(watchdog_timeout_s=None)
        assert cfg.watchdog_timeout_s is None

    def test_watchdog_below_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="watchdog_timeout_s must be"):
            HystrixGuardConfig(watchdog_timeout_s=0.05)

    def test_watchdog_above_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="watchdog_timeout_s must be"):
            HystrixGuardConfig(watchdog_timeout_s=601.0)

    def test_frozen_dataclass(self) -> None:
        cfg = HystrixGuardConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.failure_threshold = 99  # type: ignore[misc]


# ── CircuitState enum ───────────────────────────────────────────────


class TestCircuitState:
    def test_three_states(self) -> None:
        assert {s.value for s in CircuitState} == {"closed", "open", "half_open"}

    def test_str_enum_value_comparison(self) -> None:
        assert CircuitState.CLOSED == "closed"


# ── HystrixGuard — happy path ──────────────────────────────────────


class TestHystrixGuardHappyPath:
    @pytest.mark.asyncio()
    async def test_starts_closed(self) -> None:
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A")
        assert guard.state is CircuitState.CLOSED
        assert guard.failure_count == 0

    @pytest.mark.asyncio()
    async def test_successful_call_keeps_closed(self) -> None:
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A")
        async with guard.run():
            pass
        assert guard.state is CircuitState.CLOSED
        assert guard.failure_count == 0

    @pytest.mark.asyncio()
    async def test_owner_and_key_exposed(self) -> None:
        guard = HystrixGuard(owner=VoiceStage.TTS, key="dev-key")
        assert guard.owner is VoiceStage.TTS
        assert guard.key == "dev-key"


# ── Circuit breaker transitions ────────────────────────────────────


class TestCircuitBreaker:
    @pytest.mark.asyncio()
    async def test_n_failures_open_circuit(self) -> None:
        cfg = HystrixGuardConfig(failure_threshold=3, watchdog_timeout_s=None)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        for _ in range(3):
            with pytest.raises(RuntimeError):  # noqa: PT012
                async with guard.run():
                    msg = "boom"
                    raise RuntimeError(msg)
        assert guard.state is CircuitState.OPEN
        assert guard.failure_count == 3

    @pytest.mark.asyncio()
    async def test_open_rejects_with_circuit_open_error(self) -> None:
        cfg = HystrixGuardConfig(failure_threshold=1, watchdog_timeout_s=None)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        with pytest.raises(RuntimeError):  # noqa: PT012
            async with guard.run():
                msg = "boom"
                raise RuntimeError(msg)
        assert guard.state is CircuitState.OPEN
        with pytest.raises(CircuitOpenError, match="circuit OPEN"):
            async with guard.run():
                pass

    @pytest.mark.asyncio()
    async def test_recovery_timeout_promotes_to_half_open(self) -> None:
        cfg = HystrixGuardConfig(
            failure_threshold=1,
            recovery_timeout_s=1.0,
            watchdog_timeout_s=None,
        )
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        # Inject a fake monotonic clock so we don't sleep.
        fake_now = [0.0]
        guard._monotonic = lambda: fake_now[0]  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):  # noqa: PT012
            async with guard.run():
                msg = "boom"
                raise RuntimeError(msg)
        assert guard.state is CircuitState.OPEN
        # Advance past recovery_timeout_s.
        fake_now[0] = 5.0
        assert guard.state is CircuitState.HALF_OPEN

    @pytest.mark.asyncio()
    async def test_half_open_success_closes_circuit(self) -> None:
        cfg = HystrixGuardConfig(
            failure_threshold=1,
            recovery_timeout_s=1.0,
            watchdog_timeout_s=None,
        )
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        fake_now = [0.0]
        guard._monotonic = lambda: fake_now[0]  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):  # noqa: PT012
            async with guard.run():
                msg = "boom"
                raise RuntimeError(msg)
        fake_now[0] = 5.0
        # Successful probe.
        async with guard.run():
            pass
        assert guard.state is CircuitState.CLOSED
        assert guard.failure_count == 0

    @pytest.mark.asyncio()
    async def test_half_open_failure_immediately_reopens(self) -> None:
        """Hystrix canonical: ONE failed probe re-opens, doesn't wait
        for failure_threshold again."""
        cfg = HystrixGuardConfig(
            failure_threshold=5,
            recovery_timeout_s=1.0,
            watchdog_timeout_s=None,
        )
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        fake_now = [0.0]
        guard._monotonic = lambda: fake_now[0]  # type: ignore[method-assign]
        # Trip with 5 failures.
        for _ in range(5):
            with pytest.raises(RuntimeError):  # noqa: PT012
                async with guard.run():
                    msg = "boom"
                    raise RuntimeError(msg)
        assert guard.state is CircuitState.OPEN
        fake_now[0] = 5.0
        # First probe fails — should re-open immediately, NOT need 5 more failures.
        with pytest.raises(RuntimeError):  # noqa: PT012
            async with guard.run():
                msg = "boom2"
                raise RuntimeError(msg)
        assert guard.state is CircuitState.OPEN

    @pytest.mark.asyncio()
    async def test_success_resets_failure_count_below_threshold(self) -> None:
        cfg = HystrixGuardConfig(failure_threshold=3, watchdog_timeout_s=None)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        # 2 failures (below threshold).
        for _ in range(2):
            with pytest.raises(RuntimeError):  # noqa: PT012
                async with guard.run():
                    msg = "boom"
                    raise RuntimeError(msg)
        assert guard.failure_count == 2
        # 1 success — failure count should reset.
        async with guard.run():
            pass
        assert guard.failure_count == 0
        assert guard.state is CircuitState.CLOSED


# ── Bulkhead ───────────────────────────────────────────────────────


class TestBulkhead:
    @pytest.mark.asyncio()
    async def test_max_concurrent_blocks_extra_caller(self) -> None:
        cfg = HystrixGuardConfig(max_concurrent=2, watchdog_timeout_s=None)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)

        gate = asyncio.Event()
        entered = asyncio.Event()
        entered_count = 0

        async def long_call() -> None:
            nonlocal entered_count
            async with guard.run():
                entered_count += 1
                if entered_count >= 2:
                    entered.set()
                await gate.wait()

        t1 = asyncio.create_task(long_call())
        t2 = asyncio.create_task(long_call())
        await entered.wait()
        # 3rd call must fail-fast — bulkhead full.
        with pytest.raises(BulkheadFullError, match="bulkhead full"):
            async with guard.run():
                pass
        gate.set()
        await asyncio.gather(t1, t2)

    @pytest.mark.asyncio()
    async def test_slot_released_on_exception(self) -> None:
        cfg = HystrixGuardConfig(
            failure_threshold=100,  # never trip CB
            max_concurrent=1,
            watchdog_timeout_s=None,
        )
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        with pytest.raises(RuntimeError):  # noqa: PT012
            async with guard.run():
                msg = "boom"
                raise RuntimeError(msg)
        # Slot should be free again.
        async with guard.run():
            pass

    @pytest.mark.asyncio()
    async def test_available_slots_initial(self) -> None:
        cfg = HystrixGuardConfig(max_concurrent=5, watchdog_timeout_s=None)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        assert guard.available_slots == 5


# ── Watchdog ───────────────────────────────────────────────────────


class TestWatchdog:
    @pytest.mark.asyncio()
    async def test_fast_call_within_deadline_succeeds(self) -> None:
        cfg = HystrixGuardConfig(watchdog_timeout_s=1.0)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        async with guard.run():
            await asyncio.sleep(0.01)
        assert guard.state is CircuitState.CLOSED

    @pytest.mark.asyncio()
    async def test_slow_call_fires_watchdog(self) -> None:
        cfg = HystrixGuardConfig(watchdog_timeout_s=0.1)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        with pytest.raises(WatchdogFiredError, match="watchdog fired"):
            async with guard.run():
                await asyncio.sleep(2.0)

    @pytest.mark.asyncio()
    async def test_watchdog_fire_counts_as_circuit_failure(self) -> None:
        cfg = HystrixGuardConfig(failure_threshold=2, watchdog_timeout_s=0.1)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        for _ in range(2):
            with pytest.raises(WatchdogFiredError):  # noqa: PT012
                async with guard.run():
                    await asyncio.sleep(2.0)
        assert guard.state is CircuitState.OPEN
        assert guard.failure_count == 2

    @pytest.mark.asyncio()
    async def test_watchdog_disabled_allows_long_calls(self) -> None:
        cfg = HystrixGuardConfig(watchdog_timeout_s=None)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        async with guard.run():
            await asyncio.sleep(0.05)
        assert guard.state is CircuitState.CLOSED


# ── Exception hierarchy ────────────────────────────────────────────


class TestExceptionHierarchy:
    def test_all_inherit_from_base(self) -> None:
        assert issubclass(CircuitOpenError, HystrixGuardError)
        assert issubclass(BulkheadFullError, HystrixGuardError)
        assert issubclass(WatchdogFiredError, HystrixGuardError)

    @pytest.mark.asyncio()
    async def test_caller_can_catch_base(self) -> None:
        cfg = HystrixGuardConfig(failure_threshold=1, watchdog_timeout_s=None)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        with pytest.raises(RuntimeError):  # noqa: PT012
            async with guard.run():
                msg = "boom"
                raise RuntimeError(msg)
        with pytest.raises(HystrixGuardError):
            async with guard.run():
                pass


# ── GuardRegistry — LRU + dedup ────────────────────────────────────


class TestGuardRegistry:
    def test_returns_same_guard_for_same_key(self) -> None:
        reg = GuardRegistry(owner=VoiceStage.STT)
        a = reg.guard_for("dev-A")
        b = reg.guard_for("dev-A")
        assert a is b

    def test_distinct_keys_distinct_guards(self) -> None:
        reg = GuardRegistry(owner=VoiceStage.STT)
        a = reg.guard_for("dev-A")
        b = reg.guard_for("dev-B")
        assert a is not b

    def test_empty_key_rejected(self) -> None:
        reg = GuardRegistry(owner=VoiceStage.STT)
        with pytest.raises(ValueError, match="key must be a non-empty"):
            reg.guard_for("")

    def test_lru_evicts_oldest(self) -> None:
        reg = GuardRegistry(owner=VoiceStage.STT, maxsize=3)
        guards = {k: reg.guard_for(k) for k in ("a", "b", "c")}
        assert len(reg) == 3
        # Touch 'a' so 'b' becomes the oldest.
        reg.guard_for("a")
        # Add 'd' — should evict 'b'.
        reg.guard_for("d")
        assert len(reg) == 3
        # Re-fetching 'b' should produce a NEW guard (different identity).
        new_b = reg.guard_for("b")
        assert new_b is not guards["b"]
        # 'a' and 'c' and 'd' should still be the originals where possible.
        assert reg.guard_for("a") is guards["a"]

    def test_maxsize_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="maxsize must be"):
            GuardRegistry(owner=VoiceStage.STT, maxsize=0)

    def test_uses_passed_config(self) -> None:
        cfg = HystrixGuardConfig(failure_threshold=7, watchdog_timeout_s=None)
        reg = GuardRegistry(owner=VoiceStage.STT, config=cfg)
        guard = reg.guard_for("dev-A")
        assert guard._config.failure_threshold == 7

    def test_owner_propagated(self) -> None:
        reg = GuardRegistry(owner=VoiceStage.TTS)
        guard = reg.guard_for("dev-A")
        assert guard.owner is VoiceStage.TTS

    def test_reset_clears_cache(self) -> None:
        reg = GuardRegistry(owner=VoiceStage.STT)
        a1 = reg.guard_for("dev-A")
        reg.reset()
        a2 = reg.guard_for("dev-A")
        assert a1 is not a2


# ── Per-key isolation ──────────────────────────────────────────────


class TestPerKeyIsolation:
    @pytest.mark.asyncio()
    async def test_one_key_failing_does_not_open_other(self) -> None:
        cfg = HystrixGuardConfig(failure_threshold=1, watchdog_timeout_s=None)
        reg = GuardRegistry(owner=VoiceStage.STT, config=cfg)
        bad = reg.guard_for("dev-bad")
        good = reg.guard_for("dev-good")
        with pytest.raises(RuntimeError):  # noqa: PT012
            async with bad.run():
                msg = "boom"
                raise RuntimeError(msg)
        assert bad.state is CircuitState.OPEN
        # Other key's guard untouched.
        assert good.state is CircuitState.CLOSED
        async with good.run():
            pass


# ── Telemetry sanity (events flow through M2) ───────────────────────


class TestTelemetryIntegration:
    @pytest.mark.asyncio()
    async def test_circuit_open_emits_drop_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import _hystrix_guard

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(_hystrix_guard, "record_stage_event", _capture)
        cfg = HystrixGuardConfig(failure_threshold=1, watchdog_timeout_s=None)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        with pytest.raises(RuntimeError):  # noqa: PT012
            async with guard.run():
                msg = "boom"
                raise RuntimeError(msg)
        # Now circuit is OPEN — next call should record a DROP w/ circuit_open.
        with pytest.raises(CircuitOpenError):
            async with guard.run():
                pass
        kinds = [(s, k.value, et) for (s, k, et) in recorded]
        assert (VoiceStage.STT, "drop", "circuit_open") in kinds

    @pytest.mark.asyncio()
    async def test_bulkhead_full_emits_drop_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorded: list[tuple[Any, Any, Any]] = []
        from sovyx.voice import _hystrix_guard

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(_hystrix_guard, "record_stage_event", _capture)
        cfg = HystrixGuardConfig(max_concurrent=1, watchdog_timeout_s=None)
        guard = HystrixGuard(owner=VoiceStage.STT, key="dev-A", config=cfg)
        gate = asyncio.Event()

        async def long_call() -> None:
            async with guard.run():
                await gate.wait()

        t = asyncio.create_task(long_call())
        await asyncio.sleep(0.01)
        with pytest.raises(BulkheadFullError):
            async with guard.run():
                pass
        gate.set()
        await t
        kinds = [(s, k.value, et) for (s, k, et) in recorded]
        assert (VoiceStage.STT, "drop", "bulkhead_full") in kinds
