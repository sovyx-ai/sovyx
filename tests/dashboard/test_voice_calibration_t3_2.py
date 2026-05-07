"""T3.2 mission tests — voice calibration wizard endpoints.

Mission: ``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 3.

Endpoints under ``/api/voice/calibration``:

* ``POST /start`` -- spawn job; HTTP 202
* ``GET /jobs/{id}`` -- snapshot; HTTP 200 / 404
* ``POST /jobs/{id}/cancel`` -- touch .cancel; HTTP 200 (idempotent)
* ``GET /preview-fingerprint`` -- HTTP 200 always (slow_path in v0.30.16)
* ``WS /jobs/{id}/stream?token=...`` -- live progress

Tests mock ``WizardOrchestrator.run`` so the endpoint exercises spawn()
without invoking the real 8-12 min calibration pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.voice.calibration import (
    HardwareFingerprint,
    WizardJobState,
    WizardProgressTracker,
    WizardStatus,
)

_TOKEN = "test-token-voice-calibration"  # noqa: S105 -- test fixture token


# ====================================================================
# Helpers
# ====================================================================


def _build_app(*, tmp_path: Path) -> Any:
    from sovyx.engine.config import DatabaseConfig, EngineConfig

    app = create_app(token=_TOKEN)
    app.state.engine_config = EngineConfig(
        data_dir=tmp_path,
        database=DatabaseConfig(data_dir=tmp_path),
    )
    return app


def _client(app: Any) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _fingerprint() -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-05T18:00:00Z",
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


def _seed_progress(
    *, data_dir: Path, mind_id: str, status: WizardStatus = WizardStatus.PENDING
) -> Path:
    """Hand-write a progress.jsonl entry so GET / cancel see a job."""
    job_dir = data_dir / "voice_calibration" / mind_id
    job_dir.mkdir(parents=True, exist_ok=True)
    tracker = WizardProgressTracker(job_dir / "progress.jsonl")
    state = WizardJobState(
        job_id=mind_id,
        mind_id=mind_id,
        status=status,
        progress=0.0,
        current_stage_message="seeded",
        created_at_utc="2026-05-05T18:00:00Z",
        updated_at_utc="2026-05-05T18:00:00Z",
    )
    tracker.append(state)
    return job_dir


# ====================================================================
# POST /start
# ====================================================================


class TestStartEndpoint:
    """POST /api/voice/calibration/start spawns a job; HTTP 202."""

    def test_start_returns_202_with_job_id_and_stream_url(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        # Mock orchestrator.run so spawn doesn't actually run the 8-12 min pipeline.
        with patch(
            "sovyx.dashboard.routes.voice_calibration.WizardOrchestrator.run",
            new=AsyncMock(),
        ):
            response = _client(app).post(
                "/api/voice/calibration/start",
                json={"mind_id": "default"},
            )
        assert response.status_code == 202
        body = response.json()
        assert body["job_id"] == "default"
        assert body["stream_url"] == "/api/voice/calibration/jobs/default/stream"

    def test_start_returns_409_when_job_already_in_flight(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        # Seed a non-terminal snapshot for the same mind.
        _seed_progress(data_dir=tmp_path, mind_id="default", status=WizardStatus.PROBING)
        response = _client(app).post(
            "/api/voice/calibration/start",
            json={"mind_id": "default"},
        )
        assert response.status_code == 409
        assert "in flight" in response.text.lower()

    def test_start_returns_401_without_auth(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        # No auth headers.
        client = TestClient(app)
        response = client.post(
            "/api/voice/calibration/start",
            json={"mind_id": "default"},
        )
        assert response.status_code == 401

    def test_start_returns_422_on_empty_mind_id(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        response = _client(app).post(
            "/api/voice/calibration/start",
            json={"mind_id": ""},
        )
        assert response.status_code == 422

    def test_start_after_terminal_job_is_permitted(self, tmp_path: Path) -> None:
        # Re-running calibration after a prior DONE/FAILED is allowed.
        app = _build_app(tmp_path=tmp_path)
        _seed_progress(data_dir=tmp_path, mind_id="default", status=WizardStatus.DONE)
        with patch(
            "sovyx.dashboard.routes.voice_calibration.WizardOrchestrator.run",
            new=AsyncMock(),
        ):
            response = _client(app).post(
                "/api/voice/calibration/start",
                json={"mind_id": "default"},
            )
        assert response.status_code == 202


# ====================================================================
# rc.12 (anti-pattern #35) — mind_id sentinel resolution
# ====================================================================


class TestStartEndpointMindIdResolution:
    """Frontend hardcodes ``mind_id="default"`` in onboarding +
    Settings; backend MUST resolve it to the real active mind via
    ``resolve_active_mind_id_for_request``. Else the calibration
    profile lands at ``<data_dir>/default/calibration.json`` even
    when the operator's actual mind is "meu-mind", silently breaking
    persistence on next ``sovyx start``.
    """

    def test_explicit_non_default_mind_id_skips_resolver(self, tmp_path: Path) -> None:
        """When the operator passes an explicit mind_id (not the
        sentinel), the resolver MUST NOT run — multi-mind operators
        explicitly targeting a specific mind get exactly that mind.
        """
        app = _build_app(tmp_path=tmp_path)
        with patch(
            "sovyx.dashboard.routes.voice_calibration.WizardOrchestrator.run",
            new=AsyncMock(),
        ):
            response = _client(app).post(
                "/api/voice/calibration/start",
                json={"mind_id": "meu-mind"},
            )
        assert response.status_code == 202
        body = response.json()
        assert body["job_id"] == "meu-mind"
        assert body["resolved_mind_id"] == "meu-mind"
        assert body["resolved_mind_id_source"] == "request_body"
        assert body["stream_url"] == "/api/voice/calibration/jobs/meu-mind/stream"

    def test_sentinel_default_no_registry_falls_back_to_default(self, tmp_path: Path) -> None:
        """No MindManager registered (fresh install): the resolver
        falls back to the literal "default" with source
        ``fallback_default``. Preserves pre-rc.12 behaviour for
        operators who haven't set up a mind yet.
        """
        app = _build_app(tmp_path=tmp_path)
        with patch(
            "sovyx.dashboard.routes.voice_calibration.WizardOrchestrator.run",
            new=AsyncMock(),
        ):
            response = _client(app).post(
                "/api/voice/calibration/start",
                json={"mind_id": "default"},
            )
        assert response.status_code == 202
        body = response.json()
        assert body["job_id"] == "default"
        assert body["resolved_mind_id"] == "default"
        assert body["resolved_mind_id_source"] == "fallback_default"

    def test_sentinel_default_resolves_via_app_state(self, tmp_path: Path) -> None:
        """When the dashboard cached the active mind on
        ``app.state.mind_id`` (the canonical post-T1.2 path), the
        sentinel resolves to that value. THIS IS THE FIX for the
        operator running ``sovyx init meu-mind && sovyx start`` and
        clicking onboarding step 4 — calibration MUST land at
        ``<data_dir>/meu-mind/`` not ``<data_dir>/default/``.
        """
        app = _build_app(tmp_path=tmp_path)
        # Simulate the dashboard server's startup-time mind cache.
        app.state.mind_id = "meu-mind"
        with patch(
            "sovyx.dashboard.routes.voice_calibration.WizardOrchestrator.run",
            new=AsyncMock(),
        ):
            response = _client(app).post(
                "/api/voice/calibration/start",
                json={"mind_id": "default"},
            )
        assert response.status_code == 202
        body = response.json()
        assert body["job_id"] == "meu-mind"
        assert body["resolved_mind_id"] == "meu-mind"
        assert body["resolved_mind_id_source"] == "app_state"
        # Stream URL uses the resolved mind_id, NOT the request body's
        # "default" — frontend's subsequent /jobs/{job_id}/* calls
        # operate on the real mind.
        assert body["stream_url"] == "/api/voice/calibration/jobs/meu-mind/stream"

    def test_sentinel_default_resolves_via_mind_manager(self, tmp_path: Path) -> None:
        """When no app_state cache but a live MindManager is registered,
        resolver walks ``MindManager.get_active_minds()``. Source
        ``mind_manager``.
        """
        from unittest.mock import MagicMock

        from sovyx.engine.bootstrap import MindManager
        from sovyx.engine.registry import ServiceRegistry

        app = _build_app(tmp_path=tmp_path)
        registry = ServiceRegistry()
        manager = MagicMock(spec=MindManager)
        manager.get_active_minds.return_value = ["meu-mind"]
        registry.register_instance(MindManager, manager)
        app.state.registry = registry
        with patch(
            "sovyx.dashboard.routes.voice_calibration.WizardOrchestrator.run",
            new=AsyncMock(),
        ):
            response = _client(app).post(
                "/api/voice/calibration/start",
                json={"mind_id": "default"},
            )
        assert response.status_code == 202
        body = response.json()
        assert body["job_id"] == "meu-mind"
        assert body["resolved_mind_id"] == "meu-mind"
        assert body["resolved_mind_id_source"] == "mind_manager"

    def test_409_message_uses_resolved_mind_id(self, tmp_path: Path) -> None:
        """The 409 conflict message MUST cite the RESOLVED mind_id so
        the operator sees the real mind name, not the sentinel
        ``default`` they never typed.
        """
        app = _build_app(tmp_path=tmp_path)
        app.state.mind_id = "meu-mind"
        # Seed a non-terminal job for the RESOLVED mind_id.
        _seed_progress(data_dir=tmp_path, mind_id="meu-mind", status=WizardStatus.PROBING)
        response = _client(app).post(
            "/api/voice/calibration/start",
            json={"mind_id": "default"},
        )
        assert response.status_code == 409
        assert "meu-mind" in response.text


# ====================================================================
# rc.12 — POST /rollback + GET /backups
# ====================================================================


def _seed_persisted_profile(data_dir: Path, mind_id: str, profile_id: str) -> None:
    """Helper: write a real CalibrationProfile to <data_dir>/<mind_id>/.

    Uses save_calibration_profile so the rotation chain is exercised
    end-to-end (legacy migration + .bak.N rotation).
    """
    from dataclasses import replace

    from sovyx.voice.calibration._persistence import save_calibration_profile
    from tests.unit.voice.calibration.test_persistence import _profile

    base = _profile(signature=None)
    save_calibration_profile(
        replace(base, mind_id=mind_id, profile_id=profile_id),
        data_dir=data_dir,
    )


class TestRollbackEndpoint:
    """rc.12 — ``POST /api/voice/calibration/rollback`` walks the
    multi-generation backup chain. Same sentinel-resolution contract
    as /start (anti-pattern #35)."""

    def test_rollback_409_when_chain_empty(self, tmp_path: Path) -> None:
        """No backups: 409 with operator-friendly message."""
        app = _build_app(tmp_path=tmp_path)
        # Save only one profile -- no .bak.1 yet (first save doesn't
        # rotate anything; the chain is built up by subsequent saves).
        _seed_persisted_profile(tmp_path, "default", "11111111-1111-1111-1111-111111111111")
        response = _client(app).post(
            "/api/voice/calibration/rollback",
            json={"mind_id": "default"},
        )
        assert response.status_code == 409
        assert "exhausted" in response.text.lower() or "nothing" in response.text.lower()

    def test_rollback_succeeds_with_chain(self, tmp_path: Path) -> None:
        """Two saves → 1 backup → rollback succeeds + remaining = 0."""
        app = _build_app(tmp_path=tmp_path)
        _seed_persisted_profile(tmp_path, "default", "11111111-1111-1111-1111-111111111111")
        _seed_persisted_profile(tmp_path, "default", "22222222-2222-2222-2222-222222222222")
        response = _client(app).post(
            "/api/voice/calibration/rollback",
            json={"mind_id": "default"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["backup_generations_remaining"] == 0
        assert body["restored_path"].endswith("calibration.json")
        assert body["resolved_mind_id"] == "default"

    def test_rollback_chain_can_be_consumed_multiple_times(self, tmp_path: Path) -> None:
        """4 saves → 3 backups → 3 successive rollbacks succeed,
        4th returns 409."""
        app = _build_app(tmp_path=tmp_path)
        for i in range(4):
            _seed_persisted_profile(tmp_path, "default", f"3333333{i}-3333-3333-3333-333333333333")
        client = _client(app)
        for expected_remaining in (2, 1, 0):
            response = client.post(
                "/api/voice/calibration/rollback",
                json={"mind_id": "default"},
            )
            assert response.status_code == 200
            assert response.json()["backup_generations_remaining"] == expected_remaining
        # 4th call → 409.
        response = client.post(
            "/api/voice/calibration/rollback",
            json={"mind_id": "default"},
        )
        assert response.status_code == 409

    def test_rollback_resolves_mind_id_sentinel(self, tmp_path: Path) -> None:
        """rc.12 + anti-pattern #35: rollback also resolves ``default``
        sentinel via the active-mind resolver. Frontend's hardcoded
        ``"default"`` rolls back the operator's actual mind."""
        app = _build_app(tmp_path=tmp_path)
        app.state.mind_id = "meu-mind"
        _seed_persisted_profile(tmp_path, "meu-mind", "44444444-4444-4444-4444-444444444444")
        _seed_persisted_profile(tmp_path, "meu-mind", "55555555-5555-5555-5555-555555555555")
        response = _client(app).post(
            "/api/voice/calibration/rollback",
            json={"mind_id": "default"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["resolved_mind_id"] == "meu-mind"
        assert body["resolved_mind_id_source"] == "app_state"
        assert "meu-mind" in body["restored_path"]

    def test_rollback_default_body_uses_sentinel(self, tmp_path: Path) -> None:
        """Empty body works because ``mind_id`` defaults to ``default``."""
        app = _build_app(tmp_path=tmp_path)
        _seed_persisted_profile(tmp_path, "default", "66666666-6666-6666-6666-666666666666")
        _seed_persisted_profile(tmp_path, "default", "77777777-7777-7777-7777-777777777777")
        response = _client(app).post("/api/voice/calibration/rollback", json={})
        assert response.status_code == 200

    def test_rollback_requires_auth(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        client_no_auth = TestClient(app)
        response = client_no_auth.post(
            "/api/voice/calibration/rollback", json={"mind_id": "default"}
        )
        assert response.status_code == 401


class TestBackupsEndpoint:
    """rc.12 — ``GET /api/voice/calibration/backups`` enumerates the
    chain so the dashboard's RollbackButton can render
    enabled/disabled correctly without a wasted POST."""

    def test_backups_empty_on_fresh_install(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        response = _client(app).get("/api/voice/calibration/backups")
        assert response.status_code == 200
        body = response.json()
        assert body["generations"] == []

    def test_backups_lists_all_chain_generations(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        for i in range(4):  # 1 initial + 3 rotations = chain full at 3 gens
            _seed_persisted_profile(tmp_path, "default", f"8888888{i}-8888-8888-8888-888888888888")
        response = _client(app).get("/api/voice/calibration/backups")
        assert response.status_code == 200
        body = response.json()
        assert body["generations"] == [1, 2, 3]
        assert body["mind_id"] == "default"

    def test_backups_resolves_mind_id_sentinel(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        app.state.mind_id = "meu-mind"
        _seed_persisted_profile(tmp_path, "meu-mind", "99999999-9999-9999-9999-999999999999")
        _seed_persisted_profile(tmp_path, "meu-mind", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        response = _client(app).get("/api/voice/calibration/backups")
        assert response.status_code == 200
        body = response.json()
        assert body["mind_id"] == "meu-mind"
        assert body["generations"] == [1]

    def test_backups_requires_auth(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        client_no_auth = TestClient(app)
        response = client_no_auth.get("/api/voice/calibration/backups")
        assert response.status_code == 401


# ====================================================================
# GET /jobs/{id}
# ====================================================================


class TestGetJobEndpoint:
    """GET /api/voice/calibration/jobs/{id} returns snapshot or 404."""

    def test_get_returns_200_with_snapshot(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        _seed_progress(data_dir=tmp_path, mind_id="default")
        response = _client(app).get("/api/voice/calibration/jobs/default")
        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == "default"
        assert body["status"] == "pending"

    def test_get_returns_404_when_no_progress(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        response = _client(app).get("/api/voice/calibration/jobs/ghost")
        assert response.status_code == 404


# ====================================================================
# POST /jobs/{id}/cancel
# ====================================================================


class TestCancelEndpoint:
    """POST /api/voice/calibration/jobs/{id}/cancel touches .cancel; idempotent."""

    def test_cancel_creates_cancel_file(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        _seed_progress(data_dir=tmp_path, mind_id="default")
        response = _client(app).post("/api/voice/calibration/jobs/default/cancel")
        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == "default"
        assert body["cancel_signal_written"] is True
        assert body["already_terminal"] is False
        assert (tmp_path / "voice_calibration" / "default" / ".cancel").exists()

    def test_cancel_is_idempotent(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        _seed_progress(data_dir=tmp_path, mind_id="default")
        client = _client(app)
        client.post("/api/voice/calibration/jobs/default/cancel")
        response = client.post("/api/voice/calibration/jobs/default/cancel")
        assert response.status_code == 200

    def test_cancel_already_terminal_reports_so(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        _seed_progress(data_dir=tmp_path, mind_id="default", status=WizardStatus.DONE)
        response = _client(app).post("/api/voice/calibration/jobs/default/cancel")
        assert response.status_code == 200
        assert response.json()["already_terminal"] is True

    def test_cancel_creates_dir_for_unknown_job(self, tmp_path: Path) -> None:
        # Cancel for a job that doesn't exist still writes the .cancel
        # file (creates the dir on demand). Operator's intent is signal-
        # writing; orchestrator would no-op if it never starts.
        app = _build_app(tmp_path=tmp_path)
        response = _client(app).post("/api/voice/calibration/jobs/unborn/cancel")
        assert response.status_code == 200


# ====================================================================
# GET /preview-fingerprint
# ====================================================================


class TestPreviewFingerprintEndpoint:
    """GET /api/voice/calibration/preview-fingerprint returns slow_path in v0.30.16."""

    def test_preview_returns_fingerprint_and_recommendation(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        with patch(
            "sovyx.dashboard.routes.voice_calibration.capture_fingerprint",
            return_value=_fingerprint(),
        ):
            response = _client(app).get("/api/voice/calibration/preview-fingerprint")
        assert response.status_code == 200
        body = response.json()
        assert body["fingerprint_hash"] == _fingerprint().fingerprint_hash
        assert body["audio_stack"] == "pipewire"
        assert body["system_vendor"] == "Sony"
        assert body["system_product"] == "VAIO"
        # v0.30.16 always slow_path.
        assert body["recommendation"] == "slow_path"


# ====================================================================
# WebSocket stream
# ====================================================================


class TestStreamWebSocket:
    """WS /api/voice/calibration/jobs/{id}/stream emits progress events."""

    def test_ws_emits_seeded_events_and_closes_on_terminal(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        _seed_progress(data_dir=tmp_path, mind_id="default", status=WizardStatus.DONE)
        client = TestClient(app)
        with client.websocket_connect(
            f"/api/voice/calibration/jobs/default/stream?token={_TOKEN}"
        ) as ws:
            msg = ws.receive_json()
            assert msg["job_id"] == "default"
            assert msg["status"] == "done"
        # Connection closes after terminal -- receive raises on next call.

    def test_ws_rejects_bad_token(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        _seed_progress(data_dir=tmp_path, mind_id="default")
        client = TestClient(app)
        # WebSocketTestSession raises WebSocketDisconnect on bad-token
        # close (1008). We catch + verify the close happened.
        from starlette.websockets import WebSocketDisconnect

        try:
            with client.websocket_connect(
                "/api/voice/calibration/jobs/default/stream?token=wrong"
            ) as ws:
                ws.receive_json()
        except WebSocketDisconnect as exc:
            assert exc.code == 1008
        else:
            raise AssertionError("Expected WebSocketDisconnect on bad token")


# ====================================================================
# GET / POST /feature-flag (T3.10 wire-up, v0.30.22)
# ====================================================================


class TestFeatureFlagEndpoints:
    """GET returns the boot value; POST mutates the in-memory copy."""

    def test_get_returns_default_true(self, tmp_path: Path) -> None:
        """rc.10 (Agent 2 fix #1): default flipped from False → True per
        config docstring's own promise. Fresh-user dashboard onboarding
        now mounts the auto-fix calibration wizard automatically.
        """
        app = _build_app(tmp_path=tmp_path)
        response = _client(app).get("/api/voice/calibration/feature-flag")
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["runtime_override_active"] is False

    def test_get_reflects_engine_config_value(self, tmp_path: Path) -> None:
        from sovyx.engine.config import DatabaseConfig, EngineConfig, VoiceFeaturesConfig

        app = create_app(token=_TOKEN)
        app.state.engine_config = EngineConfig(
            data_dir=tmp_path,
            database=DatabaseConfig(data_dir=tmp_path),
            voice=VoiceFeaturesConfig(calibration_wizard_enabled=True),
        )
        response = _client(app).get("/api/voice/calibration/feature-flag")
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["runtime_override_active"] is False

    def test_post_flips_in_memory_value(self, tmp_path: Path) -> None:
        """rc.10: default is now True (Agent 2 fix #1). Test now flips
        OFF (True → False) to exercise the runtime-override path —
        matches the operator-realistic case of "I have hardware that
        doesn't need the wizard, so I'm opting out".
        """
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        # Initial: enabled (rc.10 default)
        initial = client.get("/api/voice/calibration/feature-flag")
        assert initial.json()["enabled"] is True
        # POST flips to disabled
        response = client.post("/api/voice/calibration/feature-flag", json={"enabled": False})
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        assert body["runtime_override_active"] is True

        # Subsequent GET reflects the runtime override
        response2 = client.get("/api/voice/calibration/feature-flag")
        assert response2.json()["enabled"] is False
        assert response2.json()["runtime_override_active"] is True

    def test_post_returns_404_when_no_engine_config(self, tmp_path: Path) -> None:
        # App without engine_config registered.
        app = create_app(token=_TOKEN)
        response = _client(app).post("/api/voice/calibration/feature-flag", json={"enabled": True})
        assert response.status_code == 404
        _ = tmp_path  # unused

    def test_endpoints_require_auth(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        client_no_auth = TestClient(app)
        get_response = client_no_auth.get("/api/voice/calibration/feature-flag")
        assert get_response.status_code == 401
        post_response = client_no_auth.post(
            "/api/voice/calibration/feature-flag", json={"enabled": True}
        )
        assert post_response.status_code == 401

    def test_get_returns_platform_supported_true_on_linux(self, tmp_path: Path) -> None:
        """rc.11 (EIXO 2): platform_supported field gates the wizard mount.

        On Linux the bash diag toolkit can run, so the field is True and
        the frontend mounts the wizard normally.
        """
        from sovyx.dashboard.routes import voice_calibration as vc_route

        app = _build_app(tmp_path=tmp_path)
        with patch.object(vc_route.sys, "platform", "linux"):
            response = _client(app).get("/api/voice/calibration/feature-flag")
        assert response.status_code == 200
        body = response.json()
        assert body["platform_supported"] is True

    def test_get_returns_platform_supported_false_on_windows(self, tmp_path: Path) -> None:
        """rc.11 (EIXO 2): on Windows the bash diag toolkit cannot run
        (DiagPrerequisiteError at ``_runner.py:_check_prerequisites``).

        The frontend MUST gate the wizard mount on the conjunction
        ``enabled AND platform_supported`` so Windows operators don't
        see a wizard that immediately falls through to FALLBACK.
        """
        from sovyx.dashboard.routes import voice_calibration as vc_route

        app = _build_app(tmp_path=tmp_path)
        with patch.object(vc_route.sys, "platform", "win32"):
            response = _client(app).get("/api/voice/calibration/feature-flag")
        assert response.status_code == 200
        body = response.json()
        assert body["platform_supported"] is False
        # `enabled` (the wizard mount intent) is independent of platform
        # support — operators who flipped the flag stay flipped; the
        # frontend short-circuits on platform_supported only.
        assert body["enabled"] is True

    def test_get_returns_platform_supported_false_on_macos(self, tmp_path: Path) -> None:
        """rc.11 (EIXO 2): macOS is also non-Linux; same gate applies."""
        from sovyx.dashboard.routes import voice_calibration as vc_route

        app = _build_app(tmp_path=tmp_path)
        with patch.object(vc_route.sys, "platform", "darwin"):
            response = _client(app).get("/api/voice/calibration/feature-flag")
        assert response.status_code == 200
        body = response.json()
        assert body["platform_supported"] is False

    def test_post_returns_platform_supported(self, tmp_path: Path) -> None:
        """rc.11 (EIXO 2): POST response also carries platform_supported
        so the dashboard's runtime-toggle UX surfaces the same gate.
        """
        from sovyx.dashboard.routes import voice_calibration as vc_route

        app = _build_app(tmp_path=tmp_path)
        with patch.object(vc_route.sys, "platform", "darwin"):
            response = _client(app).post(
                "/api/voice/calibration/feature-flag", json={"enabled": False}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["platform_supported"] is False
        assert body["enabled"] is False

    def test_get_no_engine_config_still_returns_platform_supported(self, tmp_path: Path) -> None:
        """rc.11 (EIXO 2): the no-engine-config fallback path also
        carries platform_supported so the frontend's gate works on a
        dashboard whose daemon never registered a config.
        """
        from sovyx.dashboard.routes import voice_calibration as vc_route

        app = create_app(token=_TOKEN)
        with patch.object(vc_route.sys, "platform", "win32"):
            response = _client(app).get("/api/voice/calibration/feature-flag")
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        assert body["platform_supported"] is False
        _ = tmp_path  # unused
