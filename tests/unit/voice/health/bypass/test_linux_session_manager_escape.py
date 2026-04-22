"""T12.1 — unit tests for ``LinuxSessionManagerEscapeBypass``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice._capture_task import (
    SessionManagerRestartResult,
    SessionManagerRestartVerdict,
)
from sovyx.voice.device_enum import DeviceEntry, DeviceKind
from sovyx.voice.health.bypass._linux_session_manager_escape import (
    LinuxSessionManagerEscapeBypass,
    _find_preferred_session_manager_target,
)
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.contract import BypassContext


def _entry(
    *,
    index: int,
    name: str,
    kind: DeviceKind,
    in_ch: int = 64,
) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.strip().lower()[:30],
        host_api_index=0,
        host_api_name="ALSA",
        max_input_channels=in_ch,
        max_output_channels=0,
        default_samplerate=48000,
        is_os_default=False,
        kind=kind,
    )


def _context(
    *,
    platform: str = "linux",
    kind: str = DeviceKind.HARDWARE.value,
    capture_task: object | None = None,
) -> BypassContext:
    return BypassContext(
        endpoint_guid="{guid}",
        endpoint_friendly_name="HD-Audio Generic: SN6180 Analog (hw:1,0)",
        host_api_name="ALSA",
        platform_key=platform,
        capture_task=capture_task or MagicMock(),
        probe_fn=AsyncMock(),
        current_device_index=4,
        current_device_kind=kind,
    )


def _healthy_restart(device_index: int) -> SessionManagerRestartResult:
    return SessionManagerRestartResult(
        verdict=SessionManagerRestartVerdict.SESSION_MANAGER_ENGAGED,
        engaged=True,
        host_api="ALSA",
        device=device_index,
        sample_rate=48_000,
    )


class TestEligibility:
    @pytest.mark.asyncio()
    async def test_not_linux_ineligible(self) -> None:
        strategy = LinuxSessionManagerEscapeBypass()
        ctx = _context(platform="win32")
        elig = await strategy.probe_eligibility(ctx)
        assert elig.applicable is False
        assert elig.reason == "not_linux_platform"

    @pytest.mark.asyncio()
    async def test_not_hardware_ineligible(self) -> None:
        strategy = LinuxSessionManagerEscapeBypass()
        ctx = _context(kind=DeviceKind.SESSION_MANAGER_VIRTUAL.value)
        elig = await strategy.probe_eligibility(ctx)
        assert elig.applicable is False
        assert elig.reason == "endpoint_not_hardware_source"

    @pytest.mark.asyncio()
    async def test_no_target_ineligible(self) -> None:
        strategy = LinuxSessionManagerEscapeBypass()
        ctx = _context()
        # Enumeration returns only the current device (no session-
        # manager virtual or default available).
        with patch(
            "sovyx.voice.device_enum.enumerate_devices",
            return_value=[
                _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE, in_ch=2),
            ],
        ):
            elig = await strategy.probe_eligibility(ctx)
        assert elig.applicable is False
        assert elig.reason == "no_session_manager_target_available"

    @pytest.mark.asyncio()
    async def test_eligible_when_pipewire_present(self) -> None:
        strategy = LinuxSessionManagerEscapeBypass()
        ctx = _context()
        devices = [
            _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE, in_ch=2),
            _entry(index=6, name="pipewire", kind=DeviceKind.SESSION_MANAGER_VIRTUAL),
        ]
        with patch(
            "sovyx.voice.device_enum.enumerate_devices",
            return_value=devices,
        ):
            elig = await strategy.probe_eligibility(ctx)
        assert elig.applicable is True


class TestApplyPreferenceOrder:
    @pytest.mark.asyncio()
    async def test_pipewire_preferred_over_default(self) -> None:
        strategy = LinuxSessionManagerEscapeBypass()
        capture_task = MagicMock()
        capture_task.request_session_manager_restart = AsyncMock(
            return_value=_healthy_restart(6),
        )
        ctx = _context(capture_task=capture_task)
        devices = [
            _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE, in_ch=2),
            _entry(index=6, name="pipewire", kind=DeviceKind.SESSION_MANAGER_VIRTUAL),
            _entry(index=7, name="default", kind=DeviceKind.OS_DEFAULT),
        ]
        with patch(
            "sovyx.voice.device_enum.enumerate_devices",
            return_value=devices,
        ):
            result = await strategy.apply(ctx)
        assert result == "session_manager_engaged"
        call_kwargs = capture_task.request_session_manager_restart.call_args.kwargs
        assert call_kwargs["target_device"].index == 6

    @pytest.mark.asyncio()
    async def test_pulse_when_no_pipewire(self) -> None:
        strategy = LinuxSessionManagerEscapeBypass()
        capture_task = MagicMock()
        capture_task.request_session_manager_restart = AsyncMock(
            return_value=_healthy_restart(8),
        )
        ctx = _context(capture_task=capture_task)
        devices = [
            _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE, in_ch=2),
            _entry(index=8, name="pulse", kind=DeviceKind.SESSION_MANAGER_VIRTUAL),
            _entry(index=7, name="default", kind=DeviceKind.OS_DEFAULT),
        ]
        with patch(
            "sovyx.voice.device_enum.enumerate_devices",
            return_value=devices,
        ):
            await strategy.apply(ctx)
        call_kwargs = capture_task.request_session_manager_restart.call_args.kwargs
        assert call_kwargs["target_device"].index == 8  # pulse before default

    @pytest.mark.asyncio()
    async def test_default_when_no_virtual(self) -> None:
        strategy = LinuxSessionManagerEscapeBypass()
        capture_task = MagicMock()
        capture_task.request_session_manager_restart = AsyncMock(
            return_value=_healthy_restart(7),
        )
        ctx = _context(capture_task=capture_task)
        devices = [
            _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE, in_ch=2),
            _entry(index=7, name="default", kind=DeviceKind.OS_DEFAULT),
        ]
        with patch(
            "sovyx.voice.device_enum.enumerate_devices",
            return_value=devices,
        ):
            await strategy.apply(ctx)
        call_kwargs = capture_task.request_session_manager_restart.call_args.kwargs
        assert call_kwargs["target_device"].index == 7


class TestApplyFailureModes:
    @pytest.mark.asyncio()
    async def test_no_target_raises_bypass_apply_error(self) -> None:
        strategy = LinuxSessionManagerEscapeBypass()
        ctx = _context()
        with (
            patch(
                "sovyx.voice.device_enum.enumerate_devices",
                return_value=[_entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE, in_ch=2)],
            ),
            pytest.raises(BypassApplyError) as exc_info,
        ):
            await strategy.apply(ctx)
        assert "no session-manager target" in str(exc_info.value)

    @pytest.mark.asyncio()
    async def test_downgraded_verdict_raises_bypass_apply_error(self) -> None:
        strategy = LinuxSessionManagerEscapeBypass()
        capture_task = MagicMock()
        capture_task.request_session_manager_restart = AsyncMock(
            return_value=SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.DOWNGRADED_TO_ALSA_HW,
                engaged=False,
                host_api="ALSA",
                device=4,
                sample_rate=16_000,
                detail="no sibling",
            ),
        )
        ctx = _context(capture_task=capture_task)
        devices = [
            _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE, in_ch=2),
            _entry(index=6, name="pipewire", kind=DeviceKind.SESSION_MANAGER_VIRTUAL),
        ]
        with (
            patch("sovyx.voice.device_enum.enumerate_devices", return_value=devices),
            pytest.raises(BypassApplyError),
        ):
            await strategy.apply(ctx)

    @pytest.mark.asyncio()
    async def test_open_failed_raises_bypass_apply_error(self) -> None:
        strategy = LinuxSessionManagerEscapeBypass()
        capture_task = MagicMock()
        capture_task.request_session_manager_restart = AsyncMock(
            return_value=SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.OPEN_FAILED_NO_STREAM,
                engaged=False,
                host_api="ALSA",
                device=4,
                detail="open failed",
            ),
        )
        ctx = _context(capture_task=capture_task)
        devices = [
            _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE, in_ch=2),
            _entry(index=6, name="pipewire", kind=DeviceKind.SESSION_MANAGER_VIRTUAL),
        ]
        with (
            patch("sovyx.voice.device_enum.enumerate_devices", return_value=devices),
            pytest.raises(BypassApplyError),
        ):
            await strategy.apply(ctx)


class TestFindPreferredTarget:
    def test_excludes_current_device(self) -> None:
        ctx = _context()  # current_device_index=4
        devices = [
            _entry(index=4, name="pipewire", kind=DeviceKind.SESSION_MANAGER_VIRTUAL),
            _entry(index=6, name="pipewire", kind=DeviceKind.SESSION_MANAGER_VIRTUAL),
        ]
        with patch("sovyx.voice.device_enum.enumerate_devices", return_value=devices):
            target = _find_preferred_session_manager_target(ctx)
        # Current device (index 4, host_api "ALSA") must be excluded.
        assert target is not None
        assert target.index == 6

    def test_enumeration_failure_returns_none(self) -> None:
        ctx = _context()

        def raise_(*_a: object, **_kw: object) -> None:
            raise RuntimeError("enum broke")

        with patch(
            "sovyx.voice.device_enum.enumerate_devices",
            side_effect=raise_,
        ):
            assert _find_preferred_session_manager_target(ctx) is None


class TestRevertIsNoop:
    @pytest.mark.asyncio()
    async def test_revert_returns_none(self) -> None:
        strategy = LinuxSessionManagerEscapeBypass()
        ctx = _context()
        assert await strategy.revert(ctx) is None
