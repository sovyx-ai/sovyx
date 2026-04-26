"""Tests for the POST /api/voice/kb/contribute route (Step 10).

Three contracts pinned:

* Auth — without a valid Bearer token the endpoint returns 401.
* Consent — payload with ``consent.acknowledged: false`` returns 400
  with a clear message.
* Storage — happy path writes a JSON artefact under
  ``<data_dir>/voice/contributed_profiles/`` and returns 201 with the
  resolved path.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 10.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-fixo"


def _payload(consent: bool = True) -> dict:
    return {
        "profile_id_candidate": "realtek_alc256_thinkpad_t14",
        "consent": {
            "acknowledged": consent,
            "locale": "en-US",
            "consent_at_iso": "2026-04-25T14:00:00Z",
        },
        "fingerprint": {
            "codec_id": "10EC:0256",
            "system_vendor": "LENOVO",
            "system_product": "ThinkPad T14 Gen 4",
            "distro": "ubuntu-24.04",
            "kernel": "6.8.0-50-generic",
            "audio_stack": "pipewire",
        },
        "measurement": {
            "amixer_dump_before": "Simple mixer control 'Capture',0\n  Capabilities: cvolume\n  ...",
            "amixer_dump_after": "Simple mixer control 'Capture',0\n  Capabilities: cvolume\n  ...",
            "capture_rms_dbfs": -22.5,
            "capture_silero_prob": 0.87,
            "capture_peak_dbfs": -6.0,
        },
        "candidate_yaml": ("schema_version: 1\nprofile_id: realtek_alc256_thinkpad_t14\n..."),
        "operator_handle": "tester",
    }


@pytest.fixture(autouse=True)
def _isolate_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[Path, None, None]:
    """Force EngineConfig.data_dir to a tmp path so the contribution
    artefact is written somewhere we can clean up between tests."""
    monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture
def client() -> TestClient:
    app = create_app(token=_TOKEN)
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


@pytest.fixture
def unauth_client() -> TestClient:
    app = create_app(token=_TOKEN)
    return TestClient(app)


class TestAuth:
    def test_missing_token_returns_401(self, unauth_client: TestClient) -> None:
        response = unauth_client.post("/api/voice/kb/contribute", json=_payload())
        assert response.status_code == 401

    def test_wrong_token_returns_401(self) -> None:
        app = create_app(token=_TOKEN)
        client = TestClient(app, headers={"Authorization": "Bearer wrong-token"})
        response = client.post("/api/voice/kb/contribute", json=_payload())
        assert response.status_code == 401


class TestConsentGate:
    def test_consent_false_returns_400(self, client: TestClient) -> None:
        response = client.post("/api/voice/kb/contribute", json=_payload(consent=False))
        assert response.status_code == 400
        assert "consent" in response.json()["detail"].lower()


class TestHappyPath:
    def test_consent_true_writes_artefact_and_returns_201(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        response = client.post("/api/voice/kb/contribute", json=_payload())
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "stored"
        assert body["artefact_path"]
        assert body["telemetry_uploaded"] is False

        # Verify the artefact lives under the tmp data_dir/voice/
        # contributed_profiles/ tree.
        artefact = Path(body["artefact_path"])
        assert artefact.is_file()
        assert "contributed_profiles" in artefact.parts
        # Sanity-check the saved JSON contains the operator's payload.
        saved = json.loads(artefact.read_text(encoding="utf-8"))
        assert saved["profile_id_candidate"] == "realtek_alc256_thinkpad_t14"
        assert saved["fingerprint"]["codec_id"] == "10EC:0256"
        assert saved["measurement"]["capture_silero_prob"] == 0.87


class TestSchemaValidation:
    def test_invalid_profile_id_rejected_at_pydantic_layer(
        self,
        client: TestClient,
    ) -> None:
        bad_payload = _payload()
        bad_payload["profile_id_candidate"] = "INVALID UPPERCASE"
        response = client.post("/api/voice/kb/contribute", json=bad_payload)
        assert response.status_code == 422  # FastAPI's pydantic validation

    def test_silero_prob_out_of_range_rejected(
        self,
        client: TestClient,
    ) -> None:
        bad_payload = _payload()
        bad_payload["measurement"]["capture_silero_prob"] = 1.5
        response = client.post("/api/voice/kb/contribute", json=bad_payload)
        assert response.status_code == 422
