"""Tests for the band-aid #34 / #28 / F3 / F4 wire-ups in voice factory.

Each gate is opt-in via VoiceTuningConfig with default OFF for the
risky ones (mic / LLM raise / WARN at startup) and default ON for
the read-only observability ones (PipeWire / UCM detection logged).

This test file pins:

* The defaults so a future config refactor can't silently flip them.
* The opt-in DENIED / unreachable behaviour so the gates fire when
  enabled.
* The error / log surface contracts (VoicePermissionError carries
  remediation_hint; LLM unreachable logs a structured WARN).
* The graceful-fallback paths (probe failure logs WARN, never
  raises beyond VoicePermissionError).
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.factory import (
    VoicePermissionError,
    _maybe_check_llm_reachable,
    _maybe_check_mic_permission,
    _maybe_log_alsa_ucm_status,
    _maybe_log_pipewire_status,
    _maybe_start_audio_service_watchdog,
)

_FACTORY_LOGGER = "sovyx.voice.factory"


# ── Defaults pin (regression guard) ───────────────────────────────


class TestWireupDefaults:
    """Lock in the default opt-in semantics so a future config
    refactor can't silently flip behaviour for existing deployments."""

    def test_mic_permission_check_default_false(self) -> None:
        assert VoiceTuningConfig().voice_check_mic_permission_enabled is False

    def test_llm_reachable_check_default_false(self) -> None:
        assert VoiceTuningConfig().voice_check_llm_reachable_enabled is False

    def test_pipewire_detection_default_true(self) -> None:
        assert VoiceTuningConfig().voice_pipewire_detection_enabled is True

    def test_alsa_ucm_detection_default_true(self) -> None:
        assert VoiceTuningConfig().voice_alsa_ucm_detection_enabled is True

    def test_audio_service_watchdog_default_false(self) -> None:
        assert VoiceTuningConfig().voice_audio_service_watchdog_enabled is False


# ── Mic permission gate (band-aid #34 wire-up) ────────────────────


class TestMicPermissionGate:
    def test_disabled_default_is_noop(self) -> None:
        # No exceptions raised, no probe called.
        with patch("sovyx.voice.health._mic_permission.check_microphone_permission") as mock_probe:
            _maybe_check_mic_permission()
        mock_probe.assert_not_called()

    def test_enabled_granted_proceeds_silently(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sovyx.voice.health._mic_permission import (
            MicPermissionReport,
            MicPermissionStatus,
        )

        granted = MicPermissionReport(status=MicPermissionStatus.GRANTED)
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_check_mic_permission_enabled=True),
            ),
            patch(
                "sovyx.voice.health._mic_permission.check_microphone_permission",
                return_value=granted,
            ),
        ):
            caplog.set_level(logging.INFO, logger=_FACTORY_LOGGER)
            _maybe_check_mic_permission()
        # No error log; no exception raised.
        denied_events = [r for r in caplog.records if "denied" in r.message.lower()]
        assert denied_events == []

    def test_enabled_denied_raises_voice_permission_error(self) -> None:
        from sovyx.voice.health._mic_permission import (
            MicPermissionReport,
            MicPermissionStatus,
        )

        denied = MicPermissionReport(
            status=MicPermissionStatus.DENIED,
            machine_value="Allow",
            user_value="Deny",
        )
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_check_mic_permission_enabled=True),
            ),
            patch(
                "sovyx.voice.health._mic_permission.check_microphone_permission",
                return_value=denied,
            ),
            pytest.raises(VoicePermissionError) as exc_info,
        ):
            _maybe_check_mic_permission()
        # Carries the structured fields the dashboard renders.
        assert exc_info.value.platform_status == "denied"
        # OS-conditional case ("Privacy & security" on Linux/Win,
        # "Privacy & Security" on macOS) — match case-insensitively.
        assert "privacy &" in exc_info.value.remediation_hint.lower()
        assert "all-zero frames" in str(exc_info.value)

    def test_enabled_unknown_logs_info_and_proceeds(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sovyx.voice.health._mic_permission import (
            MicPermissionReport,
            MicPermissionStatus,
        )

        unknown = MicPermissionReport(
            status=MicPermissionStatus.UNKNOWN,
            notes=("test stub",),
        )
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_check_mic_permission_enabled=True),
            ),
            patch(
                "sovyx.voice.health._mic_permission.check_microphone_permission",
                return_value=unknown,
            ),
        ):
            caplog.set_level(logging.INFO, logger=_FACTORY_LOGGER)
            _maybe_check_mic_permission()  # No exception.
        # Logged the unknown status.
        unknown_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.factory.mic_permission_unknown"
        ]
        assert len(unknown_events) == 1

    def test_enabled_probe_crash_logs_warn_does_not_raise(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_check_mic_permission_enabled=True),
            ),
            patch(
                "sovyx.voice.health._mic_permission.check_microphone_permission",
                side_effect=RuntimeError("probe boom"),
            ),
        ):
            caplog.set_level(logging.WARNING, logger=_FACTORY_LOGGER)
            _maybe_check_mic_permission()  # Must not raise.
        crash_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.factory.mic_permission_probe_failed"
        ]
        assert len(crash_events) == 1
        assert crash_events[0]["error_type"] == "RuntimeError"


# ── LLM reachable gate (band-aid #28 wire-up) ─────────────────────


class TestLlmReachableGate:
    @pytest.mark.asyncio
    async def test_disabled_default_is_noop(self) -> None:
        # Even with a router available, the gate is OFF → no check.
        mock_router = MagicMock()
        mock_router._providers = [MagicMock(name="anthropic", is_available=True)]
        # No exceptions; check_llm_reachable never invoked.
        with patch(
            "sovyx.voice.health.preflight.check_llm_reachable",
        ) as mock_check:
            await _maybe_check_llm_reachable(router=mock_router)
        mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_enabled_no_router_skips_silently(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with patch(
            "sovyx.engine.config.VoiceTuningConfig",
            return_value=MagicMock(voice_check_llm_reachable_enabled=True),
        ):
            caplog.set_level(logging.INFO, logger=_FACTORY_LOGGER)
            await _maybe_check_llm_reachable(router=None)
        skip_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.factory.llm_check_skipped_no_router"
        ]
        assert len(skip_events) == 1

    @pytest.mark.asyncio
    async def test_enabled_unreachable_logs_warn_does_not_raise(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_router = MagicMock()
        mock_router._providers = []  # Empty → check returns FAIL.

        with patch(
            "sovyx.engine.config.VoiceTuningConfig",
            return_value=MagicMock(voice_check_llm_reachable_enabled=True),
        ):
            caplog.set_level(logging.WARNING, logger=_FACTORY_LOGGER)
            await _maybe_check_llm_reachable(router=mock_router)  # Must not raise.

        warn_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.factory.llm_unreachable_at_startup"
        ]
        assert len(warn_events) == 1
        assert "No LLM providers configured" in warn_events[0]["voice.action_required"]

    @pytest.mark.asyncio
    async def test_enabled_check_crash_logs_warn_no_raise(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_router = MagicMock()
        # Inject a check_llm_reachable that crashes when called.
        crashing_check = MagicMock(
            side_effect=RuntimeError("check construction boom"),
        )
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_check_llm_reachable_enabled=True),
            ),
            patch(
                "sovyx.voice.health.preflight.check_llm_reachable",
                crashing_check,
            ),
        ):
            caplog.set_level(logging.WARNING, logger=_FACTORY_LOGGER)
            await _maybe_check_llm_reachable(router=mock_router)  # Must not raise.

        crash_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.factory.llm_check_probe_failed"
        ]
        assert len(crash_events) == 1


# ── PipeWire observability (F3 wire-up) ───────────────────────────


class TestPipeWireObservability:
    def test_disabled_does_nothing(self) -> None:
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_pipewire_detection_enabled=False),
            ),
            patch("sovyx.voice.health._pipewire.detect_pipewire") as mock_detect,
        ):
            _maybe_log_pipewire_status()
        mock_detect.assert_not_called()

    def test_enabled_logs_status(self, caplog: pytest.LogCaptureFixture) -> None:
        from sovyx.voice.health._pipewire import PipeWireReport, PipeWireStatus

        report = PipeWireReport(
            status=PipeWireStatus.RUNNING_WITH_ECHO_CANCEL,
            socket_present=True,
            pactl_available=True,
            pactl_info_ok=True,
            server_name="PulseAudio (on PipeWire 1.2.0)",
            modules_loaded=("module-echo-cancel",),
            echo_cancel_loaded=True,
        )
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_pipewire_detection_enabled=True),
            ),
            patch(
                "sovyx.voice.health._pipewire.detect_pipewire",
                return_value=report,
            ),
        ):
            caplog.set_level(logging.INFO, logger=_FACTORY_LOGGER)
            _maybe_log_pipewire_status()

        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get("event") == "voice.factory.pipewire_status"
        ]
        assert len(events) == 1
        assert events[0]["voice.pipewire_status"] == "running_with_echo_cancel"
        assert events[0]["voice.pipewire_echo_cancel_loaded"] is True

    def test_probe_crash_logs_warn_no_raise(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_pipewire_detection_enabled=True),
            ),
            patch(
                "sovyx.voice.health._pipewire.detect_pipewire",
                side_effect=RuntimeError("probe boom"),
            ),
        ):
            caplog.set_level(logging.WARNING, logger=_FACTORY_LOGGER)
            _maybe_log_pipewire_status()  # Must not raise.
        crash_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.factory.pipewire_detection_failed"
        ]
        assert len(crash_events) == 1


# ── ALSA UCM observability (F4 wire-up) ───────────────────────────


class TestAlsaUcmObservability:
    def test_disabled_does_nothing(self) -> None:
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_alsa_ucm_detection_enabled=False),
            ),
            patch("sovyx.voice.health._alsa_ucm.detect_ucm") as mock_detect,
        ):
            _maybe_log_alsa_ucm_status()
        mock_detect.assert_not_called()

    def test_enabled_logs_status(self, caplog: pytest.LogCaptureFixture) -> None:
        from sovyx.voice.health._alsa_ucm import UcmReport, UcmStatus

        report = UcmReport(
            status=UcmStatus.ACTIVE,
            card_id="0",
            alsaucm_available=True,
            verbs=("HiFi", "VoiceCall"),
            active_verb="HiFi",
        )
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_alsa_ucm_detection_enabled=True),
            ),
            patch(
                "sovyx.voice.health._alsa_ucm.detect_ucm",
                return_value=report,
            ),
        ):
            caplog.set_level(logging.INFO, logger=_FACTORY_LOGGER)
            _maybe_log_alsa_ucm_status()

        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get("event") == "voice.factory.alsa_ucm_status"
        ]
        assert len(events) == 1
        assert events[0]["voice.ucm_status"] == "active"
        assert events[0]["voice.ucm_active_verb"] == "HiFi"

    def test_probe_crash_logs_warn_no_raise(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_alsa_ucm_detection_enabled=True),
            ),
            patch(
                "sovyx.voice.health._alsa_ucm.detect_ucm",
                side_effect=RuntimeError("probe boom"),
            ),
        ):
            caplog.set_level(logging.WARNING, logger=_FACTORY_LOGGER)
            _maybe_log_alsa_ucm_status()  # Must not raise.
        crash_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.factory.alsa_ucm_detection_failed"
        ]
        assert len(crash_events) == 1


# ── Error contract ────────────────────────────────────────────────


class TestVoicePermissionError:
    def test_subclasses_voice_factory_error(self) -> None:
        from sovyx.voice.factory import VoiceFactoryError

        err = VoicePermissionError("test")
        assert isinstance(err, VoiceFactoryError)

    def test_carries_structured_fields(self) -> None:
        err = VoicePermissionError(
            "msg",
            remediation_hint="open Settings",
            platform_status="denied",
        )
        assert err.remediation_hint == "open Settings"
        assert err.platform_status == "denied"

    def test_default_field_values(self) -> None:
        err = VoicePermissionError("msg")
        assert err.remediation_hint == ""
        assert err.platform_status == ""


# ── Audio service watchdog (WI2 wire-up) ──────────────────────────


class TestAudioServiceWatchdog:
    """Tests for the WI2 wire-up + Step 3 capability dispatch migration.

    Each test resets the process-wide :class:`CapabilityResolver`
    singleton so a probe verdict cached under a patched ``sys.platform``
    (e.g., the non-windows skip test patching to ``"linux"``) does not
    leak into the next test that expects a fresh probe under the real
    platform.
    """

    @pytest.fixture(autouse=True)
    def _reset_resolver_singleton(self) -> Generator[None, None, None]:
        from sovyx.voice.health._capabilities import (
            reset_default_resolver_for_tests,
        )

        reset_default_resolver_for_tests()
        yield
        reset_default_resolver_for_tests()

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self) -> None:
        # Default config — watchdog is OFF.
        result = await _maybe_start_audio_service_watchdog()
        assert result is None

    @pytest.mark.asyncio
    async def test_enabled_non_windows_returns_none_with_log(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import sys

        # Step 3 (X1 Phase 3): the gate now reads
        # ``resolver.has(Capability.AUDIOSRV_QUERY)`` instead of
        # ``sys.platform != "win32"``. The probe internally requires
        # both Windows AND ``sc.exe`` on PATH; ``patch.object(sys,
        # "platform", "linux")`` makes the probe return False, so the
        # capability-absent skip path fires.
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_audio_service_watchdog_enabled=True),
            ),
            patch.object(sys, "platform", "linux"),
        ):
            caplog.set_level(logging.INFO, logger=_FACTORY_LOGGER)
            result = await _maybe_start_audio_service_watchdog()
        assert result is None
        skip_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event")
            == "voice.factory.audio_service_watchdog_skipped_capability_absent"
        ]
        assert len(skip_events) == 1

    @pytest.mark.asyncio
    async def test_enabled_windows_starts_watchdog(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import sys

        from sovyx.voice.health._windows_audio_service import (
            AudioServiceStatus,
            WindowsServiceReport,
            WindowsServiceState,
        )

        # Stub the underlying query so the watchdog's loop doesn't
        # actually invoke sc.exe.
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
        # Step 3 capability dispatch: the gate is now
        # ``resolver.has(Capability.AUDIOSRV_QUERY)`` which probes
        # platform=win32 AND shutil.which("sc.exe") is not None. On
        # the Linux CI runner sc.exe is absent, so we additionally
        # patch shutil.which to simulate the Windows host.
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_audio_service_watchdog_enabled=True),
            ),
            patch.object(sys, "platform", "win32"),
            patch(
                "sovyx.voice.health._capabilities.shutil.which",
                return_value=r"C:\Windows\System32\sc.exe",
            ),
            patch(
                "sovyx.voice.health._windows_audio_service.query_audio_service_status",
                return_value=healthy,
            ),
        ):
            caplog.set_level(logging.INFO, logger=_FACTORY_LOGGER)
            watchdog = await _maybe_start_audio_service_watchdog()

        assert watchdog is not None
        assert watchdog.is_running
        # Cleanup — must not leak the asyncio task.
        await watchdog.stop()
        # Confirm it logged the start event.
        start_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.factory.audio_service_watchdog_started"
        ]
        assert len(start_events) == 1

    @pytest.mark.asyncio
    async def test_start_failure_logs_warn_returns_none(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import sys

        # Force the import inside the helper to fail by injecting a
        # broken module surface. Step 3 capability gate also requires
        # patched shutil.which (see test_enabled_windows_starts_watchdog
        # for the rationale).
        with (
            patch(
                "sovyx.engine.config.VoiceTuningConfig",
                return_value=MagicMock(voice_audio_service_watchdog_enabled=True),
            ),
            patch.object(sys, "platform", "win32"),
            patch(
                "sovyx.voice.health._capabilities.shutil.which",
                return_value=r"C:\Windows\System32\sc.exe",
            ),
            patch(
                "sovyx.voice.health._windows_audio_service.AudioServiceWatchdog",
                side_effect=RuntimeError("ctor boom"),
            ),
        ):
            caplog.set_level(logging.WARNING, logger=_FACTORY_LOGGER)
            result = await _maybe_start_audio_service_watchdog()
        assert result is None
        crash_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.factory.audio_service_watchdog_start_failed"
        ]
        assert len(crash_events) == 1


pytestmark = pytest.mark.timeout(15)
