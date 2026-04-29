"""Tests for the Voice Windows Paranoid Mission v0.24.0 route stubs.

Pins the wire contract for two foundation-phase endpoints that ship
empty-state responses in v0.24.0 and get populated by the v0.25.0
wire-up phase:

* ``GET /api/voice/restart-history`` — capture-task restart timeline
  filtered to ``CaptureRestartFrame`` entries from the pipeline ring
  buffer (mission §C, schema:
  ``VoiceRestartHistoryResponseSchema``).
* ``GET /api/voice/bypass-tier-status`` — current bypass-tier health
  snapshot + per-tier attempt / success counters since pipeline start
  (mission §B, schema: ``VoiceBypassTierStatusResponseSchema``).

Both endpoints follow the established route patterns:

* Auth — bearer-token required, 401 when missing.
* Registry absent — 503 when ``app.state.registry`` is unset (boot
  in progress) or ``Engine not running`` payload.
* Happy path — 200 with the documented empty-state shape.

Mission spec:
``docs-internal/missions/MISSION-voice-windows-paranoid-2026-04-26.md``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-fixo"


@pytest.fixture
def app_with_registry() -> Any:  # noqa: ANN401 — fixture returns Starlette app
    """App with a stub registry attached. The stubs don't touch the
    registry yet (v0.24.0), but we still attach one so the 503 path
    isn't accidentally exercised by the happy-path tests."""
    app = create_app(token=_TOKEN)
    registry = MagicMock()
    app.state.registry = registry
    return app


@pytest.fixture
def client(app_with_registry: Any) -> TestClient:  # noqa: ANN401
    return TestClient(
        app_with_registry,
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )


# ── /api/voice/restart-history ──────────────────────────────────────


class TestRestartHistoryAuth:
    def test_missing_token_returns_401(self, app_with_registry: Any) -> None:  # noqa: ANN401
        client = TestClient(app_with_registry)
        response = client.get("/api/voice/restart-history")
        assert response.status_code == 401


class TestRestartHistoryRegistryAbsent:
    def test_missing_registry_returns_503(self) -> None:
        app = create_app(token=_TOKEN)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/restart-history")
        assert response.status_code == 503
        assert response.json()["error"] == "Engine not running"


class TestRestartHistoryHappyPathV024Stub:
    """Empty-payload happy path — when no restart has occurred since the
    daemon started OR the pipeline isn't registered yet, ``frames`` is
    the empty list. The dashboard renders the empty-state placeholder."""

    def test_returns_empty_frames_array(self, client: TestClient) -> None:
        response = client.get("/api/voice/restart-history")
        assert response.status_code == 200
        body = response.json()
        assert body["frames"] == []
        assert body["total"] == 0
        assert body["limit"] == 50  # default

    def test_limit_clamped_to_256(self, client: TestClient) -> None:
        response = client.get("/api/voice/restart-history?limit=10000")
        assert response.status_code == 200
        body = response.json()
        assert body["limit"] == 256

    def test_limit_clamped_to_1_minimum(self, client: TestClient) -> None:
        response = client.get("/api/voice/restart-history?limit=0")
        assert response.status_code == 200
        body = response.json()
        assert body["limit"] == 1

    def test_limit_negative_clamped_to_1(self, client: TestClient) -> None:
        response = client.get("/api/voice/restart-history?limit=-5")
        assert response.status_code == 200
        body = response.json()
        assert body["limit"] == 1

    def test_response_keys_match_zod_schema_contract(self, client: TestClient) -> None:
        """Wire contract: keys MUST match VoiceRestartHistoryResponseSchema
        in dashboard/src/types/schemas.ts so the frontend safeParse
        succeeds without surprise key drift."""
        response = client.get("/api/voice/restart-history")
        assert response.status_code == 200
        body = response.json()
        # Required keys per the zod schema (frames is required;
        # limit + total are .optional() but always present here).
        assert set(body.keys()) == {"frames", "total", "limit"}


class TestRestartHistoryWiredUpPayloadT33:
    """T33 — pin the populated-payload contract.

    When ``VoicePipeline`` is registered AND ``frame_history``
    contains :class:`CaptureRestartFrame` instances (T32 emitters
    fired), the endpoint MUST return them serialised via
    ``_frame_to_dict`` in newest-first order with ``total`` reflecting
    the unfiltered count and ``limit`` capping the slice.
    """

    @pytest.fixture
    def pipeline_with_frames(self) -> Any:  # noqa: ANN401
        """A MagicMock pipeline whose ``frame_history`` contains
        three CaptureRestartFrames + two non-restart frames (to
        verify the isinstance filter)."""
        from sovyx.voice.pipeline._frame_types import (
            CaptureRestartFrame,
            CaptureRestartReason,
            UserStartedSpeakingFrame,
        )

        frames = [
            UserStartedSpeakingFrame(
                frame_type="UserStartedSpeaking",
                timestamp_monotonic=10.0,
                source="wake_word",
            ),
            CaptureRestartFrame(
                frame_type="CaptureRestart",
                timestamp_monotonic=20.0,
                restart_reason=CaptureRestartReason.APO_DEGRADED.value,
                old_host_api="Windows WASAPI",
                new_host_api="Windows WASAPI",
                old_signal_processing_mode="shared",
                new_signal_processing_mode="exclusive",
                bypass_tier=3,
            ),
            UserStartedSpeakingFrame(
                frame_type="UserStartedSpeaking",
                timestamp_monotonic=30.0,
                source="wake_word",
            ),
            CaptureRestartFrame(
                frame_type="CaptureRestart",
                timestamp_monotonic=40.0,
                restart_reason=CaptureRestartReason.MANUAL.value,
                old_signal_processing_mode="exclusive",
                new_signal_processing_mode="shared",
                bypass_tier=0,
            ),
            CaptureRestartFrame(
                frame_type="CaptureRestart",
                timestamp_monotonic=50.0,
                restart_reason=CaptureRestartReason.APO_DEGRADED.value,
                old_signal_processing_mode="session_manager",
                new_signal_processing_mode="alsa_hw_direct",
                bypass_tier=2,
            ),
        ]
        pipeline = MagicMock()
        pipeline.frame_history = tuple(frames)
        return pipeline

    @pytest.fixture
    def app_with_pipeline(self, pipeline_with_frames: Any) -> Any:  # noqa: ANN401
        """App whose registry is wired to return the populated
        pipeline for ``VoicePipeline`` lookups."""
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        app = create_app(token=_TOKEN)
        registry = MagicMock()
        registry.is_registered = MagicMock(side_effect=lambda cls: cls is VoicePipeline)
        registry.get = MagicMock(
            side_effect=lambda cls: pipeline_with_frames if cls is VoicePipeline else None
        )
        app.state.registry = registry
        return app

    @pytest.fixture
    def populated_client(self, app_with_pipeline: Any) -> TestClient:  # noqa: ANN401
        return TestClient(
            app_with_pipeline,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    def test_returns_only_capture_restart_frames(self, populated_client: TestClient) -> None:
        """Filter out non-CaptureRestart frames — the endpoint is
        scoped to substrate-mutation events only."""
        response = populated_client.get("/api/voice/restart-history")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3
        assert len(body["frames"]) == 3
        assert all(f["frame_type"] == "CaptureRestart" for f in body["frames"])

    def test_returns_newest_first_order(self, populated_client: TestClient) -> None:
        """Dashboard renders newest at the top — the endpoint MUST
        deliver them in that order."""
        response = populated_client.get("/api/voice/restart-history")
        body = response.json()
        timestamps = [f["timestamp_monotonic"] for f in body["frames"]]
        assert timestamps == [50.0, 40.0, 20.0], (
            f"Expected newest-first ordering [50, 40, 20]; got {timestamps}"
        )

    def test_limit_caps_response_but_total_unfiltered(self, populated_client: TestClient) -> None:
        """``total`` reflects the FULL count of restart frames in the
        ring buffer; ``frames`` is the limited slice. Operators
        querying with a small limit can still see the true count for
        capacity planning."""
        response = populated_client.get("/api/voice/restart-history?limit=2")
        body = response.json()
        assert body["total"] == 3
        assert len(body["frames"]) == 2
        assert body["limit"] == 2

    def test_serialised_frame_carries_all_capturerestart_fields(
        self, populated_client: TestClient
    ) -> None:
        """The serialiser (_frame_to_dict via dataclasses.asdict)
        MUST emit every CaptureRestartFrame field so the dashboard's
        zod schema validation passes."""
        response = populated_client.get("/api/voice/restart-history")
        body = response.json()
        first = body["frames"][0]
        # Every CaptureRestartFrame field MUST be present.
        for field in (
            "frame_type",
            "timestamp_monotonic",
            "utterance_id",
            "restart_reason",
            "old_host_api",
            "new_host_api",
            "old_device_id",
            "new_device_id",
            "old_signal_processing_mode",
            "new_signal_processing_mode",
            "recovery_latency_ms",
            "bypass_tier",
        ):
            assert field in first, f"missing field {field!r} in serialised frame"


# ── /api/voice/bypass-tier-status ───────────────────────────────────


class TestBypassTierStatusAuth:
    def test_missing_token_returns_401(self, app_with_registry: Any) -> None:  # noqa: ANN401
        client = TestClient(app_with_registry)
        response = client.get("/api/voice/bypass-tier-status")
        assert response.status_code == 401


class TestBypassTierStatusRegistryAbsent:
    def test_missing_registry_returns_503(self) -> None:
        app = create_app(token=_TOKEN)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/bypass-tier-status")
        assert response.status_code == 503
        assert response.json()["error"] == "Engine not running"


class TestBypassTierStatusHappyPathV024Stub:
    """v0.24.0 foundation: empty-state shape, no strategies wired yet."""

    def test_returns_empty_state_shape(self, client: TestClient) -> None:
        response = client.get("/api/voice/bypass-tier-status")
        assert response.status_code == 200
        body = response.json()
        # current_bypass_tier=null when no bypass is engaged
        # (the v0.24.0 baseline — no strategies registered yet).
        assert body["current_bypass_tier"] is None
        # Per-tier counters all zero (no attempts yet).
        assert body["tier1_raw_attempted"] == 0
        assert body["tier1_raw_succeeded"] == 0
        assert body["tier2_host_api_rotate_attempted"] == 0
        assert body["tier2_host_api_rotate_succeeded"] == 0
        assert body["tier3_wasapi_exclusive_attempted"] == 0
        assert body["tier3_wasapi_exclusive_succeeded"] == 0

    def test_response_keys_match_zod_schema_contract(self, client: TestClient) -> None:
        """Wire contract: keys MUST match
        VoiceBypassTierStatusResponseSchema in
        dashboard/src/types/schemas.ts."""
        response = client.get("/api/voice/bypass-tier-status")
        assert response.status_code == 200
        body = response.json()
        expected_keys = {
            "current_bypass_tier",
            "tier1_raw_attempted",
            "tier1_raw_succeeded",
            "tier2_host_api_rotate_attempted",
            "tier2_host_api_rotate_succeeded",
            "tier3_wasapi_exclusive_attempted",
            "tier3_wasapi_exclusive_succeeded",
        }
        assert set(body.keys()) == expected_keys

    def test_counter_values_are_nonnegative_integers(self, client: TestClient) -> None:
        """Zod refinement: ``z.number().int().nonnegative()`` for every
        counter. The stub returns 0; this test pins the type contract."""
        response = client.get("/api/voice/bypass-tier-status")
        body = response.json()
        for key in (
            "tier1_raw_attempted",
            "tier1_raw_succeeded",
            "tier2_host_api_rotate_attempted",
            "tier2_host_api_rotate_succeeded",
            "tier3_wasapi_exclusive_attempted",
            "tier3_wasapi_exclusive_succeeded",
        ):
            value = body[key]
            assert isinstance(value, int), f"{key} must be int"
            assert value >= 0, f"{key} must be >= 0"

    def test_current_bypass_tier_is_null_or_int_in_range(self, client: TestClient) -> None:
        """Zod: ``z.number().int().min(0).max(3).nullable()``. v0.24.0
        always returns null; wire-up may return 0/1/2/3."""
        response = client.get("/api/voice/bypass-tier-status")
        body = response.json()
        value = body["current_bypass_tier"]
        assert value is None or (isinstance(value, int) and 0 <= value <= 3)
