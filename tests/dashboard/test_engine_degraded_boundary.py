"""Boundary round-trip tests for ``EngineDegradedResponse``.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§T1.6 + §T1.13.

Quality Gate 8 (Mission C2 §T4.1 static checker) requires every
``Model.model_validate(helper_dict)`` call at a route boundary to have
a paired round-trip test that exercises the producer's actual
in-memory shape. The composite ``/api/engine/degraded`` endpoint is
populated from :class:`sovyx.engine._degraded_store.EngineDegradedStore`
which now has three producer sites (Phase 1 §T1.2 / §T1.3 / §T1.4); this
file pins the contract for the empty case + each axis solo + the
3-axis composite + the future-additive case.

Forward-additive (``model_config = {"extra": "allow"}``) is preserved
— a Phase 3 ack field MUST NOT break the round-trip.
"""

from __future__ import annotations

from sovyx.dashboard.routes.engine_degraded import (
    EngineDegradedResponse,
    _compute_composite_severity,
)
from tests.dashboard._boundary_helpers import assert_boundary_accepts


def _empty_payload() -> dict[str, object]:
    return {
        "axes": [],
        "composite_severity": None,
        "composite_axis_count": 0,
        "ack": {"acked": False},
    }


def _voice_axis() -> dict[str, object]:
    return {
        "axis": "voice",
        "reason": "failover_ladder_exhausted",
        "severity": "error",
        "title_token": "degraded.voice.ladderExhausted.title",
        "body_token": "degraded.voice.ladderExhausted.body",
        "action_chips": [
            {
                "label_token": "degraded.voice.ladderExhausted.viewHistory",
                "action": "navigate",
                "target": "/voice/health",
                "style": "primary",
            },
        ],
        "metadata": {
            "candidates_unreachable": ["razer-usb", "pipewire-default"],
            "candidates_tried": 2,
            "ladder_id": "abc123def456",
        },
        "first_observed_monotonic": 1.0,
        "last_observed_monotonic": 1.5,
        "occurrence_count": 1,
    }


def _llm_axis() -> dict[str, object]:
    return {
        "axis": "llm",
        "reason": "no_llm_provider",
        "severity": "error",
        "title_token": "degraded.llm.noProvider.title",
        "body_token": "degraded.llm.noProvider.body",
        "action_chips": [
            {
                "label_token": "degraded.llm.noProvider.installOllama",
                "action": "external_link",
                "target": "https://ollama.ai",
                "style": "primary",
            },
        ],
        "metadata": {
            "checked_keys": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
            "ollama_available": False,
        },
        "first_observed_monotonic": 0.1,
        "last_observed_monotonic": 0.1,
        "occurrence_count": 1,
    }


def _stt_axis() -> dict[str, object]:
    return {
        "axis": "stt",
        "reason": "stt_language_coerced",
        "severity": "warn",
        "title_token": "degraded.stt.languageCoerced.title",
        "body_token": "degraded.stt.languageCoerced.body",
        "action_chips": [
            {
                "label_token": "degraded.stt.languageCoerced.switchToEnglish",
                "action": "navigate",
                "target": "/settings/voice",
                "style": "default",
            },
        ],
        "metadata": {
            "requested_language": "pt",
            "coerced_language": "en",
        },
        "first_observed_monotonic": 0.5,
        "last_observed_monotonic": 0.5,
        "occurrence_count": 1,
    }


class TestEngineDegradedResponseBoundary:
    """``/api/engine/degraded`` accepts every realistic producer shape."""

    def test_empty_payload_round_trips(self) -> None:
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=_empty_payload,
            field_assertions={
                "composite_severity": None,
                "composite_axis_count": 0,
            },
        )
        assert response.axes == []
        assert response.ack.acked is False

    def test_single_voice_axis_warn_round_trips(self) -> None:
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [_voice_axis()],
                "composite_severity": "warn",
                "composite_axis_count": 1,
                "ack": {"acked": False},
            },
            field_assertions={
                "composite_severity": "warn",
                "composite_axis_count": 1,
            },
        )
        assert response.axes[0].axis == "voice"
        assert response.axes[0].action_chips[0].action == "navigate"

    def test_two_axis_error_round_trips(self) -> None:
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [_voice_axis(), _llm_axis()],
                "composite_severity": "error",
                "composite_axis_count": 2,
                "ack": {"acked": False},
            },
            field_assertions={
                "composite_severity": "error",
                "composite_axis_count": 2,
            },
        )
        assert {a.axis for a in response.axes} == {"voice", "llm"}

    def test_three_axis_critical_replays_operator_session(self) -> None:
        """Mission C4 §T1.13 — the canonical L374 + L858 + L1063
        operator-session composite. Severity escalates to critical."""
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [_voice_axis(), _llm_axis(), _stt_axis()],
                "composite_severity": "critical",
                "composite_axis_count": 3,
                "ack": {"acked": False},
            },
            field_assertions={
                "composite_severity": "critical",
                "composite_axis_count": 3,
            },
        )
        assert {a.axis for a in response.axes} == {"voice", "llm", "stt"}

    def test_ack_state_populated_round_trips(self) -> None:
        """Phase 3 ack state shape MUST round-trip cleanly even though
        Phase 1 doesn't write to it. Forward-additive contract."""
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [_voice_axis()],
                "composite_severity": "warn",
                "composite_axis_count": 1,
                "ack": {
                    "acked": True,
                    "acked_at_ts": 1700000000,
                    "ttl_sec": 3600,
                    "ttl_remaining_sec": 3540,
                    "operator_id": "op-hash-123",
                },
            },
        )
        assert response.ack.acked is True
        assert response.ack.ttl_remaining_sec == 3540

    def test_future_axis_passes_through(self) -> None:
        """Mission C4 §16 — future axes (brain, bridges, plugin) extend
        the payload without a schema migration thanks to extra-allow."""
        future_axis = {
            "axis": "brain",
            "reason": "embedding_model_unavailable",
            "severity": "warn",
            "title_token": "degraded.brain.embedding.title",
            "body_token": "degraded.brain.embedding.body",
            "action_chips": [],
            "metadata": {},
            "first_observed_monotonic": 0.0,
            "last_observed_monotonic": 0.0,
            "occurrence_count": 1,
            "future_extra_field": "tolerated",
        }
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [future_axis],
                "composite_severity": "warn",
                "composite_axis_count": 1,
                "ack": {"acked": False},
            },
        )
        assert response.axes[0].axis == "brain"

    def test_extra_top_level_field_passes_through(self) -> None:
        """Phase 2 may add governor counters at the top level. Forward-
        additive."""
        assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [],
                "composite_severity": None,
                "composite_axis_count": 0,
                "ack": {"acked": False},
                "phase2_governor_state": {
                    "auto_restart_count": 0,
                    "max_retries": 3,
                },
            },
        )

    def test_c5_dashboard_axis_bundle_partial_round_trips(self) -> None:
        """Mission C5 §T2.3 — the new ``axis="dashboard"`` entry with
        ``reason="bundle_partial"`` round-trips through the existing
        forward-additive schema without a migration. Proves the C4
        contract holds for the 4th axis-consumer.
        """
        dashboard_axis = {
            "axis": "dashboard",
            "reason": "bundle_partial",
            "severity": "error",
            "title_token": "degraded.dashboard.bundle_partial.title",
            "body_token": "degraded.dashboard.bundle_partial.partial.body",
            "action_chips": [
                {
                    "label_token": "degraded.dashboard.reinstall",
                    "action": "external_link",
                    "target": "https://sovyx.dev/docs/install/troubleshooting#reinstall",
                    "style": "primary",
                },
                {
                    "label_token": "degraded.dashboard.runDoctor",
                    "action": "external_link",
                    "target": "https://sovyx.dev/docs/cli/doctor#dashboard",
                    "style": "default",
                },
            ],
            "metadata": {
                "verdict": "partial",
                "missing_count": 3,
                "missing_sample": [
                    "assets/dashboard-BLNxX04a.js",
                    "assets/api-CmBjhza2.js",
                    "assets/index-DIHUuQiC.js",
                ],
                "static_dir": "/home/op/.local/share/pipx/venvs/sovyx/lib/python3.12/site-packages/sovyx/dashboard/static",
                "scan_duration_ms": 4.213,
            },
            "first_observed_monotonic": 1.5,
            "last_observed_monotonic": 1.5,
            "occurrence_count": 1,
        }
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [dashboard_axis],
                "composite_severity": "error",
                "composite_axis_count": 1,
                "ack": {"acked": False},
            },
        )
        assert response.axes[0].axis == "dashboard"
        assert response.axes[0].reason == "bundle_partial"
        assert response.axes[0].severity == "error"
        assert response.axes[0].metadata["verdict"] == "partial"

    def test_c5_dashboard_axis_bundle_missing_round_trips(self) -> None:
        """Mission C5 §T2.3 — ``reason="bundle_missing"`` with the
        full critical-severity treatment + verdict-discriminated body
        token (the same reason covers
        INDEX_HTML_MISSING / STATIC_DIR_MISSING / LEGACY_INDEX_HTML_NO_ASSETS
        per the spec — the verdict carries on metadata).
        """
        dashboard_axis = {
            "axis": "dashboard",
            "reason": "bundle_missing",
            "severity": "critical",
            "title_token": "degraded.dashboard.bundle_missing.title",
            "body_token": "degraded.dashboard.bundle_missing.static_dir_missing.body",
            "action_chips": [
                {
                    "label_token": "degraded.dashboard.reinstall",
                    "action": "external_link",
                    "target": "https://sovyx.dev/docs/install/troubleshooting#reinstall",
                    "style": "primary",
                },
            ],
            "metadata": {
                "verdict": "static_dir_missing",
                "missing_count": 0,
                "missing_sample": [],
                "static_dir": "/nonexistent/static",
                "scan_duration_ms": 0.05,
            },
            "first_observed_monotonic": 2.0,
            "last_observed_monotonic": 2.0,
            "occurrence_count": 1,
        }
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [dashboard_axis],
                "composite_severity": "critical",
                "composite_axis_count": 1,
                "ack": {"acked": False},
            },
        )
        assert response.axes[0].severity == "critical"
        assert response.axes[0].metadata["verdict"] == "static_dir_missing"

    def test_c5_dashboard_axis_compounds_with_voice_axis(self) -> None:
        """Mission C5 §1.4 cross-coupling — the dashboard axis renders
        alongside any in-flight voice/llm/stt axes. Composite severity
        escalates with distinct axis count (2 = error).
        """
        voice_axis = {
            "axis": "voice",
            "reason": "failover_ladder_exhausted",
            "severity": "error",
            "title_token": "degraded.voice.failoverExhausted.title",
            "body_token": "degraded.voice.failoverExhausted.body",
            "action_chips": [],
            "metadata": {},
            "first_observed_monotonic": 1.0,
            "last_observed_monotonic": 1.0,
            "occurrence_count": 1,
        }
        dashboard_axis = {
            "axis": "dashboard",
            "reason": "bundle_partial",
            "severity": "error",
            "title_token": "degraded.dashboard.bundle_partial.title",
            "body_token": "degraded.dashboard.bundle_partial.partial.body",
            "action_chips": [],
            "metadata": {"verdict": "partial", "missing_count": 1},
            "first_observed_monotonic": 1.2,
            "last_observed_monotonic": 1.2,
            "occurrence_count": 1,
        }
        response = assert_boundary_accepts(
            EngineDegradedResponse,
            helper_factory=lambda: {
                "axes": [voice_axis, dashboard_axis],
                "composite_severity": "error",
                "composite_axis_count": 2,
                "ack": {"acked": False},
            },
        )
        axes_by_axis = {axis.axis for axis in response.axes}
        assert axes_by_axis == {"voice", "dashboard"}
        assert response.composite_axis_count == 2


class TestComputeCompositeSeverity:
    """Mission C4 §T1.6 — ADR-D6 severity escalation invariants."""

    def test_zero_axes_none(self) -> None:
        assert _compute_composite_severity(0) is None

    def test_zero_axes_negative_defensive(self) -> None:
        # Defensive — caller should never pass negative, but the
        # implementation MUST short-circuit cleanly.
        assert _compute_composite_severity(-1) is None

    def test_one_axis_warn(self) -> None:
        assert _compute_composite_severity(1) == "warn"

    def test_two_axes_error(self) -> None:
        assert _compute_composite_severity(2) == "error"

    def test_three_axes_critical(self) -> None:
        assert _compute_composite_severity(3) == "critical"

    def test_many_axes_critical(self) -> None:
        assert _compute_composite_severity(8) == "critical"
