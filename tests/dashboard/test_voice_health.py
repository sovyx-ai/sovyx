"""Tests for ``/api/voice/health*`` — the L7 REST surface (ADR §4.7)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.voice.health import (
    CaptureOverrides,
    Combo,
    ComboStore,
    Diagnosis,
    EndpointQuarantine,
    ProbeMode,
    ProbeResult,
    RemediationHint,
)
from sovyx.voice.health._factory_integration import (
    resolve_capture_overrides_path,
    resolve_combo_store_path,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

_TOKEN = "test-token-voice-health"


def _current_platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


_PLATFORM_KEY = _current_platform_key()
_HOST_API = {"win32": "WASAPI", "linux": "ALSA", "darwin": "CoreAudio"}[_PLATFORM_KEY]


def _make_combo() -> Combo:
    return Combo(
        host_api=_HOST_API,
        sample_rate=16_000,
        channels=1,
        sample_format="int16",
        exclusive=True,
        auto_convert=False,
        frames_per_buffer=480,
        platform_key=_PLATFORM_KEY,
    )


def _seed_combo_store(data_dir: Path, *, endpoint_guid: str = "EP-A") -> None:
    """Persist one ComboEntry via the canonical ``record_winning`` path."""
    path = resolve_combo_store_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = ComboStore(path)
    store.load()
    probe_result = ProbeResult(
        diagnosis=Diagnosis.HEALTHY,
        mode=ProbeMode.COLD,
        combo=_make_combo(),
        vad_max_prob=None,
        vad_mean_prob=None,
        rms_db=-32.0,
        callbacks_fired=10,
        duration_ms=1500,
    )
    store.record_winning(
        endpoint_guid,
        device_friendly_name="Test Mic",
        device_interface_name=rf"\\?\SWD#MMDEVAPI#{endpoint_guid}",
        device_class="capture",
        endpoint_fxproperties_sha="sha-fx",
        combo=_make_combo(),
        probe=probe_result,
        detected_apos=(),
        cascade_attempts_before_success=1,
    )


def _seed_capture_overrides(data_dir: Path, *, endpoint_guid: str = "EP-B") -> None:
    """Pin a combo via the canonical ``CaptureOverrides.pin`` path."""
    path = resolve_capture_overrides_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    overrides = CaptureOverrides(path)
    overrides.load()
    overrides.pin(
        endpoint_guid,
        device_friendly_name="Pinned Mic",
        combo=_make_combo(),
        source="user",
        reason="test-seed",
    )


def _canned_probe_result(
    diagnosis: Diagnosis = Diagnosis.HEALTHY,
) -> ProbeResult:
    return ProbeResult(
        diagnosis=diagnosis,
        mode=ProbeMode.COLD,
        combo=_make_combo(),
        vad_max_prob=None,
        vad_mean_prob=None,
        rms_db=-30.0,
        callbacks_fired=10,
        duration_ms=1500,
        error=None,
        remediation=RemediationHint(code="remediation.ok", severity="info"),
    )


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def app(data_dir: Path) -> FastAPI:
    application = create_app(token=_TOKEN)
    registry = MagicMock()
    registry.is_registered.return_value = False
    registry.resolve = AsyncMock()
    application.state.registry = registry
    application.state.engine_config = SimpleNamespace(
        database=SimpleNamespace(data_dir=data_dir),
    )
    return application


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class TestAuth:
    """All endpoints require the Bearer token."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/voice/health"),
            ("POST", "/api/voice/health/reprobe"),
            ("POST", "/api/voice/health/forget"),
            ("POST", "/api/voice/health/pin"),
        ],
    )
    def test_requires_bearer_token(
        self,
        app: FastAPI,
        method: str,
        path: str,
    ) -> None:
        unauth = TestClient(app)
        resp = unauth.request(method, path, json={})
        assert resp.status_code == 401  # noqa: PLR2004


class TestGetSnapshot:
    """GET /api/voice/health."""

    def test_empty_snapshot(self, client: TestClient, data_dir: Path) -> None:
        resp = client.get("/api/voice/health")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["combo_store"] == []
        assert body["overrides"] == []
        assert body["voice_enabled"] is False
        assert body["data_dir"] == str(data_dir)

    def test_returns_store_entries(self, client: TestClient, data_dir: Path) -> None:
        _seed_combo_store(data_dir, endpoint_guid="EP-SEED")
        resp = client.get("/api/voice/health")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert len(body["combo_store"]) == 1
        entry = body["combo_store"][0]
        assert entry["endpoint_guid"] == "EP-SEED"
        assert entry["winning_combo"]["sample_rate"] == 16_000  # noqa: PLR2004
        assert entry["pinned"] is False

    def test_returns_override_entries(
        self,
        client: TestClient,
        data_dir: Path,
    ) -> None:
        _seed_capture_overrides(data_dir, endpoint_guid="EP-OVR")
        resp = client.get("/api/voice/health")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert len(body["overrides"]) == 1
        assert body["overrides"][0]["endpoint_guid"] == "EP-OVR"
        assert body["overrides"][0]["pinned_by"] == "user"

    def test_voice_enabled_when_capture_task_registered(
        self,
        app: FastAPI,
        client: TestClient,
    ) -> None:
        app.state.registry.is_registered.return_value = True
        resp = client.get("/api/voice/health")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["voice_enabled"] is True


class TestReprobe:
    """POST /api/voice/health/reprobe."""

    def test_cold_with_explicit_combo_runs_probe(
        self,
        client: TestClient,
    ) -> None:
        probe_mock = AsyncMock(return_value=_canned_probe_result())
        body = {
            "endpoint_guid": "EP-X",
            "device_index": 3,
            "mode": "cold",
            "combo": {
                "host_api": _HOST_API,
                "sample_rate": 16_000,
                "channels": 1,
                "sample_format": "int16",
                "exclusive": True,
                "auto_convert": False,
                "frames_per_buffer": 480,
            },
        }
        with patch("sovyx.dashboard.routes.voice_health.probe", probe_mock):
            resp = client.post("/api/voice/health/reprobe", json=body)
        assert resp.status_code == 200  # noqa: PLR2004
        payload = resp.json()
        assert payload["endpoint_guid"] == "EP-X"
        assert payload["result"]["diagnosis"] == Diagnosis.HEALTHY.value
        probe_mock.assert_awaited_once()
        kwargs = probe_mock.await_args.kwargs
        assert kwargs["mode"] is ProbeMode.COLD
        assert kwargs["device_index"] == 3  # noqa: PLR2004
        assert kwargs["vad"] is None

    def test_cold_without_combo_and_no_history_returns_404(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/api/voice/health/reprobe",
            json={"endpoint_guid": "EP-NONE", "device_index": 0, "mode": "cold"},
        )
        assert resp.status_code == 404  # noqa: PLR2004

    def test_cold_without_combo_uses_stored_entry(
        self,
        client: TestClient,
        data_dir: Path,
    ) -> None:
        _seed_combo_store(data_dir, endpoint_guid="EP-HIST")
        probe_mock = AsyncMock(return_value=_canned_probe_result())
        with patch("sovyx.dashboard.routes.voice_health.probe", probe_mock):
            resp = client.post(
                "/api/voice/health/reprobe",
                json={
                    "endpoint_guid": "EP-HIST",
                    "device_index": 0,
                    "mode": "cold",
                },
            )
        assert resp.status_code == 200  # noqa: PLR2004
        combo_arg = probe_mock.await_args.kwargs["combo"]
        assert combo_arg.sample_rate == 16_000  # noqa: PLR2004

    def test_warm_without_registered_vad_returns_409(
        self,
        app: FastAPI,
        client: TestClient,
        data_dir: Path,
    ) -> None:
        _seed_combo_store(data_dir, endpoint_guid="EP-WARM")
        app.state.registry.is_registered.return_value = False
        resp = client.post(
            "/api/voice/health/reprobe",
            json={
                "endpoint_guid": "EP-WARM",
                "device_index": 0,
                "mode": "warm",
            },
        )
        assert resp.status_code == 409  # noqa: PLR2004

    def test_warm_with_registered_vad_runs_probe(
        self,
        app: FastAPI,
        client: TestClient,
        data_dir: Path,
    ) -> None:
        _seed_combo_store(data_dir, endpoint_guid="EP-WARM-OK")
        fake_vad = MagicMock(name="SileroVAD")
        app.state.registry.is_registered.return_value = True
        app.state.registry.resolve = AsyncMock(return_value=fake_vad)
        probe_mock = AsyncMock(return_value=_canned_probe_result())
        with patch("sovyx.dashboard.routes.voice_health.probe", probe_mock):
            resp = client.post(
                "/api/voice/health/reprobe",
                json={
                    "endpoint_guid": "EP-WARM-OK",
                    "device_index": 0,
                    "mode": "warm",
                },
            )
        assert resp.status_code == 200  # noqa: PLR2004
        kwargs = probe_mock.await_args.kwargs
        assert kwargs["mode"] is ProbeMode.WARM
        assert kwargs["vad"] is fake_vad

    def test_probe_raises_returns_503(
        self,
        client: TestClient,
        data_dir: Path,
    ) -> None:
        _seed_combo_store(data_dir, endpoint_guid="EP-BOOM")
        probe_mock = AsyncMock(side_effect=RuntimeError("portaudio down"))
        with patch("sovyx.dashboard.routes.voice_health.probe", probe_mock):
            resp = client.post(
                "/api/voice/health/reprobe",
                json={
                    "endpoint_guid": "EP-BOOM",
                    "device_index": 0,
                    "mode": "cold",
                },
            )
        assert resp.status_code == 503  # noqa: PLR2004

    def test_invalid_combo_returns_409(self, client: TestClient) -> None:
        resp = client.post(
            "/api/voice/health/reprobe",
            json={
                "endpoint_guid": "EP-BAD",
                "device_index": 0,
                "mode": "cold",
                "combo": {
                    "host_api": _HOST_API,
                    "sample_rate": 9_999,  # not in ALLOWED_SAMPLE_RATES
                    "channels": 1,
                    "sample_format": "int16",
                    "exclusive": True,
                    "auto_convert": False,
                    "frames_per_buffer": 480,
                },
            },
        )
        assert resp.status_code == 409  # noqa: PLR2004

    def test_device_index_omitted_resolves_from_friendly_name(
        self,
        client: TestClient,
        data_dir: Path,
    ) -> None:
        """Dashboard reprobe sends no ``device_index``; backend must resolve
        it from the ComboEntry's friendly name via PortAudio (Bug 001)."""
        _seed_combo_store(data_dir, endpoint_guid="EP-RESOLVE")
        probe_mock = AsyncMock(return_value=_canned_probe_result())
        resolver = MagicMock(return_value=7)
        with (
            patch("sovyx.dashboard.routes.voice_health.probe", probe_mock),
            patch(
                "sovyx.dashboard.routes.voice_health._lookup_device_index_by_name",
                resolver,
            ),
        ):
            resp = client.post(
                "/api/voice/health/reprobe",
                json={"endpoint_guid": "EP-RESOLVE", "mode": "cold"},
            )
        assert resp.status_code == 200  # noqa: PLR2004
        resolver.assert_called_once_with("Test Mic")
        assert probe_mock.await_args.kwargs["device_index"] == 7  # noqa: PLR2004

    def test_device_index_omitted_and_no_match_returns_503(
        self,
        client: TestClient,
        data_dir: Path,
    ) -> None:
        _seed_combo_store(data_dir, endpoint_guid="EP-UNPLUG")
        probe_mock = AsyncMock(return_value=_canned_probe_result())
        with (
            patch("sovyx.dashboard.routes.voice_health.probe", probe_mock),
            patch(
                "sovyx.dashboard.routes.voice_health._lookup_device_index_by_name",
                MagicMock(return_value=None),
            ),
        ):
            resp = client.post(
                "/api/voice/health/reprobe",
                json={"endpoint_guid": "EP-UNPLUG", "mode": "cold"},
            )
        assert resp.status_code == 503  # noqa: PLR2004
        probe_mock.assert_not_awaited()

    def test_infinite_rms_db_does_not_500(
        self,
        client: TestClient,
        data_dir: Path,
    ) -> None:
        """Muted mic / stream-open failure returns ``rms_db=-inf`` — the
        route must clamp it to a JSON-serialisable value (Bug 005)."""
        _seed_combo_store(data_dir, endpoint_guid="EP-MUTED")
        muted_result = ProbeResult(
            diagnosis=Diagnosis.MUTED,
            mode=ProbeMode.WARM,
            combo=_make_combo(),
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=float("-inf"),
            callbacks_fired=0,
            duration_ms=0,
            error=None,
        )
        probe_mock = AsyncMock(return_value=muted_result)
        with patch("sovyx.dashboard.routes.voice_health.probe", probe_mock):
            resp = client.post(
                "/api/voice/health/reprobe",
                json={
                    "endpoint_guid": "EP-MUTED",
                    "device_index": 0,
                    "mode": "cold",
                },
            )
        assert resp.status_code == 200  # noqa: PLR2004
        payload = resp.json()
        assert payload["result"]["diagnosis"] == Diagnosis.MUTED.value
        assert payload["result"]["rms_db"] == -90.0  # noqa: PLR2004 — clamped

    def test_duration_out_of_range_is_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/voice/health/reprobe",
            json={
                "endpoint_guid": "EP",
                "device_index": 0,
                "mode": "cold",
                "duration_ms": 99,  # below Field(ge=100)
            },
        )
        assert resp.status_code == 422  # noqa: PLR2004


class TestForget:
    """POST /api/voice/health/forget."""

    def test_absent_endpoint_returns_invalidated_false(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/api/voice/health/forget",
            json={"endpoint_guid": "EP-ABSENT", "reason": "test"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["endpoint_guid"] == "EP-ABSENT"
        assert body["invalidated"] is False

    def test_present_endpoint_returns_invalidated_true(
        self,
        client: TestClient,
        data_dir: Path,
    ) -> None:
        _seed_combo_store(data_dir, endpoint_guid="EP-KILL")
        resp = client.post(
            "/api/voice/health/forget",
            json={"endpoint_guid": "EP-KILL", "reason": "rotate"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["invalidated"] is True

        # `ComboStore.invalidate` drops the entry outright — a fresh load sees none.
        fresh = ComboStore(resolve_combo_store_path(data_dir))
        fresh.load()
        assert fresh.get("EP-KILL") is None


class TestPin:
    """POST /api/voice/health/pin."""

    def test_happy_path_writes_override(
        self,
        client: TestClient,
        data_dir: Path,
    ) -> None:
        body = {
            "endpoint_guid": "EP-PIN",
            "device_friendly_name": "Pinned Device",
            "combo": {
                "host_api": _HOST_API,
                "sample_rate": 16_000,
                "channels": 1,
                "sample_format": "int16",
                "exclusive": True,
                "auto_convert": False,
                "frames_per_buffer": 480,
            },
            "source": "user",
            "reason": "dashboard-pin",
        }
        resp = client.post("/api/voice/health/pin", json=body)
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json() == {"endpoint_guid": "EP-PIN", "pinned": True}

        # The file really exists on disk with the expected content.
        overrides_path = resolve_capture_overrides_path(data_dir)
        assert overrides_path.exists()
        data = json.loads(overrides_path.read_text(encoding="utf-8"))
        assert list(data["overrides"].keys()) == ["EP-PIN"]
        assert data["overrides"]["EP-PIN"]["pinned_by"] == "user"

    def test_invalid_source_is_422(self, client: TestClient) -> None:
        body = {
            "endpoint_guid": "EP-X",
            "device_friendly_name": "X",
            "combo": {
                "host_api": _HOST_API,
                "sample_rate": 16_000,
                "channels": 1,
                "sample_format": "int16",
                "exclusive": True,
                "auto_convert": False,
                "frames_per_buffer": 480,
            },
            "source": "malicious-actor",  # not in Literal
        }
        resp = client.post("/api/voice/health/pin", json=body)
        assert resp.status_code == 422  # noqa: PLR2004

    def test_invalid_combo_is_409(self, client: TestClient) -> None:
        body = {
            "endpoint_guid": "EP-Y",
            "device_friendly_name": "Y",
            "combo": {
                "host_api": _HOST_API,
                "sample_rate": 7_777,  # invalid
                "channels": 1,
                "sample_format": "int16",
                "exclusive": True,
                "auto_convert": False,
                "frames_per_buffer": 480,
            },
            "source": "user",
        }
        resp = client.post("/api/voice/health/pin", json=body)
        assert resp.status_code == 409  # noqa: PLR2004


class TestDataDirFallback:
    """When ``engine_config`` is absent, fall back to ``~/.sovyx``."""

    def test_no_engine_config_uses_home_default(
        self,
        tmp_path: Path,
    ) -> None:
        application = create_app(token=_TOKEN)
        application.state.registry = MagicMock()
        application.state.registry.is_registered.return_value = False
        # No engine_config attribute at all.
        with patch(
            "sovyx.dashboard.routes.voice_health.Path.home",
            return_value=tmp_path,
        ):
            client = TestClient(
                application,
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )
            resp = client.get("/api/voice/health")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["data_dir"] == str(tmp_path / ".sovyx")


class TestQuarantineEndpoint:
    """GET /api/voice/health/quarantine — §4.4.7 kernel-invalidated surface."""

    def test_requires_bearer_token(self, app: FastAPI) -> None:
        unauth = TestClient(app)
        resp = unauth.get("/api/voice/health/quarantine")
        assert resp.status_code == 401  # noqa: PLR2004

    def test_empty_snapshot_returns_empty_entries(self, app: FastAPI, client: TestClient) -> None:
        # Inject a fresh quarantine so singleton state doesn't bleed in.
        app.state.quarantine = EndpointQuarantine(quarantine_s=60.0, maxsize=8)
        resp = client.get("/api/voice/health/quarantine")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body == {"entries": [], "count": 0}

    def test_populated_snapshot_returns_entries(self, app: FastAPI, client: TestClient) -> None:
        q = EndpointQuarantine(quarantine_s=60.0, maxsize=8)
        q.add(
            endpoint_guid="{GUID-1}",
            device_friendly_name="Razer BlackShark V2 Pro",
            device_interface_name=r"\\?\USB#VID_1532",
            host_api="Windows WASAPI",
            reason="probe",
        )
        app.state.quarantine = q

        resp = client.get("/api/voice/health/quarantine")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["count"] == 1
        entry = body["entries"][0]
        assert entry["endpoint_guid"] == "{GUID-1}"
        assert entry["device_friendly_name"] == "Razer BlackShark V2 Pro"
        assert entry["device_interface_name"] == r"\\?\USB#VID_1532"
        assert entry["host_api"] == "Windows WASAPI"
        assert entry["reason"] == "probe"
        # seconds_until_expiry is clamped to non-negative and ≤ quarantine_s.
        assert 0.0 <= entry["seconds_until_expiry"] <= 60.0  # noqa: PLR2004

    def test_quarantine_count_on_health_snapshot(self, app: FastAPI, client: TestClient) -> None:
        q = EndpointQuarantine(quarantine_s=60.0, maxsize=8)
        q.add(endpoint_guid="{G-A}", host_api="WASAPI")
        q.add(endpoint_guid="{G-B}", host_api="WASAPI")
        app.state.quarantine = q

        resp = client.get("/api/voice/health")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["quarantine_count"] == 2  # noqa: PLR2004

    def test_app_state_quarantine_overrides_singleton(
        self, app: FastAPI, client: TestClient
    ) -> None:
        # Set a sentinel store on app.state. Reading the endpoint must
        # surface that store, not the process-wide singleton.
        fresh = EndpointQuarantine(quarantine_s=60.0, maxsize=4)
        fresh.add(endpoint_guid="{SENTINEL}", host_api="WASAPI")
        app.state.quarantine = fresh

        resp = client.get("/api/voice/health/quarantine")
        body = resp.json()
        guids = [e["endpoint_guid"] for e in body["entries"]]
        assert "{SENTINEL}" in guids
