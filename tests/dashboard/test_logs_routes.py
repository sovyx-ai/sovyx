"""Tests for ``/api/logs/*`` REST endpoints.

The Phase-10 logs router exposes seven HTTP endpoints (and one
WebSocket) that the dashboard's log viewer, saga timeline, narrative
panel, causality graph, and anomaly stream all depend on. These
tests pin every documented branch:

* FTS5-backed paths when ``app.state.fts_indexer`` is wired.
* File-scan fall-backs when the indexer is absent.
* Auth gating via the shared ``verify_token`` dependency.
* Query-parameter validation (limits, length caps).
* Graceful error responses (503 when the FTS sidecar is missing on
  a route that requires it).

The router is a thin orchestration layer over ``FTSIndexer`` and
``query_logs``/``query_saga``; all unit-level behaviour of those
helpers is covered separately. These tests verify the *wiring*:
parameters flow into the right helper, and the responses are
shaped exactly as the dashboard's zod schemas expect.

Aligned with IMPL-OBSERVABILITY-001 §16 (Phase 10, Task 10.2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app

if TYPE_CHECKING:
    from fastapi import FastAPI

_TOKEN = "test-token-logs"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


# ── App fixtures ────────────────────────────────────────────────────────────


@pytest.fixture()
def app_no_indexer() -> FastAPI:
    """App without ``fts_indexer`` on state — exercises the file-scan paths."""
    return create_app(token=_TOKEN)


@pytest.fixture()
def app_with_indexer() -> tuple[FastAPI, AsyncMock]:
    """App with a mocked async ``fts_indexer.search`` on state."""
    application = create_app(token=_TOKEN)
    indexer = MagicMock()
    indexer.search = AsyncMock(return_value=[])
    application.state.fts_indexer = indexer
    return application, indexer.search


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, headers=_AUTH)


def _seed_log_file(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    """Write *entries* as JSONL to a temp file and return its Path."""
    log_file = tmp_path / "sovyx.log"
    log_file.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )
    return log_file


# ── /api/logs (legacy file-scan) ────────────────────────────────────────────


class TestGetLogsLegacy:
    """``GET /api/logs`` always uses the file-scan helper, never FTS."""

    def test_returns_empty_when_no_log_file(self, app_no_indexer: FastAPI) -> None:
        # ``app.state.log_file`` is unset → query_logs(None) → [].
        resp = _client(app_no_indexer).get("/api/logs")
        assert resp.status_code == 200
        assert resp.json() == {"entries": []}

    def test_filters_flow_into_query_logs(self, app_no_indexer: FastAPI, tmp_path: Path) -> None:
        log_file = _seed_log_file(
            tmp_path,
            [
                {
                    "event": "engine_started",
                    "level": "info",
                    "logger": "sovyx.engine",
                    "timestamp": "2026-04-01T10:00:00",
                },
                {
                    "event": "db_error",
                    "level": "error",
                    "logger": "sovyx.persistence",
                    "timestamp": "2026-04-01T10:00:01",
                },
            ],
        )
        app_no_indexer.state.log_file = log_file
        resp = _client(app_no_indexer).get(
            "/api/logs",
            params={"level": "error", "limit": 10},
        )
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["event"] == "db_error"

    def test_limit_zero_returns_no_entries(self, app_no_indexer: FastAPI, tmp_path: Path) -> None:
        log_file = _seed_log_file(
            tmp_path,
            [
                {
                    "event": "x",
                    "level": "info",
                    "logger": "sovyx",
                    "timestamp": "2026-04-01T10:00:00",
                },
            ],
        )
        app_no_indexer.state.log_file = log_file
        resp = _client(app_no_indexer).get("/api/logs", params={"limit": 0})
        assert resp.status_code == 200
        assert resp.json() == {"entries": []}

    def test_limit_above_cap_is_rejected(self, app_no_indexer: FastAPI) -> None:
        # Pydantic Query(le=1000) → 422.
        resp = _client(app_no_indexer).get("/api/logs", params={"limit": 1001})
        assert resp.status_code == 422


# ── /api/logs/search (FTS5) ─────────────────────────────────────────────────


class TestSearchLogsFts:
    """``GET /api/logs/search`` requires the FTS indexer; 503 otherwise."""

    def test_503_when_indexer_missing(self, app_no_indexer: FastAPI) -> None:
        resp = _client(app_no_indexer).get("/api/logs/search", params={"q": "anything"})
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "fts_indexer not configured"
        assert body["fallback"] == "/api/logs"

    def test_filters_forwarded_to_indexer(
        self, app_with_indexer: tuple[FastAPI, AsyncMock]
    ) -> None:
        app, search = app_with_indexer
        search.return_value = [
            {
                "timestamp": "2026-04-01T10:00:00Z",
                "level": "info",
                "logger": "sovyx.brain",
                "event": "concept_created",
                "message": "ok",
                "saga_id": "abc",
                "content": "{}",
                "snippet": "<mark>ok</mark>",
            }
        ]
        resp = _client(app).get(
            "/api/logs/search",
            params={
                "q": "concept",
                "level": "INFO",
                "logger": "sovyx.brain",
                "saga_id": "abc",
                "since": "2026-04-01T00:00:00Z",
                "until": "2026-04-02T00:00:00Z",
                "limit": 50,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["query"] == "concept"
        assert body["count"] == 1
        assert body["filters"]["level"] == "INFO"
        assert body["filters"]["saga_id"] == "abc"
        # The mock recorded the call — check the structured args were unpacked.
        kwargs = search.call_args.kwargs
        assert kwargs["level"] == "INFO"
        assert kwargs["logger_name"] == "sovyx.brain"
        assert kwargs["saga_id"] == "abc"
        assert kwargs["limit"] == 50
        # since/until got parsed into unix epoch floats.
        assert isinstance(kwargs["since_unix"], float)
        assert isinstance(kwargs["until_unix"], float)

    def test_invalid_iso_timestamps_become_none(
        self, app_with_indexer: tuple[FastAPI, AsyncMock]
    ) -> None:
        app, search = app_with_indexer
        resp = _client(app).get(
            "/api/logs/search",
            params={"q": "x", "since": "not-a-date", "until": "also-bad"},
        )
        assert resp.status_code == 200
        kwargs = search.call_args.kwargs
        assert kwargs["since_unix"] is None
        assert kwargs["until_unix"] is None

    def test_limit_cap_enforced(self, app_with_indexer: tuple[FastAPI, AsyncMock]) -> None:
        app, _ = app_with_indexer
        resp = _client(app).get("/api/logs/search", params={"q": "x", "limit": 9999})
        assert resp.status_code == 422


# ── /api/logs/sagas/{saga_id} ───────────────────────────────────────────────


class TestGetSaga:
    """Saga endpoint prefers FTS, falls back to file scan."""

    def test_uses_indexer_when_available(
        self, app_with_indexer: tuple[FastAPI, AsyncMock]
    ) -> None:
        app, search = app_with_indexer
        search.return_value = [
            {"timestamp": "2026-04-01T10:00:01Z", "event": "b"},
            {"timestamp": "2026-04-01T10:00:00Z", "event": "a"},
        ]
        resp = _client(app).get("/api/logs/sagas/abc123")
        assert resp.status_code == 200
        body = resp.json()
        assert body["saga_id"] == "abc123"
        # Sorted chronologically (oldest first).
        events = [e["event"] for e in body["entries"]]
        assert events == ["a", "b"]
        kwargs = search.call_args.kwargs
        assert kwargs["saga_id"] == "abc123"

    def test_falls_back_to_query_saga_when_no_indexer(
        self, app_no_indexer: FastAPI, tmp_path: Path
    ) -> None:
        # Seed a file with two saga entries and an unrelated row.
        log_file = _seed_log_file(
            tmp_path,
            [
                {
                    "event": "noise",
                    "level": "info",
                    "logger": "sovyx",
                    "timestamp": "2026-04-01T10:00:00",
                },
                {
                    "event": "saga_step",
                    "level": "info",
                    "logger": "sovyx.cognitive",
                    "saga_id": "xyz",
                    "timestamp": "2026-04-01T10:00:01",
                },
            ],
        )
        app_no_indexer.state.log_file = log_file
        resp = _client(app_no_indexer).get("/api/logs/sagas/xyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["saga_id"] == "xyz"
        # query_saga returned only the matching entry.
        assert len(body["entries"]) == 1
        assert body["entries"][0]["event"] == "saga_step"

    def test_saga_id_length_validated(self, app_no_indexer: FastAPI) -> None:
        resp = _client(app_no_indexer).get(f"/api/logs/sagas/{'x' * 65}")
        assert resp.status_code == 422


# ── /api/logs/sagas/{saga_id}/story ─────────────────────────────────────────


class TestSagaStory:
    """Narrative endpoint delegates to ``build_user_journey``."""

    def test_unconfigured_log_file_returns_sentinel(self, app_no_indexer: FastAPI) -> None:
        resp = _client(app_no_indexer).get("/api/logs/sagas/abc/story")
        assert resp.status_code == 200
        body = resp.json()
        assert body["saga_id"] == "abc"
        assert body["story"] == "(log file not configured)"
        assert body["locale"] == "pt-BR"

    def test_locale_threaded_to_renderer(self, app_no_indexer: FastAPI, tmp_path: Path) -> None:
        log_file = tmp_path / "x.log"
        log_file.touch()
        app_no_indexer.state.log_file = log_file
        with patch(
            "sovyx.observability.narrative.build_user_journey",
            return_value="rendered story",
        ) as build:
            resp = _client(app_no_indexer).get(
                "/api/logs/sagas/abc/story",
                params={"locale": "en-US"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["locale"] == "en-US"
        assert body["story"] == "rendered story"
        # The renderer received the saga id and locale we requested.
        assert build.call_args.args[0] == "abc"
        assert build.call_args.kwargs["locale"] == "en-US"

    def test_invalid_locale_rejected(self, app_no_indexer: FastAPI) -> None:
        resp = _client(app_no_indexer).get(
            "/api/logs/sagas/abc/story",
            params={"locale": "fr-FR"},
        )
        assert resp.status_code == 422


# ── /api/logs/sagas/{saga_id}/causality ─────────────────────────────────────


class TestSagaCausality:
    """Causality edges expose ``span_id``/``cause_id`` from the embedded envelope."""

    def test_edges_unpacked_from_content_json(
        self, app_with_indexer: tuple[FastAPI, AsyncMock]
    ) -> None:
        app, search = app_with_indexer
        # FTS rows ship the original JSON line as ``content`` — the
        # route parses it to extract span_id / cause_id.
        envelope_a = json.dumps({"event": "step_a", "span_id": "span-a", "cause_id": None})
        envelope_b = json.dumps({"event": "step_b", "span_id": "span-b", "cause_id": "span-a"})
        search.return_value = [
            {
                "timestamp": "2026-04-01T10:00:00Z",
                "level": "info",
                "logger": "sovyx.cognitive",
                "event": "step_a",
                "content": envelope_a,
            },
            {
                "timestamp": "2026-04-01T10:00:01Z",
                "level": "info",
                "logger": "sovyx.cognitive",
                "event": "step_b",
                "content": envelope_b,
            },
        ]
        resp = _client(app).get("/api/logs/sagas/saga-1/causality")
        assert resp.status_code == 200
        body = resp.json()
        assert body["saga_id"] == "saga-1"
        assert len(body["edges"]) == 2
        assert body["edges"][0]["id"] == "span-a"
        assert body["edges"][0]["cause_id"] is None
        assert body["edges"][1]["id"] == "span-b"
        assert body["edges"][1]["cause_id"] == "span-a"

    def test_missing_content_yields_null_span(
        self, app_with_indexer: tuple[FastAPI, AsyncMock]
    ) -> None:
        # File-scan rows lack ``content``; the route must still emit
        # an edge (with id=None) so the dashboard can render placeholders.
        app, search = app_with_indexer
        search.return_value = [
            {
                "timestamp": "2026-04-01T10:00:00Z",
                "level": "info",
                "logger": "sovyx.cognitive",
                "event": "loose_event",
            }
        ]
        resp = _client(app).get("/api/logs/sagas/saga-2/causality")
        assert resp.status_code == 200
        edges = resp.json()["edges"]
        assert len(edges) == 1
        assert edges[0]["id"] is None
        assert edges[0]["cause_id"] is None
        assert edges[0]["event"] == "loose_event"

    def test_malformed_content_does_not_crash(
        self, app_with_indexer: tuple[FastAPI, AsyncMock]
    ) -> None:
        app, search = app_with_indexer
        search.return_value = [
            {
                "timestamp": "2026-04-01T10:00:00Z",
                "level": "info",
                "logger": "sovyx",
                "event": "x",
                "content": "this is not json",
            }
        ]
        resp = _client(app).get("/api/logs/sagas/saga-3/causality")
        assert resp.status_code == 200
        # Edge still produced, parser swallowed the bad content.
        assert len(resp.json()["edges"]) == 1


# ── /api/logs/anomalies ─────────────────────────────────────────────────────


class TestGetAnomalies:
    """Anomaly endpoint searches the ``anomaly.*`` event prefix."""

    def test_uses_indexer_with_prefix_match(
        self, app_with_indexer: tuple[FastAPI, AsyncMock]
    ) -> None:
        app, search = app_with_indexer
        search.return_value = [
            {
                "timestamp": "2026-04-01T10:00:00Z",
                "level": "warning",
                "logger": "sovyx.observability.anomaly",
                "event": "anomaly.first_occurrence",
                "message": "novel signature",
            }
        ]
        resp = _client(app).get("/api/logs/anomalies", params={"limit": 25})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        # FTS prefix syntax is ``anomaly*``.
        assert search.call_args.args[0] == "anomaly*"
        assert search.call_args.kwargs["limit"] == 25

    def test_falls_back_to_substring_search_without_indexer(
        self, app_no_indexer: FastAPI, tmp_path: Path
    ) -> None:
        log_file = _seed_log_file(
            tmp_path,
            [
                {
                    "event": "anomaly.latency_spike",
                    "level": "warning",
                    "logger": "sovyx.observability.anomaly",
                    "timestamp": "2026-04-01T10:00:00",
                },
                {
                    "event": "engine_started",
                    "level": "info",
                    "logger": "sovyx.engine",
                    "timestamp": "2026-04-01T10:00:01",
                },
            ],
        )
        app_no_indexer.state.log_file = log_file
        resp = _client(app_no_indexer).get("/api/logs/anomalies")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["entries"][0]["event"] == "anomaly.latency_spike"


# ── Auth ────────────────────────────────────────────────────────────────────


class TestAuthGate:
    """Every JSON route shares the ``verify_token`` dependency."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/logs",
            "/api/logs/search?q=x",
            "/api/logs/sagas/abc",
            "/api/logs/sagas/abc/story",
            "/api/logs/sagas/abc/causality",
            "/api/logs/anomalies",
        ],
    )
    def test_unauthenticated_request_rejected(self, app_no_indexer: FastAPI, path: str) -> None:
        # No Authorization header → 401 from verify_token.
        client = TestClient(app_no_indexer)
        resp = client.get(path)
        assert resp.status_code == 401

    @pytest.mark.parametrize(
        "path",
        [
            "/api/logs",
            "/api/logs/sagas/abc",
            "/api/logs/anomalies",
        ],
    )
    def test_wrong_token_rejected(self, app_no_indexer: FastAPI, path: str) -> None:
        client = TestClient(
            app_no_indexer,
            headers={"Authorization": "Bearer wrong-token"},
        )
        resp = client.get(path)
        assert resp.status_code == 401
