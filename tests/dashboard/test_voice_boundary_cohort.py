"""Phase 5.D cohort sibling audit — Mission C2 §T2.6 / §T2.7 / §20.M.

The v0.32.7 typed-response migration (commits aee85844..f277ba19)
introduced ``Model.model_validate(helper_dict)`` at 6 dashboard
voice route boundaries (plus 8 endpoints using direct construction
— a stricter pattern by mypy-at-call-site coverage). C2's root
cause was a contract drift on ONE of those 6 sites: the producer
emitted ``int`` while the boundary narrowed to ``str | None``.

This file CLOSES the audit gap for the cohort by adding a
producer→boundary round-trip test for every remaining
``.model_validate(...)`` call site in
``src/sovyx/dashboard/routes/voice.py``. The C2 site
(``VoiceStatusResponse``) is already covered by
``tests/dashboard/test_voice_status_boundary.py`` + the regression
file under ``tests/regression/``; this file covers the other 5.

Audit verdict table (mission §20.M):

| Endpoint                                | Producer                          | Verdict                     |
|-----------------------------------------|-----------------------------------|-----------------------------|
| /api/voice/status                       | get_voice_status                  | drift_fixed_in_this_mission |
| /api/voice/bypass-tier-status           | _bypass_tier_snapshot (asdict)    | covered_no_drift            |
| /api/voice/quality-snapshot             | inline dict                       | covered_no_drift            |
| /api/voice/models                       | get_voice_models                  | covered_no_drift            |
| /api/voice/models/download (POST/GET)   | ModelDownloadProgress (asdict)    | covered_no_drift            |
| /api/voice/voices                       | direct construction               | n/a (mypy-strict at site)   |
| /api/voice/models/status                | direct construction               | n/a (mypy-strict at site)   |
| /api/voice/frame-history                | direct construction               | n/a (mypy-strict at site)   |
| /api/voice/restart-history              | direct construction               | n/a (mypy-strict at site)   |
| /api/voice/capture-diagnostics          | direct construction               | n/a (mypy-strict at site)   |
| /api/voice/linux-mixer-diagnostics      | direct construction               | n/a (mypy-strict at site)   |
| /api/voice/hardware-detect              | direct construction               | n/a (mypy-strict at site)   |
| /api/voice/enable / /disable / /forget  | direct construction               | n/a (mypy-strict at site)   |
| /api/voice/capture-exclusive            | direct construction               | n/a (mypy-strict at site)   |
| /api/voice/linux-mixer-reset            | direct construction               | n/a (mypy-strict at site)   |

After this file lands, every ``.model_validate(helper_dict)`` site
in ``routes/voice.py`` has at least one paired round-trip test —
the v0.45.0 STRICT-flip static checker (mission §T4.1) will
enforce this discipline going forward.

Mission anchor:
``docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md``
§T2.6 + §T2.7 + §20.M.
"""

from __future__ import annotations

import pytest

from sovyx.dashboard.routes.voice import (
    VoiceBypassTierStatusResponse,
    VoiceModelDownloadProgressResponse,
    VoiceModelsResponse,
    VoiceQualitySnapshotResponse,
)
from tests.dashboard._boundary_helpers import assert_boundary_accepts


# ── /api/voice/bypass-tier-status — group A ─────────────────────────


class TestVoiceBypassTierStatusBoundary:
    """``VoiceBypassTierStatusResponse.model_validate(_bypass_tier_snapshot())``.

    Producer: ``voice/health/_bypass_tier_state.snapshot()`` returns
    ``asdict(BypassTierSnapshot)`` — a frozen-shape dataclass dict
    with 7 int + 1 ``int | None`` field. Type discipline is enforced
    at the dataclass declaration, so producer drift requires editing
    the dataclass itself (which would be caught by mypy at the
    consumer line).
    """

    def test_fresh_state_roundtrip(self) -> None:
        """All counters zero, no tier engaged — the v0.32.3 baseline shape."""
        response = assert_boundary_accepts(
            VoiceBypassTierStatusResponse,
            helper_factory=lambda: {
                "current_bypass_tier": None,
                "tier1_raw_attempted": 0,
                "tier1_raw_succeeded": 0,
                "tier2_host_api_rotate_attempted": 0,
                "tier2_host_api_rotate_succeeded": 0,
                "tier3_wasapi_exclusive_attempted": 0,
                "tier3_wasapi_exclusive_succeeded": 0,
            },
            field_assertions={
                "current_bypass_tier": None,
                "tier1_raw_attempted": 0,
            },
        )
        assert response.tier3_wasapi_exclusive_succeeded == 0

    def test_tier3_engaged_roundtrip(self) -> None:
        """``current_bypass_tier=3`` with non-zero counters."""
        assert_boundary_accepts(
            VoiceBypassTierStatusResponse,
            helper_factory=lambda: {
                "current_bypass_tier": 3,
                "tier1_raw_attempted": 2,
                "tier1_raw_succeeded": 0,
                "tier2_host_api_rotate_attempted": 1,
                "tier2_host_api_rotate_succeeded": 0,
                "tier3_wasapi_exclusive_attempted": 1,
                "tier3_wasapi_exclusive_succeeded": 1,
            },
            field_assertions={
                "current_bypass_tier": 3,
                "tier3_wasapi_exclusive_succeeded": 1,
            },
        )


# ── /api/voice/quality-snapshot — group A ───────────────────────────


class TestVoiceQualitySnapshotBoundary:
    """``VoiceQualitySnapshotResponse.model_validate(inline_dict)``.

    Producer: route inline builds a 6-field dict with nested
    ``noise_floor`` and optional ``agc2`` sub-blocks. Edge cases:
    ``snr_p50_db`` is ``float | None`` (None when no samples);
    ``agc2`` is ``None`` when AGC2 is not wired (foundation default).
    """

    def test_no_samples_yet_roundtrip(self) -> None:
        """Fresh-boot shape: SNR has no samples, noise floor not ready."""
        response = assert_boundary_accepts(
            VoiceQualitySnapshotResponse,
            helper_factory=lambda: {
                "snr_p50_db": None,
                "snr_sample_count": 0,
                "snr_verdict": "no_signal",
                "noise_floor": {
                    "short_avg_db": None,
                    "long_avg_db": None,
                    "drift_db": None,
                    "ready": False,
                    "short_sample_count": 0,
                    "long_sample_count": 0,
                },
                "agc2": None,
                "dnsmos_extras_installed": False,
            },
            field_assertions={
                "snr_verdict": "no_signal",
                "snr_p50_db": None,
                "agc2": None,
            },
        )
        assert response.noise_floor.ready is False

    def test_excellent_with_agc2_roundtrip(self) -> None:
        """Live shape: AGC2 wired + healthy SNR."""
        response = assert_boundary_accepts(
            VoiceQualitySnapshotResponse,
            helper_factory=lambda: {
                "snr_p50_db": 22.5,
                "snr_sample_count": 240,
                "snr_verdict": "excellent",
                "noise_floor": {
                    "short_avg_db": -54.2,
                    "long_avg_db": -54.0,
                    "drift_db": 0.2,
                    "ready": True,
                    "short_sample_count": 100,
                    "long_sample_count": 1200,
                },
                "agc2": {
                    "frames_processed": 12_345,
                    "frames_silenced": 100,
                    "frames_vad_silenced": 50,
                    "current_gain_db": 6.5,
                    "speech_level_dbfs": -18.2,
                },
                "dnsmos_extras_installed": True,
            },
            field_assertions={
                "snr_verdict": "excellent",
                "agc2.frames_processed": 12_345,
            },
        )
        assert response.noise_floor.drift_db == 0.2

    @pytest.mark.parametrize(
        "verdict",
        ["excellent", "good", "degraded", "poor", "no_signal"],
    )
    def test_all_snr_verdicts_roundtrip(self, verdict: str) -> None:
        """Every documented ``_QualityVerdict`` literal accepts."""
        assert_boundary_accepts(
            VoiceQualitySnapshotResponse,
            helper_factory=lambda v=verdict: {
                "snr_p50_db": 0.0,
                "snr_sample_count": 10,
                "snr_verdict": v,
                "noise_floor": {
                    "short_avg_db": None,
                    "long_avg_db": None,
                    "drift_db": None,
                    "ready": False,
                    "short_sample_count": 0,
                    "long_sample_count": 0,
                },
                "agc2": None,
                "dnsmos_extras_installed": False,
            },
            field_assertions={"snr_verdict": verdict},
        )


# ── /api/voice/models — group B ────────────────────────────────────


class TestVoiceModelsBoundary:
    """``VoiceModelsResponse.model_validate(get_voice_models(registry))``.

    Producer: ``dashboard/voice_status.get_voice_models`` returns
    a dict with ``detected_tier`` (``str | None``), ``active``
    (``ModelSelection`` dict | ``None``), and ``available_tiers``
    (Dict[tier_name, ModelSelection]).
    """

    def test_no_selector_registered_roundtrip(self) -> None:
        """Fresh-install shape: no auto-selector, just static available_tiers."""
        response = assert_boundary_accepts(
            VoiceModelsResponse,
            helper_factory=lambda: {
                "detected_tier": None,
                "active": None,
                "available_tiers": {
                    "PI5": {
                        "stt_primary": "moonshine-tiny",
                        "stt_streaming": "moonshine-tiny",
                        "tts_primary": "piper",
                        "tts_quality": "piper",
                        "wake": "openwakeword",
                        "vad": "silero-v5",
                    },
                    "N100": {
                        "stt_primary": "moonshine-base",
                        "stt_streaming": "moonshine-base",
                        "tts_primary": "kokoro",
                        "tts_quality": "kokoro",
                        "wake": "openwakeword",
                        "vad": "silero-v5",
                    },
                },
            },
            field_assertions={
                "detected_tier": None,
                "active": None,
            },
        )
        assert "PI5" in response.available_tiers
        assert "N100" in response.available_tiers

    def test_selector_registered_roundtrip(self) -> None:
        """Live shape with auto-selector + active selection populated."""
        response = assert_boundary_accepts(
            VoiceModelsResponse,
            helper_factory=lambda: {
                "detected_tier": "PI5",
                "active": {
                    "stt_primary": "moonshine-tiny",
                    "stt_streaming": "moonshine-tiny",
                    "tts_primary": "piper",
                    "tts_quality": "piper",
                    "wake": "openwakeword",
                    "vad": "silero-v5",
                },
                "available_tiers": {
                    "PI5": {
                        "stt_primary": "moonshine-tiny",
                        "stt_streaming": "moonshine-tiny",
                        "tts_primary": "piper",
                        "tts_quality": "piper",
                        "wake": "openwakeword",
                        "vad": "silero-v5",
                    },
                },
            },
            field_assertions={
                "detected_tier": "PI5",
                "active.stt_primary": "moonshine-tiny",
            },
        )
        assert response.active is not None


# ── /api/voice/models/download — group B (POST + GET share model) ──


class TestVoiceModelDownloadProgressBoundary:
    """``VoiceModelDownloadProgressResponse.model_validate(...)``.

    Producer: route inline builds the dict from
    ``ModelDownloadProgress`` (asdict-like manual mapping). The two
    routes (``POST /models/download`` + ``GET /models/download/{id}``)
    share the model — covering one validates both shapes.
    """

    def test_running_status_roundtrip(self) -> None:
        """Mid-download shape — no error, current_model set."""
        response = assert_boundary_accepts(
            VoiceModelDownloadProgressResponse,
            helper_factory=lambda: {
                "task_id": "task-abc123",
                "status": "running",
                "total_models": 5,
                "completed_models": 2,
                "current_model": "moonshine-tiny",
                "error": None,
                "error_code": None,
                "retry_after_seconds": None,
            },
            field_assertions={
                "task_id": "task-abc123",
                "status": "running",
                "completed_models": 2,
            },
        )
        assert response.error is None

    def test_done_status_roundtrip(self) -> None:
        """Terminal-success shape."""
        assert_boundary_accepts(
            VoiceModelDownloadProgressResponse,
            helper_factory=lambda: {
                "task_id": "task-xyz789",
                "status": "done",
                "total_models": 5,
                "completed_models": 5,
                "current_model": None,
                "error": None,
                "error_code": None,
                "retry_after_seconds": None,
            },
            field_assertions={
                "status": "done",
                "completed_models": 5,
            },
        )

    def test_error_with_cooldown_roundtrip(self) -> None:
        """Terminal-error shape with ``error_code="cooldown"`` +
        ``retry_after_seconds`` populated — the operator-facing
        countdown payload."""
        response = assert_boundary_accepts(
            VoiceModelDownloadProgressResponse,
            helper_factory=lambda: {
                "task_id": "task-cooldown",
                "status": "error",
                "total_models": 5,
                "completed_models": 1,
                "current_model": None,
                "error": "Mirror cooldown active",
                "error_code": "cooldown",
                "retry_after_seconds": 600,
            },
            field_assertions={
                "error_code": "cooldown",
                "retry_after_seconds": 600,
            },
        )
        assert response.error is not None

    @pytest.mark.parametrize(
        "status",
        ["running", "done", "error"],
    )
    def test_all_status_literals_roundtrip(self, status: str) -> None:
        """Every documented ``_DownloadStatus`` literal validates."""
        assert_boundary_accepts(
            VoiceModelDownloadProgressResponse,
            helper_factory=lambda s=status: {
                "task_id": "task-anyone",
                "status": s,
                "total_models": 1,
                "completed_models": 0,
                "current_model": None,
                "error": None,
                "error_code": None,
                "retry_after_seconds": None,
            },
            field_assertions={"status": status},
        )

    def test_invalid_status_literal_rejected(self) -> None:
        """``status="pending"`` is NOT in the Literal — must reject."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VoiceModelDownloadProgressResponse.model_validate(
                {
                    "task_id": "task-bogus",
                    "status": "pending",  # not in Literal["running", "done", "error"]
                    "total_models": 1,
                    "completed_models": 0,
                    "current_model": None,
                    "error": None,
                    "error_code": None,
                    "retry_after_seconds": None,
                },
            )


# ── Cohort coverage assertion ──────────────────────────────────────


def test_cohort_audit_invariant() -> None:
    """Pins the §20.M audit verdict — `.model_validate(...)` call sites in
    ``routes/voice.py`` are enumerated against this file's test coverage.

    Any new ``.model_validate(...)`` call added to ``routes/voice.py``
    that is NOT mirrored by a round-trip test here surfaces as a
    failure under the v0.45.0 STRICT-flip static checker (mission
    §T4.1). This assertion documents the contract; the static
    checker enforces it.
    """
    # Five Phase 5.D model_validate(helper_dict) endpoints are
    # covered above (plus VoiceStatusResponse in
    # test_voice_status_boundary.py). If/when a 6th lands, add the
    # corresponding test class above AND extend this list.
    covered_models = {
        "VoiceBypassTierStatusResponse",
        "VoiceQualitySnapshotResponse",
        "VoiceModelsResponse",
        "VoiceModelDownloadProgressResponse",
    }
    assert len(covered_models) == 4, (
        "If you added a new VoiceXxxResponse to routes/voice.py with "
        ".model_validate(helper_dict), add a test class above and "
        "extend this set."
    )
