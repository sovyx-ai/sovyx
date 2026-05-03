"""T1 mission tests — GET /api/voice/wake-word/status endpoint.

Mission: ``MISSION-wake-word-ui-2026-05-03.md`` §T1 (D1+D2).

Validates: auth, response shape, NONE-strategy reporting, healthy
mind, cold-start behavior (registry / engine_config not ready), and
the full mixed-list operator scenario.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.engine.types import MindId
from sovyx.voice._wake_word_router import WakeWordRouter

_TOKEN = "test-token-voice-wake-word-status"  # noqa: S105 — test fixture token


# ── Helpers ──────────────────────────────────────────────────────────


def _write_mind_yaml(
    data_dir: Path,
    mind_id: str,
    *,
    wake_word: str = "Aria",
    wake_word_enabled: bool = False,
    voice_language: str = "en",
) -> None:
    mind_dir = data_dir / mind_id
    mind_dir.mkdir(parents=True, exist_ok=True)
    enabled_str = "true" if wake_word_enabled else "false"
    (mind_dir / "mind.yaml").write_text(
        f"id: {mind_id}\n"
        f"name: {mind_id.capitalize()}\n"
        f"wake_word: {wake_word}\n"
        f"wake_word_enabled: {enabled_str}\n"
        f"voice_language: {voice_language}\n",
        encoding="utf-8",
    )


def _write_pretrained_model(data_dir: Path, name: str) -> Path:
    pool = data_dir / "wake_word_models" / "pretrained"
    pool.mkdir(parents=True, exist_ok=True)
    target = pool / f"{name}.onnx"
    target.write_bytes(b"fake onnx bytes")
    return target


def _patch_onnxruntime() -> object:
    mock_ort = MagicMock()
    mock_ort.SessionOptions.return_value = MagicMock()
    mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
    session = MagicMock()
    inputs_meta = MagicMock()
    inputs_meta.name = "input"
    session.get_inputs.return_value = [inputs_meta]
    session.run.side_effect = lambda *_a, **_kw: [np.array([[0.1]], dtype=np.float32)]
    mock_ort.InferenceSession.return_value = session
    return patch.dict("sys.modules", {"onnxruntime": mock_ort})


def _build_app(
    *,
    tmp_path: Path,
    pipeline: object | None = None,
    no_engine_config: bool = False,
) -> Any:
    """Build a test app. ``pipeline`` is the live pipeline mock OR
    None (voice subsystem not running). ``no_engine_config=True``
    simulates the daemon-still-booting state."""
    from sovyx.engine.config import DatabaseConfig, EngineConfig
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

    app = create_app(token=_TOKEN)
    if not no_engine_config:
        app.state.engine_config = EngineConfig(
            data_dir=tmp_path,
            database=DatabaseConfig(data_dir=tmp_path),
        )

    registry = MagicMock()
    if pipeline is None:
        registry.is_registered = MagicMock(return_value=False)
    else:

        def _is_registered(cls: object) -> bool:
            return cls is VoicePipeline

        registry.is_registered = MagicMock(side_effect=_is_registered)
        registry.resolve = AsyncMock(return_value=pipeline)
    app.state.registry = registry
    return app


# ── Auth ─────────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_token_returns_401(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app)
        response = client.get("/api/voice/wake-word/status")
        assert response.status_code == 401  # noqa: PLR2004


# ── Cold-start (engine_config not ready) ─────────────────────────────


class TestColdStart:
    def test_no_engine_config_returns_empty_minds(self, tmp_path: Path) -> None:
        """Daemon still booting — engine_config not yet on app.state.
        Returns empty list with 200, NOT 503, so the dashboard renders
        the no-minds-yet placeholder cleanly."""
        app = _build_app(tmp_path=tmp_path, no_engine_config=True)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/wake-word/status")
        assert response.status_code == 200  # noqa: PLR2004
        assert response.json() == {"minds": []}

    def test_no_minds_on_disk_returns_empty_minds(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/wake-word/status")
        assert response.status_code == 200  # noqa: PLR2004
        assert response.json() == {"minds": []}


# ── Voice subsystem not running ──────────────────────────────────────


class TestVoiceSubsystemNotRunning:
    """When VoicePipeline isn't registered (cold-start, voice disabled),
    the helper still returns per-mind entries; just all with
    ``runtime_registered=False``."""

    def test_runtime_registered_always_false_when_no_pipeline(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_pretrained_model(tmp_path, "aria")

        app = _build_app(tmp_path=tmp_path, pipeline=None)  # no pipeline
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/wake-word/status")

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert len(body["minds"]) == 1
        entry = body["minds"][0]
        assert entry["mind_id"] == "aria"
        assert entry["wake_word_enabled"] is True
        assert entry["runtime_registered"] is False  # cold-start
        assert entry["resolution_strategy"] == "exact"
        assert entry["model_path"].endswith("aria.onnx")


# ── Live pipeline with router ────────────────────────────────────────


class TestLivePipelineWithRouter:
    def test_runtime_registered_true_for_registered_mind(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_pretrained_model(tmp_path, "aria")

        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        # Build a real router with aria registered.
        router = WakeWordRouter()
        with _patch_onnxruntime():
            router.register_mind(MindId("aria"), model_path=Path("/fake/aria.onnx"))

        pipeline = MagicMock(spec=VoicePipeline)
        pipeline._wake_word_router = router  # noqa: SLF001

        app = _build_app(tmp_path=tmp_path, pipeline=pipeline)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/wake-word/status")

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert body["minds"][0]["runtime_registered"] is True


# ── Silent-degradation gap closed ────────────────────────────────────


class TestSilentDegradeGapClosed:
    """The v0.28.3 T2 silent-degradation observability gap: an
    operator persists ``wake_word_enabled=true`` for a mind whose
    ONNX is missing → boot tolerance catches + degrades to
    router=None → operator's dashboard previously showed nothing.

    Post-T1, the dashboard surfaces ``runtime_registered=False`` +
    ``last_error=<remediation>`` so the operator gets actionable
    signal."""

    def test_broken_mind_surfaces_last_error(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "lucia", wake_word="Lucia", wake_word_enabled=True)
        # NO pretrained model — the broken-state condition.

        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        # Pipeline registered, but router is None (T2 degraded path).
        pipeline = MagicMock(spec=VoicePipeline)
        pipeline._wake_word_router = None  # noqa: SLF001

        app = _build_app(tmp_path=tmp_path, pipeline=pipeline)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/wake-word/status")

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert len(body["minds"]) == 1
        entry = body["minds"][0]
        assert entry["wake_word_enabled"] is True
        assert entry["runtime_registered"] is False
        assert entry["resolution_strategy"] == "none"
        assert entry["last_error"] is not None
        assert "train-wake-word" in entry["last_error"]


# ── Mixed list (the realistic operator scenario) ─────────────────────


class TestMixedListIntegration:
    def test_three_mind_states_render_correctly(self, tmp_path: Path) -> None:
        """One healthy + registered, one configured-but-not-running,
        one disabled."""
        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_mind_yaml(tmp_path, "lucia", wake_word="Lucia", wake_word_enabled=True)
        _write_mind_yaml(tmp_path, "joao", wake_word="Joao", wake_word_enabled=False)
        _write_pretrained_model(tmp_path, "aria")

        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        router = WakeWordRouter()
        with _patch_onnxruntime():
            router.register_mind(MindId("aria"), model_path=Path("/fake/aria.onnx"))

        pipeline = MagicMock(spec=VoicePipeline)
        pipeline._wake_word_router = router  # noqa: SLF001

        app = _build_app(tmp_path=tmp_path, pipeline=pipeline)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/wake-word/status")

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert len(body["minds"]) == 3
        by_mind = {entry["mind_id"]: entry for entry in body["minds"]}

        assert by_mind["aria"]["runtime_registered"] is True
        assert by_mind["aria"]["resolution_strategy"] == "exact"
        assert by_mind["aria"]["last_error"] is None

        assert by_mind["lucia"]["runtime_registered"] is False
        assert by_mind["lucia"]["resolution_strategy"] == "none"
        assert by_mind["lucia"]["last_error"] is not None

        assert by_mind["joao"]["wake_word_enabled"] is False
        assert by_mind["joao"]["resolution_strategy"] is None
        assert by_mind["joao"]["last_error"] is None


# ── Response shape contract (pydantic-validated) ─────────────────────


class TestResponseShape:
    def test_response_matches_pydantic_schema(self, tmp_path: Path) -> None:
        """Pin the wire format. If any field name drifts, this test
        breaks BEFORE the frontend zod schemas catch it at runtime."""
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=False)
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.get("/api/voice/wake-word/status")

        assert response.status_code == 200  # noqa: PLR2004
        body = response.json()
        assert "minds" in body
        assert isinstance(body["minds"], list)
        entry = body["minds"][0]
        # Exact field set — adding / removing changes the API.
        # T1 of v0.29.1 added matched_name + phoneme_distance.
        expected_fields = {
            "mind_id",
            "wake_word",
            "voice_language",
            "wake_word_enabled",
            "runtime_registered",
            "model_path",
            "resolution_strategy",
            "matched_name",
            "phoneme_distance",
            "last_error",
        }
        assert set(entry.keys()) == expected_fields
