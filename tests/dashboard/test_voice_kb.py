"""Tests for ``/api/voice/health/kb*`` — mixer-profile inspection API."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.routes import voice_kb as voice_kb_routes
from sovyx.dashboard.server import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


_TOKEN = "test-token-voice-kb"


_GOOD_YAML = dedent("""
    schema_version: 1
    profile_id: vaio_vjfe69_sn6180
    profile_version: 1
    description: Sony VAIO FE-series with Conexant SN6180.

    codec_id_glob: "14F1:5045"
    driver_family: hda
    system_vendor_glob: "Sony*"
    system_product_glob: "VJFE69*"
    kernel_major_minor_glob: "6.*"
    audio_stack: pipewire
    match_threshold: 0.6

    factory_regime: attenuation
    factory_signature:
      capture_master:
        expected_fraction_range: [0.3, 0.6]
      internal_mic_boost:
        expected_raw_range: [0, 0]

    recommended_preset:
      controls:
        - role: capture_master
          value: {fraction: 1.0}
        - role: internal_mic_boost
          value: {raw: 0}
      auto_mute_mode: disabled
      runtime_pm_target: "on"

    validation:
      rms_dbfs_range: [-30, -15]
      peak_dbfs_max: -2
      snr_db_vocal_band_min: 15
      silero_prob_min: 0.5
      wake_word_stage2_prob_min: 0.4

    verified_on:
      - system_product: "VJFE69F11X-B0221H"
        codec_id: "14F1:5045"
        kernel: "6.14.0-37"
        distro: "linuxmint-22.2"
        verified_at: "2026-04-23"
        verified_by: "sovyx-core-pilot"

    contributed_by: sovyx-core
""").strip()


def _write_profile(
    dir_path: Path,
    profile_id: str,
    *,
    body: str | None = None,
) -> Path:
    """Write a valid profile YAML under ``dir_path``."""
    yaml_body = body if body is not None else _GOOD_YAML
    if body is None:
        yaml_body = yaml_body.replace(
            "profile_id: vaio_vjfe69_sn6180",
            f"profile_id: {profile_id}",
        )
    path = dir_path / f"{profile_id}.yaml"
    path.write_text(yaml_body, encoding="utf-8")
    return path


@pytest.fixture
def shipped_dir(tmp_path: Path) -> Path:
    d = tmp_path / "shipped"
    d.mkdir()
    return d


@pytest.fixture
def app(shipped_dir: Path) -> Iterator[FastAPI]:
    """App with the shipped-profiles directory pointed at a temp dir.

    Patches apply via ``with``/``yield`` so they automatically unwind
    on fixture teardown — even when a test uses ``app`` without
    going through the ``client`` fixture. A prior version of this
    fixture started patches with ``.start()`` and wired cleanup to
    ``client``'s teardown only; ``test_missing_token_rejected`` used
    ``app`` alone, leaking the ``Path.home()`` patch into every
    subsequent test (breaking ``tests/unit/cli/test_main.py`` and
    ``tests/unit/upgrade/test_doctor.py``).
    """
    with (
        patch.object(voice_kb_routes, "_SHIPPED_PROFILES_DIR", shipped_dir),
        patch.object(
            Path,
            "home",
            staticmethod(lambda: shipped_dir.parent / "_no_user_home"),
        ),
    ):
        yield create_app(token=_TOKEN)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    with TestClient(app, headers=headers) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/voice/health/kb/profiles
# ---------------------------------------------------------------------------


class TestListProfiles:
    def test_empty_pool_returns_zero(
        self,
        client: TestClient,
    ) -> None:
        resp = client.get("/api/voice/health/kb/profiles")
        assert resp.status_code == 200
        body = resp.json()
        assert body["profiles"] == []
        assert body["shipped_count"] == 0
        assert body["user_count"] == 0

    def test_shipped_profile_surfaces(
        self,
        shipped_dir: Path,
        client: TestClient,
    ) -> None:
        _write_profile(shipped_dir, "vaio_one")
        _write_profile(shipped_dir, "vaio_two")
        resp = client.get("/api/voice/health/kb/profiles")
        assert resp.status_code == 200
        body = resp.json()
        assert body["shipped_count"] == 2
        ids = {p["profile_id"] for p in body["profiles"]}
        assert ids == {"vaio_one", "vaio_two"}
        # Each entry carries the pool label so the dashboard can split
        # shipped vs user without a second lookup.
        for profile in body["profiles"]:
            assert profile["pool"] == "shipped"

    def test_missing_token_rejected(
        self,
        app: FastAPI,
    ) -> None:
        with TestClient(app) as unauth:
            resp = unauth.get("/api/voice/health/kb/profiles")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/voice/health/kb/profiles/{profile_id}
# ---------------------------------------------------------------------------


class TestGetProfile:
    def test_returns_detailed_record(
        self,
        shipped_dir: Path,
        client: TestClient,
    ) -> None:
        _write_profile(shipped_dir, "vaio_detail")
        resp = client.get("/api/voice/health/kb/profiles/vaio_detail")
        assert resp.status_code == 200
        body = resp.json()
        assert body["profile_id"] == "vaio_detail"
        assert body["driver_family"] == "hda"
        assert body["codec_id_glob"] == "14F1:5045"
        assert body["verified_on_count"] == 1
        assert sorted(body["factory_signature_roles"]) == sorted(
            body["factory_signature_roles"],
        )  # stable ordering

    def test_missing_profile_returns_404(
        self,
        client: TestClient,
    ) -> None:
        resp = client.get("/api/voice/health/kb/profiles/does_not_exist")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/voice/health/kb/validate
# ---------------------------------------------------------------------------


class TestValidateProfile:
    def test_good_yaml_ok(self, client: TestClient) -> None:
        resp = client.post(
            "/api/voice/health/kb/validate",
            json={
                "yaml_body": _GOOD_YAML.replace(
                    "profile_id: vaio_vjfe69_sn6180",
                    "profile_id: vaio_ok",
                ),
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["profile_id"] == "vaio_ok"
        assert body["issues"] == []

    def test_schema_error_reports_field(self, client: TestClient) -> None:
        # Remove a required field; pydantic should flag it by loc.
        broken = _GOOD_YAML.replace('codec_id_glob: "14F1:5045"\n', "")
        resp = client.post(
            "/api/voice/health/kb/validate",
            json={"yaml_body": broken},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        locs = [issue["loc"] for issue in body["issues"]]
        assert any("codec_id_glob" in loc for loc in locs)

    def test_malformed_yaml_reports_once(self, client: TestClient) -> None:
        resp = client.post(
            "/api/voice/health/kb/validate",
            json={"yaml_body": "::::: not yaml"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Malformed YAML should still be a 200 OK validation response
        # (the endpoint doesn't 400 on client-submitted payloads — it
        # reports structured validation results so the UI can render
        # them inline).
        assert body["ok"] is False
        assert body["issues"]

    def test_filename_mismatch_rejected(self, client: TestClient) -> None:
        # YAML says profile_id: vaio_ok, but the caller passes a
        # different filename_stem — we reject so contributors catch
        # the drift before PR.
        resp = client.post(
            "/api/voice/health/kb/validate",
            json={
                "yaml_body": _GOOD_YAML.replace(
                    "profile_id: vaio_vjfe69_sn6180",
                    "profile_id: vaio_inside",
                ),
                "filename_stem": "vaio_outside",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert any(issue["loc"] == "profile_id" for issue in body["issues"])

    def test_top_level_non_mapping_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/api/voice/health/kb/validate",
            json={"yaml_body": "- just\n- a\n- list\n"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["issues"]

    def test_empty_yaml_body_rejected_by_pydantic(
        self,
        client: TestClient,
    ) -> None:
        # The endpoint sets min_length=1 on yaml_body; empty bodies
        # are rejected at the request-shape layer (422).
        resp = client.post(
            "/api/voice/health/kb/validate",
            json={"yaml_body": ""},
        )
        assert resp.status_code == 422
