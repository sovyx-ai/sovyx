"""Mission C1 commit 4a — plumbing tests.

Covers the additive extensions that land in commit 4a (no coordinator
dispatch change yet):

* :class:`QuarantineEntry.derived_reason` field + :meth:`EndpointQuarantine.add`
  preserve-on-recheck (§T1.7.a).
* :func:`is_apo_class_reason` / :func:`is_recheck_eligible` classifier
  helpers (§T1.7.b).
* :class:`BypassContext.pipeline_ref` field (§T1.4.a).
* :class:`RmsSummary` dataclass (§T1.2.a).
* :func:`_compute_rms_summary` module helper (§T1.2.a).

Coordinator integration + ladder + dispatch tests land in commit 4b.

Mission anchor:
    docs-internal/missions/MISSION-c1-vad-mute-reclassification-2026-05-14.md
    §T1.2.a + §T1.4.a + §T1.7.a + §T1.7.b + §20.M.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import numpy as np
import pytest

from sovyx.voice.health._quarantine import (
    EndpointQuarantine,
    QuarantineEntry,
    is_apo_class_reason,
    is_recheck_eligible,
)
from sovyx.voice.health.contract import (
    BypassContext,
    IntegrityResult,
    IntegrityVerdict,
    RmsSummary,
)


class TestQuarantineEntryDerivedReason:
    """T1.7.a — QuarantineEntry.derived_reason field + add() preserve-on-recheck."""

    def test_entry_has_derived_reason_field_default_empty(self) -> None:
        # Backward compat: legacy callers that don't set derived_reason
        # get empty string default. Pre-mission entries on disk would
        # also load with this default if persisted (they aren't today;
        # the store is per-boot per _quarantine.py:25-27).
        entry = QuarantineEntry(
            endpoint_guid="guid-1",
            device_friendly_name="Mic",
            device_interface_name="",
            host_api="WASAPI",
            added_at_monotonic=time.monotonic(),
            expires_at_monotonic=time.monotonic() + 3600.0,
            reason="apo_degraded",
        )
        assert entry.derived_reason == ""

    def test_entry_accepts_explicit_derived_reason(self) -> None:
        entry = QuarantineEntry(
            endpoint_guid="guid-1",
            device_friendly_name="Mic",
            device_interface_name="",
            host_api="WASAPI",
            added_at_monotonic=time.monotonic(),
            expires_at_monotonic=time.monotonic() + 3600.0,
            reason="apo_degraded",
            derived_reason="vad_frontend_dead",
        )
        assert entry.derived_reason == "vad_frontend_dead"

    def test_add_with_explicit_derived_reason(self) -> None:
        q = EndpointQuarantine(quarantine_s=10.0)
        entry = q.add(
            endpoint_guid="guid-1",
            reason="apo_degraded",
            derived_reason="vad_frontend_dead",
        )
        assert entry.reason == "apo_degraded"
        assert entry.derived_reason == "vad_frontend_dead"

    def test_add_with_default_none_inherits_from_prior_entry(self) -> None:
        # Mission C1 §T1.7.a guarantee: re-adding with derived_reason=None
        # (the default — matches the watchdog rechecker's contract)
        # preserves the prior entry's derived_reason.
        q = EndpointQuarantine(quarantine_s=10.0)
        q.add(
            endpoint_guid="guid-1",
            reason="apo_degraded",
            derived_reason="vad_frontend_dead",
        )
        # Lifecycle re-add (watchdog recheck pattern): different reason,
        # NO derived_reason kwarg → must inherit.
        new_entry = q.add(
            endpoint_guid="guid-1",
            reason="watchdog_recheck",
            # derived_reason omitted → default None → inherit
        )
        assert new_entry.reason == "watchdog_recheck"
        assert new_entry.derived_reason == "vad_frontend_dead", (
            "TTL re-extension MUST preserve the verdict-derived "
            "classification (Mission C1 §T1.7.a closes Agent-2 audit "
            "C-Risk-2)."
        )

    def test_add_with_default_none_no_prior_entry_yields_empty(self) -> None:
        # First-time add with derived_reason=None (default) on a guid
        # that has no prior entry → derived_reason stays empty string.
        # Callers that want a specific derived_reason MUST pass it
        # explicitly on the first add.
        q = EndpointQuarantine(quarantine_s=10.0)
        new_entry = q.add(endpoint_guid="guid-new", reason="apo_degraded")
        assert new_entry.derived_reason == ""

    def test_add_with_explicit_empty_derived_reason_clears_inherited(self) -> None:
        # Operators may explicitly clear the derived tag by passing "" —
        # only None means "inherit". This lets a future migration path
        # rewrite stale derived_reason values deterministically.
        q = EndpointQuarantine(quarantine_s=10.0)
        q.add(
            endpoint_guid="guid-1",
            reason="apo_degraded",
            derived_reason="vad_frontend_dead",
        )
        new_entry = q.add(
            endpoint_guid="guid-1",
            reason="watchdog_recheck",
            derived_reason="",
        )
        assert new_entry.derived_reason == "", (
            "Explicit empty derived_reason kwarg is treated as a "
            "deliberate clear, NOT as inherit-from-prior."
        )


class TestIsApoClassReason:
    """T1.7.b — APO recheck-loop classifier."""

    def test_apo_degraded_is_apo_class(self) -> None:
        assert is_apo_class_reason("apo_degraded") is True

    def test_vad_frontend_dead_is_apo_class(self) -> None:
        # Mission C1: vad_frontend_dead routes through the APO recheck
        # loop because the warm-probe re-evaluation pattern is the same.
        # (The kernel-rechecker excludes it separately via is_recheck_eligible.)
        assert is_apo_class_reason("vad_frontend_dead") is True

    def test_format_mismatch_is_apo_class(self) -> None:
        assert is_apo_class_reason("format_mismatch") is True

    def test_unrelated_reasons_not_apo_class(self) -> None:
        # Legacy lifecycle reasons that should NOT route to the APO loop.
        assert is_apo_class_reason("watchdog_recheck") is False
        assert is_apo_class_reason("probe_pinned") is False
        assert is_apo_class_reason("probe_store") is False
        assert is_apo_class_reason("probe_cascade") is False
        assert is_apo_class_reason("factory_integration") is False

    def test_empty_string_not_apo_class(self) -> None:
        # Pre-mission entries with no derived_reason default to empty;
        # the fallback to entry.reason is the caller's responsibility.
        assert is_apo_class_reason("") is False

    def test_unknown_reason_not_apo_class(self) -> None:
        # Defensive — an unrecognised string defaults to NOT apo-class.
        # This is safer than the opposite (a new lifecycle reason added
        # in a future version shouldn't accidentally pull entries into
        # the warm recheck loop).
        assert is_apo_class_reason("future_reason_v0_46") is False


class TestIsRecheckEligible:
    """T1.7.b — kernel rechecker eligibility filter."""

    def test_apo_degraded_is_eligible(self) -> None:
        # Recheck-eligible per the original APO recheck design — a Voice
        # Clarity APO can retire after a Windows Update, freeing the
        # endpoint without daemon restart.
        assert is_recheck_eligible("apo_degraded") is True

    def test_vad_frontend_dead_is_not_eligible(self) -> None:
        # Mission C1: vad_frontend_dead recovery happens BEFORE
        # quarantine via the reset ladder. A cold-probe re-attempt of
        # the quarantined endpoint will just re-detect the same dead
        # Silero session state.
        assert is_recheck_eligible("vad_frontend_dead") is False

    def test_format_mismatch_is_not_eligible(self) -> None:
        # Same logic — format mismatch recovery is engage_frame_normalizer
        # (T1.8), not a re-probe.
        assert is_recheck_eligible("format_mismatch") is False

    def test_legacy_reasons_eligible_by_default(self) -> None:
        # Pre-existing reasons — preserve pre-mission behavior.
        assert is_recheck_eligible("watchdog_recheck") is True
        assert is_recheck_eligible("probe_pinned") is True
        assert is_recheck_eligible("probe_store") is True

    def test_empty_string_eligible(self) -> None:
        # Pre-mission entries on disk would have empty derived_reason;
        # the rechecker MUST still process them (the kernel-invalidated
        # rechecker is the pre-mission consumer and runs across all
        # entries).
        assert is_recheck_eligible("") is True


class TestRmsSummary:
    """T1.2.a — RmsSummary dataclass contract."""

    def test_construct_with_values(self) -> None:
        summary = RmsSummary(rms_db=-30.0, samples_observed=16_000)
        assert summary.rms_db == pytest.approx(-30.0)
        assert summary.samples_observed == 16_000

    def test_empty_factory(self) -> None:
        # Canonical empty sentinel — RMS at floor, zero samples.
        empty = RmsSummary.empty()
        assert empty.samples_observed == 0
        # Mirrors capture_integrity.py _RMS_FLOOR_DB convention.
        assert empty.rms_db == pytest.approx(-120.0)

    def test_is_frozen(self) -> None:
        # frozen=True + slots=True per the dataclass decorator.
        summary = RmsSummary(rms_db=-30.0, samples_observed=16_000)
        with pytest.raises((AttributeError, TypeError)):  # type: ignore[arg-type]
            summary.rms_db = -25.0  # type: ignore[misc]


class TestComputeRmsSummary:
    """T1.2.a — _compute_rms_summary module helper."""

    def test_empty_buffer_yields_empty_summary(self) -> None:
        from sovyx.voice._capture_task import _compute_rms_summary

        empty = np.zeros(0, dtype=np.int16)
        result = _compute_rms_summary(empty)
        assert result.samples_observed == 0
        assert result.rms_db == pytest.approx(-120.0)

    def test_silent_buffer_returns_floor(self) -> None:
        # All-zero buffer → RMS floor per the canonical helper.
        from sovyx.voice._capture_task import _compute_rms_summary

        silent = np.zeros(16_000, dtype=np.int16)
        result = _compute_rms_summary(silent)
        assert result.samples_observed == 16_000
        assert result.rms_db == pytest.approx(-120.0)

    def test_loud_buffer_returns_finite_rms(self) -> None:
        # Full-scale sine wave → RMS near 0 dBFS (peak ±32767 / sqrt(2)).
        from sovyx.voice._capture_task import _compute_rms_summary

        samples = np.full(16_000, 16_384, dtype=np.int16)  # half-scale constant
        result = _compute_rms_summary(samples)
        assert result.samples_observed == 16_000
        # Half-scale constant: 16384/32768 = 0.5; dBFS = 20*log10(0.5) = -6.02
        assert -7.0 < result.rms_db < -5.0

    def test_none_buffer_yields_empty(self) -> None:
        # Defensive — None should not crash the helper.
        from sovyx.voice._capture_task import _compute_rms_summary

        result = _compute_rms_summary(None)
        assert result.samples_observed == 0


class TestBypassContextPipelineRef:
    """T1.4.a — BypassContext.pipeline_ref field."""

    def test_default_none(self) -> None:
        # Backward compat: legacy callers that don't pass pipeline_ref
        # get None default. v0.43 BypassContext construction sites are
        # unchanged at this point — pre-mission strategy iteration paths
        # never reference pipeline_ref.
        ctx = BypassContext(
            endpoint_guid="guid-1",
            endpoint_friendly_name="Mic",
            host_api_name="WASAPI",
            platform_key="linux",
            capture_task=object(),  # type: ignore[arg-type]
            probe_fn=lambda: _make_inconclusive_awaitable(),  # type: ignore[arg-type, return-value]
        )
        assert ctx.pipeline_ref is None

    def test_accepts_explicit_pipeline_ref(self) -> None:
        # Mission C1 T1.4.a — production factory will pass the live
        # VoicePipeline so the reset ladder can call .reset_vad() /
        # .swap_vad() on the correct instance (NOT the probe's VAD —
        # capture_integrity.py:185-189 cross-contamination guard).
        sentinel_pipeline = object()
        ctx = BypassContext(
            endpoint_guid="guid-1",
            endpoint_friendly_name="Mic",
            host_api_name="WASAPI",
            platform_key="linux",
            capture_task=object(),  # type: ignore[arg-type]
            probe_fn=lambda: _make_inconclusive_awaitable(),  # type: ignore[arg-type, return-value]
            pipeline_ref=sentinel_pipeline,
        )
        assert ctx.pipeline_ref is sentinel_pipeline


async def _make_inconclusive_awaitable() -> IntegrityResult:
    """Helper — minimal IntegrityResult-returning awaitable for BypassContext.probe_fn."""
    return IntegrityResult(
        verdict=IntegrityVerdict.INCONCLUSIVE,
        endpoint_guid="guid-1",
        rms_db=-120.0,
        vad_max_prob=0.0,
        spectral_flatness=0.0,
        spectral_rolloff_hz=0.0,
        duration_s=0.0,
        probed_at_utc=datetime.now(UTC),
        raw_frames=0,
    )
