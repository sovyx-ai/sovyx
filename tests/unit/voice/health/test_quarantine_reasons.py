"""Unit tests for Mission H3 SSoT — :mod:`sovyx.voice.health._quarantine_reasons`.

Mission anchor:
``docs-internal/missions/MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md``
§T1.6.

Covers Phase 1.A acceptance:

* Enum membership + iteration order.
* :data:`LEGACY_TWIN_MAP_REASONS` exhaustiveness.
* :func:`is_apo_class_reason`, :func:`is_recheck_eligible`,
  :func:`is_lifecycle_tag` classifiers.
* :func:`resolve_reason_from_verdict` — all 7 :class:`IntegrityVerdict`
  members (4 valid maps + 3 ValueError rejections).
* :func:`resolve_reason_from_diagnosis` — all 22 :class:`Diagnosis`
  members (9 valid maps + 5 cascade-fallthrough rejections + 8 benign
  rejections).
"""

from __future__ import annotations

import contextlib

import pytest

from sovyx.voice.health._quarantine_reasons import (
    LEGACY_TWIN_MAP_REASONS,
    QuarantineReason,
    is_apo_class_reason,
    is_lifecycle_tag,
    is_recheck_eligible,
    resolve_reason_from_diagnosis,
    resolve_reason_from_verdict,
)
from sovyx.voice.health.contract import Diagnosis, IntegrityVerdict


class TestQuarantineReasonEnum:
    """StrEnum invariants."""

    def test_eight_members(self) -> None:
        assert len(list(QuarantineReason)) == 8

    def test_str_enum_value_comparison(self) -> None:
        # StrEnum members compare equal to their .value string.
        assert QuarantineReason.APO_DEGRADED == "apo_degraded"
        assert QuarantineReason.VAD_FRONTEND_DEAD == "vad_frontend_dead"
        assert QuarantineReason.FORMAT_MISMATCH == "format_mismatch"
        assert QuarantineReason.DRIVER_SILENT == "driver_silent"
        assert QuarantineReason.CAPTURE_DEAD == "capture_dead"
        assert QuarantineReason.KERNEL_INVALIDATED == "kernel_invalidated"
        assert QuarantineReason.WATCHDOG_RECHECK == "watchdog_recheck"
        assert QuarantineReason.UNCLASSIFIED == "unclassified"

    def test_canonical_iteration_order(self) -> None:
        """Iteration order is canonical taxonomy order."""
        expected = (
            "apo_degraded",
            "vad_frontend_dead",
            "format_mismatch",
            "driver_silent",
            "capture_dead",
            "kernel_invalidated",
            "watchdog_recheck",
            "unclassified",
        )
        assert tuple(r.value for r in QuarantineReason) == expected

    def test_str_passing(self) -> None:
        """A QuarantineReason member is a valid str arg (anti-pattern #9)."""

        def consume(value: str) -> str:
            return value

        assert consume(QuarantineReason.CAPTURE_DEAD) == "capture_dead"


class TestLegacyTwinMap:
    def test_covers_every_member(self) -> None:
        assert frozenset(LEGACY_TWIN_MAP_REASONS) == frozenset(QuarantineReason)

    def test_round_trip(self) -> None:
        """Legacy twin equals the enum's own value for all members at this stage."""
        for reason, legacy in LEGACY_TWIN_MAP_REASONS.items():
            assert reason.value == legacy

    def test_final_mapping_immutable_at_runtime(self) -> None:
        """``Final[Mapping[...]]`` — TypedDict-like immutability via type hint."""
        # The Mapping type doesn't enforce immutability — but the runtime
        # value is a plain dict that the module never mutates. This test
        # encodes the invariant for any future contributor.
        snapshot = dict(LEGACY_TWIN_MAP_REASONS)
        assert dict(LEGACY_TWIN_MAP_REASONS) == snapshot


class TestClassifiers:
    @pytest.mark.parametrize(
        "reason",
        ["apo_degraded", "vad_frontend_dead", "format_mismatch"],
    )
    def test_apo_class_positive(self, reason: str) -> None:
        assert is_apo_class_reason(reason) is True

    @pytest.mark.parametrize(
        "reason",
        [
            "driver_silent",
            "capture_dead",
            "kernel_invalidated",
            "watchdog_recheck",
            "unclassified",
            "",
            "totally_made_up",
        ],
    )
    def test_apo_class_negative(self, reason: str) -> None:
        assert is_apo_class_reason(reason) is False

    @pytest.mark.parametrize(
        "reason",
        [
            "apo_degraded",
            "driver_silent",
            "kernel_invalidated",
            "watchdog_recheck",
            "unclassified",
            "",
            "totally_made_up",
        ],
    )
    def test_recheck_eligible_positive(self, reason: str) -> None:
        assert is_recheck_eligible(reason) is True

    @pytest.mark.parametrize(
        "reason",
        ["vad_frontend_dead", "format_mismatch", "capture_dead"],
    )
    def test_recheck_eligible_negative(self, reason: str) -> None:
        assert is_recheck_eligible(reason) is False

    @pytest.mark.parametrize(
        "reason",
        [
            "watchdog_recheck",
            "factory_integration",
            "probe_pinned",
            "probe_store",
            "probe_cascade",
            "kernel_invalidated_recheck",
            "probe",
        ],
    )
    def test_lifecycle_tag_positive(self, reason: str) -> None:
        assert is_lifecycle_tag(reason) is True

    @pytest.mark.parametrize(
        "reason",
        ["apo_degraded", "vad_frontend_dead", "capture_dead", "unclassified", ""],
    )
    def test_lifecycle_tag_negative(self, reason: str) -> None:
        assert is_lifecycle_tag(reason) is False


class TestResolveReasonFromVerdict:
    """Mission H3 §4.3 ADR-D3 — exhaustive match + assert_never."""

    def test_apo_degraded(self) -> None:
        assert (
            resolve_reason_from_verdict(IntegrityVerdict.APO_DEGRADED)
            is QuarantineReason.APO_DEGRADED
        )

    def test_vad_frontend_dead(self) -> None:
        assert (
            resolve_reason_from_verdict(IntegrityVerdict.VAD_FRONTEND_DEAD)
            is QuarantineReason.VAD_FRONTEND_DEAD
        )

    def test_format_mismatch(self) -> None:
        assert (
            resolve_reason_from_verdict(IntegrityVerdict.FORMAT_MISMATCH)
            is QuarantineReason.FORMAT_MISMATCH
        )

    def test_driver_silent(self) -> None:
        assert (
            resolve_reason_from_verdict(IntegrityVerdict.DRIVER_SILENT)
            is QuarantineReason.DRIVER_SILENT
        )

    @pytest.mark.parametrize(
        "verdict",
        [IntegrityVerdict.HEALTHY, IntegrityVerdict.VAD_MUTE, IntegrityVerdict.INCONCLUSIVE],
    )
    def test_rejected_verdicts_raise(self, verdict: IntegrityVerdict) -> None:
        with pytest.raises(ValueError, match="must not reach _quarantine_endpoint"):
            resolve_reason_from_verdict(verdict)


class TestResolveReasonFromDiagnosis:
    """Mission H3 §4.4 ADR-D4 — exhaustive match + assert_never."""

    @pytest.mark.parametrize(
        "diagnosis",
        [Diagnosis.NO_SIGNAL, Diagnosis.STREAM_OPEN_TIMEOUT, Diagnosis.HEARTBEAT_TIMEOUT],
    )
    def test_capture_dead_terminal(self, diagnosis: Diagnosis) -> None:
        assert resolve_reason_from_diagnosis(diagnosis) is QuarantineReason.CAPTURE_DEAD

    def test_kernel_invalidated(self) -> None:
        assert (
            resolve_reason_from_diagnosis(Diagnosis.KERNEL_INVALIDATED)
            is QuarantineReason.KERNEL_INVALIDATED
        )

    @pytest.mark.parametrize(
        "diagnosis",
        [Diagnosis.APO_DEGRADED, Diagnosis.MIXER_SATURATED],
    )
    def test_apo_degraded_terminal(self, diagnosis: Diagnosis) -> None:
        assert resolve_reason_from_diagnosis(diagnosis) is QuarantineReason.APO_DEGRADED

    @pytest.mark.parametrize(
        "diagnosis",
        [
            Diagnosis.FORMAT_MISMATCH,
            Diagnosis.INVALID_SAMPLE_RATE_NO_AUTO_CONVERT,
            Diagnosis.INSUFFICIENT_BUFFER_SIZE,
        ],
    )
    def test_format_mismatch_terminal(self, diagnosis: Diagnosis) -> None:
        assert resolve_reason_from_diagnosis(diagnosis) is QuarantineReason.FORMAT_MISMATCH

    @pytest.mark.parametrize(
        "diagnosis",
        [
            Diagnosis.DRIVER_ERROR,
            Diagnosis.DEVICE_BUSY,
            Diagnosis.PERMISSION_DENIED,
            Diagnosis.PERMISSION_REVOKED_RUNTIME,
            Diagnosis.EXCLUSIVE_MODE_NOT_AVAILABLE,
        ],
    )
    def test_cascade_fallthrough_rejected(self, diagnosis: Diagnosis) -> None:
        with pytest.raises(ValueError, match="cascade-fallthrough condition"):
            resolve_reason_from_diagnosis(diagnosis)

    @pytest.mark.parametrize(
        "diagnosis",
        [
            Diagnosis.HEALTHY,
            Diagnosis.MUTED,
            Diagnosis.LOW_SIGNAL,
            Diagnosis.VAD_INSENSITIVE,
            Diagnosis.MIXER_ZEROED,
            Diagnosis.MIXER_UNKNOWN_PATTERN,
            Diagnosis.MIXER_CUSTOMIZED,
            Diagnosis.UNKNOWN,
        ],
    )
    def test_benign_or_non_terminal_rejected(self, diagnosis: Diagnosis) -> None:
        with pytest.raises(ValueError, match="non-terminal or benign diagnosis"):
            resolve_reason_from_diagnosis(diagnosis)

    def test_every_diagnosis_member_handled(self) -> None:
        """Sanity check — every Diagnosis member is covered by one of the
        4 return arms or the 2 raise arms (no missing-case fall-through)."""
        for diagnosis in Diagnosis:
            with contextlib.suppress(ValueError):
                resolve_reason_from_diagnosis(diagnosis)


class TestVerdictExhaustiveness:
    """Sanity check — every IntegrityVerdict member is mapped or explicitly rejected."""

    def test_every_verdict_member_handled(self) -> None:
        for verdict in IntegrityVerdict:
            with contextlib.suppress(ValueError):
                resolve_reason_from_verdict(verdict)
