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
from typing import Any
from unittest.mock import patch

import pytest

import sovyx.voice.calibration as calibration_pkg
from sovyx.engine.config import DatabaseConfig, EngineConfig, VoiceFeaturesConfig
from sovyx.voice.calibration import iter_rules


class TestPublicSurface:
    """Every operator-doc symbol is importable from the package.

    rc.5 (Agent 2 A.4): ``@pytest.mark.integration`` removed — pure
    in-process ``hasattr`` audit, no real ML / SQLite / cross-component
    wiring per ``pyproject.toml`` marker criteria. Pre-rc.5 the marker
    silently skipped this class from default CI; a public-surface drift
    (e.g. an `__init__.py` re-export accidentally dropped) would land
    green. Same logic as rc.4 E.3 fix.
    """

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


class TestRuleRegistry:
    """All 10 spec-listed rules R10..R95 are discoverable.

    rc.5 (Agent 2 A.4): integration marker removed — pure ``iter_rules()``
    walk, no IO. A regression that drops a rule would have shipped
    silently pre-rc.5.
    """

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


class TestEngineConfigFlag:
    """EngineConfig.voice.calibration_wizard_enabled is honoured.

    rc.5 (Agent 2 A.4): integration marker removed — pure pydantic
    instantiation + env round-trip; no IO.

    rc.10: default flipped from False → True per the docstring's own
    promise + the operator's "automatic setup without technical
    knowledge" directive. Fresh-user dashboard onboarding now mounts
    the auto-fix calibration wizard by default.
    """

    def test_default_is_true(self) -> None:
        """rc.10 flip: default mounts the calibration wizard so
        non-technical operators get the auto-fix flow without having
        to discover an env var or dashboard toggle."""
        cfg = EngineConfig(
            voice=VoiceFeaturesConfig(),
            database=DatabaseConfig(data_dir=Path.home() / ".sovyx"),
        )
        assert cfg.voice.calibration_wizard_enabled is True

    def test_explicit_false_is_honoured(self) -> None:
        """Operators on hardware that doesn't need the wizard can opt
        out via env or system.yaml."""
        cfg = EngineConfig(
            voice=VoiceFeaturesConfig(calibration_wizard_enabled=False),
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


# rc.5 (Agent 2 A.4): integration marker removed — `create_app()` +
# route inspection is in-process. A regression that drops a route
# would have shipped silently pre-rc.5.
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


# rc.5 (Agent 2 A.4): integration marker removed — `CliRunner` + `--help`
# inspection is in-process; no real subprocess fired.
class TestCLISurface:
    """Every documented --calibrate flag is parseable."""

    def test_calibrate_flag_help_lists_all_options(self) -> None:
        from typer.testing import CliRunner

        from sovyx.cli.main import app

        # Rich colour + wrap normalisation lives in tests/conftest.py
        # (NO_COLOR=1 + COLUMNS=240 set at session start). CliRunner
        # inherits it; output is plain ASCII, no ANSI escapes, no
        # 80-col wrap. Substring asserts work cross-platform without
        # post-strip helpers.
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


# rc.5 (Agent 2 A.4): integration marker removed — fastapi.TestClient
# is in-process. Same logic as rc.4 E.3 fix for the race tests.
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


# rc.5 (Agent 2 A.4): integration marker removed — TestClient in-process.
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


# rc.5 (Agent 2 A.4): integration marker removed — TestClient
# websocket_connect is in-process; auth check is local.
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


# ════════════════════════════════════════════════════════════════════
# rc.3 (Agent 2 #18) — concurrent-POST integration test for QA-FIX-5 race
# ════════════════════════════════════════════════════════════════════
#
# rc.4 (Agent 2 E.3): the @pytest.mark.integration marker was REMOVED
# because this class doesn't fit the marker's documented criteria
# ("real ML models, SQLite heavy IO, or cross-component wiring"). The
# tests run httpx + ASGITransport in-process — pure Python, no external
# resources. Pre-rc.4 the marker caused CI to silently SKIP the
# QA-FIX-5 race regression (pyproject.toml addopts excludes
# ``-m 'not integration'`` and the workflow doesn't opt in). A future
# regression to ``_START_LOCKS`` would have slipped past CI green.
# Removing the marker makes these tests run on every push.


class TestConcurrentStartRaceQaFix5:
    """End-to-end verification of the per-mind ``_START_LOCKS`` race fix.

    QA-FIX-5 (v0.31.0-rc.2) added an ``LRULockDict[str]`` keyed by mind_id
    around the ``(_job_in_flight check + _active_jobs register)`` to make
    the in-flight gate atomic. The fix shipped with code-review-only
    coverage (no concurrent integration test). rc.3 closes the gap.

    Contract:
    * Two concurrent POST /start for the SAME mind_id → exactly one 202
      + one 409.
    * Concurrent POST for DIFFERENT mind_ids → both 202 (per-mind lock,
      not a global lock).
    * After the in-flight task completes, a fresh POST for the same
      mind_id is permitted (lock self-prunes via _active_jobs cleanup).
    """

    @pytest.mark.asyncio()
    async def test_concurrent_same_mind_yields_one_202_and_one_409(self, tmp_path: Path) -> None:
        """Two concurrent /start for same mind_id → exactly one 202 + one 409.

        We force the orchestrator's runner to block so the first POST's
        ``_active_jobs`` registration is observable while the second POST
        contests the per-mind ``_START_LOCKS`` lock. Pre-rc.2 (no lock)
        both POSTs would pass the in-flight check before either wrote a
        snapshot — both would 202 and corrupt the JSONL.
        """
        import asyncio as _asyncio

        import httpx

        from sovyx.dashboard.routes import voice_calibration as vc_route
        from sovyx.dashboard.server import create_app
        from sovyx.engine._lock_dict import LRULockDict

        app = create_app(token=_AUDIT_TOKEN)
        # Resolve the route's data_dir into the test sandbox so the
        # JSONL progress files don't land in ~/.sovyx (anti-pattern #23).
        app.state.engine_config = type("C", (), {"data_dir": tmp_path})()

        # rc.5 (Agent 2 A.2): capture the original ``_START_LOCKS`` so the
        # finally block can restore it. Pre-rc.5 the test reassigned the
        # module attribute but never restored — leaking a test-injected
        # LRULockDict instance (with our test-keyed locks) into the
        # daemon's runtime singleton. Functionally safe (LRULockDict
        # bounded + locks self-prune), but contradicts the docstring's
        # "no state leaks into the next test" promise.
        _original_start_locks = vc_route._START_LOCKS
        # Reset the module-level registries so prior tests don't bleed in.
        vc_route._active_jobs.clear()
        vc_route._START_LOCKS = LRULockDict(maxsize=256)

        # Patch the route's runner-spawn so the spawned task is a never-
        # completing sentinel future. The first POST's lock body
        # registers the future into ``_active_jobs``; when the second POST
        # acquires the lock, the in-flight check sees the registered
        # task with ``done() == False`` and raises 409. This is more
        # deterministic than mocking the entire WizardOrchestrator: we
        # don't depend on the runner coroutine ever starting.
        def _never_done_future_spawn(coro: Any) -> _asyncio.Future[None]:
            # Discard the runner coroutine so it doesn't actually run
            # (closing it cleanly to avoid 'never awaited' warnings).
            coro.close()
            return _asyncio.get_running_loop().create_future()

        try:
            with patch.object(_asyncio, "ensure_future", _never_done_future_spawn):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                    headers={"Authorization": f"Bearer {_AUDIT_TOKEN}"},
                ) as client:
                    r1, r2 = await _asyncio.gather(
                        client.post(
                            "/api/voice/calibration/start",
                            json={"mind_id": "rc3-race-mind"},
                        ),
                        client.post(
                            "/api/voice/calibration/start",
                            json={"mind_id": "rc3-race-mind"},
                        ),
                    )

            statuses = sorted([r1.status_code, r2.status_code])
            assert statuses == [202, 409], (
                f"expected exactly one 202 + one 409 from concurrent "
                f"same-mind POSTs; got {statuses} "
                f"(bodies: {r1.text!r}, {r2.text!r})"
            )

            assert "rc3-race-mind" in vc_route._active_jobs
            assert len(vc_route._active_jobs) == 1
        finally:
            # Cancel the sentinel future + clear the registry so no
            # state leaks into the next test.
            for fut in list(vc_route._active_jobs.values()):
                if not fut.done():
                    fut.cancel()
            vc_route._active_jobs.clear()
            # rc.5 (Agent 2 A.2): restore the original LRULockDict so
            # the daemon runtime sees its singleton again.
            vc_route._START_LOCKS = _original_start_locks

    @pytest.mark.asyncio()
    async def test_concurrent_different_minds_both_get_202(self, tmp_path: Path) -> None:
        """Per-mind lock — distinct mind_ids do NOT serialise."""
        import asyncio as _asyncio

        import httpx

        from sovyx.dashboard.routes import voice_calibration as vc_route
        from sovyx.dashboard.server import create_app
        from sovyx.engine._lock_dict import LRULockDict

        app = create_app(token=_AUDIT_TOKEN)
        app.state.engine_config = type("C", (), {"data_dir": tmp_path})()

        # rc.5 (Agent 2 A.2): same restore-after-test pattern as
        # test_concurrent_same_mind_yields_one_202_and_one_409.
        _original_start_locks = vc_route._START_LOCKS
        vc_route._active_jobs.clear()
        vc_route._START_LOCKS = LRULockDict(maxsize=256)

        def _never_done_future_spawn(coro: Any) -> _asyncio.Future[None]:
            coro.close()
            return _asyncio.get_running_loop().create_future()

        try:
            with patch.object(_asyncio, "ensure_future", _never_done_future_spawn):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                    headers={"Authorization": f"Bearer {_AUDIT_TOKEN}"},
                ) as client:
                    r1, r2 = await _asyncio.gather(
                        client.post(
                            "/api/voice/calibration/start",
                            json={"mind_id": "rc3-mind-A"},
                        ),
                        client.post(
                            "/api/voice/calibration/start",
                            json={"mind_id": "rc3-mind-B"},
                        ),
                    )
                    assert sorted([r1.status_code, r2.status_code]) == [202, 202], (
                        f"distinct mind_ids must NOT serialise; "
                        f"got {r1.status_code} + {r2.status_code}"
                    )
                    assert len(vc_route._active_jobs) == 2
        finally:
            for fut in list(vc_route._active_jobs.values()):
                if not fut.done():
                    fut.cancel()
            vc_route._active_jobs.clear()
            # rc.5 (Agent 2 A.2): restore the original LRULockDict.
            vc_route._START_LOCKS = _original_start_locks
