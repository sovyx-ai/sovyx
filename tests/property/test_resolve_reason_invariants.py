"""Hypothesis property tests for Mission H3 verdict→reason resolvers.

Mission anchor:
``docs-internal/missions/MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md``
§T1.6 §10.3.

Invariants verified across the entire IntegrityVerdict + Diagnosis member
spaces, by sampling Hypothesis strategies on the StrEnum members.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.voice.health._quarantine_reasons import (
    LEGACY_TWIN_MAP_REASONS,
    QuarantineReason,
    is_apo_class_reason,
    is_recheck_eligible,
    resolve_reason_from_diagnosis,
    resolve_reason_from_verdict,
)
from sovyx.voice.health.contract import Diagnosis, IntegrityVerdict

_VALID_VERDICTS = (
    IntegrityVerdict.APO_DEGRADED,
    IntegrityVerdict.VAD_FRONTEND_DEAD,
    IntegrityVerdict.FORMAT_MISMATCH,
    IntegrityVerdict.DRIVER_SILENT,
)
_REJECTED_VERDICTS = (
    IntegrityVerdict.HEALTHY,
    IntegrityVerdict.VAD_MUTE,
    IntegrityVerdict.INCONCLUSIVE,
)

# Diagnosis members that produce a valid QuarantineReason.
_VALID_DIAGNOSES = (
    Diagnosis.NO_SIGNAL,
    Diagnosis.STREAM_OPEN_TIMEOUT,
    Diagnosis.HEARTBEAT_TIMEOUT,
    Diagnosis.KERNEL_INVALIDATED,
    Diagnosis.APO_DEGRADED,
    Diagnosis.MIXER_SATURATED,
    Diagnosis.FORMAT_MISMATCH,
    Diagnosis.INVALID_SAMPLE_RATE_NO_AUTO_CONVERT,
    Diagnosis.INSUFFICIENT_BUFFER_SIZE,
)


class TestResolverInvariants:
    @given(verdict=st.sampled_from(_VALID_VERDICTS))
    @settings(max_examples=20)
    def test_verdict_resolver_returns_quarantine_reason(self, verdict: IntegrityVerdict) -> None:
        result = resolve_reason_from_verdict(verdict)
        assert isinstance(result, QuarantineReason)

    @given(verdict=st.sampled_from(_REJECTED_VERDICTS))
    @settings(max_examples=20)
    def test_verdict_resolver_rejects_benign(self, verdict: IntegrityVerdict) -> None:
        with pytest.raises(ValueError):
            resolve_reason_from_verdict(verdict)

    @given(verdict=st.sampled_from(_VALID_VERDICTS))
    @settings(max_examples=20)
    def test_verdict_resolver_deterministic(self, verdict: IntegrityVerdict) -> None:
        a = resolve_reason_from_verdict(verdict)
        b = resolve_reason_from_verdict(verdict)
        assert a is b

    @given(diagnosis=st.sampled_from(_VALID_DIAGNOSES))
    @settings(max_examples=30)
    def test_diagnosis_resolver_returns_quarantine_reason(self, diagnosis: Diagnosis) -> None:
        result = resolve_reason_from_diagnosis(diagnosis)
        assert isinstance(result, QuarantineReason)
        # The returned reason MUST be one of the terminal-quarantine
        # taxonomy members (no diagnosis maps to WATCHDOG_RECHECK /
        # UNCLASSIFIED via this resolver).
        assert result in {
            QuarantineReason.APO_DEGRADED,
            QuarantineReason.VAD_FRONTEND_DEAD,
            QuarantineReason.FORMAT_MISMATCH,
            QuarantineReason.DRIVER_SILENT,
            QuarantineReason.CAPTURE_DEAD,
            QuarantineReason.KERNEL_INVALIDATED,
        }

    @given(reason=st.sampled_from(list(QuarantineReason)))
    @settings(max_examples=16)
    def test_classifier_partition(self, reason: QuarantineReason) -> None:
        """APO-class ∩ recheck-ineligible = {vad_frontend_dead, format_mismatch}."""
        apo = is_apo_class_reason(reason.value)
        eligible = is_recheck_eligible(reason.value)
        # Mutual-exclusion check on the known partition.
        if reason in (QuarantineReason.VAD_FRONTEND_DEAD, QuarantineReason.FORMAT_MISMATCH):
            assert apo is True and eligible is False
        elif reason is QuarantineReason.CAPTURE_DEAD:
            assert apo is False and eligible is False
        elif reason is QuarantineReason.APO_DEGRADED:
            assert apo is True and eligible is True
        else:
            assert apo is False and eligible is True

    @given(reason=st.sampled_from(list(QuarantineReason)))
    @settings(max_examples=16)
    def test_legacy_twin_present_for_every_member(self, reason: QuarantineReason) -> None:
        assert reason in LEGACY_TWIN_MAP_REASONS
        assert LEGACY_TWIN_MAP_REASONS[reason] == reason.value

    @given(reason=st.sampled_from(list(QuarantineReason)))
    @settings(max_examples=8)
    def test_str_round_trip_via_value(self, reason: QuarantineReason) -> None:
        """A reason.value string round-trips through ``str()``."""
        as_str = str(reason)
        assert as_str == reason.value
