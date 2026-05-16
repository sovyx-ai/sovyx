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
