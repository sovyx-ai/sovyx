"""Integration tests for ``/api/voice/platform-diagnostics``.

Verifies the cross-OS aggregation endpoint that exposes the F1
inventory's MA1/MA2/MA5/MA6/F3/F4/WI2/#34 detectors via a single
auth-protected dashboard route. The endpoint adapts its branches
to the host OS (linux/win32/darwin) and returns 200 with structured
notes even when individual probes fail — never 5xx on probe issues.

Coverage:

* Auth required (401 without token).
* Linux branch populated when sys.platform == "linux".
* Windows branch populated when sys.platform == "win32".
* Darwin branch populated when sys.platform == "darwin".
* Unknown platforms return ``platform="other"`` with mic_permission
  still attempted.
* Probe failures inside any branch produce structured-but-nonempty
  responses (defensive isolation).
* Response model shape stable (regression guard for dashboard
  consumers).
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI  # noqa: TC002 — pytest fixture types resolved at runtime
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-platform-diag"


@pytest.fixture()
def app() -> FastAPI:
    return create_app(token=_TOKEN)


@pytest.fixture()
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}) as c:
        yield c


@pytest.fixture()
def unauth_client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


# ── Auth ──────────────────────────────────────────────────────────


class TestAuth:
    def test_no_token_returns_401(self, unauth_client: TestClient) -> None:
        response = unauth_client.get("/api/voice/platform-diagnostics")
        assert response.status_code == 401

    def test_wrong_token_returns_401(self, app: FastAPI) -> None:
        with TestClient(app, headers={"Authorization": "Bearer wrong"}) as c:
            response = c.get("/api/voice/platform-diagnostics")
        assert response.status_code == 401


# ── Response shape (regression guard) ─────────────────────────────


class TestResponseShape:
    def test_response_has_top_level_keys(self, client: TestClient) -> None:
        response = client.get("/api/voice/platform-diagnostics")
        assert response.status_code == 200
        body = response.json()
        # Top-level keys must exist regardless of platform.
        assert "platform" in body
        assert "mic_permission" in body
        # Platform-specific branches: always present (null when not
        # the host platform). Pydantic serialises optional fields
        # as None which JSON-encodes as null.
        assert "linux" in body
        assert "windows" in body
        assert "macos" in body

    def test_mic_permission_payload_has_required_fields(
        self,
        client: TestClient,
    ) -> None:
        response = client.get("/api/voice/platform-diagnostics")
        body = response.json()
        mp = body["mic_permission"]
        # Fields the dashboard renders must always be present.
        assert "status" in mp
        assert "notes" in mp
        assert "remediation_hint" in mp


# ── Per-OS branch population ──────────────────────────────────────


class TestLinuxBranch:
    def test_linux_branch_populated_on_linux(
        self,
        client: TestClient,
    ) -> None:
        with patch.object(sys, "platform", "linux"):
            response = client.get("/api/voice/platform-diagnostics")
        assert response.status_code == 200
        body = response.json()
        assert body["platform"] == "linux"
        assert body["linux"] is not None
        assert body["windows"] is None
        assert body["macos"] is None
        # Linux-specific fields.
        assert "pipewire" in body["linux"]
        assert "alsa_ucm" in body["linux"]
        assert "status" in body["linux"]["pipewire"]
        assert "status" in body["linux"]["alsa_ucm"]


class TestWindowsBranch:
    def test_windows_branch_populated_on_win32(
        self,
        client: TestClient,
    ) -> None:
        with patch.object(sys, "platform", "win32"):
            response = client.get("/api/voice/platform-diagnostics")
        assert response.status_code == 200
        body = response.json()
        assert body["platform"] == "win32"
        assert body["windows"] is not None
        assert body["linux"] is None
        assert body["macos"] is None
        assert "audio_service" in body["windows"]
        assert "audiosrv" in body["windows"]["audio_service"]
        assert "audio_endpoint_builder" in body["windows"]["audio_service"]


class TestDarwinBranch:
    def test_darwin_branch_populated_on_darwin(
        self,
        client: TestClient,
    ) -> None:
        with patch.object(sys, "platform", "darwin"):
            response = client.get("/api/voice/platform-diagnostics")
        assert response.status_code == 200
        body = response.json()
        assert body["platform"] == "darwin"
        assert body["macos"] is not None
        assert body["linux"] is None
        assert body["windows"] is None
        assert "hal_plugins" in body["macos"]
        assert "bluetooth" in body["macos"]
        assert "code_signing" in body["macos"]


class TestUnknownPlatform:
    def test_freebsd_returns_other(self, client: TestClient) -> None:
        with patch.object(sys, "platform", "freebsd"):
            response = client.get("/api/voice/platform-diagnostics")
        assert response.status_code == 200
        body = response.json()
        assert body["platform"] == "other"
        # All per-OS branches null on unknown platform.
        assert body["linux"] is None
        assert body["windows"] is None
        assert body["macos"] is None
        # mic_permission still attempted (and returns UNKNOWN on
        # unknown platform per its own contract).
        assert body["mic_permission"]["status"] == "unknown"


# ── Probe failure isolation ───────────────────────────────────────


class TestProbeFailureIsolation:
    """A failure inside any one detector MUST NOT cause a 5xx — the
    endpoint always returns 200 with the failure noted in the
    structured response. This is the load-bearing contract that
    keeps the dashboard rendering even when probes break."""

    def test_pipewire_probe_crash_returns_200_with_notes(
        self,
        client: TestClient,
    ) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch(
                "sovyx.voice.health._pipewire.detect_pipewire",
                side_effect=RuntimeError("synthetic boom"),
            ),
        ):
            response = client.get("/api/voice/platform-diagnostics")
        assert response.status_code == 200
        body = response.json()
        assert body["linux"] is not None
        # The pipewire branch fell back to "unknown" with a probe-
        # failed note. The endpoint kept going.
        assert body["linux"]["pipewire"]["status"] == "unknown"
        assert any("probe failed" in n.lower() for n in body["linux"]["pipewire"]["notes"])

    def test_macos_hal_probe_crash_keeps_other_macos_branches(
        self,
        client: TestClient,
    ) -> None:
        # HAL probe crashes; bluetooth + code_signing still report.
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice._hal_detector_mac.detect_hal_plugins",
                side_effect=RuntimeError("hal boom"),
            ),
        ):
            response = client.get("/api/voice/platform-diagnostics")
        assert response.status_code == 200
        body = response.json()
        # HAL section degraded but present.
        assert body["macos"]["hal_plugins"]["plugins"] == []
        assert any("probe failed" in n.lower() for n in body["macos"]["hal_plugins"]["notes"])
        # Bluetooth + code signing still populated (non-null).
        assert "devices" in body["macos"]["bluetooth"]
        assert "verdict" in body["macos"]["code_signing"]


# ── Mic permission OS-aware remediation ──────────────────────────


class TestMicPermissionOsAware:
    def test_mic_permission_status_field_is_string(
        self,
        client: TestClient,
    ) -> None:
        # The status enum value is serialised to its string token
        # (StrEnum) so the dashboard can switch on it without
        # importing the enum.
        response = client.get("/api/voice/platform-diagnostics")
        body = response.json()
        assert isinstance(body["mic_permission"]["status"], str)
        # Must be one of the documented tokens.
        assert body["mic_permission"]["status"] in (
            "granted",
            "denied",
            "unknown",
        )


pytestmark = pytest.mark.timeout(30)
