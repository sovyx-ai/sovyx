"""Tests for the MA1+MA5+MA6 macOS diagnostics factory wire-up (Step 5).

Mission §1.5: invoke the macOS audio diagnostic trio at boot when on
darwin + system_profiler is available. Default ON for read-only
observability. Each probe failure is isolated so a single broken
detector cannot block the other two from running.

Three contracts pinned per probe (HAL / entitlement / Bluetooth):

* Capability gate via ``Capability.COREAUDIO_VPIO`` — non-darwin
  hosts skip silently (no log noise on Windows / Linux boot).
* Probe exception isolation — synthetic RuntimeError in any one probe
  emits a structured WARN but does NOT short-circuit the function.
* Telemetry fields match the dashboard schema (one INFO record per
  probe, with the load-bearing predicates surfaced as named fields).

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 5.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.factory import _maybe_log_macos_diagnostics

_FACTORY_LOGGER = "sovyx.voice.factory"


@pytest.fixture(autouse=True)
def _reset_resolver_singleton() -> Generator[None, None, None]:
    from sovyx.voice.health._capabilities import (
        reset_default_resolver_for_tests,
    )

    reset_default_resolver_for_tests()
    yield
    reset_default_resolver_for_tests()


class TestMacosDiagnosticsDefaults:
    def test_macos_diagnostics_default_true(self) -> None:
        """Default ON — read-only observability is safe by default."""
        assert VoiceTuningConfig().voice_probe_macos_diagnostics_enabled is True


class TestMacosDiagnosticsGates:
    @pytest.mark.asyncio
    async def test_disabled_returns_silently(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "SOVYX_TUNING__VOICE__VOICE_PROBE_MACOS_DIAGNOSTICS_ENABLED",
            "false",
        )
        result = await _maybe_log_macos_diagnostics()
        assert result is None

    @pytest.mark.asyncio
    async def test_capability_absent_no_op_no_log(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Non-darwin hosts skip silently (no log noise)."""
        from sovyx.voice.health._capabilities import (
            Capability,
            CapabilityResolver,
        )

        all_false_resolver = CapabilityResolver(
            probes={cap: lambda: False for cap in Capability},
        )
        with (
            patch(
                "sovyx.voice.health._capabilities.get_default_resolver",
                return_value=all_false_resolver,
            ),
            caplog.at_level("DEBUG", logger=_FACTORY_LOGGER),
        ):
            result = await _maybe_log_macos_diagnostics()

        assert result is None
        # No darwin-specific log records should fire on non-darwin.
        macos_records = [r for r in caplog.records if "voice.macos" in str(r.msg)]
        assert macos_records == []


class TestMacosDiagnosticsHappyPath:
    @pytest.mark.asyncio
    async def test_all_three_probes_invoked_when_capability_present(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sovyx.voice._bluetooth_profile_mac import (
            BluetoothReport,
        )
        from sovyx.voice._codesign_verify_mac import (
            EntitlementReport,
            EntitlementVerdict,
        )
        from sovyx.voice._hal_detector_mac import (
            HalPluginCategory,
            HalPluginEntry,
            HalReport,
        )
        from sovyx.voice.health._capabilities import (
            Capability,
            CapabilityResolver,
        )

        # Resolver where COREAUDIO_VPIO probes True.
        resolver = CapabilityResolver(
            probes={
                cap: (lambda: True) if cap == Capability.COREAUDIO_VPIO else (lambda: False)
                for cap in Capability
            },
        )

        # Synthetic reports.
        hal_report = HalReport(
            plugins=(
                HalPluginEntry(
                    bundle_name="krisp",
                    path="/Library/Audio/Plug-Ins/HAL/krisp.driver",
                    category=HalPluginCategory.AUDIO_ENHANCEMENT,
                    friendly_label="Krisp (noise suppression)",
                ),
            ),
        )
        entitlement_report = EntitlementReport(
            verdict=EntitlementVerdict.PRESENT,
            executable_path="/Applications/Sovyx.app/Contents/MacOS/sovyx",
        )
        bt_report = BluetoothReport(devices=())

        with (
            patch(
                "sovyx.voice.health._capabilities.get_default_resolver",
                return_value=resolver,
            ),
            patch(
                "sovyx.voice._hal_detector_mac.detect_hal_plugins",
                return_value=hal_report,
            ),
            patch(
                "sovyx.voice._codesign_verify_mac.verify_microphone_entitlement",
                return_value=entitlement_report,
            ),
            patch(
                "sovyx.voice._bluetooth_profile_mac.detect_bluetooth_audio_profile",
                return_value=bt_report,
            ),
            caplog.at_level("INFO", logger=_FACTORY_LOGGER),
        ):
            await _maybe_log_macos_diagnostics()

        # HAL probe emits one INFO when plugins are non-empty.
        hal_records = [
            r for r in caplog.records if "voice.macos.hal_plugins_detected" in str(r.msg)
        ]
        assert len(hal_records) == 1

        # Entitlement probe always emits one INFO (always relevant).
        entitlement_records = [
            r for r in caplog.records if "voice.macos.entitlement_verified" in str(r.msg)
        ]
        assert len(entitlement_records) == 1

        # Bluetooth probe always emits one INFO (baseline-zero).
        bt_records = [
            r for r in caplog.records if "voice.macos.bluetooth_profile_detected" in str(r.msg)
        ]
        assert len(bt_records) == 1


class TestMacosDiagnosticsExceptionIsolation:
    @pytest.mark.asyncio
    async def test_one_probe_exception_does_not_short_circuit_others(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A synthetic RuntimeError in detect_hal_plugins must not
        prevent the entitlement + Bluetooth probes from running."""
        from sovyx.voice._bluetooth_profile_mac import BluetoothReport
        from sovyx.voice._codesign_verify_mac import (
            EntitlementReport,
            EntitlementVerdict,
        )
        from sovyx.voice.health._capabilities import (
            Capability,
            CapabilityResolver,
        )

        resolver = CapabilityResolver(
            probes={
                cap: (lambda: True) if cap == Capability.COREAUDIO_VPIO else (lambda: False)
                for cap in Capability
            },
        )

        with (
            patch(
                "sovyx.voice.health._capabilities.get_default_resolver",
                return_value=resolver,
            ),
            patch(
                "sovyx.voice._hal_detector_mac.detect_hal_plugins",
                side_effect=RuntimeError("synthetic HAL probe failure"),
            ),
            patch(
                "sovyx.voice._codesign_verify_mac.verify_microphone_entitlement",
                return_value=EntitlementReport(verdict=EntitlementVerdict.UNKNOWN),
            ),
            patch(
                "sovyx.voice._bluetooth_profile_mac.detect_bluetooth_audio_profile",
                return_value=BluetoothReport(),
            ),
            # Capture both the WARNING for the failed HAL probe AND
            # the INFO records that the surviving probes emit.
            caplog.at_level("INFO", logger=_FACTORY_LOGGER),
        ):
            # Must not raise.
            await _maybe_log_macos_diagnostics()

        hal_warn = [r for r in caplog.records if "macos_hal_probe_failed" in str(r.msg)]
        assert len(hal_warn) == 1
        # The other two probes still run + log.
        entitlement_records = [
            r for r in caplog.records if "voice.macos.entitlement_verified" in str(r.msg)
        ]
        bt_records = [
            r for r in caplog.records if "voice.macos.bluetooth_profile_detected" in str(r.msg)
        ]
        assert len(entitlement_records) == 1
        assert len(bt_records) == 1
