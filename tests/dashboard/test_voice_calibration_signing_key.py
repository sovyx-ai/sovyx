"""Tests for the BT.B.3 calibration signing-key dashboard endpoints.

Mission: ``MISSION-voice-v0_32_0-structural-closure-2026-05-08.md``
Phase B BT.B.3.

Endpoints:

* ``GET /api/voice/calibration/signing-key`` — read-only status probe.
* ``POST /api/voice/calibration/generate-signing-key`` — generate the
  Ed25519 keypair under ``<data_dir>/<mind_id>/calibration.signing-key.{priv,pub}``.

Tests cover happy path, 409 already-exists without ``force``, force
overwrite, mutex serialisation under concurrent POSTs, and auth.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.voice.calibration._key_generation import (
    PRIVATE_KEY_FILENAME,
    PUBLIC_KEY_FILENAME,
)

_TOKEN = "test-token-signing-key"  # noqa: S105 -- test fixture token


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


# ====================================================================
# GET /signing-key
# ====================================================================


class TestSigningKeyStatusEndpoint:
    def test_status_when_key_missing(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        response = _client(app).get(
            "/api/voice/calibration/signing-key",
            params={"mind_id": "absent"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["exists"] is False
        assert body["fingerprint_short"] is None
        assert body["public_key_path"] is None

    def test_status_after_generation_returns_fingerprint(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        # Generate first.
        gen = _client(app).post(
            "/api/voice/calibration/generate-signing-key",
            json={"mind_id": "test-mind"},
        )
        assert gen.status_code == 200, gen.text
        # Now status reports exists=True + the same fingerprint.
        status = _client(app).get(
            "/api/voice/calibration/signing-key",
            params={"mind_id": "test-mind"},
        )
        assert status.status_code == 200
        body = status.json()
        assert body["exists"] is True
        assert body["fingerprint_short"] == gen.json()["fingerprint_short"]
        assert body["public_key_path"] is not None
        assert body["public_key_path"].endswith(PUBLIC_KEY_FILENAME)


# ====================================================================
# POST /generate-signing-key
# ====================================================================


class TestGenerateSigningKeyEndpoint:
    def test_happy_path_returns_public_key_and_paths(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        response = _client(app).post(
            "/api/voice/calibration/generate-signing-key",
            json={"mind_id": "happy-mind"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert "BEGIN PUBLIC KEY" in body["public_key_pem"]
        assert body["public_key_path"].endswith(PUBLIC_KEY_FILENAME)
        assert body["private_key_path"].endswith(PRIVATE_KEY_FILENAME)
        assert len(body["fingerprint_short"]) == 8
        assert body["mode"] == "created"
        # NEVER expose the private key bytes in the response.
        assert "BEGIN PRIVATE KEY" not in str(body)
        assert "BEGIN ED25519 PRIVATE KEY" not in str(body)
        # Files exist on disk.
        priv = tmp_path / body["resolved_mind_id"] / PRIVATE_KEY_FILENAME
        pub = tmp_path / body["resolved_mind_id"] / PUBLIC_KEY_FILENAME
        assert priv.is_file()
        assert pub.is_file()

    def test_returns_409_when_key_exists_without_force(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        first = _client(app).post(
            "/api/voice/calibration/generate-signing-key",
            json={"mind_id": "existing"},
        )
        assert first.status_code == 200
        # Second POST without force MUST return 409.
        second = _client(app).post(
            "/api/voice/calibration/generate-signing-key",
            json={"mind_id": "existing"},
        )
        assert second.status_code == 409
        detail = second.json().get("detail", "")
        assert "already exists" in detail

    def test_force_overwrites_and_emits_forced_mode(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        first = _client(app).post(
            "/api/voice/calibration/generate-signing-key",
            json={"mind_id": "force-test"},
        )
        assert first.status_code == 200
        priv = tmp_path / "force-test" / PRIVATE_KEY_FILENAME
        first_bytes = priv.read_bytes()
        # Force overwrite.
        second = _client(app).post(
            "/api/voice/calibration/generate-signing-key",
            json={"mind_id": "force-test", "force": True},
        )
        assert second.status_code == 200
        body = second.json()
        assert body["mode"] == "forced"
        # New key material on disk.
        assert priv.read_bytes() != first_bytes

    def test_returns_401_without_auth(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        no_auth = TestClient(app)
        response = no_auth.post(
            "/api/voice/calibration/generate-signing-key",
            json={"mind_id": "default"},
        )
        assert response.status_code == 401


# ====================================================================
# Concurrency: per-mind mutex serialises POSTs
# ====================================================================


class TestSigningKeyMutex:
    """Mirror of QA-FIX-5 (rc.2) on /start: two near-simultaneous POSTs
    for the same mind cannot both succeed-then-overwrite. The lock makes
    (check-exists + write) atomic so the second request observes the
    first's write and returns 409.
    """

    def test_concurrent_posts_serialise_first_wins_second_409(
        self,
        tmp_path: Path,
    ) -> None:
        from httpx import ASGITransport, AsyncClient

        app = _build_app(tmp_path=tmp_path)

        async def _drive() -> tuple[int, int]:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
                headers={"Authorization": f"Bearer {_TOKEN}"},
            ) as client:
                a, b = await asyncio.gather(
                    client.post(
                        "/api/voice/calibration/generate-signing-key",
                        json={"mind_id": "race-test"},
                    ),
                    client.post(
                        "/api/voice/calibration/generate-signing-key",
                        json={"mind_id": "race-test"},
                    ),
                )
                return a.status_code, b.status_code

        codes = asyncio.run(_drive())
        # One MUST win (200) and the other MUST get 409 — the lock
        # serialises them so the second observes the first's write.
        assert sorted(codes) == [200, 409]
        # Exactly one keypair on disk (the winner's).
        priv = tmp_path / "race-test" / PRIVATE_KEY_FILENAME
        assert priv.is_file()


# ====================================================================
# Sentinel resolution (anti-pattern #35)
# ====================================================================


class TestSigningKeyMindIdResolution:
    def test_explicit_non_default_mind_id_lands_in_correct_directory(
        self,
        tmp_path: Path,
    ) -> None:
        app = _build_app(tmp_path=tmp_path)
        response = _client(app).post(
            "/api/voice/calibration/generate-signing-key",
            json={"mind_id": "my-explicit-mind"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["resolved_mind_id"] == "my-explicit-mind"
        assert (tmp_path / "my-explicit-mind" / PRIVATE_KEY_FILENAME).is_file()


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/voice/calibration/signing-key",
    ],
)
class TestSigningKeyAuth:
    def test_get_status_requires_auth(self, tmp_path: Path, endpoint: str) -> None:
        app = _build_app(tmp_path=tmp_path)
        no_auth = TestClient(app)
        response = no_auth.get(endpoint)
        assert response.status_code == 401
