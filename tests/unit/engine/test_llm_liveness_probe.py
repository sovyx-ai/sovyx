"""Unit tests — `sovyx.engine._llm_liveness_probe.LLMLivenessProbe` (Mission C6 §T2.5).

Coverage: start/stop lifecycle + kill-switch + verdict-transition dispatch
+ grace-period filter + cancellation hygiene + idempotent re-tick.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.engine._degraded_store import (
    get_default_degraded_store,
    reset_default_degraded_store,
)
from sovyx.engine._llm_dispatch import dispatch_llm_discovery_verdict
from sovyx.engine._llm_liveness_probe import LLMLivenessProbe
from sovyx.engine.config import LLMTuningConfig
from sovyx.llm._provider_health import DiscoveryVerdict, scan_llm_provider_health

# All cloud-LLM env vars the discovery scanner reads — cleared in tests that
# assert NO_PROVIDER_CONFIGURED so an operator key in the CI shell can't leak.
_CLOUD_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "XGROK_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
)


def _record_boot_no_provider() -> None:
    """Simulate the boot-time dispatch that records ``axis="llm"``."""
    report = scan_llm_provider_health(
        env={},
        ollama_ping_result=False,
        ollama_models=None,
        default_provider="",
        default_model="",
    )
    assert report.verdict is DiscoveryVerdict.NO_PROVIDER_CONFIGURED
    dispatch_llm_discovery_verdict(report)


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


def _make_tuning(
    *,
    enabled: bool = True,
    interval_sec: float = 10.0,
    grace_sec: float = 0.0,
) -> LLMTuningConfig:
    return LLMTuningConfig(
        liveness_check_enabled=enabled,
        liveness_check_interval_sec=interval_sec,
        provider_unhealthy_grace_period_sec=grace_sec,
    )


def _make_ollama(*, is_available: bool, models: list[str] | None = None) -> MagicMock:
    mock = MagicMock()
    mock.is_available = is_available
    mock.ping = AsyncMock(return_value=is_available)
    mock.list_models = AsyncMock(return_value=models or [])
    return mock


def _make_mind_config(default_provider: str = "", default_model: str = "") -> MagicMock:
    mind = MagicMock()
    mind.llm.default_provider = default_provider
    mind.llm.default_model = default_model
    return mind


def _make_router() -> MagicMock:
    router = MagicMock()
    router.update_discovery_report = MagicMock()
    return router


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_disabled_does_not_spawn_task(self) -> None:
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=_make_ollama(is_available=True),
            config=_make_tuning(enabled=False),
            mind_config=_make_mind_config(),
        )
        await probe.start()
        assert probe._task is None

    @pytest.mark.asyncio
    async def test_enabled_spawns_task(self) -> None:
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=_make_ollama(is_available=True, models=["a:b"]),
            config=_make_tuning(enabled=True, interval_sec=600.0),
            mind_config=_make_mind_config(),
        )
        await probe.start()
        try:
            assert probe._task is not None
            assert not probe._task.done()
        finally:
            await probe.stop()


class TestStartIsIdempotent:
    @pytest.mark.asyncio
    async def test_double_start_does_not_double_spawn(self) -> None:
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=_make_ollama(is_available=True, models=["a:b"]),
            config=_make_tuning(interval_sec=600.0),
            mind_config=_make_mind_config(),
        )
        await probe.start()
        first_task = probe._task
        try:
            await probe.start()
            assert probe._task is first_task
        finally:
            await probe.stop()


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_before_start_is_no_op(self) -> None:
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=_make_ollama(is_available=True),
            config=_make_tuning(),
            mind_config=_make_mind_config(),
        )
        await probe.stop()
        assert probe._task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_task_cleanly(self) -> None:
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=_make_ollama(is_available=True, models=["a:b"]),
            config=_make_tuning(interval_sec=600.0),
            mind_config=_make_mind_config(),
        )
        await probe.start()
        await probe.stop()
        assert probe._task is None


class TestTickUpdatesRouter:
    @pytest.mark.asyncio
    async def test_tick_calls_update_discovery_report(self) -> None:
        router = _make_router()
        probe = LLMLivenessProbe(
            router=router,
            ollama_provider=_make_ollama(is_available=True, models=["a:b"]),
            config=_make_tuning(),
            mind_config=_make_mind_config(),
        )
        await probe._tick()
        assert router.update_discovery_report.call_count == 1


class TestVerdictTransition:
    @pytest.mark.asyncio
    async def test_first_tick_sets_baseline_without_dispatch(self) -> None:
        """First tick records the baseline; no transition log fires."""
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=_make_ollama(is_available=True, models=["a:b"]),
            config=_make_tuning(grace_sec=0.0),
            mind_config=_make_mind_config(),
        )
        await probe._tick()
        assert probe._last_verdict is DiscoveryVerdict.FULLY_AVAILABLE
        assert get_default_degraded_store().snapshot() == []

    @pytest.mark.asyncio
    async def test_healthy_to_unhealthy_with_zero_grace_dispatches(self) -> None:
        """With grace=0, healthy→unhealthy transition dispatches immediately."""
        ollama = _make_ollama(is_available=True, models=["a:b"])
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=ollama,
            config=_make_tuning(grace_sec=0.0),
            mind_config=_make_mind_config(),
        )
        # First tick sets baseline = FULLY_AVAILABLE
        await probe._tick()
        # Flip Ollama down
        ollama.is_available = False
        ollama.ping = AsyncMock(return_value=False)
        ollama.list_models = AsyncMock(return_value=[])
        # Second tick should detect transition and dispatch
        await probe._tick()
        entries = get_default_degraded_store().snapshot()
        assert len(entries) == 1
        assert entries[0].axis == "llm"
        assert entries[0].reason == "no_provider_configured"

    @pytest.mark.asyncio
    async def test_unhealthy_to_healthy_clears_axis(self) -> None:
        """Recovery always promotes immediately (no grace penalty on recovery)."""
        ollama = _make_ollama(is_available=False)
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=ollama,
            config=_make_tuning(grace_sec=0.0),
            mind_config=_make_mind_config(),
        )
        await probe._tick()  # baseline NO_PROVIDER_CONFIGURED
        # Recover
        ollama.is_available = True
        ollama.ping = AsyncMock(return_value=True)
        ollama.list_models = AsyncMock(return_value=["llama3.1:latest"])
        await probe._tick()
        # FULLY_AVAILABLE dispatch should clear the axis
        assert get_default_degraded_store().snapshot() == []
        assert probe._last_verdict is DiscoveryVerdict.FULLY_AVAILABLE


class TestSameVerdictNoTransition:
    @pytest.mark.asyncio
    async def test_repeated_same_verdict_no_dispatch(self) -> None:
        """Two healthy ticks in a row → no transition events, no store changes."""
        ollama = _make_ollama(is_available=True, models=["a:b"])
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=ollama,
            config=_make_tuning(grace_sec=0.0),
            mind_config=_make_mind_config(),
        )
        await probe._tick()
        await probe._tick()
        # Both healthy — no store entries.
        assert get_default_degraded_store().snapshot() == []


class TestBootVerdictReconciliation:
    """LIVE-1 Bug A — Edge 2. Seeding the probe with the boot verdict makes the
    FIRST tick a real transition check, so a recovery that lands in the
    boot→first-tick window is not masked by silent baselining."""

    @pytest.mark.asyncio
    async def test_seeded_boot_verdict_clears_axis_on_first_tick_recovery(self) -> None:
        _record_boot_no_provider()
        assert len(get_default_degraded_store().snapshot()) == 1
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=_make_ollama(is_available=True, models=["a:b"]),
            config=_make_tuning(grace_sec=0.0),
            mind_config=_make_mind_config(),
            boot_verdict=DiscoveryVerdict.NO_PROVIDER_CONFIGURED,
        )
        # First tick observes FULLY_AVAILABLE; previous=boot NO_PROVIDER →
        # transition → clear_axis("llm").
        await probe._tick()
        assert get_default_degraded_store().snapshot() == []
        assert probe._last_verdict is DiscoveryVerdict.FULLY_AVAILABLE

    @pytest.mark.asyncio
    async def test_unseeded_first_tick_masks_recovery_regression_guard(self) -> None:
        """Contrast guard: WITHOUT the boot-verdict seed (legacy behavior) the
        first tick silently baselines and the boot entry stays stuck — this is
        the exact LIVE-1 stale-banner bug. Documents why the seed is required."""
        _record_boot_no_provider()
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=_make_ollama(is_available=True, models=["a:b"]),
            config=_make_tuning(grace_sec=0.0),
            mind_config=_make_mind_config(),
        )  # boot_verdict defaults to None → legacy masking path
        await probe._tick()
        assert len(get_default_degraded_store().snapshot()) == 1

    @pytest.mark.asyncio
    async def test_seeded_same_verdict_no_thrash(self) -> None:
        """Seeded boot verdict + first tick still NO_PROVIDER → no duplicate
        dispatch; the boot entry remains untouched."""
        _record_boot_no_provider()
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=_make_ollama(is_available=False),
            config=_make_tuning(grace_sec=0.0),
            mind_config=_make_mind_config(),
            boot_verdict=DiscoveryVerdict.NO_PROVIDER_CONFIGURED,
        )
        await probe._tick()
        snap = get_default_degraded_store().snapshot()
        assert len(snap) == 1
        assert snap[0].reason == "no_provider_configured"


class TestRefreshNow:
    """LIVE-1 Bug A — Edge 1. ``refresh_now`` re-scans and dispatches the
    current verdict UNCONDITIONALLY (bypassing the transition gate) so a
    hot-registered provider clears ``axis="llm"`` synchronously."""

    @pytest.mark.asyncio
    async def test_refresh_now_clears_axis_on_recovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _record_boot_no_provider()
        assert len(get_default_degraded_store().snapshot()) == 1
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        router = _make_router()
        probe = LLMLivenessProbe(
            router=router,
            ollama_provider=_make_ollama(is_available=False),
            config=_make_tuning(grace_sec=0.0),
            mind_config=_make_mind_config(default_provider="openai", default_model="gpt-4o"),
            boot_verdict=DiscoveryVerdict.NO_PROVIDER_CONFIGURED,
        )
        await probe.refresh_now()
        assert get_default_degraded_store().snapshot() == []
        assert probe._last_verdict is DiscoveryVerdict.FULLY_AVAILABLE
        assert router.update_discovery_report.call_count == 1

    @pytest.mark.asyncio
    async def test_refresh_now_records_when_still_no_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in _CLOUD_KEY_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=_make_ollama(is_available=False),
            config=_make_tuning(grace_sec=0.0),
            mind_config=_make_mind_config(),
        )
        await probe.refresh_now()
        snap = get_default_degraded_store().snapshot()
        assert len(snap) == 1
        assert snap[0].axis == "llm"
        assert snap[0].reason == "no_provider_configured"

    @pytest.mark.asyncio
    async def test_refresh_now_dispatches_unconditionally_even_same_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No transition gating: refresh dispatches even when the freshly
        scanned verdict equals ``_last_verdict`` — an explicit operator action
        is not a blip to debounce."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        probe = LLMLivenessProbe(
            router=_make_router(),
            ollama_provider=_make_ollama(is_available=False),
            config=_make_tuning(grace_sec=0.0),
            mind_config=_make_mind_config(default_provider="openai", default_model="gpt-4o"),
        )
        probe._last_verdict = DiscoveryVerdict.FULLY_AVAILABLE  # already healthy
        seen: list[DiscoveryVerdict] = []
        monkeypatch.setattr(
            "sovyx.engine._llm_liveness_probe.dispatch_llm_discovery_verdict",
            lambda report: seen.append(report.verdict),
        )
        await probe.refresh_now()
        assert seen == [DiscoveryVerdict.FULLY_AVAILABLE]
