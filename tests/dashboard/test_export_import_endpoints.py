"""Tests for /api/export, /api/import, /api/voice/status, /api/voice/models endpoints."""

from __future__ import annotations

import io
import json
import zipfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app

# ── Fixtures ──


_FIXED_TOKEN = "export-import-test-token-fixed"


@pytest.fixture()
def token() -> str:
    """Return the fixed test token."""
    return _FIXED_TOKEN


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """TestClient with auth token properly wired.

    Uses unittest.mock.patch to guarantee _ensure_token returns our
    fixed token, then verifies _server_token was set correctly.
    """
    import sovyx.dashboard.server as _srv

    with patch.object(_srv, "_ensure_token", return_value=_FIXED_TOKEN):
        app = create_app()
    # Verify the token was wired correctly
    assert _srv._server_token == _FIXED_TOKEN, (
        f"_server_token mismatch: {_srv._server_token!r} != {_FIXED_TOKEN!r}"
    )
    return TestClient(app)


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_FIXED_TOKEN}"}


@pytest.fixture()
def sample_archive_bytes() -> bytes:
    """Create a minimal .sovyx-mind ZIP archive in memory."""
    buf = io.BytesIO()
    manifest = {
        "format_version": 1,
        "sovyx_version": "0.5.0",
        "mind_id": "test-mind",
        "mind_name": "Test Mind",
        "exported_at": "2026-01-01T00:00:00+00:00",
        "statistics": {},
        "gdpr": {},
    }
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("brain.db", b"fake-db-content")
    return buf.getvalue()


# ── Export Endpoint Tests ──


class TestExportEndpoint:
    """Tests for GET /api/export."""

    def test_export_requires_auth(self, client: TestClient) -> None:
        """401 without auth token."""
        resp = client.get("/api/export")
        assert resp.status_code == 401

    def test_export_503_without_registry(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """503 when engine not running (no registry)."""
        resp = client.get("/api/export", headers=auth_headers)
        assert resp.status_code == 503

    def test_export_returns_zip_on_success(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
        tmp_path: Path,
    ) -> None:
        """200 with ZIP content on successful export."""
        # Create a real zip file to serve
        archive_path = tmp_path / "test.sovyx-mind"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("manifest.json", "{}")

        mock_registry = MagicMock()
        client.app.state.registry = mock_registry

        with (
            patch(
                "sovyx.dashboard._shared.get_active_mind_id",
                new_callable=AsyncMock,
                return_value="aria",
            ),
            patch(
                "sovyx.dashboard.export_import.export_mind",
                new_callable=AsyncMock,
                return_value=archive_path,
            ),
        ):
            resp = client.get("/api/export", headers=auth_headers)

        assert resp.status_code == 200
        assert "application/zip" in resp.headers.get("content-type", "")

    def test_export_500_on_internal_error(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """500 when export raises an unexpected exception."""
        client.app.state.registry = MagicMock()

        with (
            patch(
                "sovyx.dashboard._shared.get_active_mind_id",
                new_callable=AsyncMock,
                return_value="aria",
            ),
            patch(
                "sovyx.dashboard.export_import.export_mind",
                new_callable=AsyncMock,
                side_effect=ValueError("unexpected"),
            ),
        ):
            resp = client.get("/api/export", headers=auth_headers)

        assert resp.status_code == 500


# ── Import Endpoint Tests ──


class TestImportEndpoint:
    """Tests for POST /api/import."""

    def test_import_requires_auth(self, client: TestClient) -> None:
        """401 without auth token."""
        resp = client.post("/api/import")
        assert resp.status_code == 401

    def test_import_503_without_registry(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """503 when engine not running."""
        resp = client.post(
            "/api/import",
            headers=auth_headers,
            content=b"something",
        )
        assert resp.status_code == 503

    def test_import_422_without_multipart(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """422 when content-type is not multipart/form-data."""
        client.app.state.registry = MagicMock()
        resp = client.post(
            "/api/import",
            headers={**auth_headers, "content-type": "application/json"},
            content=b"{}",
        )
        assert resp.status_code == 422

    def test_import_success(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
        sample_archive_bytes: bytes,
    ) -> None:
        """200 with import result on success."""
        client.app.state.registry = MagicMock()

        mock_result = {
            "mind_id": "test-mind",
            "concepts_imported": 10,
            "episodes_imported": 5,
            "relations_imported": 3,
            "warnings": [],
        }

        with patch(
            "sovyx.dashboard.export_import.import_mind",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                "/api/import",
                headers=auth_headers,
                files={"file": ("test.sovyx-mind", sample_archive_bytes, "application/zip")},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["mind_id"] == "test-mind"


# ── Voice Status Endpoint Tests ──


class TestVoiceStatusEndpoint:
    """Tests for GET /api/voice/status."""

    def test_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/api/voice/status")
        assert resp.status_code == 401

    def test_503_without_registry(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        resp = client.get("/api/voice/status", headers=auth_headers)
        assert resp.status_code == 503

    def test_returns_status(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        """200 with voice status when registry is available."""
        client.app.state.registry = MagicMock()

        mock_status = {
            "pipeline": {"running": False, "state": "not_configured"},
            "stt": {"engine": None},
            "tts": {"engine": None},
        }

        with patch(
            "sovyx.dashboard.voice_status.get_voice_status",
            new_callable=AsyncMock,
            return_value=mock_status,
        ):
            resp = client.get("/api/voice/status", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["pipeline"]["running"] is False


# ── Voice Models Endpoint Tests ──


class TestVoiceModelsEndpoint:
    """Tests for GET /api/voice/models."""

    def test_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/api/voice/models")
        assert resp.status_code == 401

    def test_503_without_registry(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        resp = client.get("/api/voice/models", headers=auth_headers)
        assert resp.status_code == 503

    def test_returns_models(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        """200 with voice models when registry is available."""
        client.app.state.registry = MagicMock()

        mock_models = {
            "detected_tier": None,
            "active": None,
            "available_tiers": {"PI5": {"stt_primary": "moonshine-tiny"}},
        }

        with patch(
            "sovyx.dashboard.voice_status.get_voice_models",
            new_callable=AsyncMock,
            return_value=mock_models,
        ):
            resp = client.get("/api/voice/models", headers=auth_headers)

        assert resp.status_code == 200
        assert "available_tiers" in resp.json()
