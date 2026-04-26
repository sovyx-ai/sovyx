"""Tests for the GET /api/voice/frame-history endpoint (Step 15).

Two contracts pinned:

* Auth — without a valid Bearer token the endpoint returns 401.
* Pipeline absent — when the engine registry has no VoicePipeline,
  the endpoint returns 503 with a clear error message.
* Happy path — when the pipeline is registered, the endpoint returns
  ``{"frames": [...], "total_recorded": int, "limit_applied": int}``.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 15.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.voice.pipeline._config import VoicePipelineConfig
from sovyx.voice.pipeline._frame_types import (
    EndFrame,
    UserStartedSpeakingFrame,
)
from sovyx.voice.pipeline._orchestrator import VoicePipeline

_TOKEN = "test-token-fixo"


def _make_pipeline() -> VoicePipeline:
    return VoicePipeline(
        config=VoicePipelineConfig(),
        vad=MagicMock(),
        wake_word=MagicMock(),
        stt=AsyncMock(),
        tts=AsyncMock(),
        event_bus=None,
    )


@pytest.fixture
def app_with_pipeline() -> Any:  # noqa: ANN401 — fixture returns Starlette app
    """App with a real VoicePipeline registered + a few synthetic frames."""
    pipeline = _make_pipeline()
    pipeline._record_frame(
        UserStartedSpeakingFrame(
            frame_type="UserStartedSpeaking",
            timestamp_monotonic=time.monotonic(),
            source="wake_word",
        ),
    )
    pipeline._record_frame(
        EndFrame(
            frame_type="End",
            timestamp_monotonic=time.monotonic(),
            reason="from_recording",
        ),
    )

    app = create_app(token=_TOKEN)
    # Inject a minimal registry stub that returns our pipeline.
    registry = MagicMock()
    registry.get.return_value = pipeline
    app.state.registry = registry
    return app


@pytest.fixture
def client(app_with_pipeline: Any) -> TestClient:  # noqa: ANN401
    return TestClient(
        app_with_pipeline,
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )


class TestAuth:
    def test_missing_token_returns_401(self, app_with_pipeline: Any) -> None:  # noqa: ANN401
        client = TestClient(app_with_pipeline)
        response = client.get("/api/voice/frame-history")
        assert response.status_code == 401


class TestRegistryAbsent:
    def test_missing_registry_returns_503(self) -> None:
        app = create_app(token=_TOKEN)
        # Don't set app.state.registry.
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/frame-history")
        assert response.status_code == 503

    def test_pipeline_not_registered_returns_503(self) -> None:
        app = create_app(token=_TOKEN)
        registry = MagicMock()
        registry.get.side_effect = KeyError("VoicePipeline")
        app.state.registry = registry
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/frame-history")
        assert response.status_code == 503


class TestHappyPath:
    def test_returns_frames_with_total_recorded(self, client: TestClient) -> None:
        response = client.get("/api/voice/frame-history")
        assert response.status_code == 200
        body = response.json()
        assert "frames" in body
        assert body["total_recorded"] == 2
        assert body["limit_applied"] == 100  # default
        assert len(body["frames"]) == 2
        # Verify the frames carry their discriminator + fields.
        frame_types = [f["frame_type"] for f in body["frames"]]
        assert "UserStartedSpeaking" in frame_types
        assert "End" in frame_types

    def test_limit_clamped_to_256(self, client: TestClient) -> None:
        response = client.get("/api/voice/frame-history?limit=10000")
        assert response.status_code == 200
        body = response.json()
        assert body["limit_applied"] == 256

    def test_limit_clamped_to_1_minimum(self, client: TestClient) -> None:
        response = client.get("/api/voice/frame-history?limit=0")
        assert response.status_code == 200
        body = response.json()
        assert body["limit_applied"] == 1
        assert len(body["frames"]) == 1
