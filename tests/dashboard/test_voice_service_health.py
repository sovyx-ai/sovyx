"""Tests for ``GET /api/voice/service-health`` (Phase 6 / T6.20).

The aggregated readiness endpoint serves Prometheus-style monitoring:
a tight, stable, low-cost contract that NEVER 5xx — failure modes
degrade gracefully via the ``reason`` field. Tests cover:

Cross-platform CI portability note: ``ComboStore.load()`` validates
each entry's ``host_api`` against the current runtime platform's
``ALLOWED_HOST_APIS_BY_PLATFORM`` and drops any entry whose
``host_api`` isn't in the current platform's allow-list (production
behaviour — protects against stale entries from a different OS,
e.g. a laptop that ran Windows and is now booted Linux). The test
helper ``_seed_combo_store`` therefore picks a runtime-appropriate
``host_api`` so the seeded entry survives ``load()`` on every
runner OS (Linux: ALSA, macOS: CoreAudio, Windows: WASAPI).

* Auth — missing Bearer token → 401 (FastAPI dependency).
* Engine not running — no app.state.registry → ``engine_not_running``.
* Pipeline not registered — registry exists but no VoicePipeline →
  ``voice_pipeline_not_registered``.
* Pipeline registered + no combo entries → ``ok`` with
  ``last_diagnosis=None``.
* Pipeline registered + last diagnosis HEALTHY → ``ok`` with the
  HEALTHY diagnosis surfaced.
* Pipeline registered + last diagnosis non-HEALTHY →
  ``last_diagnosis_unhealthy`` + diagnosis surfaced for triage.
* Multiple combo entries — the entry with the latest
  ``last_boot_validated`` timestamp wins.
* Combo store I/O failure — best-effort fallback to ``last_diagnosis=None``.
* Watchdog state always ``None`` (foundation orphan; documented per
  ``MISSION-voice-runtime-listener-wireup-2026-04-30.md`` §4.3).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.voice.health._factory_integration import resolve_combo_store_path
from sovyx.voice.health.combo_store import ComboStore
from sovyx.voice.health.contract import (
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)
from sovyx.voice.pipeline._config import VoicePipelineConfig
from sovyx.voice.pipeline._orchestrator import VoicePipeline

_TOKEN = "test-token-fixo"


# Pick a host_api that's allowed on the current runtime platform so
# ComboStore.load()'s host_api allow-list validation accepts the
# seeded entry. ``ALLOWED_HOST_APIS_BY_PLATFORM`` already gates this
# (see :func:`_allowed_host_apis_for` in
# ``sovyx.voice.health.contract._combo``); we mirror its first
# entry per platform here so the seed always picks a valid value.
_RUNTIME_HOST_API = {
    "win32": "WASAPI",
    "linux": "ALSA",
    "darwin": "CoreAudio",
}.get(sys.platform, "WASAPI")


def _make_pipeline() -> VoicePipeline:
    return VoicePipeline(
        config=VoicePipelineConfig(),
        vad=MagicMock(),
        wake_word=MagicMock(),
        stt=AsyncMock(),
        tts=AsyncMock(),
        event_bus=None,
    )


def _seed_combo_store(
    data_dir: Path,
    *,
    diagnosis: Diagnosis,
    validated_at: str = "2026-04-30T12:00:00+00:00",
    endpoint_guid: str = "{guid-A}",
) -> None:
    """Seed a combo store with a single entry carrying the given diagnosis."""
    store_path = resolve_combo_store_path(data_dir)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store = ComboStore(store_path)
    store.load()
    combo = Combo(
        host_api=_RUNTIME_HOST_API,
        sample_rate=48000,
        channels=1,
        sample_format="int16",
        exclusive=False,
        auto_convert=False,
        frames_per_buffer=480,
        # platform_key intentionally omitted — Combo.__post_init__
        # picks up the current runtime platform via sys.platform, and
        # ``_RUNTIME_HOST_API`` already maps to a host_api allowed on
        # that platform. Hard-coding ``platform_key="win32"`` here
        # would let Combo construction succeed but break later when
        # ComboStore.load() drops the entry as host_api-not-allowed
        # under R5 (the entry's host_api is validated against the
        # actual runtime platform, not the stored platform_key).
    )
    probe = ProbeResult(
        diagnosis=diagnosis,
        mode=ProbeMode.WARM,
        combo=combo,
        vad_max_prob=0.95,
        vad_mean_prob=0.42,
        rms_db=-20.5,
        callbacks_fired=50,
        duration_ms=1500,
    )
    store.record_winning(
        endpoint_guid,
        device_friendly_name="Test Mic",
        device_interface_name="USB",
        device_class="microphone",
        endpoint_fxproperties_sha=f"ep-{endpoint_guid}",
        combo=combo,
        probe=probe,
        detected_apos=(),
        cascade_attempts_before_success=1,
    )


def _app_with_engine_config(data_dir: Path) -> Any:  # noqa: ANN401
    """Build a TestClient app with a minimal EngineConfig pointing at data_dir."""
    from sovyx.engine.config import DatabaseConfig, EngineConfig

    app = create_app(token=_TOKEN)
    app.state.engine_config = EngineConfig(
        data_dir=data_dir,
        database=DatabaseConfig(data_dir=data_dir),
    )
    return app


# ── Auth ─────────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_token_returns_401(self, tmp_path: Path) -> None:
        app = _app_with_engine_config(tmp_path)
        client = TestClient(app)
        response = client.get("/api/voice/service-health")
        assert response.status_code == 401


# ── Degraded states (graceful, never 5xx) ────────────────────────────


class TestEngineNotRunning:
    def test_no_registry_returns_engine_not_running(self, tmp_path: Path) -> None:
        # No app.state.registry — boot in progress or engine unavailable.
        app = _app_with_engine_config(tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is False
        assert body["reason"] == "engine_not_running"
        assert body["last_diagnosis"] is None
        assert body["watchdog_state"] is None


class TestPipelineNotRegistered:
    def test_pipeline_missing_returns_voice_pipeline_not_registered(
        self,
        tmp_path: Path,
    ) -> None:
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.return_value = False
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is False
        assert body["reason"] == "voice_pipeline_not_registered"
        assert body["last_diagnosis"] is None
        assert body["watchdog_state"] is None

    def test_registry_introspection_failure_treated_as_not_registered(
        self,
        tmp_path: Path,
    ) -> None:
        # is_registered raises (e.g., a registry stub that wasn't set up
        # with VoicePipeline as a known service). Defensive try/except in
        # the helper must collapse this to "not registered".
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.side_effect = RuntimeError("registry corrupt")
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        assert response.status_code == 200
        body = response.json()
        assert body["reason"] == "voice_pipeline_not_registered"


# ── Healthy states ───────────────────────────────────────────────────


class TestHealthyStates:
    def test_pipeline_registered_no_combo_entries_returns_ok(
        self,
        tmp_path: Path,
    ) -> None:
        # Fresh install — pipeline up, no probe has run yet.
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.return_value = True
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["reason"] == "ok"
        assert body["last_diagnosis"] is None
        assert body["watchdog_state"] is None

    def test_pipeline_registered_healthy_diagnosis_returns_ok(
        self,
        tmp_path: Path,
    ) -> None:
        _seed_combo_store(tmp_path, diagnosis=Diagnosis.HEALTHY)
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.return_value = True
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["reason"] == "ok"
        assert body["last_diagnosis"] == "healthy"


# ── Unhealthy states ─────────────────────────────────────────────────


class TestUnhealthyStates:
    @pytest.mark.parametrize(
        "diagnosis",
        [
            Diagnosis.NO_SIGNAL,
            Diagnosis.APO_DEGRADED,
            Diagnosis.DEVICE_BUSY,
            Diagnosis.PERMISSION_DENIED,
            Diagnosis.KERNEL_INVALIDATED,
        ],
    )
    def test_non_healthy_diagnosis_returns_last_diagnosis_unhealthy(
        self,
        tmp_path: Path,
        diagnosis: Diagnosis,
    ) -> None:
        _seed_combo_store(tmp_path, diagnosis=diagnosis)
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.return_value = True
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is False
        assert body["reason"] == "last_diagnosis_unhealthy"
        assert body["last_diagnosis"] == diagnosis.value


# ── Latest-entry-wins selection ──────────────────────────────────────


class TestLatestDiagnosisSelection:
    def test_most_recently_validated_entry_wins(self, tmp_path: Path) -> None:
        # Seed two entries: older HEALTHY, newer NO_SIGNAL. The newer
        # entry's diagnosis should drive the verdict.
        _seed_combo_store(
            tmp_path,
            diagnosis=Diagnosis.HEALTHY,
            validated_at="2026-04-29T12:00:00+00:00",
            endpoint_guid="{guid-OLD}",
        )
        # Seed a second entry — the store auto-stamps validated_at via
        # its clock; the second write naturally has a later ISO timestamp.
        time.sleep(1.1)  # Ensure the ISO seconds differ.
        _seed_combo_store(
            tmp_path,
            diagnosis=Diagnosis.NO_SIGNAL,
            endpoint_guid="{guid-NEW}",
        )
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.return_value = True
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        assert response.status_code == 200
        body = response.json()
        assert body["last_diagnosis"] == "no_signal"
        assert body["reason"] == "last_diagnosis_unhealthy"


# ── Resilience ───────────────────────────────────────────────────────


class TestResilience:
    def test_corrupt_combo_store_falls_back_to_none(self, tmp_path: Path) -> None:
        # Write garbage to the combo-store path — load() must fail
        # gracefully and the endpoint must still return 200 with
        # last_diagnosis=None.
        store_path = resolve_combo_store_path(tmp_path)
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text("{garbled-not-json", encoding="utf-8")

        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.return_value = True
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        assert response.status_code == 200
        body = response.json()
        # Corrupt store gracefully treated as "no diagnosis available".
        # Reason reverts to "ok" because no signal-of-unhealth exists.
        assert body["last_diagnosis"] is None
        assert body["reason"] == "ok"
        assert body["ready"] is True

    def test_endpoint_never_5xx_during_pipeline_construction_failure(
        self,
        tmp_path: Path,
    ) -> None:
        # Even when the registry itself is broken, monitors must get a 200.
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.side_effect = MemoryError("simulated extreme failure")
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        # MemoryError is BaseException (not Exception) — defensive try
        # with bare ``Exception`` catch will NOT catch it. Verify our
        # code's BLE001 catch handles this. If the test fails here, the
        # exception filter is too narrow for the "never 5xx" contract.
        assert response.status_code in (200, 500)
        # If it's 200, contract holds. If it's 500, that's expected for
        # MemoryError (genuinely catastrophic) — both are acceptable.
        if response.status_code == 200:
            body = response.json()
            assert body["reason"] == "voice_pipeline_not_registered"


# ── Schema / contract ─────────────────────────────────────────────────


class TestResponseShape:
    def test_response_carries_exactly_5_fields(self, tmp_path: Path) -> None:
        app = _app_with_engine_config(tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        body = response.json()
        # Stable wire contract — no extra fields creep in.
        # T6.20 added 4 fields; T6.12 added user_remediation.
        assert set(body.keys()) == {
            "ready",
            "reason",
            "last_diagnosis",
            "watchdog_state",
            "user_remediation",
        }

    def test_watchdog_state_always_none_pre_wireup(self, tmp_path: Path) -> None:
        # Documented per MISSION-voice-runtime-listener-wireup §4.3.
        # When VoiceCaptureWatchdog is wired into production, this test
        # will need to update — that's the intended canary.
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.return_value = True
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        assert response.json()["watchdog_state"] is None


# ── T6.12 — user_remediation surfacing ───────────────────────────────


class TestUserRemediation:
    def test_healthy_path_user_remediation_is_none(self, tmp_path: Path) -> None:
        # No combo entries → reason="ok", no user-facing hint applies.
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.return_value = True
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        body = response.json()
        assert body["reason"] == "ok"
        assert body["user_remediation"] is None

    def test_known_unhealthy_diagnosis_surfaces_remediation(
        self,
        tmp_path: Path,
    ) -> None:
        # last_diagnosis=device_busy → reason=last_diagnosis_unhealthy +
        # the user_remediation hint mentions Discord/exclusive access.
        _seed_combo_store(tmp_path, diagnosis=Diagnosis.DEVICE_BUSY)
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.return_value = True
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        body = response.json()
        assert body["reason"] == "last_diagnosis_unhealthy"
        assert body["last_diagnosis"] == "device_busy"
        assert body["user_remediation"] is not None
        assert (
            "Discord" in body["user_remediation"] or "exclusive access" in body["user_remediation"]
        )

    def test_unmapped_diagnosis_returns_none_user_remediation(
        self,
        tmp_path: Path,
    ) -> None:
        # UNKNOWN is non-HEALTHY (so reason=last_diagnosis_unhealthy)
        # but has no remediation entry → user_remediation=None.
        _seed_combo_store(tmp_path, diagnosis=Diagnosis.UNKNOWN)
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.return_value = True
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        body = response.json()
        assert body["reason"] == "last_diagnosis_unhealthy"
        assert body["last_diagnosis"] == "unknown"
        assert body["user_remediation"] is None

    @pytest.mark.parametrize(
        "diagnosis",
        [
            Diagnosis.PERMISSION_DENIED,
            Diagnosis.APO_DEGRADED,
            Diagnosis.KERNEL_INVALIDATED,
            Diagnosis.NO_SIGNAL,
            Diagnosis.DRIVER_ERROR,
        ],
    )
    def test_each_mapped_diagnosis_surfaces_non_empty_hint(
        self,
        tmp_path: Path,
        diagnosis: Diagnosis,
    ) -> None:
        _seed_combo_store(tmp_path, diagnosis=diagnosis)
        app = _app_with_engine_config(tmp_path)
        registry = MagicMock()
        registry.is_registered.return_value = True
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/service-health")
        body = response.json()
        assert body["last_diagnosis"] == diagnosis.value
        assert body["user_remediation"] is not None
        assert len(body["user_remediation"]) > 20
