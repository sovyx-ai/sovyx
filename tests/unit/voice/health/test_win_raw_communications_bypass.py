"""Unit tests for :class:`WindowsRawCommunicationsBypass` — Tier 1 RAW
+ Communications bypass stub.

Voice Windows Paranoid Mission §D2 / Tier 1 — v0.24.0 foundation
phase ships the strategy class with eligibility logic + flag-gated
apply placeholder. Actual ``IAudioClient3::SetClientProperties`` COM
bindings land in v0.25.0 wire-up (mission task T27).

Test surface pinned by this file:

* Eligibility branches:
  - non-Windows platform → ``not_win32_platform``
  - flag False (foundation default) → ``raw_communications_bypass_disabled_by_tuning``
  - flag True + Windows → applicable=True (placeholder; v0.25.0
    wire-up tightens with ``RawProcessingSupported`` MMDevice probe).
* :meth:`apply` always raises ``BypassApplyError(reason="strategy_disabled")``
  in v0.24.0 — defence-in-depth gate so a future direct-call test
  fails loudly instead of silently doing nothing.
* :meth:`revert` is a no-op (the v0.24.0 apply never engages).
* Strategy name is ``win.raw_communications`` — stable wire contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest

from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.bypass._win_raw_communications import (
    WindowsRawCommunicationsBypass,
)
from sovyx.voice.health.contract import BypassContext


@dataclass
class _FakeCaptureTask:
    async def request_exclusive_restart(self) -> Any:  # pragma: no cover
        raise AssertionError("unused in v0.24.0 stub")


def _ctx(
    *,
    host_api_name: str = "Windows WASAPI",
    platform_key: str = "win32",
) -> BypassContext:
    async def _probe() -> Any:  # pragma: no cover
        raise AssertionError("unused in eligibility")

    return BypassContext(
        endpoint_guid="{guid-A}",
        endpoint_friendly_name="Test Mic",
        host_api_name=host_api_name,
        platform_key=platform_key,
        capture_task=_FakeCaptureTask(),  # type: ignore[arg-type]
        probe_fn=_probe,
    )


@pytest.fixture(autouse=True)
def _clear_voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every SOVYX_TUNING__VOICE__* env var so each test runs
    against the documented defaults without leakage between tests."""
    for key in list(os.environ):
        if key.startswith("SOVYX_TUNING__VOICE__"):
            monkeypatch.delenv(key, raising=False)


# ── Strategy identity ───────────────────────────────────────────────


class TestStrategyName:
    def test_name_is_stable_wire_contract(self) -> None:
        bypass = WindowsRawCommunicationsBypass()
        # Stable across minor versions — dashboards + per-strategy
        # metric counter attributes key on this exact string.
        assert bypass.name == "win.raw_communications"


# ── Eligibility ─────────────────────────────────────────────────────


class TestEligibilityNonWindows:
    """Non-Windows always rejects regardless of the flag — Linux +
    macOS hot-plug events flow through their dedicated detectors."""

    @pytest.mark.asyncio()
    async def test_linux_returns_not_win32(self) -> None:
        bypass = WindowsRawCommunicationsBypass()
        result = await bypass.probe_eligibility(_ctx(platform_key="linux"))
        assert result.applicable is False
        assert result.reason == "not_win32_platform"
        assert result.estimated_cost_ms == 0

    @pytest.mark.asyncio()
    async def test_darwin_returns_not_win32(self) -> None:
        bypass = WindowsRawCommunicationsBypass()
        result = await bypass.probe_eligibility(_ctx(platform_key="darwin"))
        assert result.applicable is False
        assert result.reason == "not_win32_platform"

    @pytest.mark.asyncio()
    async def test_non_win32_rejected_even_with_flag_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operator setting the flag on Linux still gets rejected —
        not_win32 takes precedence over disabled_by_tuning."""
        monkeypatch.setenv("SOVYX_TUNING__VOICE__BYPASS_TIER1_RAW_ENABLED", "true")
        bypass = WindowsRawCommunicationsBypass()
        result = await bypass.probe_eligibility(_ctx(platform_key="linux"))
        assert result.applicable is False
        assert result.reason == "not_win32_platform"


class TestEligibilityFlagDefault:
    """Foundation default ``bypass_tier1_raw_enabled=False`` —
    eligibility blocks before reaching the apply stub."""

    @pytest.mark.asyncio()
    async def test_default_flag_disabled_returns_disabled_by_tuning(self) -> None:
        bypass = WindowsRawCommunicationsBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="MME"))
        assert result.applicable is False
        assert result.reason == "raw_communications_bypass_disabled_by_tuning"
        assert result.estimated_cost_ms == 0

    @pytest.mark.asyncio()
    async def test_default_flag_disabled_on_wasapi_endpoint(self) -> None:
        """Even WASAPI endpoints get rejected when flag is False —
        the flag is the master gate for the whole strategy."""
        bypass = WindowsRawCommunicationsBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="Windows WASAPI"))
        assert result.applicable is False
        assert result.reason == "raw_communications_bypass_disabled_by_tuning"


class TestEligibilityFlagEnabled:
    """Operator opt-in path — flag True + Windows → applicable=True."""

    @pytest.mark.asyncio()
    async def test_flag_enabled_passes_on_mme(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_TUNING__VOICE__BYPASS_TIER1_RAW_ENABLED", "true")
        bypass = WindowsRawCommunicationsBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="MME"))
        assert result.applicable is True
        assert result.reason == ""
        assert result.estimated_cost_ms > 0  # cost hint populated

    @pytest.mark.asyncio()
    async def test_flag_enabled_passes_on_directsound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier 1 covers MME/DS/WDM-KS/WASAPI uniformly via the
        per-MMDevice property surface — orthogonal to PortAudio
        host_api wrapper."""
        monkeypatch.setenv("SOVYX_TUNING__VOICE__BYPASS_TIER1_RAW_ENABLED", "true")
        bypass = WindowsRawCommunicationsBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="Windows DirectSound"))
        assert result.applicable is True

    @pytest.mark.asyncio()
    async def test_flag_enabled_passes_on_wasapi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_TUNING__VOICE__BYPASS_TIER1_RAW_ENABLED", "true")
        bypass = WindowsRawCommunicationsBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="Windows WASAPI"))
        assert result.applicable is True


# ── Apply (v0.24.0 placeholder) ─────────────────────────────────────


class TestApplyV024Placeholder:
    """v0.24.0 apply path always raises with stable reason token —
    coordinator never reaches it in production (eligibility blocks
    first), but defence-in-depth gate."""

    @pytest.mark.asyncio()
    async def test_apply_raises_strategy_disabled_when_flag_false(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bypass = WindowsRawCommunicationsBypass()
        caplog.set_level("WARNING")
        with pytest.raises(BypassApplyError) as exc_info:
            await bypass.apply(_ctx(host_api_name="MME"))
        assert exc_info.value.reason == "strategy_disabled"
        # WARN log emitted with target_version=v0.25.0 hint.
        matching = [
            r
            for r in caplog.records
            if "voice.bypass.win_raw_communications.apply_not_yet_wired" in r.getMessage()
        ]
        assert len(matching) == 1
        assert "v0.25.0" in matching[0].getMessage()

    @pytest.mark.asyncio()
    async def test_apply_raises_strategy_disabled_when_flag_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even when an operator flips the flag in v0.24.0, the apply
        path raises ``strategy_disabled`` — the COM bindings aren't
        wired yet. Eligibility passes (placeholder) but apply explicitly
        documents the not-wired state with a stable token."""
        monkeypatch.setenv("SOVYX_TUNING__VOICE__BYPASS_TIER1_RAW_ENABLED", "true")
        bypass = WindowsRawCommunicationsBypass()
        with pytest.raises(BypassApplyError) as exc_info:
            await bypass.apply(_ctx(host_api_name="MME"))
        assert exc_info.value.reason == "strategy_disabled"


# ── Revert (v0.24.0 no-op) ─────────────────────────────────────────


class TestRevertV024Noop:
    """v0.24.0 apply never engages, so revert is a no-op. Idempotent
    per the PlatformBypassStrategy contract."""

    @pytest.mark.asyncio()
    async def test_revert_returns_none(self) -> None:
        bypass = WindowsRawCommunicationsBypass()
        result = await bypass.revert(_ctx())
        assert result is None

    @pytest.mark.asyncio()
    async def test_revert_idempotent(self) -> None:
        bypass = WindowsRawCommunicationsBypass()
        await bypass.revert(_ctx())
        await bypass.revert(_ctx())  # second call — no error


# ── Lazy export from bypass/__init__.py ─────────────────────────────


class TestLazyExport:
    def test_imports_via_package_attribute_access(self) -> None:
        """``from sovyx.voice.health.bypass import WindowsRawCommunicationsBypass``
        works via the lazy ``__getattr__`` in
        ``sovyx.voice.health.bypass.__init__``."""
        from sovyx.voice.health.bypass import (
            WindowsRawCommunicationsBypass as Cls,
        )

        assert Cls is WindowsRawCommunicationsBypass
