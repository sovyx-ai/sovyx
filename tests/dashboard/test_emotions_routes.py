"""Tests for /api/emotions/* endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-emotions"


@pytest.fixture()
def app():
    application = create_app(token=_TOKEN)
    registry = MagicMock()
    registry.is_registered.return_value = False
    registry.resolve = AsyncMock()
    application.state.registry = registry
    application.state.mind_config = MagicMock()
    application.state.mind_config.configure_mock(name="test")
    application.state.mind_config.id = "test-mind"
    return application


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class TestCurrentMood:
    """GET /api/emotions/current."""

    def test_returns_empty_without_registry(self, client: TestClient) -> None:
        resp = client.get("/api/emotions/current")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["episode_count"] == 0
        assert data["label"] == "No data"
        assert "valence" in data
        assert "arousal" in data
        assert "dominance" in data

    def test_no_auth_401(self) -> None:
        app = create_app(token=_TOKEN)
        c = TestClient(app)
        resp = c.get("/api/emotions/current")
        assert resp.status_code == 401  # noqa: PLR2004


class TestTimeline:
    """GET /api/emotions/timeline."""

    def test_returns_empty_list(self, client: TestClient) -> None:
        resp = client.get("/api/emotions/timeline?period=7d")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["points"] == []
        assert data["period"] == "7d"

    def test_default_period(self, client: TestClient) -> None:
        resp = client.get("/api/emotions/timeline")
        assert resp.json()["period"] == "7d"


class TestTriggers:
    """GET /api/emotions/triggers."""

    def test_returns_empty_triggers(self, client: TestClient) -> None:
        resp = client.get("/api/emotions/triggers?limit=5")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["triggers"] == []


class TestDistribution:
    """GET /api/emotions/distribution."""

    def test_returns_empty_distribution(self, client: TestClient) -> None:
        resp = client.get("/api/emotions/distribution?period=30d")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["total"] == 0
        assert data["period"] == "30d"
        assert "positive_active" in data["distribution"]


class TestMoodClassification:
    """Test PAD → label mapping."""

    def test_quadrant_labels(self) -> None:
        from sovyx.dashboard.routes.emotions import _classify_quadrant

        assert _classify_quadrant(0.5, 0.5) == "positive_active"
        assert _classify_quadrant(0.5, 0.0) == "positive_passive"
        assert _classify_quadrant(-0.5, 0.5) == "negative_active"
        assert _classify_quadrant(-0.5, -0.5) == "negative_passive"
        assert _classify_quadrant(0.0, 0.0) == "neutral"

    def test_dominance_modifier(self) -> None:
        from sovyx.dashboard.routes.emotions import _mood_label

        label_confident = _mood_label(0.5, 0.5, 0.5)
        assert "Confident" in label_confident["label"]

        label_uncertain = _mood_label(0.5, 0.5, -0.5)
        assert "Uncertain" in label_uncertain["label"]

    def test_neutral_range(self) -> None:
        from sovyx.dashboard.routes.emotions import _classify_quadrant

        assert _classify_quadrant(0.1, 0.1) == "neutral"
        assert _classify_quadrant(-0.1, -0.1) == "neutral"
        assert _classify_quadrant(0.19, 0.19) == "neutral"
