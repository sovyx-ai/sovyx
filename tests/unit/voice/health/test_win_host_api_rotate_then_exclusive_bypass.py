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


class TestApplyV025WireUp:
    """T28 wire-up — 2-phase rotate-then-exclusive apply.

    Phase A (request_host_api_rotate) and Phase B
    (request_exclusive_restart) each have their own verdict; the
    strategy translates the combined outcome into one of:

    * ``rotated_then_exclusive_engaged`` (both phases engaged)
    * ``rotated_then_exclusive_downgraded`` (Phase A engaged but
      Phase B fell to shared)

    On Phase A failure, raises ``BypassApplyError(reason=rotate_*)``
    so the coordinator records FAILED_TO_APPLY.
    """

    def _make_fake_capture_task(
        self,
        rotate_engaged: bool = True,
        rotate_verdict_value: str = "rotated_success",
        rotate_detail: str | None = None,
        exclusive_engaged: bool = True,
    ) -> Any:
        """Build a mock capture_task whose phase-A and phase-B
        outcomes are configurable — covers happy / downgrade /
        failure paths uniformly."""
        from unittest.mock import AsyncMock, MagicMock

        rotate_verdict = MagicMock()
        rotate_verdict.value = rotate_verdict_value

        rotate_result = MagicMock()
        rotate_result.engaged = rotate_engaged
        rotate_result.verdict = rotate_verdict
        rotate_result.detail = rotate_detail

        excl_verdict = MagicMock()
        excl_verdict.value = "exclusive_engaged" if exclusive_engaged else "downgraded_to_shared"
        excl_result = MagicMock()
        excl_result.engaged = exclusive_engaged
        excl_result.verdict = excl_verdict
        excl_result.detail = None

        task = MagicMock()
        task._host_api_name = "MME"  # initial source host_api
        task.request_host_api_rotate = AsyncMock(return_value=rotate_result)
        task.request_exclusive_restart = AsyncMock(return_value=excl_result)
        task.request_shared_restart = AsyncMock()
        return task

    @pytest.mark.asyncio()
    async def test_apply_returns_engaged_when_both_phases_succeed(
        self, both_flags_enabled: None
    ) -> None:
        del both_flags_enabled
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        task = self._make_fake_capture_task(rotate_engaged=True, exclusive_engaged=True)
        ctx = BypassContext(
            endpoint_guid="{guid-A}",
            endpoint_friendly_name="Test Mic",
            host_api_name="MME",
            platform_key="win32",
            capture_task=task,
            probe_fn=lambda: None,  # type: ignore[arg-type,return-value]
        )
        result = await bypass.apply(ctx)
        assert result == "rotated_then_exclusive_engaged"
        task.request_host_api_rotate.assert_awaited_once_with(
            target_host_api="Windows WASAPI",
            target_exclusive=False,
        )
        task.request_exclusive_restart.assert_awaited_once()
        # Source host_api captured for revert.
        assert bypass._source_host_api == "MME"  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_apply_returns_downgraded_when_phase_b_fails(
        self, both_flags_enabled: None
    ) -> None:
        """Phase A engaged, Phase B fell to shared — still net-positive
        because WASAPI shared has fewer APO layers than legacy host
        APIs."""
        del both_flags_enabled
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        task = self._make_fake_capture_task(rotate_engaged=True, exclusive_engaged=False)
        ctx = BypassContext(
            endpoint_guid="{guid-B}",
            endpoint_friendly_name="Test Mic",
            host_api_name="DirectSound",
            platform_key="win32",
            capture_task=task,
            probe_fn=lambda: None,  # type: ignore[arg-type,return-value]
        )
        result = await bypass.apply(ctx)
        assert result == "rotated_then_exclusive_downgraded"

    @pytest.mark.asyncio()
    async def test_apply_raises_when_phase_a_fails(self, both_flags_enabled: None) -> None:
        """Phase A failure → BypassApplyError with stable
        ``rotate_<verdict>`` reason token. Coordinator records
        FAILED_TO_APPLY."""
        del both_flags_enabled
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        task = self._make_fake_capture_task(
            rotate_engaged=False,
            rotate_verdict_value="no_target_sibling",
            rotate_detail="no Windows WASAPI sibling for endpoint",
        )
        ctx = BypassContext(
            endpoint_guid="{guid-C}",
            endpoint_friendly_name="Test Mic",
            host_api_name="MME",
            platform_key="win32",
            capture_task=task,
            probe_fn=lambda: None,  # type: ignore[arg-type,return-value]
        )
        with pytest.raises(BypassApplyError) as exc_info:
            await bypass.apply(ctx)
        assert exc_info.value.reason == "rotate_no_target_sibling"
        # Phase B never invoked when Phase A failed.
        task.request_exclusive_restart.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_apply_raises_capture_task_not_running_when_none(
        self, both_flags_enabled: None
    ) -> None:
        """Defensive — coordinator wire-up bug that passes None
        capture_task surfaces as a structured failure."""
        del both_flags_enabled
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        ctx = BypassContext(
            endpoint_guid="{guid-D}",
            endpoint_friendly_name="Test Mic",
            host_api_name="MME",
            platform_key="win32",
            capture_task=None,  # type: ignore[arg-type]
            probe_fn=lambda: None,  # type: ignore[arg-type,return-value]
        )
        with pytest.raises(BypassApplyError) as exc_info:
            await bypass.apply(ctx)
        assert exc_info.value.reason == "capture_task_not_running"


# ── Revert (v0.24.0 no-op) ─────────────────────────────────────────


class TestRevertV025WireUp:
    """T28 wire-up — 2-step revert: request_shared_restart +
    request_host_api_rotate(target=source_host_api). Best-effort:
    swallows individual step failures so the coordinator's teardown
    completes."""

    @pytest.mark.asyncio()
    async def test_revert_calls_shared_then_rotate_back(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        bypass = WindowsHostApiRotateThenExclusiveBypass()
        bypass._source_host_api = "MME"  # type: ignore[attr-defined]

        task = MagicMock()
        task.request_shared_restart = AsyncMock()
        task.request_host_api_rotate = AsyncMock()
        ctx = BypassContext(
            endpoint_guid="{guid-A}",
            endpoint_friendly_name="Test Mic",
            host_api_name="Windows WASAPI",
            platform_key="win32",
            capture_task=task,
            probe_fn=lambda: None,  # type: ignore[arg-type,return-value]
        )
        await bypass.revert(ctx)
        task.request_shared_restart.assert_awaited_once()
        task.request_host_api_rotate.assert_awaited_once_with(
            target_host_api="MME",
            target_exclusive=False,
        )

    @pytest.mark.asyncio()
    async def test_revert_skips_rotate_when_source_was_wasapi(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        bypass = WindowsHostApiRotateThenExclusiveBypass()
        # Source already on WASAPI — apply would have been a no-op
        # rotation; revert skips the rotate-back step too.
        bypass._source_host_api = "Windows WASAPI"  # type: ignore[attr-defined]

        task = MagicMock()
        task.request_shared_restart = AsyncMock()
        task.request_host_api_rotate = AsyncMock()
        ctx = BypassContext(
            endpoint_guid="{guid-B}",
            endpoint_friendly_name="Test Mic",
            host_api_name="Windows WASAPI",
            platform_key="win32",
            capture_task=task,
            probe_fn=lambda: None,  # type: ignore[arg-type,return-value]
        )
        await bypass.revert(ctx)
        task.request_shared_restart.assert_awaited_once()
        # Rotate-back skipped.
        task.request_host_api_rotate.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_revert_swallows_shared_restart_failure(self) -> None:
        """Best-effort revert — request_shared_restart raise must NOT
        propagate; the coordinator's teardown must complete."""
        from unittest.mock import AsyncMock, MagicMock

        bypass = WindowsHostApiRotateThenExclusiveBypass()
        bypass._source_host_api = "MME"  # type: ignore[attr-defined]

        task = MagicMock()
        task.request_shared_restart = AsyncMock(side_effect=RuntimeError("teardown"))
        task.request_host_api_rotate = AsyncMock()
        ctx = BypassContext(
            endpoint_guid="{guid-C}",
            endpoint_friendly_name="Test Mic",
            host_api_name="Windows WASAPI",
            platform_key="win32",
            capture_task=task,
            probe_fn=lambda: None,  # type: ignore[arg-type,return-value]
        )
        # Must NOT raise.
        await bypass.revert(ctx)
        task.request_host_api_rotate.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_revert_idempotent_without_apply(self) -> None:
        """Calling revert before apply (defensive shutdown path) is
        a no-op + no errors."""
        bypass = WindowsHostApiRotateThenExclusiveBypass()
        # _source_host_api stays None; revert short-circuits.
        ctx = BypassContext(
            endpoint_guid="{guid-D}",
            endpoint_friendly_name="Test Mic",
            host_api_name="Windows WASAPI",
            platform_key="win32",
            capture_task=None,  # type: ignore[arg-type]
            probe_fn=lambda: None,  # type: ignore[arg-type,return-value]
        )
        await bypass.revert(ctx)  # no error


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
