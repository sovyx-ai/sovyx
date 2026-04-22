"""Tests for Linux ALSA mixer dashboard endpoints.

Covers ``GET /api/voice/linux-mixer-diagnostics`` and
``POST /api/voice/linux-mixer-reset`` — the remediation surface for
the Linux pre-ADC gain saturation pattern.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.routes import voice as voice_routes
from sovyx.dashboard.server import create_app
from sovyx.voice.health.contract import (
    MixerApplySnapshot,
    MixerCardSnapshot,
    MixerControlSnapshot,
)

_TOKEN = "test-token-linux-mixer"


@pytest.fixture()
def app():
    application = create_app(token=_TOKEN)
    registry = MagicMock()
    registry.is_registered.return_value = False
    registry.resolve = AsyncMock()
    application.state.registry = registry
    application.state.mind_yaml_path = None
    application.state.mind_id = "test-mind"
    return application


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _ctl(
    name: str,
    *,
    current_raw: int = 31,
    max_raw: int = 31,
    saturation_risk: bool = True,
) -> MixerControlSnapshot:
    return MixerControlSnapshot(
        name=name,
        min_raw=0,
        max_raw=max_raw,
        current_raw=current_raw,
        current_db=36.0,
        max_db=36.0,
        is_boost_control=True,
        saturation_risk=saturation_risk,
    )


def _card(
    *,
    card_index: int = 1,
    card_id: str = "PCH",
    card_longname: str = "HDA Intel PCH",
    saturation_warning: bool = True,
    controls: tuple[MixerControlSnapshot, ...] | None = None,
) -> MixerCardSnapshot:
    return MixerCardSnapshot(
        card_index=card_index,
        card_id=card_id,
        card_longname=card_longname,
        controls=controls or (_ctl("Capture", saturation_risk=saturation_warning),),
        aggregated_boost_db=36.0 if saturation_warning else 0.0,
        saturation_warning=saturation_warning,
    )


class TestLinuxMixerDiagnostics:
    def test_non_linux_returns_unsupported(self, client: TestClient) -> None:
        with patch.object(voice_routes, "_is_linux", return_value=False):
            resp = client.get("/api/voice/linux-mixer-diagnostics")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["platform_supported"] is False
        assert data["amixer_available"] is False
        assert data["snapshots"] == []
        assert "aggregated_boost_db_ceiling" in data
        assert "saturation_ratio_ceiling" in data
        assert isinstance(data["reset_enabled_by_default"], bool)

    def test_linux_without_amixer_is_supported_but_empty(self, client: TestClient) -> None:
        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=False),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                return_value=[],
            ),
        ):
            resp = client.get("/api/voice/linux-mixer-diagnostics")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["platform_supported"] is True
        assert data["amixer_available"] is False
        assert data["snapshots"] == []

    def test_linux_with_saturation_returns_snapshots(self, client: TestClient) -> None:
        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=True),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                return_value=[_card(saturation_warning=True)],
            ),
        ):
            resp = client.get("/api/voice/linux-mixer-diagnostics")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["platform_supported"] is True
        assert data["amixer_available"] is True
        assert len(data["snapshots"]) == 1
        snap = data["snapshots"][0]
        assert snap["card_index"] == 1
        assert snap["saturation_warning"] is True
        assert snap["controls"][0]["name"] == "Capture"
        assert snap["controls"][0]["saturation_risk"] is True

    def test_probe_exception_degrades_gracefully(self, client: TestClient) -> None:
        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=True),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                side_effect=RuntimeError("proc unreadable"),
            ),
        ):
            resp = client.get("/api/voice/linux-mixer-diagnostics")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["snapshots"] == []


class TestLinuxMixerReset:
    def test_non_linux_is_rejected(self, client: TestClient) -> None:
        with patch.object(voice_routes, "_is_linux", return_value=False):
            resp = client.post("/api/voice/linux-mixer-reset", json={})
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "not_linux"

    def test_amixer_missing_is_rejected(self, client: TestClient) -> None:
        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=False),
        ):
            resp = client.post("/api/voice/linux-mixer-reset", json={})
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "amixer_unavailable"

    def test_invalid_card_index_rejected(self, client: TestClient) -> None:
        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=True),
        ):
            resp = client.post(
                "/api/voice/linux-mixer-reset",
                json={"card_index": "abc"},
            )
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "invalid_card_index"

    def test_no_snapshots_returns_failure(self, client: TestClient) -> None:
        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=True),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                return_value=[],
            ),
        ):
            resp = client.post("/api/voice/linux-mixer-reset", json={})
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "no_snapshots"

    def test_auto_select_requires_unique_saturating_card(self, client: TestClient) -> None:
        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=True),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                return_value=[
                    _card(card_index=0, card_id="AAA", saturation_warning=True),
                    _card(card_index=1, card_id="BBB", saturation_warning=True),
                ],
            ),
        ):
            resp = client.post("/api/voice/linux-mixer-reset", json={})
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "ambiguous_card"
        assert data["candidate_card_indexes"] == [0, 1]

    def test_auto_select_fails_when_nothing_saturating(self, client: TestClient) -> None:
        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=True),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                return_value=[_card(saturation_warning=False)],
            ),
        ):
            resp = client.post("/api/voice/linux-mixer-reset", json={})
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "not_saturating"

    def test_explicit_card_not_found(self, client: TestClient) -> None:
        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=True),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                return_value=[_card(card_index=0)],
            ),
        ):
            resp = client.post("/api/voice/linux-mixer-reset", json={"card_index": 99})
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "card_not_found"

    def test_card_with_no_saturating_controls(self, client: TestClient) -> None:
        safe_card = _card(
            saturation_warning=True,
            controls=(_ctl("Capture", saturation_risk=False),),
        )
        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=True),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                return_value=[safe_card],
            ),
        ):
            resp = client.post("/api/voice/linux-mixer-reset", json={})
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "no_controls_to_reset"

    def test_success_path_applies_reset(self, client: TestClient) -> None:
        target = _card()
        apply_snap = MixerApplySnapshot(
            card_index=1,
            reverted_controls=(("Capture", 31),),
            applied_controls=(("Capture", 15),),
        )
        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=True),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                return_value=[target],
            ),
            patch(
                "sovyx.voice.health._linux_mixer_apply.apply_mixer_reset",
                new=AsyncMock(return_value=apply_snap),
            ),
        ):
            resp = client.post("/api/voice/linux-mixer-reset", json={})
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["ok"] is True
        assert data["card_index"] == 1
        assert data["applied_controls"] == [["Capture", 15]]
        assert data["reverted_controls"] == [["Capture", 31]]

    def test_apply_failure_returns_reason_code(self, client: TestClient) -> None:
        from sovyx.voice.health.bypass._strategy import BypassApplyError

        with (
            patch.object(voice_routes, "_is_linux", return_value=True),
            patch.object(voice_routes, "_amixer_available", return_value=True),
            patch(
                "sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots",
                return_value=[_card()],
            ),
            patch(
                "sovyx.voice.health._linux_mixer_apply.apply_mixer_reset",
                new=AsyncMock(
                    side_effect=BypassApplyError("amixer timed out", reason="amixer_timeout")
                ),
            ),
        ):
            resp = client.post("/api/voice/linux-mixer-reset", json={})
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "apply_failed"
        assert data["reason_code"] == "amixer_timeout"

    def test_requires_auth(self, app) -> None:
        c = TestClient(app)
        resp = c.post("/api/voice/linux-mixer-reset", json={})
        assert resp.status_code == 401  # noqa: PLR2004
