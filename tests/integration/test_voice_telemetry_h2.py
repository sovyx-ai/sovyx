"""Mission H2 §T2.7 + F6 integration test — OTel-style structured-emission
round-trip for the v2.0.0 metadata fields.

Verifies that the three v2.0.0 schema metadata fields
(``voice.platform``, ``voice.bypass_family``,
``voice.event_schema_version``) round-trip through structlog's
stdlib-routing pipeline with their expected types intact — no type
coercion, no field dropping. This is the load-bearing F6 falsifiability
gate; the Phase 2 analyzer at ``scripts/dev/analyze_h2_telemetry.py``
exercises the same invariant against operator logs.

Mission anchor:
``docs-internal/missions/MISSION-h2-platform-neutral-event-naming-2026-05-18.md``
§T2.7 + §3 falsifiability gate F6 + §10.2.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

import pytest

from sovyx.engine.config import LoggingConfig
from sovyx.observability.logging import setup_logging
from sovyx.voice._event_names import CaptureIntegrityEvent
from sovyx.voice._platform_metadata import current_platform_token
from sovyx.voice.pipeline._capture_integrity_emit import emit_capture_integrity_event


@pytest.fixture(scope="module", autouse=True)
def _h2_structlog_stdlib_routing() -> Generator[None, None, None]:
    """Mirror tests/unit/voice/conftest.py — structlog must route through
    stdlib logging so caplog can observe emissions."""
    setup_logging(LoggingConfig(level="DEBUG", console_format="json", log_file=None))
    yield


@pytest.fixture(autouse=True)
def _clear_platform_cache() -> None:
    """Cached platform token must not bleed across tests that monkeypatch sys.platform."""
    current_platform_token.cache_clear()
    yield
    current_platform_token.cache_clear()


_WRAPPER_LOGGER = "sovyx.voice.pipeline._capture_integrity_emit"


def _records_for(caplog: pytest.LogCaptureFixture, event_name: str) -> list[dict[str, Any]]:
    return [
        r.msg
        for r in caplog.records
        if r.name == _WRAPPER_LOGGER
        and isinstance(r.msg, dict)
        and r.msg.get("event") == event_name
    ]


class TestF6OtelRoundTrip:
    """F6 — every v2.0.0 metadata field round-trips through the structlog
    pipeline without type coercion or field dropping.
    """

    def test_voice_platform_round_trips_as_string(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASSED,
            "error",
            mind_id="round_trip",
            strategies=["linux.alsa_mixer_reset"],
            voice_clarity_active=False,
            verdict="failure",
        )
        recs = _records_for(caplog, "voice.capture_integrity.bypassed")
        assert len(recs) == 1
        platform = recs[0].get("voice.platform")
        assert isinstance(platform, str), (
            f"F6 invariant — voice.platform MUST be string, got {type(platform).__name__}"
        )
        assert platform in {"linux", "windows", "darwin", "other"}

    def test_voice_bypass_family_round_trips_as_string(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASS_INEFFECTIVE,
            "error",
            mind_id="round_trip",
            strategies=[
                "linux.alsa_mixer_reset",
                "linux.alsa_capture_switch",
            ],
            voice_clarity_active=False,
        )
        recs = _records_for(caplog, "voice.capture_integrity.bypass_ineffective")
        assert len(recs) == 1
        family = recs[0].get("voice.bypass_family")
        assert isinstance(family, str), (
            f"F6 invariant — voice.bypass_family MUST be string, got {type(family).__name__}"
        )
        assert family == "alsa_capture_chain"

    def test_voice_event_schema_version_round_trips_as_literal_2_0_0(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASS_ACTIVATED,
            "warning",
            mind_id="round_trip",
            strategies=["linux.alsa_mixer_reset"],
            voice_clarity_active=False,
        )
        recs = _records_for(caplog, "voice.capture_integrity.bypass_activated")
        assert len(recs) == 1
        schema_version = recs[0].get("voice.event_schema_version")
        assert schema_version == "2.0.0", (
            "F6 invariant — voice.event_schema_version MUST be the v2.0.0 literal. "
            f"Got: {schema_version!r}"
        )

    def test_strategies_list_round_trips_as_list_of_strings(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASSED,
            "error",
            mind_id="round_trip",
            strategies=[
                "linux.alsa_mixer_reset",
                "linux.pipewire_direct",
                "linux.wireplumber_default_source",
            ],
            voice_clarity_active=False,
            verdict="failure",
        )
        recs = _records_for(caplog, "voice.capture_integrity.bypassed")
        assert len(recs) == 1
        strategies = recs[0].get("voice.strategies")
        assert isinstance(strategies, list)
        assert all(isinstance(s, str) for s in strategies)
        assert strategies == [
            "linux.alsa_mixer_reset",
            "linux.pipewire_direct",
            "linux.wireplumber_default_source",
        ]

    def test_voice_clarity_active_round_trips_as_bool(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASSED,
            "error",
            mind_id="round_trip",
            strategies=["linux.alsa_mixer_reset"],
            voice_clarity_active=False,
            verdict="failure",
        )
        recs = _records_for(caplog, "voice.capture_integrity.bypassed")
        assert len(recs) == 1
        vca = recs[0].get("voice.voice_clarity_active")
        assert isinstance(vca, bool), (
            "F6 invariant — voice.voice_clarity_active MUST round-trip as bool, "
            f"not coerced to {type(vca).__name__}"
        )
        assert vca is False

    def test_all_three_v2_0_0_fields_present_on_every_neutral_event(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Every CaptureIntegrityEvent value carries all 3 metadata fields."""
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        for event in CaptureIntegrityEvent:
            caplog.clear()
            emit_capture_integrity_event(
                event,
                "error",
                mind_id="round_trip_loop",
                strategies=["linux.alsa_mixer_reset"],
                voice_clarity_active=False,
                verdict="failure",
            )
            recs = _records_for(caplog, str(event))
            assert len(recs) == 1, f"missing neutral emission for {event}"
            for required_field in (
                "voice.platform",
                "voice.bypass_family",
                "voice.event_schema_version",
            ):
                assert required_field in recs[0], (
                    f"F6 invariant — {event} missing required v2.0.0 field {required_field!r}"
                )
