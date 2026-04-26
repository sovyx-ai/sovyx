"""Unit tests for :class:`WindowsHostApiRotateThenExclusiveBypass` —
Tier 2 host-API rotate-then-exclusive bypass stub.

Voice Windows Paranoid Mission §D2 / Tier 2 — v0.24.0 foundation
phase ships the strategy class with eligibility logic + flag-gated
apply placeholder. The 2-phase rotate-then-exclusive apply
(``request_host_api_rotate`` + ``request_exclusive_restart``) lands
in v0.25.0 wire-up (mission task T28).

Test surface pinned by this file:

* Eligibility branches:
  - non-Windows platform → ``not_win32_platform``
  - ``bypass_tier2_host_api_rotate_enabled=False`` → ``host_api_rotate_disabled_by_tuning``
  - host_api ∈ {MME, DirectSound, WDM-KS} → applicable=True
  - host_api == WASAPI → ``endpoint_already_on_wasapi`` (delegated
    to Tier 3 ``win.wasapi_exclusive``)
* :meth:`apply` always raises ``BypassApplyError(reason="strategy_disabled")``.
* :meth:`revert` is a no-op.
* Strategy name is ``win.host_api_rotate_then_exclusive``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest

from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.bypass._win_host_api_rotate_then_exclusive import (
    WindowsHostApiRotateThenExclusiveBypass,
)
from sovyx.voice.health.contract import BypassContext


@dataclass
class _FakeCaptureTask:
    async def request_exclusive_restart(self) -> Any:  # pragma: no cover
        raise AssertionError("unused in v0.24.0 stub")


def _ctx(
    *,
    host_api_name: str = "MME",
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
    for key in list(os.environ):
        if key.startswith("SOVYX_TUNING__VOICE__"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def both_flags_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2 + alignment both True — the only combo where Tier 2
    eligibility CAN return applicable=True. Other tests subset this."""
    monkeypatch.setenv("SOVYX_TUNING__VOICE__BYPASS_TIER2_HOST_API_ROTATE_ENABLED", "true")
    monkeypatch.setenv("SOVYX_TUNING__VOICE__CASCADE_HOST_API_ALIGNMENT_ENABLED", "true")


# ── Strategy identity ───────────────────────────────────────────────


class TestStrategyName:
    def test_name_is_stable_wire_contract(self) -> None:
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        assert bypass.name == "win.host_api_rotate_then_exclusive"


# ── Eligibility ─────────────────────────────────────────────────────


class TestEligibilityNonWindows:
    @pytest.mark.asyncio()
    async def test_linux_returns_not_win32(self) -> None:
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(platform_key="linux"))
        assert result.applicable is False
        assert result.reason == "not_win32_platform"

    @pytest.mark.asyncio()
    async def test_darwin_returns_not_win32(self) -> None:
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(platform_key="darwin"))
        assert result.applicable is False
        assert result.reason == "not_win32_platform"


class TestEligibilityFlagDefault:
    """Foundation default — both flags False — eligibility blocks
    on Tier 2 flag check (first gate)."""

    @pytest.mark.asyncio()
    async def test_default_flag_disabled_returns_disabled_by_tuning(self) -> None:
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="MME"))
        assert result.applicable is False
        assert result.reason == "host_api_rotate_disabled_by_tuning"


class TestCrossValidatorEnforcedAtBoot:
    """The cross-validator at
    ``engine/config.py::_enforce_paranoid_mission_dependencies``
    rejects ``bypass_tier2=True + alignment=False`` at boot. This
    test pins that the contradictory combination cannot survive
    config load — runtime eligibility never has to defend against it
    because every ``_VoiceTuning()`` call re-validates."""

    def test_tier2_without_alignment_rejected_at_config_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sovyx.engine.config import VoiceTuningConfig

        monkeypatch.setenv("SOVYX_TUNING__VOICE__BYPASS_TIER2_HOST_API_ROTATE_ENABLED", "true")
        # cascade_host_api_alignment_enabled stays default-False —
        # contradictory combination MUST be rejected at boot.
        with pytest.raises(Exception) as exc_info:
            VoiceTuningConfig()
        msg = str(exc_info.value)
        assert "bypass_tier2_host_api_rotate_enabled" in msg
        assert "cascade_host_api_alignment_enabled" in msg


class TestEligibilityHostApiFilter:
    """Tier 2 + alignment both True — eligibility now branches on
    host_api: non-WASAPI passes, WASAPI delegates to Tier 3."""

    @pytest.mark.asyncio()
    async def test_mme_passes(self, both_flags_enabled: None) -> None:
        del both_flags_enabled  # fixture applies env vars
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="MME"))
        assert result.applicable is True
        assert result.reason == ""
        assert result.estimated_cost_ms > 0

    @pytest.mark.asyncio()
    async def test_directsound_passes(self, both_flags_enabled: None) -> None:
        del both_flags_enabled
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="Windows DirectSound"))
        assert result.applicable is True

    @pytest.mark.asyncio()
    async def test_wdm_ks_passes(self, both_flags_enabled: None) -> None:
        del both_flags_enabled
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="Windows WDM-KS"))
        assert result.applicable is True

    @pytest.mark.asyncio()
    async def test_wasapi_delegates_to_tier3(self, both_flags_enabled: None) -> None:
        """WASAPI endpoints are Tier 3's surface (``win.wasapi_exclusive``).
        Tier 2 + Tier 3 partition the Windows host_api space without
        overlap — WASAPI must NOT pass Tier 2 eligibility."""
        del both_flags_enabled
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="Windows WASAPI"))
        assert result.applicable is False
        assert result.reason == "endpoint_already_on_wasapi"

    @pytest.mark.asyncio()
    async def test_unknown_host_api_delegates_to_tier3(self, both_flags_enabled: None) -> None:
        """Any host_api outside the closed allowlist {MME, DS, WDM-KS}
        is also delegated — Tier 2 only acts on the documented gap."""
        del both_flags_enabled
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="ASIO"))
        assert result.applicable is False
        assert result.reason == "endpoint_already_on_wasapi"

    @pytest.mark.asyncio()
    async def test_case_insensitive_normalisation(self, both_flags_enabled: None) -> None:
        del both_flags_enabled
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        for label in ("mme", "MME", "Mme"):
            result = await bypass.probe_eligibility(_ctx(host_api_name=label))
            assert result.applicable is True, f"{label!r} should pass"

    @pytest.mark.asyncio()
    async def test_whitespace_normalised(self, both_flags_enabled: None) -> None:
        del both_flags_enabled
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="  MME  "))
        assert result.applicable is True


# ── Apply (v0.24.0 placeholder) ─────────────────────────────────────


class TestApplyV024Placeholder:
    """v0.24.0 apply path always raises with stable reason token."""

    @pytest.mark.asyncio()
    async def test_apply_raises_strategy_disabled_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        caplog.set_level("WARNING")
        with pytest.raises(BypassApplyError) as exc_info:
            await bypass.apply(_ctx(host_api_name="MME"))
        assert exc_info.value.reason == "strategy_disabled"
        matching = [
            r
            for r in caplog.records
            if "voice.bypass.win_host_api_rotate_then_exclusive.apply_not_yet_wired"
            in r.getMessage()
        ]
        assert len(matching) == 1
        msg = matching[0].getMessage()
        assert "v0.25.0" in msg
        assert "T28" in msg

    @pytest.mark.asyncio()
    async def test_apply_raises_strategy_disabled_with_both_flags_enabled(
        self, both_flags_enabled: None
    ) -> None:
        """Even when an operator flips both flags in v0.24.0, the
        apply path raises ``strategy_disabled`` —
        ``request_host_api_rotate`` doesn't exist on AudioCaptureTask
        until v0.25.0."""
        del both_flags_enabled
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        with pytest.raises(BypassApplyError) as exc_info:
            await bypass.apply(_ctx(host_api_name="MME"))
        assert exc_info.value.reason == "strategy_disabled"


# ── Revert (v0.24.0 no-op) ─────────────────────────────────────────


class TestRevertV024Noop:
    @pytest.mark.asyncio()
    async def test_revert_returns_none(self) -> None:
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        result = await bypass.revert(_ctx())
        assert result is None

    @pytest.mark.asyncio()
    async def test_revert_idempotent(self) -> None:
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        await bypass.revert(_ctx())
        await bypass.revert(_ctx())


# ── State preservation slot for v0.25.0 wire-up ────────────────────


class TestStrategyInstanceStateSlot:
    """Per-coordinator-session strategy contract per
    ``_strategy.py:93-95`` — instance state must be safe to set
    during apply for revert to read later. v0.25.0 wire-up uses
    ``self._source_host_api`` to capture the pre-rotate host_api."""

    def test_source_host_api_initialised_to_none(self) -> None:
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        assert bypass._source_host_api is None  # type: ignore[attr-defined]


# ── Lazy export from bypass/__init__.py ─────────────────────────────


class TestLazyExport:
    def test_imports_via_package_attribute_access(self) -> None:
        from sovyx.voice.health.bypass import (
            WindowsHostApiRotateThenExclusiveBypass as Cls,
        )

        assert Cls is WindowsHostApiRotateThenExclusiveBypass
