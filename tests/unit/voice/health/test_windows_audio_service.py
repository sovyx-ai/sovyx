"""Tests for :mod:`sovyx.voice.health._windows_audio_service` (WI2)."""

from __future__ import annotations

import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.health._windows_audio_service import (
    AudioServiceStatus,
    AudioServiceWatchdog,
    WindowsServiceReport,
    WindowsServiceState,
    query_audio_service_status,
)


def _fake_run(
    *,
    audiosrv_state: str = "RUNNING",
    audiosrv_returncode: int = 0,
    audiosrv_raise: type[BaseException] | None = None,
    aeb_state: str = "RUNNING",
    aeb_returncode: int = 0,
    aeb_raise: type[BaseException] | None = None,
) -> Any:
    """Build a subprocess.run replacement that dispatches by service name."""

    def _build_stdout(state: str, service: str) -> str:
        # Mirror canonical sc.exe output format.
        state_line_value = {
            "RUNNING": "4  RUNNING",
            "STOPPED": "1  STOPPED",
            "START_PENDING": "2  START_PENDING",
            "STOP_PENDING": "3  STOP_PENDING",
            "PAUSED": "7  PAUSED",
        }.get(state, f"0  {state}")
        return (
            f"SERVICE_NAME: {service}\n"
            f"        TYPE               : 30  WIN32\n"
            f"        STATE              : {state_line_value}\n"
        )

    def _run(args: tuple[str, ...], **_kwargs: Any) -> Any:
        # args = (sc_path, "query", service_name)
        service = args[2] if len(args) >= 3 else ""  # noqa: PLR2004
        if service == "Audiosrv":
            if audiosrv_raise is not None:
                raise audiosrv_raise(args, _kwargs.get("timeout", 0))
            return MagicMock(
                returncode=audiosrv_returncode,
                stdout=_build_stdout(audiosrv_state, service),
                stderr="",
            )
        if service == "AudioEndpointBuilder":
            if aeb_raise is not None:
                raise aeb_raise(args, _kwargs.get("timeout", 0))
            return MagicMock(
                returncode=aeb_returncode,
                stdout=_build_stdout(aeb_state, service),
                stderr="",
            )
        return MagicMock(returncode=1, stdout="", stderr="unknown service")

    return _run


# ── Cross-platform branches ────────────────────────────────────────


class TestNonWindowsBranches:
    def test_linux_returns_unknown(self) -> None:
        with patch.object(sys, "platform", "linux"):
            status = query_audio_service_status()
        assert status.audiosrv.state is WindowsServiceState.UNKNOWN
        assert status.audio_endpoint_builder.state is WindowsServiceState.UNKNOWN
        assert any("non-windows" in n for n in status.audiosrv.notes)

    def test_darwin_returns_unknown(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            status = query_audio_service_status()
        assert status.audiosrv.state is WindowsServiceState.UNKNOWN


# ── Windows query branches ─────────────────────────────────────────


class TestQueryAudioServiceStatus:
    def test_sc_missing_returns_unknown(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value=None),
        ):
            status = query_audio_service_status()
        assert status.audiosrv.state is WindowsServiceState.UNKNOWN
        assert any("sc.exe binary not found" in n for n in status.audiosrv.notes)

    def test_both_running_returns_healthy(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value=r"C:\Windows\System32\sc.exe"),
            patch("subprocess.run", side_effect=_fake_run()),
        ):
            status = query_audio_service_status()
        assert status.audiosrv.state is WindowsServiceState.RUNNING
        assert status.audio_endpoint_builder.state is WindowsServiceState.RUNNING
        assert status.all_healthy is True
        assert status.degraded_services == ()

    def test_audiosrv_stopped_marks_degraded(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value=r"C:\Windows\System32\sc.exe"),
            patch("subprocess.run", side_effect=_fake_run(audiosrv_state="STOPPED")),
        ):
            status = query_audio_service_status()
        assert status.audiosrv.state is WindowsServiceState.STOPPED
        assert status.audiosrv.is_healthy is False
        assert status.all_healthy is False
        assert "Audiosrv" in status.degraded_services

    def test_aeb_paused_marks_degraded(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value=r"C:\Windows\System32\sc.exe"),
            patch("subprocess.run", side_effect=_fake_run(aeb_state="PAUSED")),
        ):
            status = query_audio_service_status()
        assert status.audio_endpoint_builder.state is WindowsServiceState.PAUSED
        assert "AudioEndpointBuilder" in status.degraded_services

    def test_pending_states_are_degraded(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value=r"C:\Windows\System32\sc.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(audiosrv_state="START_PENDING"),
            ),
        ):
            status = query_audio_service_status()
        assert status.audiosrv.state is WindowsServiceState.START_PENDING
        assert status.audiosrv.is_healthy is False

    def test_subprocess_timeout_returns_unknown_with_note(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value=r"C:\Windows\System32\sc.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(audiosrv_raise=subprocess.TimeoutExpired),
            ),
        ):
            status = query_audio_service_status()
        assert status.audiosrv.state is WindowsServiceState.UNKNOWN
        assert any("timed out" in n for n in status.audiosrv.notes)

    def test_service_not_found_returns_not_found(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value=r"C:\Windows\System32\sc.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(audiosrv_returncode=1060),
            ),
        ):
            status = query_audio_service_status()
        assert status.audiosrv.state is WindowsServiceState.NOT_FOUND

    def test_unparseable_state_line_returns_unknown(self) -> None:
        def _broken_run(args: tuple[str, ...], **_kwargs: Any) -> Any:
            return MagicMock(
                returncode=0,
                stdout="SERVICE_NAME: Audiosrv\n        TYPE               : 30  WIN32\n",
                stderr="",
            )

        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value=r"C:\Windows\System32\sc.exe"),
            patch("subprocess.run", side_effect=_broken_run),
        ):
            status = query_audio_service_status()
        assert status.audiosrv.state is WindowsServiceState.UNKNOWN
        assert any("STATE line not found" in n for n in status.audiosrv.notes)


# ── Watchdog ───────────────────────────────────────────────────────


class TestAudioServiceWatchdog:
    def test_construction_rejects_non_positive_interval(self) -> None:
        with pytest.raises(ValueError, match="interval_s must be > 0"):
            AudioServiceWatchdog(interval_s=0)
        with pytest.raises(ValueError, match="interval_s must be > 0"):
            AudioServiceWatchdog(interval_s=-1.0)

    @pytest.mark.asyncio
    async def test_start_stop_idempotent(self) -> None:
        # Mock query so the loop's to_thread doesn't actually run sc.exe.
        with patch(
            "sovyx.voice.health._windows_audio_service.query_audio_service_status",
            return_value=AudioServiceStatus(
                audiosrv=WindowsServiceReport(
                    name="Audiosrv",
                    state=WindowsServiceState.RUNNING,
                ),
                audio_endpoint_builder=WindowsServiceReport(
                    name="AudioEndpointBuilder",
                    state=WindowsServiceState.RUNNING,
                ),
            ),
        ):
            wd = AudioServiceWatchdog(interval_s=10.0)
            await wd.start()
            assert wd.is_running
            await wd.start()  # idempotent
            assert wd.is_running
            await wd.stop()
            assert not wd.is_running
            await wd.stop()  # idempotent

    @pytest.mark.asyncio
    async def test_state_change_invokes_callback(self) -> None:
        # Watchdog flips healthy → degraded between two ticks.
        seq = [
            AudioServiceStatus(
                audiosrv=WindowsServiceReport(
                    name="Audiosrv",
                    state=WindowsServiceState.RUNNING,
                ),
                audio_endpoint_builder=WindowsServiceReport(
                    name="AudioEndpointBuilder",
                    state=WindowsServiceState.RUNNING,
                ),
            ),
            AudioServiceStatus(
                audiosrv=WindowsServiceReport(
                    name="Audiosrv",
                    state=WindowsServiceState.STOPPED,
                ),
                audio_endpoint_builder=WindowsServiceReport(
                    name="AudioEndpointBuilder",
                    state=WindowsServiceState.RUNNING,
                ),
            ),
        ]
        # Drive the watchdog by hand — instantiate then call the
        # internal callback directly. This avoids needing a real
        # async clock.
        callback_calls: list[AudioServiceStatus] = []

        def cb(s: AudioServiceStatus) -> None:
            callback_calls.append(s)

        wd = AudioServiceWatchdog(on_state_change=cb, interval_s=1.0)
        wd._maybe_emit_change(seq[0])  # noqa: SLF001 — first tick records, no emit
        assert callback_calls == []
        wd._maybe_emit_change(seq[1])  # noqa: SLF001 — transition healthy→degraded
        assert len(callback_calls) == 1
        assert callback_calls[0].degraded_services == ("Audiosrv",)

    @pytest.mark.asyncio
    async def test_sustained_degraded_emits_once(self) -> None:
        callback_calls: list[AudioServiceStatus] = []
        wd = AudioServiceWatchdog(
            on_state_change=lambda s: callback_calls.append(s),
            interval_s=1.0,
        )
        degraded = AudioServiceStatus(
            audiosrv=WindowsServiceReport(
                name="Audiosrv",
                state=WindowsServiceState.STOPPED,
            ),
            audio_endpoint_builder=WindowsServiceReport(
                name="AudioEndpointBuilder",
                state=WindowsServiceState.RUNNING,
            ),
        )
        wd._maybe_emit_change(degraded)  # noqa: SLF001 — first tick, no emit
        wd._maybe_emit_change(degraded)  # noqa: SLF001 — sustained, no emit
        wd._maybe_emit_change(degraded)  # noqa: SLF001 — still sustained
        assert callback_calls == []  # No state CHANGE.

    @pytest.mark.asyncio
    async def test_recovery_transition_invokes_callback(self) -> None:
        callback_calls: list[AudioServiceStatus] = []
        wd = AudioServiceWatchdog(
            on_state_change=lambda s: callback_calls.append(s),
            interval_s=1.0,
        )
        degraded = AudioServiceStatus(
            audiosrv=WindowsServiceReport(
                name="Audiosrv",
                state=WindowsServiceState.STOPPED,
            ),
            audio_endpoint_builder=WindowsServiceReport(
                name="AudioEndpointBuilder",
                state=WindowsServiceState.RUNNING,
            ),
        )
        healthy = AudioServiceStatus(
            audiosrv=WindowsServiceReport(
                name="Audiosrv",
                state=WindowsServiceState.RUNNING,
            ),
            audio_endpoint_builder=WindowsServiceReport(
                name="AudioEndpointBuilder",
                state=WindowsServiceState.RUNNING,
            ),
        )
        wd._maybe_emit_change(degraded)  # noqa: SLF001 — first tick
        wd._maybe_emit_change(healthy)  # noqa: SLF001 — transition out of degraded
        assert len(callback_calls) == 1
        assert callback_calls[0].all_healthy is True


# ── Report contract ────────────────────────────────────────────────


class TestReportContracts:
    def test_state_enum_values_stable(self) -> None:
        assert WindowsServiceState.RUNNING.value == "running"
        assert WindowsServiceState.STOPPED.value == "stopped"
        assert WindowsServiceState.START_PENDING.value == "start_pending"
        assert WindowsServiceState.STOP_PENDING.value == "stop_pending"
        assert WindowsServiceState.PAUSED.value == "paused"
        assert WindowsServiceState.UNKNOWN.value == "unknown"
        assert WindowsServiceState.NOT_FOUND.value == "not_found"

    def test_running_report_is_healthy(self) -> None:
        r = WindowsServiceReport(name="X", state=WindowsServiceState.RUNNING)
        assert r.is_healthy is True

    def test_every_non_running_state_is_unhealthy(self) -> None:
        for state in (
            WindowsServiceState.STOPPED,
            WindowsServiceState.START_PENDING,
            WindowsServiceState.STOP_PENDING,
            WindowsServiceState.PAUSED,
            WindowsServiceState.UNKNOWN,
            WindowsServiceState.NOT_FOUND,
        ):
            r = WindowsServiceReport(name="X", state=state)
            assert r.is_healthy is False, f"{state} should NOT be healthy"
