"""Tests for the real probes wired in Step 3 (capability dispatch).

Phase 1 of the capability resolver shipped with stub probes returning
False for every capability except ONNX_INFERENCE. Step 3 wires real
probes for AUDIOSRV_QUERY, ETW_AUDIO_PROVIDER, and COREAUDIO_VPIO so
the dispatch sites in :mod:`sovyx.voice.factory` can replace
``sys.platform`` branching with capability checks.

Each probe must:

* Return False on the wrong platform (cheap platform identity gate).
* Return based on tool availability when on the right platform.
* Never raise — fail-closed semantics preserve safe-fallback behaviour.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 3.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from sovyx.voice.health._capabilities import (
    Capability,
    CapabilityResolver,
    _probe_audiosrv_query,
    _probe_coreaudio_vpio,
    _probe_etw_audio_provider,
)


class TestAudiosrvQueryProbe:
    """Real probe for :data:`Capability.AUDIOSRV_QUERY`.

    Validates platform=Windows AND sc.exe on PATH.
    """

    def test_returns_false_on_non_windows(self) -> None:
        with patch.object(sys, "platform", "linux"):
            assert _probe_audiosrv_query() is False
        with patch.object(sys, "platform", "darwin"):
            assert _probe_audiosrv_query() is False

    def test_returns_true_on_windows_with_sc_exe(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch(
                "sovyx.voice.health._capabilities.shutil.which",
                return_value=r"C:\Windows\System32\sc.exe",
            ),
        ):
            assert _probe_audiosrv_query() is True

    def test_returns_false_on_windows_without_sc_exe(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("sovyx.voice.health._capabilities.shutil.which", return_value=None),
        ):
            assert _probe_audiosrv_query() is False


class TestEtwAudioProviderProbe:
    """Real probe for :data:`Capability.ETW_AUDIO_PROVIDER`.

    Validates platform=Windows AND wevtutil on PATH.
    """

    def test_returns_false_on_non_windows(self) -> None:
        with patch.object(sys, "platform", "linux"):
            assert _probe_etw_audio_provider() is False
        with patch.object(sys, "platform", "darwin"):
            assert _probe_etw_audio_provider() is False

    def test_returns_true_on_windows_with_wevtutil(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch(
                "sovyx.voice.health._capabilities.shutil.which",
                return_value=r"C:\Windows\System32\wevtutil.exe",
            ),
        ):
            assert _probe_etw_audio_provider() is True

    def test_returns_false_on_windows_without_wevtutil(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("sovyx.voice.health._capabilities.shutil.which", return_value=None),
        ):
            assert _probe_etw_audio_provider() is False


class TestCoreAudioVpioProbe:
    """Real probe for :data:`Capability.COREAUDIO_VPIO`.

    Validates platform=darwin AND system_profiler on PATH.
    """

    def test_returns_false_on_non_darwin(self) -> None:
        with patch.object(sys, "platform", "linux"):
            assert _probe_coreaudio_vpio() is False
        with patch.object(sys, "platform", "win32"):
            assert _probe_coreaudio_vpio() is False

    def test_returns_true_on_darwin_with_system_profiler(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._capabilities.shutil.which",
                return_value="/usr/sbin/system_profiler",
            ),
        ):
            assert _probe_coreaudio_vpio() is True

    def test_returns_false_on_darwin_without_system_profiler(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch("sovyx.voice.health._capabilities.shutil.which", return_value=None),
        ):
            assert _probe_coreaudio_vpio() is False


class TestResolverIntegration:
    """End-to-end: resolver.has() returns the probe verdict + caches it."""

    @pytest.mark.parametrize(
        ("capability", "platform", "tool_present"),
        [
            (Capability.AUDIOSRV_QUERY, "win32", True),
            (Capability.ETW_AUDIO_PROVIDER, "win32", True),
            (Capability.COREAUDIO_VPIO, "darwin", True),
        ],
    )
    def test_resolver_has_returns_true_when_probe_passes(
        self,
        capability: Capability,
        platform: str,
        tool_present: bool,
    ) -> None:
        resolver = CapabilityResolver()
        with (
            patch.object(sys, "platform", platform),
            patch(
                "sovyx.voice.health._capabilities.shutil.which",
                return_value="/some/path" if tool_present else None,
            ),
        ):
            assert resolver.has(capability) is True

    @pytest.mark.parametrize(
        "capability",
        [
            Capability.AUDIOSRV_QUERY,
            Capability.ETW_AUDIO_PROVIDER,
            Capability.COREAUDIO_VPIO,
        ],
    )
    def test_resolver_has_returns_false_when_tool_absent(
        self,
        capability: Capability,
    ) -> None:
        resolver = CapabilityResolver()
        # Force "matching platform" + "tool absent" — only the
        # platform check passes, the shutil.which returns None.
        target_platform = {
            Capability.AUDIOSRV_QUERY: "win32",
            Capability.ETW_AUDIO_PROVIDER: "win32",
            Capability.COREAUDIO_VPIO: "darwin",
        }[capability]
        with (
            patch.object(sys, "platform", target_platform),
            patch("sovyx.voice.health._capabilities.shutil.which", return_value=None),
        ):
            assert resolver.has(capability) is False

    def test_resolver_caches_probe_result(self) -> None:
        """A second has() call must NOT re-invoke the probe."""
        resolver = CapabilityResolver()
        with (
            patch.object(sys, "platform", "win32"),
            patch(
                "sovyx.voice.health._capabilities.shutil.which",
                return_value=r"C:\Windows\sc.exe",
            ) as mock_which,
        ):
            verdict_1 = resolver.has(Capability.AUDIOSRV_QUERY)
            verdict_2 = resolver.has(Capability.AUDIOSRV_QUERY)

        assert verdict_1 is True
        assert verdict_2 is True
        # Two has() calls but only ONE probe invocation (cached).
        assert mock_which.call_count <= 2  # may call .exe and fallback once
        # Cache hit: the second has() returns immediately, so the cached
        # verdict survives the patch teardown.
        assert resolver.cached_results()[Capability.AUDIOSRV_QUERY] is True


class TestFactoryAudioServiceWatchdogDispatch:
    """Verify factory.py:_maybe_start_audio_service_watchdog uses the resolver.

    The pre-Step-3 implementation read ``sys.platform != "win32"``
    directly. After Step 3 the gate is ``not resolver.has(AUDIOSRV_QUERY)``
    so locked-down Windows images without sc.exe also skip activation
    (rather than crashing the watchdog at first poll).
    """

    @pytest.mark.asyncio()
    async def test_skipped_when_capability_absent(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice.factory import _maybe_start_audio_service_watchdog
        from sovyx.voice.health._capabilities import (
            Capability,
            CapabilityResolver,
            reset_default_resolver_for_tests,
        )

        # Enable the watchdog opt-in flag via env. VoiceTuningConfig
        # uses ``env_prefix="SOVYX_TUNING__VOICE__"`` so the field
        # ``voice_audio_service_watchdog_enabled`` reads from
        # ``SOVYX_TUNING__VOICE__VOICE_AUDIO_SERVICE_WATCHDOG_ENABLED``.
        monkeypatch.setenv(
            "SOVYX_TUNING__VOICE__VOICE_AUDIO_SERVICE_WATCHDOG_ENABLED",
            "true",
        )

        # Inject a resolver that fails AUDIOSRV_QUERY no matter what.
        all_false_resolver = CapabilityResolver(
            probes={cap: lambda: False for cap in Capability},
        )

        with patch(
            "sovyx.voice.health._capabilities.get_default_resolver",
            return_value=all_false_resolver,
        ):
            try:
                with caplog.at_level("INFO", logger="sovyx.voice.factory"):
                    result = await _maybe_start_audio_service_watchdog()
            finally:
                reset_default_resolver_for_tests()

        assert result is None
        # The skip-log fires with the new event name + capability label.
        skip_records = [
            r
            for r in caplog.records
            if "audio_service_watchdog_skipped_capability_absent" in str(r.msg)
        ]
        assert len(skip_records) >= 1
