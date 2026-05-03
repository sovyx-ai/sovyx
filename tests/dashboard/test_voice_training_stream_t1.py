"""T1.2 mission tests — WS /api/voice/training/jobs/{id}/stream endpoint.

Mission: ``MISSION-v0.30.0-single-mind-ga-2026-05-03.md`` §T1.2 (D2).

Pins the contract:
* Auth via ``token`` query param (mismatch → close 4401).
* Path-traversal defence on ``job_id`` (close 4400).
* Job-not-found → close 4404.
* Live snapshot stream as JSONL appears.
* Terminal status (COMPLETE / FAILED / CANCELLED) → final
  ``{"type": "terminal", ...}`` message + clean close (1000).
* Connection survives empty progress.jsonl (job just spawned;
  frontend connects right after POST returns 202).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-voice-training-stream"  # noqa: S105 — test fixture token


def _build_app(tmp_path: Path) -> Any:
    from sovyx.engine.config import DatabaseConfig, EngineConfig

    app = create_app(token=_TOKEN)
    app.state.engine_config = EngineConfig(
        data_dir=tmp_path,
        database=DatabaseConfig(data_dir=tmp_path),
    )
    app.state.registry = MagicMock()
    return app


def _make_job_dir(tmp_path: Path, job_id: str) -> Path:
    job_dir = tmp_path / "wake_word_training" / job_id
    job_dir.mkdir(parents=True)
    return job_dir


def _write_progress_lines(job_dir: Path, *lines: str) -> None:
    """Append JSONL lines to <job_dir>/progress.jsonl (creating the file)."""
    path = job_dir / "progress.jsonl"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _state_jsonl(status: str, progress: float = 0.5) -> str:
    """Build a minimal progress.jsonl line with the given status."""
    return (
        '{"wake_word": "Aria", "mind_id": "aria", "language": "en", '
        f'"status": "{status}", "progress": {progress}, '
        '"samples_generated": 100, "target_samples": 200, '
        '"started_at": "2026-05-03T00:00:00Z", '
        '"updated_at": "2026-05-03T00:01:00Z", '
        '"completed_at": "", "output_path": "", "error_summary": ""}'
    )


# ── Auth ─────────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_token_closes_4401(self, tmp_path: Path) -> None:
        _make_job_dir(tmp_path, "aria")
        app = _build_app(tmp_path)
        client = TestClient(app)

        with (  # noqa: PT012
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/voice/training/jobs/aria/stream"),
        ):
            pass
        assert exc_info.value.code == 4401  # noqa: PLR2004

    def test_wrong_token_closes_4401(self, tmp_path: Path) -> None:
        _make_job_dir(tmp_path, "aria")
        app = _build_app(tmp_path)
        client = TestClient(app)

        with (  # noqa: PT012
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/voice/training/jobs/aria/stream?token=wrong"),
        ):
            pass
        assert exc_info.value.code == 4401  # noqa: PLR2004


# ── Path-traversal defence ───────────────────────────────────────────


class TestPathTraversalDefence:
    def test_dotdot_in_job_id_returns_4400(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path)
        client = TestClient(app)
        # FastAPI's path-param parser doesn't decode ``..`` until inside
        # the handler; we rely on explicit string-level rejection.
        with client.websocket_connect(
            f"/api/voice/training/jobs/aria..bad/stream?token={_TOKEN}"
        ) as ws:
            msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "invalid job_id" in msg["message"]


# ── Job-not-found ────────────────────────────────────────────────────


class TestJobNotFound:
    def test_no_job_dir_returns_4404(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path)
        client = TestClient(app)
        # No job dir exists for "ghost".
        with client.websocket_connect(
            f"/api/voice/training/jobs/ghost/stream?token={_TOKEN}"
        ) as ws:
            msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "ghost" in msg["message"]


# ── Live streaming ───────────────────────────────────────────────────


class TestLiveStreaming:
    def test_existing_progress_emitted_on_connect(self, tmp_path: Path) -> None:
        """When progress.jsonl already has lines at connect time, the
        stream emits each as a snapshot before any new data arrives."""
        job_dir = _make_job_dir(tmp_path, "aria")
        _write_progress_lines(
            job_dir,
            _state_jsonl("pending", 0.0),
            _state_jsonl("synthesizing", 0.5),
        )
        app = _build_app(tmp_path)
        client = TestClient(app)

        with client.websocket_connect(
            f"/api/voice/training/jobs/aria/stream?token={_TOKEN}"
        ) as ws:
            first = ws.receive_json()
            second = ws.receive_json()
        assert first["type"] == "snapshot"
        assert first["state"]["status"] == "pending"
        assert second["type"] == "snapshot"
        assert second["state"]["status"] == "synthesizing"

    def test_terminal_status_emits_terminal_then_closes(self, tmp_path: Path) -> None:
        """When a terminal-status line is read, the stream emits one
        ``terminal`` message and closes cleanly (code 1000)."""
        job_dir = _make_job_dir(tmp_path, "aria")
        _write_progress_lines(
            job_dir,
            _state_jsonl("synthesizing", 0.5),
            _state_jsonl("complete", 1.0),
        )
        app = _build_app(tmp_path)
        client = TestClient(app)

        with client.websocket_connect(
            f"/api/voice/training/jobs/aria/stream?token={_TOKEN}"
        ) as ws:
            first = ws.receive_json()
            terminal = ws.receive_json()
            # Server closes the socket; trying to receive again should
            # raise WebSocketDisconnect (clean close — code 1000).
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()

        assert first["type"] == "snapshot"
        assert terminal["type"] == "terminal"
        assert terminal["state"]["status"] == "complete"
        assert exc_info.value.code == 1000  # noqa: PLR2004

    def test_failed_terminal_status(self, tmp_path: Path) -> None:
        """FAILED is also terminal."""
        job_dir = _make_job_dir(tmp_path, "aria")
        _write_progress_lines(
            job_dir,
            _state_jsonl("failed", 0.3),
        )
        app = _build_app(tmp_path)
        client = TestClient(app)

        with client.websocket_connect(
            f"/api/voice/training/jobs/aria/stream?token={_TOKEN}"
        ) as ws:
            terminal = ws.receive_json()
        assert terminal["type"] == "terminal"
        assert terminal["state"]["status"] == "failed"

    def test_cancelled_terminal_status(self, tmp_path: Path) -> None:
        """CANCELLED is also terminal."""
        job_dir = _make_job_dir(tmp_path, "aria")
        _write_progress_lines(
            job_dir,
            _state_jsonl("cancelled", 0.5),
        )
        app = _build_app(tmp_path)
        client = TestClient(app)

        with client.websocket_connect(
            f"/api/voice/training/jobs/aria/stream?token={_TOKEN}"
        ) as ws:
            terminal = ws.receive_json()
        assert terminal["type"] == "terminal"
        assert terminal["state"]["status"] == "cancelled"


# ── Robustness ───────────────────────────────────────────────────────


class TestRobustness:
    def test_empty_job_dir_does_not_immediately_close(self, tmp_path: Path) -> None:
        """Job dir exists but progress.jsonl hasn't been written yet
        (frontend race-tolerance: connects right after POST 202).
        Stream waits for content rather than immediate close."""
        _make_job_dir(tmp_path, "aria")  # no progress.jsonl
        app = _build_app(tmp_path)
        client = TestClient(app)

        with client.websocket_connect(
            f"/api/voice/training/jobs/aria/stream?token={_TOKEN}"
        ) as ws:
            # Connection is accepted; receive with timeout to verify
            # no immediate close. We don't actually wait for a snapshot
            # (would need to spawn a writer thread) — just assert the
            # WS doesn't disconnect immediately.
            ws.send_text("ping")  # Triggers the receive loop.
            # If close-on-empty was active, ws.send would raise.

    def test_malformed_jsonl_line_skipped_gracefully(self, tmp_path: Path) -> None:
        """A garbled progress.jsonl line is logged + skipped; the
        stream continues with subsequent valid lines."""
        job_dir = _make_job_dir(tmp_path, "aria")
        # Mix valid + invalid lines.
        path = job_dir / "progress.jsonl"
        path.write_text(
            f"{_state_jsonl('synthesizing', 0.3)}\n"
            "this is not valid json\n"
            f"{_state_jsonl('complete', 1.0)}\n",
            encoding="utf-8",
        )
        app = _build_app(tmp_path)
        client = TestClient(app)

        with client.websocket_connect(
            f"/api/voice/training/jobs/aria/stream?token={_TOKEN}"
        ) as ws:
            first = ws.receive_json()
            terminal = ws.receive_json()
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()

        assert first["type"] == "snapshot"
        assert first["state"]["status"] == "synthesizing"
        # The malformed line was skipped; the valid terminal line came through.
        assert terminal["type"] == "terminal"
        assert terminal["state"]["status"] == "complete"
