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
from sovyx.voice.calibration import _applier as applier_module
from sovyx.voice.calibration import _kb_cache as kb_cache
from sovyx.voice.calibration import _persistence as persistence
from sovyx.voice.calibration import _wizard_orchestrator as wo
from sovyx.voice.calibration import _wizard_progress as wizard_progress
from sovyx.voice.calibration._applier import (
    _TARGET_CLASS_HANDLERS,
    ApplyError,
    CalibrationApplier,
    register_target_class_pair,
)
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

DEPRECATED_FIELDS: frozenset[str] = frozenset()
"""v0.30.28 backward-compat aliases (mind_id / job_id / cached_mind_id /
path) were removed in v0.30.29 (P1.T9). The exempt set is now empty —
any new field that fails the heuristic must be EITHER a closed enum
(add to :data:`CLOSED_ENUM_FIELDS`) OR a hashed identifier (use
``*_hash`` suffix)."""

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
        # rc.3 (Agent 3 #1): bounded via the dispatch registry —
        # production values are TuningAdvice / LinuxMixerApply /
        # MindConfig.voice. Adding new target_class values requires a
        # ``register_target_class_pair`` call in code review, so the
        # cardinality is gated.
        "target_class",
        # ``target`` is the dotted path on the target_class
        # (e.g. ``mind.voice.vad_threshold``); strings are bounded by
        # the rule set + target_class registry. Cardinality is finite.
        "target",
        # rc.3 (Agent 3 #1): exception_type from rollback_step_failed
        # is the Python class name of the raised exception (KeyError,
        # NotImplementedError, RuntimeError, etc.). Cardinality finite.
        "exception_type",
        # rc.3: operation enum on CalibrationDecision (set/advise).
        "operation",
        # NOTE rc.4 (Agent 2 D.3): ``reason_kind`` was speculatively
        # added in rc.3 but is NEVER emitted in production code (zero
        # ``reason_kind=`` hits in src/). The actual emission uses
        # ``reason=`` (DYNAMIC_TEXT_FIELDS) which is bounded by the
        # specific reason values we hardcode in ``_lifo_rollback``
        # (``handler_unregistered_during_rollback`` /
        # ``no_revert_registered``). Removed to keep the test contract
        # aligned with production.
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
                "run_full_diag_async",
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
                "run_full_diag_async",
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


# ────────────────────────────────────────────────────────────────────
# rc.3 (Agent 3 #1): Privacy gate covers _applier.py emission sites
# ────────────────────────────────────────────────────────────────────


class TestApplierEmissionPrivacy:
    """Walk the 7 emission sites in ``_applier.py`` through the privacy
    heuristic. Pre-rc.3 the gate patched ``wo`` / ``wizard_progress`` /
    ``persistence`` / ``kb_cache`` but NOT ``_applier``; the wizard's
    ``mock CalibrationApplier.apply`` short-circuited every applier
    emission, so a future regression emitting raw ``mind_id`` instead
    of ``mind_id_hash`` on (e.g.) ``apply_failed_with_rollback`` would
    have slipped past CI.

    Drives the real ``CalibrationApplier.apply`` chain through synthetic
    target-class registrations so we don't need real mixer/yaml state.
    """

    @pytest.mark.asyncio()
    async def test_apply_started_and_succeeded_emit_no_raw_pii(self, tmp_path: Path) -> None:
        async def _noop_apply(decision: Any, snapshot: Any, applier: Any) -> int:  # noqa: ARG001
            return 0

        async def _noop_revert(token: Any, snapshot: Any, applier: Any) -> None:  # noqa: ARG001
            return None

        register_target_class_pair("PrivAuditOk", apply=_noop_apply, revert=_noop_revert)
        try:
            applier_logger = MagicMock()
            with patch.object(applier_module, "logger", applier_logger):
                profile = CalibrationProfile(
                    schema_version=1,
                    profile_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    mind_id=SUSPICIOUS_MIND_ID,
                    fingerprint=_fingerprint(),
                    measurements=_measurements(),
                    decisions=(
                        CalibrationDecision(
                            target="t.0",
                            target_class="PrivAuditOk",
                            operation="set",
                            value=0,
                            rationale="r",
                            rule_id="R_audit",
                            rule_version=1,
                            confidence=CalibrationConfidence.HIGH,
                        ),
                    ),
                    provenance=(),
                    generated_by_engine_version="0.31.0-rc.3",
                    generated_by_rule_set_version=1,
                    generated_at_utc="2026-05-06T18:02:00Z",
                    signature=None,
                )
                applier = CalibrationApplier(data_dir=tmp_path)
                await applier.apply(profile)
        finally:
            del _TARGET_CLASS_HANDLERS["PrivAuditOk"]

        events = _audited(_flatten_calls([applier_logger]))
        names = {n for n, _ in events}
        assert "voice.calibration.applier.apply_started" in names
        assert "voice.calibration.applier.apply_succeeded" in names
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks (apply_started/succeeded): {leaks}"

    @pytest.mark.asyncio()
    async def test_apply_failed_with_rollback_emits_no_raw_pii(self, tmp_path: Path) -> None:
        """apply_failed_with_rollback + rollback_step_failed both fire when
        the second decision raises and the synthetic revert blows up.
        Drives the most sensitive failure path; both emissions MUST surface
        only hashed identifiers + closed-enum target_class.
        """

        async def _apply_test(decision: Any, snapshot: Any, applier: Any) -> int:  # noqa: ARG001
            idx = int(decision.value)
            if idx == 1:
                raise ApplyError(f"synthetic fail at {idx}", decision=decision, decision_index=idx)
            return idx

        async def _revert_explodes(token: Any, snapshot: Any, applier: Any) -> None:  # noqa: ARG001
            raise RuntimeError("synthetic revert failure")

        register_target_class_pair("PrivAuditRollback", apply=_apply_test, revert=_revert_explodes)
        try:
            applier_logger = MagicMock()
            with patch.object(applier_module, "logger", applier_logger):
                profile = CalibrationProfile(
                    schema_version=1,
                    profile_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    mind_id=SUSPICIOUS_MIND_ID,
                    fingerprint=_fingerprint(),
                    measurements=_measurements(),
                    decisions=(
                        CalibrationDecision(
                            target="t.0",
                            target_class="PrivAuditRollback",
                            operation="set",
                            value=0,
                            rationale="r",
                            rule_id="R_audit",
                            rule_version=1,
                            confidence=CalibrationConfidence.HIGH,
                        ),
                        CalibrationDecision(
                            target="t.1",
                            target_class="PrivAuditRollback",
                            operation="set",
                            value=1,  # this one raises
                            rationale="r",
                            rule_id="R_audit",
                            rule_version=1,
                            confidence=CalibrationConfidence.HIGH,
                        ),
                    ),
                    provenance=(),
                    generated_by_engine_version="0.31.0-rc.3",
                    generated_by_rule_set_version=1,
                    generated_at_utc="2026-05-06T18:02:00Z",
                    signature=None,
                )
                applier = CalibrationApplier(data_dir=tmp_path)
                with pytest.raises(ApplyError):
                    await applier.apply(profile)
        finally:
            del _TARGET_CLASS_HANDLERS["PrivAuditRollback"]

        events = _audited(_flatten_calls([applier_logger]))
        names = {n for n, _ in events}
        assert "voice.calibration.applier.apply_started" in names
        assert "voice.calibration.applier.apply_failed_with_rollback" in names
        assert "voice.calibration.applier.rollback_step_failed" in names
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks (rollback chain): {leaks}"

    @pytest.mark.asyncio()
    async def test_apply_failed_sync_raise_emits_no_raw_pii(self, tmp_path: Path) -> None:
        """The eager-validation apply_failed event (no rollback path)
        fires when a single decision raises before any successful apply.
        Different code path than apply_failed_with_rollback; both must
        be privacy-clean.
        """

        async def _fail_immediately(decision: Any, snapshot: Any, applier: Any) -> int:  # noqa: ARG001
            raise ApplyError("synthetic fail", decision=decision)

        async def _noop_revert(token: Any, snapshot: Any, applier: Any) -> None:  # noqa: ARG001
            return None

        register_target_class_pair(
            "PrivAuditSyncFail", apply=_fail_immediately, revert=_noop_revert
        )
        try:
            applier_logger = MagicMock()
            with patch.object(applier_module, "logger", applier_logger):
                profile = CalibrationProfile(
                    schema_version=1,
                    profile_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    mind_id=SUSPICIOUS_MIND_ID,
                    fingerprint=_fingerprint(),
                    measurements=_measurements(),
                    decisions=(
                        CalibrationDecision(
                            target="t.0",
                            target_class="PrivAuditSyncFail",
                            operation="set",
                            value="x",
                            rationale="r",
                            rule_id="R_audit",
                            rule_version=1,
                            confidence=CalibrationConfidence.HIGH,
                        ),
                    ),
                    provenance=(),
                    generated_by_engine_version="0.31.0-rc.3",
                    generated_by_rule_set_version=1,
                    generated_at_utc="2026-05-06T18:02:00Z",
                    signature=None,
                )
                applier = CalibrationApplier(data_dir=tmp_path)
                with pytest.raises(ApplyError):
                    await applier.apply(profile)
        finally:
            del _TARGET_CLASS_HANDLERS["PrivAuditSyncFail"]

        events = _audited(_flatten_calls([applier_logger]))
        names = {n for n, _ in events}
        # The pre-validation apply_failed branch fires when a single
        # decision raises immediately (no prior success → nothing to roll
        # back). The exact event name varies by code path; what matters
        # for the privacy gate is that ALL emitted events are clean.
        assert names, "expected at least one applier emission"
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks (sync-fail path): {leaks}"

    @pytest.mark.asyncio()
    async def test_dry_run_emits_no_raw_pii(self, tmp_path: Path) -> None:
        """dry_run path emits ``apply_started`` + ``dry_run`` events."""

        async def _noop_apply(decision: Any, snapshot: Any, applier: Any) -> int:  # noqa: ARG001
            return 0

        async def _noop_revert(token: Any, snapshot: Any, applier: Any) -> None:  # noqa: ARG001
            return None

        register_target_class_pair("PrivAuditDry", apply=_noop_apply, revert=_noop_revert)
        try:
            applier_logger = MagicMock()
            with patch.object(applier_module, "logger", applier_logger):
                profile = CalibrationProfile(
                    schema_version=1,
                    profile_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    mind_id=SUSPICIOUS_MIND_ID,
                    fingerprint=_fingerprint(),
                    measurements=_measurements(),
                    decisions=(
                        CalibrationDecision(
                            target="t.0",
                            target_class="PrivAuditDry",
                            operation="set",
                            value=0,
                            rationale="r",
                            rule_id="R_audit",
                            rule_version=1,
                            confidence=CalibrationConfidence.HIGH,
                        ),
                    ),
                    provenance=(),
                    generated_by_engine_version="0.31.0-rc.3",
                    generated_by_rule_set_version=1,
                    generated_at_utc="2026-05-06T18:02:00Z",
                    signature=None,
                )
                applier = CalibrationApplier(data_dir=tmp_path)
                await applier.apply(profile, dry_run=True)
        finally:
            del _TARGET_CLASS_HANDLERS["PrivAuditDry"]

        events = _audited(_flatten_calls([applier_logger]))
        names = {n for n, _ in events}
        assert "voice.calibration.applier.dry_run" in names
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks (dry-run): {leaks}"


# ────────────────────────────────────────────────────────────────────
# rc.5 (Agent 3 #3): Widen privacy gate to engine / _measurer /
# _fingerprint / _runner loggers. Pre-rc.5 these 4 modules' emission
# sites were CLEAN by inspection but UNGATED — a future regression
# adding raw mind_id (or a path) would slip past CI. The new tests
# walk each module's logger through the privacy heuristic.
# ────────────────────────────────────────────────────────────────────


class TestEngineEmissionPrivacy:
    """Walk every emission site in ``calibration/engine.py`` through
    the privacy heuristic with a SUSPICIOUS_MIND_ID profile evaluation.
    """

    def test_engine_evaluate_emits_no_raw_pii(self, tmp_path: Path) -> None:
        from sovyx.voice.calibration import engine as engine_module
        from sovyx.voice.calibration.engine import CalibrationEngine

        engine_logger = MagicMock()
        with patch.object(engine_module, "logger", engine_logger):
            engine = CalibrationEngine()
            # Drive a full evaluate cycle with the canonical fingerprint +
            # measurements + null triage so multiple rules fire on
            # different code paths.
            profile = engine.evaluate(
                mind_id=SUSPICIOUS_MIND_ID,
                fingerprint=_fingerprint(),
                measurements=_measurements(),
                triage_result=None,
            )

        # Drive a SECOND evaluate with a winning triage so the
        # triage-gated branches fire.
        with patch.object(engine_module, "logger", engine_logger):
            engine2 = CalibrationEngine()
            profile2 = engine2.evaluate(
                mind_id=SUSPICIOUS_MIND_ID,
                fingerprint=_fingerprint(),
                measurements=_measurements(),
                triage_result=_triage(),
            )

        assert profile is not None
        assert profile2 is not None
        events = _audited(_flatten_calls([engine_logger]))
        assert events, "expected engine.evaluate to emit telemetry"
        names = {n for n, _ in events}
        # Engine MUST emit at least the run_started + run_completed events
        # so a regression that drops them is caught.
        assert any("engine" in n for n in names), (
            f"expected at least one voice.calibration.engine.* event; got {names}"
        )
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks (engine.evaluate): {leaks}"


class TestMeasurerEmissionPrivacy:
    """Walk the ``_measurer.amixer_failed`` debug emission through
    the privacy heuristic. Drives the failure path by patching
    subprocess.run to raise OSError.
    """

    def test_measurer_amixer_failure_emits_no_raw_pii(self, tmp_path: Path) -> None:
        import contextlib
        import subprocess as _subprocess

        from sovyx.voice.calibration import _measurer as measurer_module

        measurer_logger = MagicMock()

        # Patch subprocess.run inside _measurer's namespace so the
        # amixer probe raises + drives the debug emission.
        def _raise_oserror(*_args, **_kwargs):  # noqa: ANN001, ANN202
            raise OSError("synthetic amixer failure")

        # Drive a measurement capture through the failing subprocess.
        # _measurer's public API is capture_measurements.
        with (
            patch.object(measurer_module, "logger", measurer_logger),
            patch.object(measurer_module.subprocess, "run", side_effect=_raise_oserror),
        ):
            # capture_measurements with no triage / no tarball → all
            # mixer probes fall through to the failure branch. We don't
            # care if the call itself raises; only that NO logger
            # emission carries raw PII before it errors.
            from sovyx.voice.calibration._measurer import capture_measurements

            with contextlib.suppress(_subprocess.SubprocessError, OSError):
                capture_measurements(diag_tarball_root=None, triage_result=None, duration_s=0.0)

        events = _audited(_flatten_calls([measurer_logger]))
        # The amixer_failed event may or may not fire depending on which
        # probe is reached first; what matters is that NO emission leaks
        # raw PII.
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks (measurer): {leaks}"


class TestFingerprintEmissionPrivacy:
    """Walk the ``_fingerprint.subprocess_failed`` debug emission through
    the privacy heuristic.
    """

    def test_fingerprint_subprocess_failure_emits_no_raw_pii(self, tmp_path: Path) -> None:
        import contextlib

        from sovyx.voice.calibration import _fingerprint as fingerprint_module

        fingerprint_logger = MagicMock()

        def _raise_oserror(*_args, **_kwargs):  # noqa: ANN001, ANN202
            raise OSError("synthetic fingerprint subprocess failure")

        # Drive capture_fingerprint with the failing subprocess. The
        # fingerprint module probes multiple binaries (uname, lscpu,
        # dmidecode, etc.); each failure drives the debug emission.
        with (
            patch.object(fingerprint_module, "logger", fingerprint_logger),
            patch.object(fingerprint_module.subprocess, "run", side_effect=_raise_oserror),
        ):
            from sovyx.voice.calibration._fingerprint import capture_fingerprint

            # Platform-specific failure paths may surface; we only care
            # that NO logger emission carries raw PII before it errors.
            with contextlib.suppress(Exception):
                capture_fingerprint()

        events = _audited(_flatten_calls([fingerprint_logger]))
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks (fingerprint): {leaks}"


class TestRunnerEmissionPrivacy:
    """Walk the ``_runner`` emissions (full_diag_started / failed /
    completed / cancel_grace_expired / cancel_completed) through the
    privacy heuristic.
    """

    @pytest.mark.asyncio()
    async def test_runner_full_diag_emits_no_raw_pii(self, tmp_path: Path) -> None:
        from sovyx.voice.diagnostics import _runner as runner_module

        runner_logger = MagicMock()

        # Build a minimal _FakeAsyncProcess that returns 0 cleanly.
        class _FakeProc:
            pid = 12345
            returncode: int | None = None

            async def wait(self) -> int:
                self.returncode = 0
                return 0

        async def _factory(*_args, **_kwargs):  # noqa: ANN001, ANN202
            return _FakeProc()

        # Stage a fake bash extraction + a synthetic tarball under
        # output_root so run_full_diag_async finds something on success.
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        (extracted / "sovyx-voice-diag.sh").write_text("#!/bin/bash\nexit 0\n")
        output_root = tmp_path / "out"
        output_root.mkdir()
        diag_dir = output_root / "sovyx-diag-host-20260506T180000Z-deadbeef"
        diag_dir.mkdir()
        tarball = diag_dir / "sovyx-voice-diag_x.tar.gz"
        tarball.write_bytes(b"\x1f\x8b\x08\x00")

        with (
            patch.object(runner_module, "logger", runner_logger),
            patch.object(runner_module, "_check_prerequisites"),
            patch.object(runner_module, "_extract_bash_to_temp", return_value=extracted),
            patch.object(runner_module.asyncio, "create_subprocess_exec", side_effect=_factory),
        ):
            await runner_module.run_full_diag_async(output_root=output_root)

        events = _audited(_flatten_calls([runner_logger]))
        names = {n for n, _ in events}
        assert "voice.diagnostics.full_diag_started" in names
        assert "voice.diagnostics.full_diag_completed" in names
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks (runner success path): {leaks}"

    @pytest.mark.asyncio()
    async def test_runner_full_diag_failed_emits_no_raw_pii(self, tmp_path: Path) -> None:
        import contextlib

        from sovyx.voice.diagnostics import _runner as runner_module

        runner_logger = MagicMock()

        class _FakeProcFailed:
            pid = 12346
            returncode: int | None = None

            async def wait(self) -> int:
                self.returncode = 3  # selftest_failed signal
                return 3

        async def _factory(*_args, **_kwargs):  # noqa: ANN001, ANN202
            return _FakeProcFailed()

        extracted = tmp_path / "extracted2"
        extracted.mkdir()
        (extracted / "sovyx-voice-diag.sh").write_text("#!/bin/bash\nexit 3\n")
        output_root = tmp_path / "out2"
        output_root.mkdir()

        from sovyx.voice.diagnostics import DiagRunError

        with (
            patch.object(runner_module, "logger", runner_logger),
            patch.object(runner_module, "_check_prerequisites"),
            patch.object(runner_module, "_extract_bash_to_temp", return_value=extracted),
            patch.object(runner_module.asyncio, "create_subprocess_exec", side_effect=_factory),
            contextlib.suppress(DiagRunError),
        ):
            await runner_module.run_full_diag_async(output_root=output_root)

        events = _audited(_flatten_calls([runner_logger]))
        names = {n for n, _ in events}
        assert "voice.diagnostics.full_diag_failed" in names
        leaks = [leak for name, kw in events for leak in _scan_kwargs_for_leaks(name, kw)]
        assert not leaks, f"raw PII leaks (runner failure path): {leaks}"
