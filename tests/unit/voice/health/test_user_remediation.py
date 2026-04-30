"""Tests for the diagnosis → user-facing remediation map (Phase 6 / T6.12).

The map is the single source of truth for both:

* Cascade emission of ``voice_cascade_user_actionable``.
* ``GET /api/voice/service-health`` ``user_remediation`` field.

Tests pin the wire-form contract: each known diagnosis returns a
non-empty hint string; unknown / non-actionable diagnoses return
``None``; the homogeneous-histogram helper returns the tuple ONLY
when histogram has exactly one key AND that key is in the map.
"""

from __future__ import annotations

import pytest

from sovyx.voice.health._user_remediation import (
    diagnosis_user_remediation,
    homogeneous_diagnosis_remediation,
)
from sovyx.voice.health.contract import Diagnosis


class TestDiagnosisUserRemediation:
    @pytest.mark.parametrize(
        "diagnosis_value",
        [
            "device_busy",
            "permission_denied",
            "muted",
            "no_signal",
            "low_signal",
            "apo_degraded",
            "vad_insensitive",
            "format_mismatch",
            "driver_error",
            "kernel_invalidated",
        ],
    )
    def test_known_diagnoses_have_remediation(self, diagnosis_value: str) -> None:
        hint = diagnosis_user_remediation(diagnosis_value)
        assert hint is not None
        assert isinstance(hint, str)
        assert len(hint) > 20, f"hint too short to be actionable: {hint!r}"

    def test_healthy_returns_none(self) -> None:
        assert diagnosis_user_remediation("healthy") is None

    def test_unknown_returns_none(self) -> None:
        assert diagnosis_user_remediation("unknown") is None

    def test_mixer_family_returns_none(self) -> None:
        # L2.5 mixer-sanity diagnoses are internally auto-healed —
        # surfacing a user hint before sovyx finishes its retry would
        # be premature. None for the whole family.
        for diagnosis_value in (
            "mixer_zeroed",
            "mixer_saturated",
            "mixer_unknown_pattern",
            "mixer_customized",
        ):
            assert diagnosis_user_remediation(diagnosis_value) is None

    def test_typo_returns_none(self) -> None:
        # Defensive — a caller passing a typo / future-extension value
        # never crashes; it just gets None.
        assert diagnosis_user_remediation("device_buzy") is None
        assert diagnosis_user_remediation("") is None

    def test_each_active_diagnosis_either_has_hint_or_is_documented_none(self) -> None:
        # Regression guard — if a NEW Diagnosis enum value lands but
        # the ``_REMEDIATION_BY_DIAGNOSIS`` map isn't updated, this
        # test surfaces the gap. Document non-actionable values
        # explicitly here so the test fails LOUDLY instead of silently
        # treating new values as no-op.
        documented_no_hint = {
            "healthy",
            "unknown",
            "mixer_zeroed",
            "mixer_saturated",
            "mixer_unknown_pattern",
            "mixer_customized",
        }
        for diagnosis in Diagnosis:
            value = diagnosis.value
            hint = diagnosis_user_remediation(value)
            if hint is None:
                assert value in documented_no_hint, (
                    f"Diagnosis.{diagnosis.name}={value!r} has no remediation "
                    "hint and is NOT documented as no-actionable. Add it to "
                    "_REMEDIATION_BY_DIAGNOSIS or to the documented_no_hint "
                    "set in this test."
                )
            else:
                assert isinstance(hint, str)
                assert len(hint) > 20


class TestHomogeneousDiagnosisRemediation:
    def test_single_known_diagnosis_returns_tuple(self) -> None:
        result = homogeneous_diagnosis_remediation({"device_busy": 5})
        assert result is not None
        diagnosis, remediation = result
        assert diagnosis == "device_busy"
        assert "Discord" in remediation or "exclusive access" in remediation

    def test_single_unknown_diagnosis_returns_none(self) -> None:
        # Histogram is homogeneous (one key) but the key has no
        # remediation entry → None.
        assert homogeneous_diagnosis_remediation({"healthy": 8}) is None
        assert homogeneous_diagnosis_remediation({"mixer_customized": 3}) is None

    def test_empty_histogram_returns_none(self) -> None:
        assert homogeneous_diagnosis_remediation({}) is None

    def test_multiple_keys_returns_none(self) -> None:
        # Heterogeneous failure — too ambiguous to route a user-facing
        # hint. Operators get the histogram via T6.11; the user gets
        # nothing actionable.
        assert (
            homogeneous_diagnosis_remediation(
                {"device_busy": 3, "apo_degraded": 2},
            )
            is None
        )

    def test_zero_count_still_homogeneous_if_in_map(self) -> None:
        # Edge case — histogram with a single key whose count is 0
        # still has len(histogram) == 1. The helper doesn't filter on
        # count; that's the cascade's job. Document the behaviour.
        result = homogeneous_diagnosis_remediation({"device_busy": 0})
        assert result is not None
        assert result[0] == "device_busy"
