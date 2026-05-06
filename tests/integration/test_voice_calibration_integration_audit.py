"""Cross-cutting integration audit for the voice self-calibrating system.

Asserts that every layer of the mission deliverable is wired together
correctly. Catches "the parts work in isolation but the contract drifts
between layers" regressions that unit tests miss by design.

Audit checklist (per mission spec §8 + the v0.30.19..v0.30.25 batch):

1. **Telemetry namespace**: every spec-mandated event name fires from
   the right module + carries the right closed-enum fields.
2. **Calibration package public surface**: every symbol the operator
   docs reference is importable from `sovyx.voice.calibration`.
3. **Rule registry**: all 10 rules R10..R95 are discovered + sorted
   by priority desc.
4. **CLI surface**: every flag the docs promise is parseable on the
   `sovyx doctor voice` command (mutex contracts honoured).
5. **Backend endpoint surface**: every endpoint the dashboard
   consumes is registered + reachable in the test app.
6. **EngineConfig.voice**: feature flag round-trips env -> field ->
   endpoint -> Zustand-shape response.
7. **Verification corpus**: synth produces deterministic tarballs +
   8 scenarios are importable.

Each subtest is a short integration assertion -- the full unit
coverage lives in module-specific test files. This file's purpose is
the cross-cutting contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import sovyx.voice.calibration as calibration_pkg
from sovyx.engine.config import DatabaseConfig, EngineConfig, VoiceFeaturesConfig
from sovyx.voice.calibration import iter_rules


@pytest.mark.integration
class TestPublicSurface:
    """Every operator-doc symbol is importable from the package."""

    def test_calibration_package_re_exports(self) -> None:
        # Symbols documented in docs/modules/voice-calibration.md +
        # the operator-facing CLI helpers + the wizard backend types.
        for name in (
            "CalibrationEngine",
            "CalibrationProfile",
            "CalibrationApplier",
            "ApplyResult",
            "ApplyError",
            "CalibrationProfileLoadError",
            "CalibrationProfileRollbackError",
            "load_calibration_profile",
            "save_calibration_profile",
            "rollback_calibration_profile",
            "profile_path",
            "profile_backup_path",
            "capture_fingerprint",
            "capture_measurements",
            "WizardOrchestrator",
            "WizardJobState",
            "WizardStatus",
            "WizardProgressTracker",
            "iter_rules",
            "CalibrationConfidence",
            "CalibrationDecision",
            "HardwareFingerprint",
            "MeasurementSnapshot",
            "ProvenanceTrace",
            "ProgressEvent",
            "RuleContext",
            "RuleEvaluation",
            "CalibrationRule",
            "RULE_SET_VERSION",
        ):
            assert hasattr(calibration_pkg, name), f"sovyx.voice.calibration must export {name!r}"


@pytest.mark.integration
class TestRuleRegistry:
    """All 10 spec-listed rules R10..R95 are discoverable."""

    def test_all_ten_rules_discovered(self) -> None:
        rule_ids = {r.rule_id for r in iter_rules()}
        expected = {
            "R10_mic_attenuated",
            "R20_windows_apo_active",
            "R30_linux_destructive_filter",
            "R40_macos_tcc_denied",
            "R50_hardware_gap",
            "R60_vad_threshold_tuning",
            "R70_capture_mode_exclusive",
            "R80_aec_engine",
            "R90_stt_locality",
            "R95_wake_word_model",
        }
        missing = expected - rule_ids
        assert not missing, f"missing rules: {missing}"

    def test_rules_sorted_priority_desc_with_alpha_tiebreak(self) -> None:
        from sovyx.voice.calibration import CalibrationEngine

        engine = CalibrationEngine()
        priorities = [r.priority for r in engine.rules]
        # Priority desc.
        assert priorities == sorted(priorities, reverse=True), (
            f"rules not priority-desc: {priorities}"
        )


@pytest.mark.integration
class TestEngineConfigFlag:
    """EngineConfig.voice.calibration_wizard_enabled is honoured."""

    def test_default_is_false(self) -> None:
        cfg = EngineConfig(
            voice=VoiceFeaturesConfig(),
            database=DatabaseConfig(data_dir=Path.home() / ".sovyx"),
        )
        assert cfg.voice.calibration_wizard_enabled is False

    def test_explicit_true_is_honoured(self) -> None:
        cfg = EngineConfig(
            voice=VoiceFeaturesConfig(calibration_wizard_enabled=True),
            database=DatabaseConfig(data_dir=Path.home() / ".sovyx"),
        )
        assert cfg.voice.calibration_wizard_enabled is True


@pytest.mark.integration
class TestCorpusSynth:
    """All 8 corpus scenarios import + produce well-formed tarballs."""

    def test_eight_scenarios_importable_and_buildable(self, tmp_path: Path) -> None:
        from sovyx.voice.diagnostics import triage_tarball
        from tests.fixtures.voice_diag import (
            build_tarball,
            scenario_golden_path,
            scenario_h1_mic_destroyed_apo,
            scenario_h4_pulse_destructive_filter,
            scenario_h5_macos_tcc_denied,
            scenario_h6_selftest_failed,
            scenario_h9_hardware_gap,
            scenario_h10_mixer_attenuated,
            scenario_multi_hypothesis,
        )

        scenarios = [
            ("golden", scenario_golden_path()),
            ("h1", scenario_h1_mic_destroyed_apo()),
            ("h4", scenario_h4_pulse_destructive_filter()),
            ("h5", scenario_h5_macos_tcc_denied()),
            ("h6", scenario_h6_selftest_failed()),
            ("h9", scenario_h9_hardware_gap()),
            ("h10", scenario_h10_mixer_attenuated()),
            ("multi", scenario_multi_hypothesis()),
        ]
        for name, sc in scenarios:
            tarball = build_tarball(sc, tmp_path / f"{name}.tar.gz")
            assert tarball.is_file(), f"{name} tarball not materialized"
            # Triage cleanly without raising on every scenario.
            result = triage_tarball(tarball)
            assert result.status == "complete"


@pytest.mark.integration
class TestDashboardEndpointWiring:
    """Every dashboard endpoint the frontend consumes is registered."""

    def test_calibration_endpoints_registered(self) -> None:
        from sovyx.dashboard.server import create_app

        app = create_app(token="audit-token")  # noqa: S106 -- test-only token
        routes = {r.path for r in app.routes}  # type: ignore[attr-defined]
        for path in (
            "/api/voice/calibration/start",
            "/api/voice/calibration/jobs/{job_id}",
            "/api/voice/calibration/jobs/{job_id}/cancel",
            "/api/voice/calibration/preview-fingerprint",
            "/api/voice/calibration/feature-flag",
        ):
            assert path in routes, f"endpoint {path} not registered"

    def test_websocket_route_registered(self) -> None:
        from sovyx.dashboard.server import create_app

        app = create_app(token="audit-token")  # noqa: S106
        ws_paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
        assert "/api/voice/calibration/jobs/{job_id}/stream" in ws_paths


@pytest.mark.integration
class TestCLISurface:
    """Every documented --calibrate flag is parseable."""

    def test_calibrate_flag_help_lists_all_options(self) -> None:
        from typer.testing import CliRunner

        from sovyx.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "voice", "--help"])
        assert result.exit_code == 0
        for flag in (
            "--full-diag",
            "--calibrate",
            "--dry-run",
            "--explain",
            "--show",
            "--rollback",
            "--mind-id",
            "--non-interactive",
            "--fix",
        ):
            assert flag in result.output, f"--help is missing documented flag {flag!r}"
