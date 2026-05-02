"""Tests for ``/api/voice/training/*`` — Phase 8 / T8.13.

Covers all 3 endpoints (list jobs, get job detail, cancel job)
against a synthesized progress.jsonl fixture (no real training
needed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.voice.wake_word_training._progress import ProgressTracker
from sovyx.voice.wake_word_training._state import (
    TrainingJobState,
    TrainingStatus,
)

_TOKEN = "test-token-training-t813"  # noqa: S105


# ── Helpers ─────────────────────────────────────────────────────────


def _build_app(*, tmp_path: Path) -> Any:  # noqa: ANN401
    from sovyx.engine.config import DatabaseConfig, EngineConfig

    app = create_app(token=_TOKEN)
    app.state.engine_config = EngineConfig(
        data_dir=tmp_path,
        database=DatabaseConfig(data_dir=tmp_path),
    )
    return app


def _client(app: Any) -> TestClient:  # noqa: ANN401
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _seed_job(
    *,
    tmp_path: Path,
    job_id: str,
    final_status: TrainingStatus = TrainingStatus.COMPLETE,
    n_synth_events: int = 3,
) -> Path:
    """Seed a training job directory with a realistic progress.jsonl."""
    job_dir = tmp_path / "wake_word_training" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    tracker = ProgressTracker(job_dir / "progress.jsonl")
    state = TrainingJobState.initial(
        wake_word=job_id.title(),
        mind_id=job_id,
        language="en-US",
        target_samples=10,
    )
    tracker.append(state)

    state = state.with_status(
        TrainingStatus.SYNTHESIZING,
        progress=0.0,
        message="starting synth",
    )
    tracker.append(state)

    for i in range(n_synth_events):
        state = state.with_status(
            TrainingStatus.SYNTHESIZING,
            progress=(i + 1) / n_synth_events,
            samples_generated=i + 1,
            message=f"sample {i + 1}",
        )
        tracker.append(state)

    state = state.with_status(
        TrainingStatus.TRAINING,
        progress=0.0,
        message="loading negatives",
    )
    tracker.append(state)

    if final_status is TrainingStatus.COMPLETE:
        state = state.with_status(
            TrainingStatus.COMPLETE,
            progress=1.0,
            output_path=str(job_dir / "model.onnx"),
            message="trained",
        )
    elif final_status is TrainingStatus.FAILED:
        state = state.with_status(
            TrainingStatus.FAILED,
            error_summary="stub failure",
        )
    elif final_status is TrainingStatus.CANCELLED:
        state = state.with_status(
            TrainingStatus.CANCELLED,
            message="user cancelled",
        )
    tracker.append(state)
    return job_dir


# ── Auth ────────────────────────────────────────────────────────────


class TestAuth:
    def test_list_requires_token(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        response = TestClient(app).get("/api/voice/training/jobs")
        assert response.status_code == 401  # noqa: PLR2004

    def test_get_requires_token(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        response = TestClient(app).get("/api/voice/training/jobs/x")
        assert response.status_code == 401  # noqa: PLR2004

    def test_cancel_requires_token(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        response = TestClient(app).post("/api/voice/training/jobs/x/cancel")
        assert response.status_code == 401  # noqa: PLR2004


# ── GET /jobs ───────────────────────────────────────────────────────


class TestListJobs:
    def test_empty_when_no_training_root(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["jobs"] == []
        assert data["total_count"] == 0

    def test_lists_seeded_jobs(self, tmp_path: Path) -> None:
        _seed_job(tmp_path=tmp_path, job_id="lucia")
        _seed_job(
            tmp_path=tmp_path,
            job_id="muller",
            final_status=TrainingStatus.FAILED,
        )
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["total_count"] == 2  # noqa: PLR2004
        ids = {j["job_id"] for j in data["jobs"]}
        assert ids == {"lucia", "muller"}
        # Statuses + summary fields populated.
        statuses = {j["job_id"]: j["status"] for j in data["jobs"]}
        assert statuses["lucia"] == "complete"
        assert statuses["muller"] == "failed"

    def test_skips_directories_without_progress_jsonl(
        self,
        tmp_path: Path,
    ) -> None:
        # Create a "ghost" directory that has no progress.jsonl.
        (tmp_path / "wake_word_training" / "ghost").mkdir(parents=True)
        # And a real job.
        _seed_job(tmp_path=tmp_path, job_id="real")

        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs")
        assert response.status_code == 200  # noqa: PLR2004
        ids = {j["job_id"] for j in response.json()["jobs"]}
        assert ids == {"real"}

    def test_cancel_signal_surfaces_in_summary(self, tmp_path: Path) -> None:
        job_dir = _seed_job(tmp_path=tmp_path, job_id="lucia")
        # Touch the cancel signal — the job's terminal state is
        # already COMPLETE so the cancel has no real effect, but it
        # SHOULD surface in the summary's ``cancelled_signalled`` field.
        (job_dir / ".cancel").touch()

        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs")
        data = response.json()
        assert data["jobs"][0]["cancelled_signalled"] is True


# ── GET /jobs/{job_id} ──────────────────────────────────────────────


class TestGetJob:
    def test_returns_full_history(self, tmp_path: Path) -> None:
        _seed_job(tmp_path=tmp_path, job_id="lucia", n_synth_events=5)
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs/lucia")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["summary"]["job_id"] == "lucia"
        assert data["summary"]["status"] == "complete"
        # History: PENDING + SYNTHESIZING-init + 5 sample events +
        # TRAINING + COMPLETE = 9 events.
        assert len(data["history"]) == 9  # noqa: PLR2004
        assert data["history_truncated"] is False
        # First event has the canonical PENDING shape.
        assert data["history"][0]["status"] == "pending"
        assert data["history"][-1]["status"] == "complete"

    def test_missing_job_returns_404(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs/nonexistent")
        assert response.status_code == 404  # noqa: PLR2004

    def test_empty_job_id_rejected(self, tmp_path: Path) -> None:
        """Whitespace job_id reaches the handler + gets rejected
        with 400 by the explicit guard."""
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs/%20%20")
        assert response.status_code == 400  # noqa: PLR2004
        assert "non-empty" in response.json()["detail"]

    def test_path_traversal_in_job_id_rejected(self, tmp_path: Path) -> None:
        """A job_id with ``..`` that REACHES the handler is rejected
        with 400. (httpx-side URL normalisation collapses raw ``..``
        before it gets here, so this test uses the URL-encoded
        variant ``%2E%2E`` that survives normalisation.)"""
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs/%2E%2E")
        # Either 400 (our defence) or 404 (route normalisation
        # variants across starlette versions). The response MUST NOT
        # be 200 with arbitrary directory contents.
        assert response.status_code in {400, 404}

    def test_history_truncation_on_high_event_count(
        self,
        tmp_path: Path,
    ) -> None:
        # Seed 250 synth events → total events ~254, default limit=200.
        _seed_job(tmp_path=tmp_path, job_id="big", n_synth_events=250)
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs/big")
        data = response.json()
        assert data["history_truncated"] is True
        assert len(data["history"]) == 200  # noqa: PLR2004 — default limit

    def test_history_limit_query_param_honoured(self, tmp_path: Path) -> None:
        _seed_job(tmp_path=tmp_path, job_id="lucia", n_synth_events=5)
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs/lucia?limit=3")
        data = response.json()
        assert len(data["history"]) == 3  # noqa: PLR2004
        # Truncation flag must be True since 9 > 3.
        assert data["history_truncated"] is True

    def test_history_limit_clamped_to_max(self, tmp_path: Path) -> None:
        _seed_job(tmp_path=tmp_path, job_id="lucia")
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs/lucia?limit=99999")
        # No 422 — endpoint clamps internally.
        assert response.status_code == 200  # noqa: PLR2004


# ── POST /jobs/{job_id}/cancel ──────────────────────────────────────


class TestCancelJob:
    def test_creates_cancel_file(self, tmp_path: Path) -> None:
        job_dir = _seed_job(
            tmp_path=tmp_path,
            job_id="lucia",
            final_status=TrainingStatus.SYNTHESIZING,  # in-flight
            # Force a non-terminal final by overriding final_status —
            # but our helper writes a final entry; let's just check
            # the .cancel file is created regardless.
        )
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.post("/api/voice/training/jobs/lucia/cancel")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["cancel_signal_written"] is True
        assert data["job_id"] == "lucia"
        # The .cancel file exists on disk.
        assert (job_dir / ".cancel").exists()

    def test_already_terminal_flag_set(self, tmp_path: Path) -> None:
        _seed_job(
            tmp_path=tmp_path,
            job_id="lucia",
            final_status=TrainingStatus.COMPLETE,
        )
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.post("/api/voice/training/jobs/lucia/cancel")
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["already_terminal"] is True

    def test_idempotent_double_cancel(self, tmp_path: Path) -> None:
        _seed_job(tmp_path=tmp_path, job_id="lucia")
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        first = client.post("/api/voice/training/jobs/lucia/cancel")
        second = client.post("/api/voice/training/jobs/lucia/cancel")
        assert first.status_code == 200  # noqa: PLR2004
        assert second.status_code == 200  # noqa: PLR2004
        # Both report success — touch is idempotent.
        assert first.json()["cancel_signal_written"] is True
        assert second.json()["cancel_signal_written"] is True

    def test_missing_job_returns_404(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.post("/api/voice/training/jobs/nonexistent/cancel")
        assert response.status_code == 404  # noqa: PLR2004

    def test_no_engine_config_returns_503(self, tmp_path: Path) -> None:
        """When EngineConfig is unavailable (very-early boot), cancel
        refuses with 503 — better than touching a stale fallback path."""
        app = create_app(token=_TOKEN)  # no engine_config injected
        client = _client(app)
        response = client.post("/api/voice/training/jobs/lucia/cancel")
        assert response.status_code == 503  # noqa: PLR2004


# ── End-to-end smoke ────────────────────────────────────────────────


class TestRoundTrip:
    def test_seed_list_get_cancel_round_trip(self, tmp_path: Path) -> None:
        """Full operator flow: seed → list → get detail → cancel."""
        job_dir = _seed_job(
            tmp_path=tmp_path,
            job_id="aria",
            final_status=TrainingStatus.SYNTHESIZING,
        )
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)

        # List shows aria.
        list_response = client.get("/api/voice/training/jobs")
        assert list_response.status_code == 200  # noqa: PLR2004
        ids = {j["job_id"] for j in list_response.json()["jobs"]}
        assert "aria" in ids

        # Detail returns the history.
        detail = client.get("/api/voice/training/jobs/aria")
        assert detail.status_code == 200  # noqa: PLR2004
        history = detail.json()["history"]
        assert len(history) >= 2  # noqa: PLR2004 — at least PENDING + something

        # Cancel writes the .cancel file.
        cancel = client.post("/api/voice/training/jobs/aria/cancel")
        assert cancel.status_code == 200  # noqa: PLR2004
        assert (job_dir / ".cancel").exists()


# ── Surface-level: history items round-trip through JSON cleanly ────


class TestHistoryShape:
    def test_history_items_are_json_decodable(self, tmp_path: Path) -> None:
        _seed_job(tmp_path=tmp_path, job_id="lucia")
        app = _build_app(tmp_path=tmp_path)
        client = _client(app)
        response = client.get("/api/voice/training/jobs/lucia")
        # The response IS already JSON; this asserts every event has
        # the canonical keys frontends expect.
        for event in response.json()["history"]:
            assert "status" in event
            assert "wake_word" in event
            assert "progress" in event
        # Every event re-encodes cleanly.
        json.dumps(response.json())
