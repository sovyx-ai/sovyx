"""F3 forensic-replay falsifiability for Mission H2 (§T2.9 + §3 gate F3).

Replays the operator-log L1011 / L1013 / L1065 / L1067 sequence from
``docs-internal/FORENSIC-AUDIT-LOG-2026-05-14-v0.43.1.md`` §H2 as a
synthetic Linux env capture-integrity coordinator dispatch and asserts:

(a) the neutral ``voice.capture_integrity.*`` event names fire with the
    expected ``voice.platform="linux"`` + ``voice.bypass_family`` resolved
    to ``alsa_capture_chain`` (majority vote across the 5 Linux strategies
    the operator's session emitted at L1067);
(b) the legacy ``audio.apo.bypassed`` / ``voice_apo_bypass_*`` events
    continue firing per ADR-D14 dual-emission;
(c) ``voice_clarity_active=False`` carries through unchanged on the
    legacy side (matches operator's Linux Mint reality);
(d) the operator can grep ``voice.platform=linux voice.bypass_family=alsa_capture_chain``
    on the new event to disambiguate platform WITHOUT inspecting strategy
    names — closes the H2 forensic-triage gap.

Counterfactual: on pre-mission HEAD ``cd3305dc`` (before Phase 1.B
ships) the wrapper helper did not exist; the bypass coordinator emitted
ONLY legacy events. The neutral-event assertions in this test would
fail. After Phase 1.B v0.49.7 ships, both legacy AND neutral events
fire — this test passes.

Mission anchor: ``docs-internal/missions/MISSION-h2-platform-neutral-event-naming-2026-05-18.md``
§T2.9 + §3 falsifiability gate F3.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

import pytest

from sovyx.engine.config import LoggingConfig
from sovyx.observability.logging import setup_logging
from sovyx.voice._event_names import LEGACY_TWIN_MAP, CaptureIntegrityEvent
from sovyx.voice._platform_metadata import current_platform_token
from sovyx.voice.pipeline._capture_integrity_emit import emit_capture_integrity_event


# Mirror tests/unit/voice/conftest.py: structlog must route through
# stdlib logging so caplog can observe emissions. The regression test
# directory has no shared conftest for this, so we install the fixture
# locally — session-scoped so the structlog chain is set up exactly
# once for the file.
@pytest.fixture(scope="module", autouse=True)
def _h2_structlog_stdlib_routing() -> Generator[None, None, None]:
    setup_logging(LoggingConfig(level="DEBUG", console_format="json", log_file=None))
    yield


_WRAPPER_LOGGER = "sovyx.voice.pipeline._capture_integrity_emit"

# Verbatim strategy list from operator-log L1067:
# 'voice.strategies=[linux.alsa_mixer_reset, linux.session_manager_escape,
# linux.pipewire_direct, linux.wireplumber_default_source,
# linux.alsa_capture_switch]'.
_L1067_STRATEGIES = [
    "linux.alsa_mixer_reset",
    "linux.session_manager_escape",
    "linux.pipewire_direct",
    "linux.wireplumber_default_source",
    "linux.alsa_capture_switch",
]


def _events_of(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [
        r.msg
        for r in caplog.records
        if r.name == _WRAPPER_LOGGER and isinstance(r.msg, dict) and r.msg.get("event") == name
    ]


@pytest.fixture(autouse=True)
def _clear_platform_cache() -> None:
    current_platform_token.cache_clear()
    yield
    current_platform_token.cache_clear()


class TestH2ApoBypassEventMisrouteReplay:
    """F3 forensic replay of the v0.43.1 §H2 anchor sequence."""

    def test_l1065_l1067_neutral_emission_with_platform_metadata(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The L1065 + L1067 forensic anchor on Linux Mint — every
        cascade strategy was ``linux.*`` AND ``voice_clarity_active=False``
        — now resolves through the dual-emission wrapper and surfaces
        the operator's platform via ``voice.platform=linux`` +
        ``voice.bypass_family=alsa_capture_chain``.
        """
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        monkeypatch.setattr("sys.platform", "linux")
        current_platform_token.cache_clear()

        # L1065 — ``voice_apo_bypass_ineffective`` + paired legacy
        # ``audio.apo.bypassed verdict=failure``. The wrapper emits BOTH
        # logical events via 2 wrapper invocations (the bypass-coordinator
        # refactor at _bypass_coordinator_mixin.py:482-533 mirrors this).
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASS_INEFFECTIVE,
            "error",
            mind_id="jonny",
            strategies=_L1067_STRATEGIES,
            voice_clarity_active=False,
            attempts=5,
            verdicts=["applied_still_dead"] * 5,
            hint="(L1065 verbatim hint truncated for test brevity)",
        )
        emit_capture_integrity_event(
            CaptureIntegrityEvent.BYPASSED,
            "error",
            mind_id="jonny",
            strategies=_L1067_STRATEGIES,
            voice_clarity_active=False,
            verdict="failure",
            attempts=5,
            outcomes=["applied_still_dead"] * 5,
            quarantined=True,
        )

        # (a) Neutral event fires with platform metadata
        neutral_ineff = _events_of(caplog, "voice.capture_integrity.bypass_ineffective")
        assert len(neutral_ineff) == 1, (
            "Neutral voice.capture_integrity.bypass_ineffective MUST fire — "
            "this is the F3 forensic-replay assertion."
        )
        assert neutral_ineff[0]["voice.platform"] == "linux"
        assert neutral_ineff[0]["voice.bypass_family"] == "alsa_capture_chain"
        assert neutral_ineff[0]["voice.event_schema_version"] == "2.0.0"
        assert neutral_ineff[0]["voice.voice_clarity_active"] is False

        neutral_bypassed = _events_of(caplog, "voice.capture_integrity.bypassed")
        assert len(neutral_bypassed) == 1
        assert neutral_bypassed[0]["voice.platform"] == "linux"
        assert neutral_bypassed[0]["voice.bypass_family"] == "alsa_capture_chain"
        assert neutral_bypassed[0]["voice.verdict"] == "failure"
        assert neutral_bypassed[0]["voice.quarantined"] is True

        # (b) Legacy events continue firing per ADR-D14
        legacy_ineff = _events_of(caplog, "voice_apo_bypass_ineffective")
        assert len(legacy_ineff) == 1, (
            "Legacy voice_apo_bypass_ineffective MUST continue firing during "
            "the dual-emission window (ADR-D14)."
        )
        legacy_bypassed = _events_of(caplog, "audio.apo.bypassed")
        assert len(legacy_bypassed) == 1
        assert legacy_bypassed[0]["voice.verdict"] == "failure"

        # (c) voice_clarity_active=False on legacy event (matches operator
        # log L1067 ``voice.voice_clarity_active=False``)
        assert legacy_bypassed[0]["voice.voice_clarity_active"] is False
        assert legacy_ineff[0]["voice_clarity_active"] is False

        # (d) operator-side grep on neutral event yields platform without
        # inspecting the strategy list — proves the H2 triage-drift gap
        # is closed.
        platform_disambiguated_events = [
            evt
            for evt in caplog.records
            if isinstance(evt.msg, dict)
            and evt.msg.get("event", "").startswith("voice.capture_integrity.")
            and evt.msg.get("voice.platform") == "linux"
            and evt.msg.get("voice.bypass_family") == "alsa_capture_chain"
        ]
        assert len(platform_disambiguated_events) >= 2, (
            "Operator grep on `voice.platform=linux voice.bypass_family=alsa_capture_chain` "
            "MUST yield at least the bypass_ineffective + bypassed pair — this is the "
            "core H2 forensic-triage capability."
        )

    def test_legacy_twin_preserved_for_every_neutral_emission(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """ADR-D14 dual-emission discipline: every neutral event MUST
        have a legacy twin firing alongside until v0.51.0 STRICT.
        """
        caplog.set_level(logging.DEBUG, logger=_WRAPPER_LOGGER)
        for event in CaptureIntegrityEvent:
            caplog.clear()
            emit_capture_integrity_event(
                event,
                "error",
                mind_id="jonny",
                strategies=_L1067_STRATEGIES,
                voice_clarity_active=False,
                verdict="failure",
            )
            neutral_count = sum(
                1
                for r in caplog.records
                if isinstance(r.msg, dict) and r.msg.get("event") == str(event)
            )
            legacy_count = sum(
                1
                for r in caplog.records
                if isinstance(r.msg, dict) and r.msg.get("event") == LEGACY_TWIN_MAP[event]
            )
            assert neutral_count == 1, f"Neutral {event} MUST fire exactly once per wrapper call"
            assert legacy_count == 1, (
                f"Legacy {LEGACY_TWIN_MAP[event]} MUST fire alongside per ADR-D14"
            )
