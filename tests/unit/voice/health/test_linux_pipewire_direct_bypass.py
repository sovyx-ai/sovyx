"""Unit tests for :class:`LinuxPipeWireDirectBypass`.

Covers:

* ``probe_eligibility`` — not-linux, disabled-by-tuning, non-session-
  manager host, happy path.
* ``apply`` — ALSA_HW_ENGAGED success, every verdict maps to a stable
  :class:`BypassApplyError.reason` token.
* ``revert`` — SESSION_MANAGER_ENGAGED is a no-op; non-engaged verdict
  raises :class:`BypassRevertError` (B3 atomic-revert contract).
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from sovyx.voice._capture_task import (
    AlsaHwDirectRestartResult,
    AlsaHwDirectRestartVerdict,
    SessionManagerRestartResult,
    SessionManagerRestartVerdict,
)
from sovyx.voice.health.bypass import _linux_pipewire_direct as mod
from sovyx.voice.health.bypass._linux_pipewire_direct import (
    LinuxPipeWireDirectBypass,
)
from sovyx.voice.health.bypass._strategy import BypassApplyError, BypassRevertError
from sovyx.voice.health.contract import BypassContext


@dataclass
class _FakeCaptureTask:
    alsa_result: AlsaHwDirectRestartResult | None = None
    sm_result: SessionManagerRestartResult | None = None

    async def request_alsa_hw_direct_restart(self) -> AlsaHwDirectRestartResult:
        assert self.alsa_result is not None
        return self.alsa_result

    async def request_session_manager_restart(self) -> SessionManagerRestartResult:
        assert self.sm_result is not None
        return self.sm_result


def _ctx(
    *,
    platform_key: str = "linux",
    host_api_name: str = "PipeWire",
    capture_task: _FakeCaptureTask | None = None,
) -> BypassContext:
    async def _probe() -> None:  # pragma: no cover
        raise AssertionError("probe_fn must not be called")

    return BypassContext(
        endpoint_guid="guid",
        endpoint_friendly_name="Default",
        host_api_name=host_api_name,
        platform_key=platform_key,
        capture_task=capture_task or _FakeCaptureTask(),  # type: ignore[arg-type]
        probe_fn=_probe,  # type: ignore[arg-type]
    )


class TestEligibility:
    @pytest.mark.asyncio()
    async def test_not_linux(self) -> None:
        res = await LinuxPipeWireDirectBypass().probe_eligibility(_ctx(platform_key="win32"))
        assert res.applicable is False
        assert res.reason == "not_linux_platform"

    @pytest.mark.asyncio()
    async def test_disabled_by_default(self) -> None:
        # Default tuning: linux_pipewire_direct_bypass_enabled=False.
        res = await LinuxPipeWireDirectBypass().probe_eligibility(_ctx())
        assert res.applicable is False
        assert res.reason == "pipewire_direct_bypass_disabled_by_tuning"

    @pytest.mark.asyncio()
    async def test_non_session_manager_host(self) -> None:
        class _T:
            linux_pipewire_direct_bypass_enabled = True

        with patch.object(mod, "VoiceTuningConfig", return_value=_T()):
            res = await LinuxPipeWireDirectBypass().probe_eligibility(_ctx(host_api_name="ALSA"))
        assert res.applicable is False
        assert res.reason == "endpoint_not_served_by_session_manager"

    @pytest.mark.asyncio()
    async def test_applicable_when_enabled_and_session_manager(self) -> None:
        class _T:
            linux_pipewire_direct_bypass_enabled = True

        with patch.object(mod, "VoiceTuningConfig", return_value=_T()):
            res = await LinuxPipeWireDirectBypass().probe_eligibility(
                _ctx(host_api_name="PipeWire")
            )
        assert res.applicable is True
        assert res.estimated_cost_ms > 0


class TestApply:
    @pytest.mark.asyncio()
    async def test_engaged_returns_success_detail(self) -> None:
        task = _FakeCaptureTask(
            alsa_result=AlsaHwDirectRestartResult(
                verdict=AlsaHwDirectRestartVerdict.ALSA_HW_ENGAGED,
                engaged=True,
                host_api="ALSA",
                device="hw:0,0",
                sample_rate=16000,
                detail="opened",
            )
        )
        detail = await LinuxPipeWireDirectBypass().apply(_ctx(capture_task=task))
        assert detail == "alsa_hw_engaged"

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        ("verdict", "reason"),
        [
            (
                AlsaHwDirectRestartVerdict.NO_ALSA_SIBLING,
                "alsa_hw_direct_no_sibling",
            ),
            (
                AlsaHwDirectRestartVerdict.DOWNGRADED_TO_SESSION_MANAGER,
                "alsa_hw_direct_downgraded_to_session_manager",
            ),
            (
                AlsaHwDirectRestartVerdict.OPEN_FAILED_NO_STREAM,
                "alsa_hw_direct_open_failed_no_stream",
            ),
            (
                AlsaHwDirectRestartVerdict.NOT_RUNNING,
                "capture_task_not_running",
            ),
            (
                AlsaHwDirectRestartVerdict.NOT_LINUX,
                "not_linux_platform",
            ),
        ],
    )
    async def test_non_engaged_verdicts_raise(
        self,
        verdict: AlsaHwDirectRestartVerdict,
        reason: str,
    ) -> None:
        task = _FakeCaptureTask(
            alsa_result=AlsaHwDirectRestartResult(
                verdict=verdict,
                engaged=False,
                host_api=None,
                device=None,
                sample_rate=None,
                detail="dummy",
            )
        )
        with pytest.raises(BypassApplyError) as exc:
            await LinuxPipeWireDirectBypass().apply(_ctx(capture_task=task))
        assert exc.value.reason == reason


class TestRevert:
    @pytest.mark.asyncio()
    async def test_session_manager_engaged_is_clean(self) -> None:
        task = _FakeCaptureTask(
            sm_result=SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.SESSION_MANAGER_ENGAGED,
                engaged=True,
                host_api="PipeWire",
                device="default",
                sample_rate=16000,
                detail="reopened",
            )
        )
        # Must not raise.
        await LinuxPipeWireDirectBypass().revert(_ctx(capture_task=task))

    @pytest.mark.asyncio()
    async def test_non_engaged_revert_raises_bypass_revert_error(self) -> None:
        """B3: revert failure must raise BypassRevertError with structured
        reason — not log+swallow as in the pre-B3 band-aid."""
        task = _FakeCaptureTask(
            sm_result=SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.OPEN_FAILED_NO_STREAM,
                engaged=False,
                host_api=None,
                device=None,
                sample_rate=None,
                detail="couldn't reopen",
            )
        )
        with pytest.raises(BypassRevertError) as exc:
            await LinuxPipeWireDirectBypass().revert(_ctx(capture_task=task))
        assert exc.value.reason == "session_manager_restart_open_failed_no_stream"
        assert "couldn't reopen" in str(exc.value)

    @pytest.mark.parametrize(
        ("verdict", "expected_reason"),
        [
            (
                SessionManagerRestartVerdict.OPEN_FAILED_NO_STREAM,
                "session_manager_restart_open_failed_no_stream",
            ),
            (
                SessionManagerRestartVerdict.NOT_RUNNING,
                "session_manager_restart_not_running",
            ),
            (
                SessionManagerRestartVerdict.NOT_LINUX,
                "session_manager_restart_not_linux",
            ),
        ],
    )
    @pytest.mark.asyncio()
    async def test_revert_reason_token_per_verdict(
        self,
        verdict: SessionManagerRestartVerdict,
        expected_reason: str,
    ) -> None:
        """Every non-engaged verdict maps to a stable BypassRevertError reason."""
        task = _FakeCaptureTask(
            sm_result=SessionManagerRestartResult(
                verdict=verdict,
                engaged=False,
                host_api=None,
                device=None,
                sample_rate=None,
                detail=None,
            )
        )
        with pytest.raises(BypassRevertError) as exc:
            await LinuxPipeWireDirectBypass().revert(_ctx(capture_task=task))
        assert exc.value.reason == expected_reason
