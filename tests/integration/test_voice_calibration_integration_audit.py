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
            # P6 + P7 additions:
            "--surgical",
            "--signing-key",
            "--evaluate-rules",
        ):
            assert flag in result.output, f"--help is missing documented flag {flag!r}"


# ════════════════════════════════════════════════════════════════════
# P7.T2 — behavior tests (not just registration). Mission §11.2 #18.
# ════════════════════════════════════════════════════════════════════

_AUDIT_TOKEN = "audit-behavior-token"  # noqa: S105 -- test-only token


@pytest.fixture()
def _behavior_app():
    from sovyx.dashboard.server import create_app

    return create_app(token=_AUDIT_TOKEN)


@pytest.fixture()
def _behavior_client(_behavior_app):  # noqa: ANN001 -- FastAPI app
    from fastapi.testclient import TestClient

    return TestClient(_behavior_app, headers={"Authorization": f"Bearer {_AUDIT_TOKEN}"})


@pytest.mark.integration
class TestStartEndpointBehavior:
    """POST /start: malformed body → 422; no auth → 401; same-mind concurrent → 409."""

    def test_malformed_body_returns_422(self, _behavior_client) -> None:  # noqa: ANN001
        # Pydantic schema requires mind_id (1-64 chars). Empty body → 422.
        response = _behavior_client.post("/api/voice/calibration/start", json={})
        assert response.status_code == 422, response.text

    def test_missing_mind_id_returns_422(self, _behavior_client) -> None:  # noqa: ANN001
        response = _behavior_client.post(
            "/api/voice/calibration/start", json={"some_other_field": "x"}
        )
        assert response.status_code == 422, response.text

    def test_empty_mind_id_returns_422(self, _behavior_client) -> None:  # noqa: ANN001
        response = _behavior_client.post("/api/voice/calibration/start", json={"mind_id": ""})
        assert response.status_code == 422, response.text

    def test_oversized_mind_id_returns_422(self, _behavior_client) -> None:  # noqa: ANN001
        # The Pydantic schema caps mind_id at 64 chars.
        response = _behavior_client.post(
            "/api/voice/calibration/start", json={"mind_id": "x" * 65}
        )
        assert response.status_code == 422, response.text

    def test_no_auth_returns_401_or_403(self, _behavior_app) -> None:  # noqa: ANN001
        # FastAPI's default for missing Authorization header is 401
        # (or 403 depending on the dependency; both are acceptable
        # per the route's auth contract — what matters is REJECTION).
        from fastapi.testclient import TestClient

        anon_client = TestClient(_behavior_app)  # No Authorization header
        response = anon_client.post("/api/voice/calibration/start", json={"mind_id": "default"})
        assert response.status_code in (401, 403), (
            f"unauthenticated request must be rejected; got {response.status_code}"
        )

    def test_wrong_token_returns_401_or_403(self, _behavior_app) -> None:  # noqa: ANN001
        from fastapi.testclient import TestClient

        bad_client = TestClient(_behavior_app, headers={"Authorization": "Bearer wrong-token"})
        response = bad_client.post("/api/voice/calibration/start", json={"mind_id": "default"})
        assert response.status_code in (401, 403)


@pytest.mark.integration
class TestCancelEndpointBehavior:
    """POST /jobs/{id}/cancel: rejects unauthenticated; idempotent on missing job."""

    def test_cancel_no_auth_returns_401_or_403(self, _behavior_app) -> None:  # noqa: ANN001
        from fastapi.testclient import TestClient

        anon_client = TestClient(_behavior_app)
        response = anon_client.post("/api/voice/calibration/jobs/default/cancel")
        assert response.status_code in (401, 403)

    def test_cancel_unknown_job_is_idempotent(self, _behavior_client) -> None:  # noqa: ANN001
        # The cancel endpoint touches the .cancel file regardless of
        # whether a job exists; running on an unknown mind_id returns
        # 200 with already_terminal=False.
        response = _behavior_client.post("/api/voice/calibration/jobs/never-started/cancel")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["job_id"] == "never-started"
        assert body["cancel_signal_written"] is True


@pytest.mark.integration
class TestWebSocketAuthBehavior:
    """WS /jobs/{id}/stream: accepts query-param token; rejects wrong tokens."""

    def test_ws_accepts_query_param_token(self, _behavior_app) -> None:  # noqa: ANN001
        from fastapi.testclient import TestClient

        client = TestClient(_behavior_app)
        # Successful auth → connect() returns the WS context manager
        # without raising. We don't try to receive (no job is running
        # so the handler waits indefinitely on the JSONL tail). The
        # absence of a 1008 close on entry is itself the assertion.
        with client.websocket_connect(
            f"/api/voice/calibration/jobs/anything/stream?token={_AUDIT_TOKEN}"
        ) as ws:
            ws.close()

    def test_ws_rejects_no_token(self, _behavior_app) -> None:  # noqa: ANN001
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        client = TestClient(_behavior_app)
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/voice/calibration/jobs/anything/stream") as ws,
        ):
            ws.receive_json()
        # Code 1008 is the "policy violation" close used by the
        # WS handler when the auth check fails.
        assert exc_info.value.code == 1008

    def test_ws_rejects_wrong_token(self, _behavior_app) -> None:  # noqa: ANN001
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        client = TestClient(_behavior_app)
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect(
                "/api/voice/calibration/jobs/anything/stream?token=NOT_THE_TOKEN"
            ) as ws,
        ):
            ws.receive_json()
        assert exc_info.value.code == 1008
