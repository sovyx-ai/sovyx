"""T1.1 mission tests — POST /api/voice/training/jobs/start endpoint.

Mission: ``MISSION-v0.30.0-single-mind-ga-2026-05-03.md`` §T1.1 (D1).

The new endpoint spawns a fire-and-forget training job via
``observability.tasks.spawn`` (the same primitive
``brain/consolidation.py:550`` uses for the consolidation scheduler).
These tests pin the contract for:

* HTTP 202 Accepted on happy path with ``{job_id, stream_url}``.
* HTTP 503 when the trainer backend is unavailable (operator hasn't
  installed extras OR called ``register_default_backend``).
* HTTP 409 Conflict when a job with the same ``job_id`` is already
  in flight (idempotency contract).
* HTTP 422 from pydantic on malformed bodies.
* HTTP 400 when wake_word ASCII-folds to nothing (Chinese-only,
  Cyrillic-only) OR negatives_dir doesn't exist.
* HTTP 401 on missing auth.

The orchestrator construction itself (Kokoro + synthesizer + tracker)
is mocked out — that integration is covered by the T1.6 integration
test using a stub ``TrainerBackend``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-voice-training-start"  # noqa: S105 — test fixture token


# ── Helpers ──────────────────────────────────────────────────────────


def _build_app(*, tmp_path: Path) -> Any:
    from sovyx.engine.config import DatabaseConfig, EngineConfig

    app = create_app(token=_TOKEN)
    app.state.engine_config = EngineConfig(
        data_dir=tmp_path,
        database=DatabaseConfig(data_dir=tmp_path),
    )
    registry = MagicMock()
    registry.is_registered = MagicMock(return_value=False)
    app.state.registry = registry
    return app


def _make_negatives_dir(tmp_path: Path) -> Path:
    """Operator's negatives_dir — must exist for the endpoint to accept."""
    neg = tmp_path / "negatives"
    neg.mkdir()
    return neg


def _valid_body(negatives_dir: Path) -> dict[str, Any]:
    return {
        "wake_word": "Aria",
        "mind_id": "aria",
        "language": "en",
        "target_samples": 200,
        "voices": [],
        "variants": [],
        "negatives_dir": str(negatives_dir),
    }


def _patch_orchestrator_chain() -> Any:
    """Mock the orchestrator construction chain so the endpoint
    exercises spawn() without spinning up Kokoro / a real backend.

    Returns a context manager that patches:
    * resolve_default_backend → MagicMock backend with .name="stub"
    * KokoroTTS, KokoroSampleSynthesizer, TrainingOrchestrator
    * spawn → MagicMock so we can assert it was called
    """
    backend = MagicMock()
    backend.name = "stub-backend"

    # Chain mocks for the orchestrator construction. The endpoint imports
    # these at the module-symbol level via ``from ... import ...``.
    backend_resolver = MagicMock(return_value=backend)
    kokoro_cls = MagicMock()
    synthesizer_cls = MagicMock()
    orchestrator_cls = MagicMock()
    spawn_fn = MagicMock()

    # The orchestrator's run() is a coroutine; we patch the class so the
    # instance's run() returns a real coroutine (must be awaitable for spawn).
    orchestrator_instance = MagicMock()

    async def _fake_run(*_a: Any, **_kw: Any) -> None:
        return None

    orchestrator_instance.run = _fake_run
    orchestrator_cls.return_value = orchestrator_instance

    return (
        (
            patch(
                "sovyx.voice.wake_word_training.resolve_default_backend",
                backend_resolver,
            ),
            patch("sovyx.voice.tts_kokoro.KokoroTTS", kokoro_cls),
            patch(
                "sovyx.voice.wake_word_training.KokoroSampleSynthesizer",
                synthesizer_cls,
            ),
            patch(
                "sovyx.voice.wake_word_training.TrainingOrchestrator",
                orchestrator_cls,
            ),
            patch("sovyx.observability.tasks.spawn", spawn_fn),
        ),
        spawn_fn,
        backend_resolver,
    )


# ── Auth ─────────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_token_returns_401(self, tmp_path: Path) -> None:
        neg = _make_negatives_dir(tmp_path)
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app)
        response = client.post("/api/voice/training/jobs/start", json=_valid_body(neg))
        assert response.status_code == 401  # noqa: PLR2004


# ── Pydantic body validation ─────────────────────────────────────────


class TestBodyValidation:
    def test_missing_wake_word_returns_422(self, tmp_path: Path) -> None:
        neg = _make_negatives_dir(tmp_path)
        body = _valid_body(neg)
        del body["wake_word"]
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post("/api/voice/training/jobs/start", json=body)
        assert response.status_code == 422  # noqa: PLR2004

    def test_target_samples_below_minimum_returns_422(self, tmp_path: Path) -> None:
        neg = _make_negatives_dir(tmp_path)
        body = _valid_body(neg)
        body["target_samples"] = 50  # below ge=100
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post("/api/voice/training/jobs/start", json=body)
        assert response.status_code == 422  # noqa: PLR2004

    def test_target_samples_above_maximum_returns_422(self, tmp_path: Path) -> None:
        neg = _make_negatives_dir(tmp_path)
        body = _valid_body(neg)
        body["target_samples"] = 100_000  # above le=10000
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post("/api/voice/training/jobs/start", json=body)
        assert response.status_code == 422  # noqa: PLR2004


# ── Backend availability (HTTP 503) ──────────────────────────────────


class TestBackendUnavailable:
    def test_503_when_resolve_default_backend_raises(self, tmp_path: Path) -> None:
        neg = _make_negatives_dir(tmp_path)
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        with patch(
            "sovyx.voice.wake_word_training.resolve_default_backend",
            side_effect=RuntimeError("no trainer backend registered; install [training] extras"),
        ):
            response = client.post("/api/voice/training/jobs/start", json=_valid_body(neg))

        assert response.status_code == 503  # noqa: PLR2004
        detail = response.json()["detail"]
        assert "Trainer backend unavailable" in detail
        assert "register_default_backend" in detail


# ── Filesystem validation ────────────────────────────────────────────


class TestFilesystemValidation:
    def test_400_when_negatives_dir_does_not_exist(self, tmp_path: Path) -> None:
        # Don't create the negatives dir.
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        body = _valid_body(tmp_path / "nonexistent")

        with patch(
            "sovyx.voice.wake_word_training.resolve_default_backend",
            return_value=MagicMock(name="backend"),
        ):
            response = client.post("/api/voice/training/jobs/start", json=body)

        assert response.status_code == 400  # noqa: PLR2004
        assert "negatives_dir does not exist" in response.json()["detail"]

    def test_400_when_wake_word_ascii_folds_to_nothing(self, tmp_path: Path) -> None:
        """Cyrillic-only / Chinese-only wake words slugify to an
        all-underscore string. The endpoint refuses these because
        the resulting job_id is opaque + Kokoro G2P needs Latin script."""
        neg = _make_negatives_dir(tmp_path)
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        body = _valid_body(neg)
        body["wake_word"] = "你好"  # Chinese-only

        with patch(
            "sovyx.voice.wake_word_training.resolve_default_backend",
            return_value=MagicMock(name="backend"),
        ):
            response = client.post("/api/voice/training/jobs/start", json=body)

        assert response.status_code == 400  # noqa: PLR2004
        assert "no ASCII alphanumeric" in response.json()["detail"]


# ── Idempotency / 409 Conflict ───────────────────────────────────────


class TestIdempotency:
    def test_409_when_job_already_in_flight(self, tmp_path: Path) -> None:
        """Pre-existing non-terminal progress.jsonl → 409 Conflict
        with an operator-actionable detail message."""
        neg = _make_negatives_dir(tmp_path)
        app = _build_app(tmp_path=tmp_path)

        # Seed an "in flight" job state in the training root.
        job_dir = tmp_path / "wake_word_training" / "aria"
        job_dir.mkdir(parents=True)
        (job_dir / "progress.jsonl").write_text(
            # Status = SYNTHESIZING (non-terminal).
            '{"wake_word": "Aria", "mind_id": "aria", "language": "en", '
            '"status": "synthesizing", "progress": 0.5, '
            '"samples_generated": 100, "target_samples": 200, '
            '"started_at": "2026-05-03T00:00:00Z", '
            '"updated_at": "2026-05-03T00:01:00Z", '
            '"completed_at": "", "output_path": "", "error_summary": ""}\n',
            encoding="utf-8",
        )

        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        with patch(
            "sovyx.voice.wake_word_training.resolve_default_backend",
            return_value=MagicMock(name="backend"),
        ):
            response = client.post("/api/voice/training/jobs/start", json=_valid_body(neg))

        assert response.status_code == 409  # noqa: PLR2004
        detail = response.json()["detail"]
        assert "aria" in detail
        assert "already in flight" in detail

    def test_re_submit_after_terminal_state_is_permitted(self, tmp_path: Path) -> None:
        """Pre-existing TERMINAL progress.jsonl → endpoint accepts
        re-submission (operator's intent to retrain is the signal)."""
        neg = _make_negatives_dir(tmp_path)
        app = _build_app(tmp_path=tmp_path)

        job_dir = tmp_path / "wake_word_training" / "aria"
        job_dir.mkdir(parents=True)
        (job_dir / "progress.jsonl").write_text(
            # Status = COMPLETE (terminal).
            '{"wake_word": "Aria", "mind_id": "aria", "language": "en", '
            '"status": "complete", "progress": 1.0, '
            '"samples_generated": 200, "target_samples": 200, '
            '"started_at": "2026-05-03T00:00:00Z", '
            '"updated_at": "2026-05-03T00:30:00Z", '
            '"completed_at": "2026-05-03T00:30:00Z", '
            '"output_path": "/path/to/aria.onnx", "error_summary": ""}\n',
            encoding="utf-8",
        )

        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        # Mock the full orchestrator chain so spawn doesn't try to run
        # a real Kokoro instance.
        backend = MagicMock()
        backend.name = "stub"

        async def _fake_run(*_a: Any, **_kw: Any) -> None:
            return None

        orch_instance = MagicMock()
        orch_instance.run = _fake_run

        with (
            patch(
                "sovyx.voice.wake_word_training.resolve_default_backend",
                return_value=backend,
            ),
            patch("sovyx.voice.tts_kokoro.KokoroTTS"),
            patch("sovyx.voice.wake_word_training.KokoroSampleSynthesizer"),
            patch(
                "sovyx.voice.wake_word_training.TrainingOrchestrator",
                return_value=orch_instance,
            ),
            patch("sovyx.observability.tasks.spawn") as mock_spawn,
        ):
            response = client.post("/api/voice/training/jobs/start", json=_valid_body(neg))

        # Drain the coroutine that spawn was passed (avoid RuntimeWarning).
        if mock_spawn.call_args is not None:
            coro_arg = mock_spawn.call_args.args[0]
            if hasattr(coro_arg, "close"):
                coro_arg.close()

        assert response.status_code == 202  # noqa: PLR2004


# ── Happy path (HTTP 202 + spawn called) ─────────────────────────────


class TestHappyPath:
    def test_202_accepted_with_job_id_and_stream_url(self, tmp_path: Path) -> None:
        neg = _make_negatives_dir(tmp_path)
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        backend = MagicMock()
        backend.name = "stub"

        async def _fake_run(*_a: Any, **_kw: Any) -> None:
            return None

        orch_instance = MagicMock()
        orch_instance.run = _fake_run

        with (
            patch(
                "sovyx.voice.wake_word_training.resolve_default_backend",
                return_value=backend,
            ),
            patch("sovyx.voice.tts_kokoro.KokoroTTS"),
            patch("sovyx.voice.wake_word_training.KokoroSampleSynthesizer"),
            patch(
                "sovyx.voice.wake_word_training.TrainingOrchestrator",
                return_value=orch_instance,
            ),
            patch("sovyx.observability.tasks.spawn") as mock_spawn,
        ):
            response = client.post("/api/voice/training/jobs/start", json=_valid_body(neg))

        # Drain the coroutine that spawn was passed.
        if mock_spawn.call_args is not None:
            coro_arg = mock_spawn.call_args.args[0]
            if hasattr(coro_arg, "close"):
                coro_arg.close()

        assert response.status_code == 202  # noqa: PLR2004
        body = response.json()
        assert body == {
            "job_id": "aria",
            "stream_url": "/api/voice/training/jobs/aria/stream",
        }
        # spawn was called once with name="training-aria".
        mock_spawn.assert_called_once()
        kwargs = mock_spawn.call_args.kwargs
        assert kwargs["name"] == "training-aria"

    def test_diacritic_wake_word_slugifies_correctly(self, tmp_path: Path) -> None:
        """Wake_word "Lúcia" → job_id "lucia" (ASCII-fold + lowercase)."""
        neg = _make_negatives_dir(tmp_path)
        app = _build_app(tmp_path=tmp_path)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        body = _valid_body(neg)
        body["wake_word"] = "Lúcia"

        backend = MagicMock()
        backend.name = "stub"

        async def _fake_run(*_a: Any, **_kw: Any) -> None:
            return None

        orch_instance = MagicMock()
        orch_instance.run = _fake_run

        with (
            patch(
                "sovyx.voice.wake_word_training.resolve_default_backend",
                return_value=backend,
            ),
            patch("sovyx.voice.tts_kokoro.KokoroTTS"),
            patch("sovyx.voice.wake_word_training.KokoroSampleSynthesizer"),
            patch(
                "sovyx.voice.wake_word_training.TrainingOrchestrator",
                return_value=orch_instance,
            ),
            patch("sovyx.observability.tasks.spawn") as mock_spawn,
        ):
            response = client.post("/api/voice/training/jobs/start", json=body)

        if mock_spawn.call_args is not None:
            coro_arg = mock_spawn.call_args.args[0]
            if hasattr(coro_arg, "close"):
                coro_arg.close()

        assert response.status_code == 202  # noqa: PLR2004
        assert response.json()["job_id"] == "lucia"
