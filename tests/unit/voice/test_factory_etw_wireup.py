"""Tests for the WI1 ETW factory wire-up (Step 4).

Mission §1.5 + §9.1.8: invoke the Windows audio ETW probe at boot
when the operator opts in, log structured event records per channel,
fail silent (never block boot) on subprocess / parse errors.

Three contracts pinned:

* Default OFF — the opt-in flag preserves the pre-Step-4 cold-boot
  cost (15s of subprocess overhead unacceptable as a default).
* Capability gate — when enabled but the resolver reports
  :data:`Capability.ETW_AUDIO_PROVIDER` absent, log skip + return
  cleanly (no exception leak).
* Happy path — when enabled + capability present, invoke the probe
  via ``asyncio.to_thread`` and emit one INFO record per channel
  that returned events.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 4.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.factory import _maybe_log_recent_audio_etw_events

_FACTORY_LOGGER = "sovyx.voice.factory"


@pytest.fixture(autouse=True)
def _reset_resolver_singleton() -> Generator[None, None, None]:
    from sovyx.voice.health._capabilities import (
        reset_default_resolver_for_tests,
    )

    reset_default_resolver_for_tests()
    yield
    reset_default_resolver_for_tests()


class TestEtwWireupDefaults:
    def test_etw_probe_default_false(self) -> None:
        """Default OFF — probe is opt-in to preserve cold-boot latency."""
        assert VoiceTuningConfig().voice_probe_windows_etw_events_enabled is False


class TestEtwWireupGates:
    @pytest.mark.asyncio
    async def test_disabled_returns_silently(self) -> None:
        # Default config — flag is OFF.
        result = await _maybe_log_recent_audio_etw_events()
        assert result is None

    @pytest.mark.asyncio
    async def test_enabled_but_capability_absent_logs_skip(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice.health._capabilities import (
            Capability,
            CapabilityResolver,
        )

        monkeypatch.setenv(
            "SOVYX_TUNING__VOICE__VOICE_PROBE_WINDOWS_ETW_EVENTS_ENABLED",
            "true",
        )
        all_false_resolver = CapabilityResolver(
            probes={cap: lambda: False for cap in Capability},
        )
        with (
            patch(
                "sovyx.voice.health._capabilities.get_default_resolver",
                return_value=all_false_resolver,
            ),
            caplog.at_level("INFO", logger=_FACTORY_LOGGER),
        ):
            result = await _maybe_log_recent_audio_etw_events()

        assert result is None
        skip_records = [
            r for r in caplog.records if "etw_probe_skipped_capability_absent" in str(r.msg)
        ]
        assert len(skip_records) >= 1


class TestEtwWireupHappyPath:
    @pytest.mark.asyncio
    async def test_enabled_and_capability_present_invokes_probe(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice.health._capabilities import (
            Capability,
            CapabilityResolver,
        )
        from sovyx.voice.health._windows_etw import (
            EtwEvent,
            EtwEventLevel,
            EtwQueryResult,
        )

        monkeypatch.setenv(
            "SOVYX_TUNING__VOICE__VOICE_PROBE_WINDOWS_ETW_EVENTS_ENABLED",
            "true",
        )

        # Resolver where ETW_AUDIO_PROVIDER probes True.
        always_true_resolver = CapabilityResolver(
            probes={
                cap: (lambda: True) if cap == Capability.ETW_AUDIO_PROVIDER else (lambda: False)
                for cap in Capability
            },
        )

        synthetic_events = (
            EtwQueryResult(
                channel="Microsoft-Windows-Audio/Operational",
                events=(
                    EtwEvent(
                        timestamp_iso="2026-04-25T10:00:00Z",
                        level=EtwEventLevel.WARNING,
                        event_id=42,
                        provider="Microsoft-Windows-Audio",
                        description="Default device changed",
                        raw_text="<event>...</event>",
                        channel="Microsoft-Windows-Audio/Operational",
                    ),
                ),
                lookback_seconds=3600,
            ),
            EtwQueryResult(
                channel="Microsoft-Windows-Audio/PlaybackManager",
                events=(),
                lookback_seconds=3600,
                notes=("channel not found",),
            ),
            EtwQueryResult(
                channel="Microsoft-Windows-Audio/CaptureMonitor",
                events=(),
                lookback_seconds=3600,
            ),
        )

        with (
            patch(
                "sovyx.voice.health._capabilities.get_default_resolver",
                return_value=always_true_resolver,
            ),
            patch(
                "sovyx.voice.health._windows_etw.query_audio_etw_events",
                return_value=synthetic_events,
            ),
            caplog.at_level("DEBUG", logger=_FACTORY_LOGGER),
        ):
            await _maybe_log_recent_audio_etw_events()

        # One INFO record for the Operational channel (it had events).
        events_records = [r for r in caplog.records if "voice.windows.etw_events" in str(r.msg)]
        assert len(events_records) == 1
        # One DEBUG record for the PlaybackManager channel notes.
        notes_records = [
            r for r in caplog.records if "voice.windows.etw_query_notes" in str(r.msg)
        ]
        assert len(notes_records) == 1

    @pytest.mark.asyncio
    async def test_probe_exception_does_not_propagate(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice.health._capabilities import (
            Capability,
            CapabilityResolver,
        )

        monkeypatch.setenv(
            "SOVYX_TUNING__VOICE__VOICE_PROBE_WINDOWS_ETW_EVENTS_ENABLED",
            "true",
        )

        resolver = CapabilityResolver(
            probes={
                cap: (lambda: True) if cap == Capability.ETW_AUDIO_PROVIDER else (lambda: False)
                for cap in Capability
            },
        )

        with (
            patch(
                "sovyx.voice.health._capabilities.get_default_resolver",
                return_value=resolver,
            ),
            patch(
                "asyncio.to_thread",
                new=AsyncMock(side_effect=RuntimeError("synthetic probe failure")),
            ),
            caplog.at_level("WARNING", logger=_FACTORY_LOGGER),
        ):
            # Must not raise.
            result = await _maybe_log_recent_audio_etw_events()

        assert result is None
        warn_records = [r for r in caplog.records if "etw_probe_failed" in str(r.msg)]
        assert len(warn_records) >= 1
