"""Boundary round-trip tests for ``VoiceStatusResponse.degraded``.

Mission C3 §T2.8 — Quality Gate 8 (Mission C2 §T4.1 static checker)
requires every ``Model.model_validate(helper_dict)`` call in
``routes/voice.py`` to have a paired round-trip test that exercises
the producer's actual prod shape.

This file pins the ``VoiceStatusDegraded`` contract: the new field
must round-trip through ``VoiceStatusResponse.model_validate(...)``
on every shape the producer at ``dashboard/voice_status.py`` actually
emits — both pre-ladder (default-False) and post-exhaustion (degraded
with reason + candidates_unreachable populated).

Forward-additive (``extra="allow"``) policy is preserved — a future
C4 banner-UX field MUST NOT break the round-trip.
"""

from __future__ import annotations

from sovyx.dashboard.routes.voice import VoiceStatusDegraded, VoiceStatusResponse
from tests.dashboard._boundary_helpers import assert_boundary_accepts


def _baseline_status_shape(**degraded_overrides: object) -> dict[str, object]:
    """Build the producer's runtime-bound dict shape with a custom
    ``degraded`` block. Mirrors
    ``dashboard/voice_status.get_voice_status``'s initial dict.
    """
    default_degraded: dict[str, object] = {
        "degraded": False,
        "reason": None,
        "candidates_tried": 0,
        "candidates_unreachable": [],
        "last_ladder_complete_monotonic": None,
    }
    default_degraded.update(degraded_overrides)
    return {
        "pipeline": {"running": False, "state": "not_configured"},
        "capture": {"running": False, "input_device": None},
        "degraded": default_degraded,
    }


class TestVoiceStatusDegradedBoundaryRoundTrip:
    """``VoiceStatusResponse.degraded`` accepts every producer shape."""

    def test_default_pre_ladder_shape_round_trips(self) -> None:
        """Pre-ladder state — default-False on every field."""
        response = assert_boundary_accepts(
            VoiceStatusResponse,
            helper_factory=_baseline_status_shape,
            field_assertions={
                "degraded.degraded": False,
                "degraded.reason": None,
                "degraded.candidates_tried": 0,
            },
        )
        assert response.degraded.candidates_unreachable == []
        assert response.degraded.last_ladder_complete_monotonic is None

    def test_exhausted_shape_round_trips(self) -> None:
        """Post-ladder-exhaustion — reason + unreachable list populated."""
        assert_boundary_accepts(
            VoiceStatusResponse,
            helper_factory=lambda: _baseline_status_shape(
                degraded=True,
                reason="failover_ladder_exhausted",
                candidates_tried=3,
                candidates_unreachable=[
                    "hd-audio-generic-sn6180-hw10",
                    "pipewire-virtual-source-idx7",
                    "os-default-idx8",
                ],
                last_ladder_complete_monotonic=12345.678,
            ),
            field_assertions={
                "degraded.degraded": True,
                "degraded.reason": "failover_ladder_exhausted",
                "degraded.candidates_tried": 3,
                "degraded.last_ladder_complete_monotonic": 12345.678,
            },
        )

    def test_missing_degraded_block_falls_back_to_factory(self) -> None:
        """When the helper omits ``degraded`` entirely (legacy producer
        path), the default factory yields a sane ``degraded=False``.
        """
        shape = {
            "pipeline": {"running": True, "state": "idle"},
            "capture": {"running": True, "input_device": 7},
        }
        response = VoiceStatusResponse.model_validate(shape)
        assert response.degraded.degraded is False
        assert response.degraded.reason is None
        assert response.degraded.candidates_unreachable == []

    def test_extra_keys_pass_through(self) -> None:
        """Forward-additive: an unknown key in ``degraded`` MUST NOT
        cause a ValidationError. Future C4 banner-UX fields
        (``last_error_class``, ``ack_at_monotonic``) land via this
        ``extra="allow"`` escape hatch.
        """
        assert_boundary_accepts(
            VoiceStatusResponse,
            helper_factory=lambda: _baseline_status_shape(
                future_c4_field="banner_dismissed",
                future_c4_timestamp=99999.0,
            ),
            field_assertions={
                "degraded.degraded": False,
            },
        )

    def test_c4_composite_axes_populated_round_trips(self) -> None:
        """Mission C4 §T1.5 — composite_axes + composite_severity
        round-trip cleanly when EngineDegradedStore has multiple axes.
        """
        response = assert_boundary_accepts(
            VoiceStatusResponse,
            helper_factory=lambda: _baseline_status_shape(
                composite_axes=["llm", "stt", "voice"],
                composite_severity="critical",
            ),
            field_assertions={
                "degraded.composite_severity": "critical",
            },
        )
        assert response.degraded.composite_axes == ["llm", "stt", "voice"]

    def test_c4_composite_severity_warn_round_trips(self) -> None:
        """Mission C4 §T1.5 — single-axis case yields severity=warn."""
        response = assert_boundary_accepts(
            VoiceStatusResponse,
            helper_factory=lambda: _baseline_status_shape(
                composite_axes=["voice"],
                composite_severity="warn",
            ),
        )
        assert response.degraded.composite_severity == "warn"
        assert response.degraded.composite_axes == ["voice"]

    def test_c4_composite_severity_none_when_empty(self) -> None:
        """Mission C4 §T1.5 — empty composite_axes yields severity=None."""
        response = assert_boundary_accepts(
            VoiceStatusResponse,
            helper_factory=lambda: _baseline_status_shape(
                composite_axes=[],
                composite_severity=None,
            ),
        )
        assert response.degraded.composite_axes == []
        assert response.degraded.composite_severity is None

    def test_c5_dashboard_axis_alone_round_trips(self) -> None:
        """Mission C5 §T2.4 — the new ``dashboard`` axis surfaces through
        ``VoiceStatusResponse.degraded.composite_axes`` cleanly. The
        producer's ``distinct_axes()`` is forward-additive (sorted
        ``set`` of all stored axes); locking in the new axis at the
        voice_status boundary guards against silent regression on the
        ``/api/voice/status`` consumer path.
        """
        response = assert_boundary_accepts(
            VoiceStatusResponse,
            helper_factory=lambda: _baseline_status_shape(
                composite_axes=["dashboard"],
                composite_severity="warn",
            ),
            field_assertions={
                "degraded.composite_severity": "warn",
            },
        )
        assert response.degraded.composite_axes == ["dashboard"]

    def test_c5_dashboard_axis_compounds_with_voice_at_error(self) -> None:
        """Mission C5 §1.4 — 2-axis composite escalates to ``error``
        per ADR-D6 (the dashboard axis combines mechanically with the
        voice ladder-exhausted axis at the operator's v0.43.1 signature
        SHA + the C5 boot-scan dashboard verdict)."""
        response = assert_boundary_accepts(
            VoiceStatusResponse,
            helper_factory=lambda: _baseline_status_shape(
                composite_axes=["dashboard", "voice"],
                composite_severity="error",
            ),
        )
        assert sorted(response.degraded.composite_axes) == ["dashboard", "voice"]
        assert response.degraded.composite_severity == "error"

    def test_c5_dashboard_axis_in_three_axis_critical(self) -> None:
        """Mission C5 §4.6 (ADR-D6) — 3-axis composite escalates to
        ``critical``. The dashboard axis MUST coexist with the C4
        voice/llm/stt cohort without breaking the severity tiering."""
        response = assert_boundary_accepts(
            VoiceStatusResponse,
            helper_factory=lambda: _baseline_status_shape(
                composite_axes=["dashboard", "llm", "voice"],
                composite_severity="critical",
            ),
        )
        assert sorted(response.degraded.composite_axes) == ["dashboard", "llm", "voice"]
        assert response.degraded.composite_severity == "critical"

    def test_c4_ack_fields_round_trip(self) -> None:
        """Mission C4 §T1.5 — Phase 3 ack fields are accepted (None
        until first operator ack)."""
        response = assert_boundary_accepts(
            VoiceStatusResponse,
            helper_factory=lambda: _baseline_status_shape(
                ack_at_monotonic=12345.6,
                ack_ttl_sec=3600,
                ack_operator_id="abc123",
                last_resurfaced_monotonic=99999.9,
            ),
        )
        assert response.degraded.ack_at_monotonic == 12345.6
        assert response.degraded.ack_ttl_sec == 3600
        assert response.degraded.ack_operator_id == "abc123"
        assert response.degraded.last_resurfaced_monotonic == 99999.9

    def test_c4_ack_fields_default_none(self) -> None:
        """Mission C4 §T1.5 — pre-Phase-3 producer omits ack_* fields;
        boundary accepts the omission cleanly."""
        response = assert_boundary_accepts(
            VoiceStatusResponse,
            helper_factory=_baseline_status_shape,
        )
        assert response.degraded.ack_at_monotonic is None
        assert response.degraded.ack_ttl_sec is None
        assert response.degraded.ack_operator_id is None
        assert response.degraded.last_resurfaced_monotonic is None


class TestVoiceStatusDegradedModelDirect:
    """Direct ``VoiceStatusDegraded.model_validate`` smoke."""

    def test_full_shape(self) -> None:
        instance = VoiceStatusDegraded.model_validate(
            {
                "degraded": True,
                "reason": "failover_ladder_exhausted",
                "candidates_tried": 2,
                "candidates_unreachable": ["dev_a", "dev_b"],
                "last_ladder_complete_monotonic": 1234.5,
            },
        )
        assert instance.degraded is True
        assert instance.reason == "failover_ladder_exhausted"
        assert instance.candidates_tried == 2
        assert instance.candidates_unreachable == ["dev_a", "dev_b"]
        assert instance.last_ladder_complete_monotonic == 1234.5

    def test_empty_shape_uses_defaults(self) -> None:
        instance = VoiceStatusDegraded.model_validate({})
        assert instance.degraded is False
        assert instance.reason is None
        assert instance.candidates_tried == 0
        assert instance.candidates_unreachable == []
        assert instance.last_ladder_complete_monotonic is None
