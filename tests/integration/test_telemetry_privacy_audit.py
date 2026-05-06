"""Privacy CI gate for ``voice.calibration.*`` + ``voice.diagnostics.*`` telemetry.

Phase 0 of mission ``MISSION-voice-calibration-extreme-audit-2026-05-06.md``
(§4 P0.T7) closes the privacy contract: every emission site for these two
event prefixes MUST emit hashed identifiers (``mind_id_hash``,
``job_id_hash``, ``profile_id_hash``, ``cached_mind_id_hash``) and zero
filesystem paths in addition-only fields.

This test drives three end-to-end scenarios (slow-path DONE, FALLBACK,
CANCELLED) plus the direct persistence + KB cache emission sites, captures
every event via per-module logger replacement, and walks the aggregated
event list through two heuristics:

* **A — raw mind_id heuristic**: any string field longer than 16
  chars that contains non-hex chars (catches operator-set strings
  like ``"my-mind"`` / ``"jonny@example.com"`` / ``"550e8400-e29b-41d4-a716-446655440000"``).
* **B — filesystem path heuristic**: any string field starting with
  ``/``, ``\\``, ``C:``, ``D:``, ``E:`` (catches absolute paths that
  reveal operator-host layout).

Failure surface: the assertion fails with the exact
``(event_name, field_name, value)`` tuples for triage. Deprecated raw
fields (scheduled for removal in v0.30.29 per mission §4.4) are listed
in :data:`DEPRECATED_FIELDS` and exempted; that list shrinks to empty
when v0.30.29 ships.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.calibration import (
    ApplyResult,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    WizardOrchestrator,
)
from sovyx.voice.calibration import _kb_cache as kb_cache
from sovyx.voice.calibration import _persistence as persistence
from sovyx.voice.calibration import _wizard_orchestrator as wo
from sovyx.voice.calibration import _wizard_progress as wizard_progress
from sovyx.voice.calibration._persistence import (
    load_calibration_profile,
    save_calibration_profile,
)
from sovyx.voice.diagnostics import (
    AlertsSummary,
    DiagPrerequisiteError,
    DiagRunResult,
    HypothesisId,
    HypothesisVerdict,
    SchemaValidation,
    TriageResult,
)

# ────────────────────────────────────────────────────────────────────
# Privacy contract: known-deprecated fields and dynamic-text fields
# ────────────────────────────────────────────────────────────────────

DEPRECATED_FIELDS: frozenset[str] = frozenset(
    {
        # Removal scheduled for v0.30.29 per mission §4.4 backward-compat.
        "mind_id",
        "job_id",
        "cached_mind_id",
        "path",
    }
)

DYNAMIC_TEXT_FIELDS: frozenset[str] = frozenset(
    {
        # Operator-facing message strings (NOT identifiers); cardinality is
        # naturally bounded by the closed enum status / step / fallback_reason
        # mapping, but the human-readable strings themselves vary.
        "current_stage_message",
        "message",
        # Exception summary text — bounded by the exception types we raise,
        # but contains arbitrary content that triggers heuristic A.
        "error_summary",
        "reason",
        # Operator-facing CLI advice strings.
        "rationale",
        "value",
        # Fingerprint hash is 64-hex (SHA256 full); legitimately longer than 16.
        "fingerprint_hash",
    }
)

# Closed-enum fields whose values are bounded but may exceed 16 chars
# (e.g. ``"slow_path_calibrate"`` is a WizardStatus value). Per the
# audit brief §2 these are documented as "no action needed" because
# their cardinality is finite and operator-set strings cannot reach
# them. Listing them here keeps the heuristic strict on UNNAMED fields.
CLOSED_ENUM_FIELDS: frozenset[str] = frozenset(
    {
        "status",  # WizardStatus enum
        "step",  # probe/fast_path/slow_path/review/fallback
        "fallback_reason",  # probe_failed/diag_prerequisite_unmet/...
        "mode",  # lenient/strict
        "signature_status",  # accepted/missing/invalid
        "triage_winner_hid",  # HypothesisId enum (e.g. H10_LINUX_MIXER_ATTENUATED)
        "audio_stack",  # pipewire/pulseaudio/alsa
        "recommendation",  # fast_path/slow_path
        "rule_id",  # R10_mic_attenuated, etc. (bounded by RULE_SET_VERSION)
        "rollback_reason",  # operator_initiated, etc.
        "failure_reason",  # closed enum
        "trigger",  # cli/wizard
        "path",  # closed enum: fast/slow/fallback/unknown (NOT a filesystem path)
        "phrase",  # bounded prompt phrase set (P3+, future-proof exempt)
        "prompt_type",  # speak/silence
        "verdict",  # signing verdict closed enum (P4+)
        "system_vendor",  # hardware vendor string from DMI (bounded)
        "system_product",  # hardware product string from DMI (bounded)
    }
)

PATH_PREFIXES: tuple[str, ...] = ("/", "\\", "C:", "D:", "E:", "F:")


def _is_hex(value: str) -> bool:
    return all(c in "0123456789abcdefABCDEF" for c in value)


def _scan_kwargs_for_leaks(event_name: str, kwargs: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return a list of ``(event_name, field_name, value)`` leak tuples.

    Skips fields in :data:`DEPRECATED_FIELDS` (slated for removal) and
    :data:`DYNAMIC_TEXT_FIELDS` (legitimate exception/operator text).
    """
    leaks: list[tuple[str, str, str]] = []
    for field_name, raw_value in kwargs.items():
        if (
            field_name in DEPRECATED_FIELDS
            or field_name in DYNAMIC_TEXT_FIELDS
            or field_name in CLOSED_ENUM_FIELDS
        ):
            continue
        if not isinstance(raw_value, str):
            continue
        if any(raw_value.startswith(prefix) for prefix in PATH_PREFIXES):
            leaks.append((event_name, field_name, raw_value))
            continue
        if len(raw_value) > 16 and not _is_hex(raw_value):
            leaks.append((event_name, field_name, raw_value))
    return leaks


def _flatten_calls(loggers: list[MagicMock]) -> list[tuple[str, dict[str, Any]]]:
    """Collect ``(event_name, kwargs)`` from every recorded log call."""
    out: list[tuple[str, dict[str, Any]]] = []
    for mock in loggers:
        for method in ("info", "warning", "error", "debug", "exception"):
            for call in getattr(mock, method).call_args_list:
                if call.args:
                    out.append((str(call.args[0]), dict(call.kwargs)))
    return out


def _audited(events: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    """Filter to events under the privacy-audit scope."""
    return [
        (name, kw)
        for (name, kw) in events
        if name.startswith("voice.calibration.") or name.startswith("voice.diagnostics.")
    ]


# ────────────────────────────────────────────────────────────────────
# Fixtures: synthetic fingerprint / measurements / triage / profile.
# Re-uses the test_wizard_telemetry shape verbatim for parity with
# the wizard orchestrator tests.
# ────────────────────────────────────────────────────────────────────


def _fingerprint() -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-06T18:00:00Z",
        distro_id="linuxmint",
        distro_id_like="debian",
        kernel_release="6.8",
        kernel_major_minor="6.8",
        cpu_model="Intel",
        cpu_cores=12,
        ram_mb=16384,
        has_gpu=False,
        gpu_vram_mb=0,
        audio_stack="pipewire",
        pipewire_version="1.0.5",
        pulseaudio_version=None,
        alsa_lib_version="ALSA",
        codec_id="10ec:0257",
        driver_family="hda",
        system_vendor="Sony",
        system_product="VAIO",
        capture_card_count=1,
        capture_devices=("Mic",),
        apo_active=False,
        apo_name=None,
        hal_interceptors=(),
        pulse_modules_destructive=(),
    )


def _measurements() -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=1,
        captured_at_utc="2026-05-06T18:01:00Z",
        duration_s=600.0,
        rms_dbfs_per_capture=(),
        vad_speech_probability_max=0.0,
        vad_speech_probability_p99=0.0,
        noise_floor_dbfs_estimate=0.0,
        capture_callback_p99_ms=0.0,
        capture_jitter_ms=0.0,
        portaudio_latency_advertised_ms=0.0,
        mixer_card_index=0,
        mixer_capture_pct=5,
        mixer_boost_pct=0,
        mixer_internal_mic_boost_pct=0,
        mixer_attenuation_regime="attenuated",
        echo_correlation_db=None,
        triage_winner_hid="H10",
        triage_winner_confidence=0.95,
    )


def _triage() -> TriageResult:
    return TriageResult(
        schema_version=1,
        toolkit="linux",
        tarball_root=Path("/tmp/diag"),
        tool_name="sovyx-voice-diag",
        tool_version="4.3",
        host="t",
        captured_at_utc="2026-05-06T18:00:00Z",
        os_descriptor="linux",
        status="complete",
        exit_code="0",
        selftest_status="pass",
        steps={},
        skip_captures=False,
        schema_validation=SchemaValidation(
            ok=True, missing_required=(), missing_recommended=(), warnings=()
        ),
        alerts=AlertsSummary(error_count=0, warn_count=0, info_count=0, error_messages=()),
        hypotheses=(
            HypothesisVerdict(
                hid=HypothesisId.H10_LINUX_MIXER_ATTENUATED,
                title="x",
                confidence=0.95,
                evidence_for=(),
                evidence_against=(),
                recommended_action="sovyx doctor voice --fix --yes",
            ),
        ),
    )


# Deliberately PII-shaped operator-set mind_id: the privacy heuristic
# MUST surface this as a leak when present in raw form.
SUSPICIOUS_MIND_ID = "operator-jonny-vaio-test"


def _profile(*, mind_id: str = SUSPICIOUS_MIND_ID) -> CalibrationProfile:
    return CalibrationProfile(
        schema_version=1,
        profile_id="11111111-2222-3333-4444-555555555555",
        mind_id=mind_id,
        fingerprint=_fingerprint(),
        measurements=_measurements(),
        decisions=(
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value="sovyx doctor voice --fix --yes",
                rationale="r10",
                rule_id="R10_mic_attenuated",
                rule_version=1,
                confidence=CalibrationConfidence.HIGH,
            ),
        ),
        provenance=(),
        generated_by_engine_version="0.30.28",
        generated_by_rule_set_version=1,
        generated_at_utc="2026-05-06T18:02:00Z",
        signature=None,
    )


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────


class TestTelemetryPrivacyAudit:
    """Every voice.calibration.* / voice.diagnostics.* event passes the
    privacy heuristics (excluding documented deprecated fields)."""

    @pytest.mark.asyncio()
    async def test_slow_path_done_emits_no_raw_pii(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        profile_path = tmp_path / SUSPICIOUS_MIND_ID / "calibration.json"
        wo_logger = MagicMock()
        progress_logger = MagicMock()
        with (
            patch.object(wo, "logger", wo_logger),
            patch.object(wizard_progress, "logger", progress_logger),
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(
                wo,
                "run_full_diag",
                return_value=DiagRunResult(
                    tarball_path=Path("/tmp/x.tar.gz"),
                    duration_s=600.0,
                    exit_code=0,
                ),
            ),
            patch.object(wo, "triage_tarball", return_value=_triage()),
            patch.object(wo, "capture_measurements", return_value=_measurements()),
            patch.object(
                wo.CalibrationEngine,
                "evaluate",
                return_value=_profile(mind_id=SUSPICIOUS_MIND_ID),
            ),
            patch.object(
                wo.CalibrationApplier,
                "apply",
                return_value=ApplyResult(
                    profile_path=profile_path,
                    applied_decisions=(),
                    skipped_decisions=(),
                    advised_actions=("sovyx doctor voice --fix --yes",),
                    dry_run=False,
                ),
            ),
            patch.object(wo, "lookup_profile", return_value=None),
            patch.object(wo, "store_profile"),
        ):
            await orch.run(job_id=SUSPICIOUS_MIND_ID, mind_id=SUSPICIOUS_MIND_ID)

        events = _audited(_flatten_calls([wo_logger, progress_logger]))
        assert events, "expected emissions during slow-path DONE run"
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks detected: {leaks}"

    @pytest.mark.asyncio()
    async def test_fallback_emits_no_raw_pii(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        wo_logger = MagicMock()
        progress_logger = MagicMock()
        with (
            patch.object(wo, "logger", wo_logger),
            patch.object(wizard_progress, "logger", progress_logger),
            patch.object(wo, "capture_fingerprint", return_value=_fingerprint()),
            patch.object(wo, "lookup_profile", return_value=None),
            patch.object(
                wo,
                "run_full_diag",
                side_effect=DiagPrerequisiteError("no bash"),
            ),
        ):
            await orch.run(job_id=SUSPICIOUS_MIND_ID, mind_id=SUSPICIOUS_MIND_ID)

        events = _audited(_flatten_calls([wo_logger, progress_logger]))
        assert events, "expected emissions during FALLBACK run"
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks detected (FALLBACK): {leaks}"

    @pytest.mark.asyncio()
    async def test_cancelled_emits_no_raw_pii(self, tmp_path: Path) -> None:
        orch = WizardOrchestrator(data_dir=tmp_path)
        # Pre-touch the .cancel file so the orchestrator's first
        # _is_cancelled() check fires immediately.
        cancel_path = orch.cancel_path(SUSPICIOUS_MIND_ID)
        cancel_path.parent.mkdir(parents=True, exist_ok=True)
        cancel_path.touch()
        wo_logger = MagicMock()
        progress_logger = MagicMock()
        with (
            patch.object(wo, "logger", wo_logger),
            patch.object(wizard_progress, "logger", progress_logger),
        ):
            await orch.run(job_id=SUSPICIOUS_MIND_ID, mind_id=SUSPICIOUS_MIND_ID)

        events = _audited(_flatten_calls([wo_logger, progress_logger]))
        assert events, "expected emissions during CANCELLED run"
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks detected (CANCELLED): {leaks}"

    def test_persistence_save_load_emits_no_raw_pii(self, tmp_path: Path) -> None:
        persistence_logger = MagicMock()
        with patch.object(persistence, "logger", persistence_logger):
            save_calibration_profile(_profile(mind_id=SUSPICIOUS_MIND_ID), data_dir=tmp_path)
            load_calibration_profile(data_dir=tmp_path, mind_id=SUSPICIOUS_MIND_ID)

        events = _audited(_flatten_calls([persistence_logger]))
        assert events, "expected emissions from persistence save+load"
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks detected (persistence): {leaks}"

    def test_kb_cache_store_lookup_emits_no_raw_pii(self, tmp_path: Path) -> None:
        kb_logger = MagicMock()
        profile = _profile(mind_id=SUSPICIOUS_MIND_ID)
        fingerprint_hash = profile.fingerprint.fingerprint_hash
        with patch.object(kb_cache, "logger", kb_logger):
            kb_cache.store_profile(profile, data_dir=tmp_path)
            cached = kb_cache.lookup_profile(data_dir=tmp_path, fingerprint_hash=fingerprint_hash)

        assert cached is not None
        events = _audited(_flatten_calls([kb_logger]))
        assert events, "expected emissions from kb_cache store+lookup"
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks detected (kb_cache): {leaks}"

    def test_progress_read_failure_emits_no_raw_pii(self, tmp_path: Path) -> None:
        progress_logger = MagicMock()
        # Construct a tracker whose path is a directory, so read_text
        # raises OSError and the progress_read_failed branch fires.
        bad_path = tmp_path / SUSPICIOUS_MIND_ID / "progress.jsonl"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.mkdir()  # directory at file path -> OSError on read
        tracker = wizard_progress.WizardProgressTracker(bad_path)
        with patch.object(wizard_progress, "logger", progress_logger):
            tracker.read_all()

        events = _audited(_flatten_calls([progress_logger]))
        assert events, "expected progress_read_failed emission"
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks detected (progress): {leaks}"

    def test_heuristic_detects_raw_mind_id(self) -> None:
        """Self-test: heuristic must trigger on a deliberately-leaky event.

        Confirms the audit isn't trivially passing because the heuristic
        is broken. Builds an event with a raw mind_id under a non-deprecated
        field name; the scan must surface it.
        """
        leaks = _scan_kwargs_for_leaks(
            "voice.calibration.test.fake",
            {"identifier": "operator-jonny-vaio-test"},
        )
        assert leaks == [("voice.calibration.test.fake", "identifier", "operator-jonny-vaio-test")]

    def test_heuristic_detects_filesystem_path(self) -> None:
        """Self-test: heuristic must trigger on a non-deprecated path field."""
        leaks = _scan_kwargs_for_leaks(
            "voice.calibration.test.fake",
            {"target_dir": "/home/user/.sovyx/voice"},
        )
        assert leaks == [("voice.calibration.test.fake", "target_dir", "/home/user/.sovyx/voice")]

    def test_heuristic_passes_hashed_identifier(self) -> None:
        """Self-test: 16-hex-char hash must NOT trigger heuristic A."""
        leaks = _scan_kwargs_for_leaks(
            "voice.calibration.test.fake",
            {"mind_id_hash": "0fae56d5786cade8"},
        )
        assert leaks == []
