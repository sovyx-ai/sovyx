"""Tests for POST /api/import/conversations + GET /api/import/{id}/progress.

Covers:
- Authentication (missing Bearer token).
- Validation (unknown platform, missing file, malformed JSON).
- Happy path (upload → background job → poll progress to completion).
- Dedup re-import (second run skips all conversations).
- Progress endpoint: unknown ID → 404, schema shape.

Implementation note on the mock DatabasePool. The router reads/writes
the ``conversation_imports`` dedup table via ``pool.read()`` /
``pool.write()``. Using a real :class:`DatabasePool` here is tempting
but leaks event loops: the pool's aiosqlite connections bind to the
loop where ``initialize()`` ran, and the router's background task
runs on :class:`TestClient`'s own internal loop — connections become
unusable. The ``_DictPool`` below is a dict-backed stand-in that
honours the exact async-context-manager surface the router calls,
with zero aiosqlite state to mis-bind.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "chatgpt" / "sample_conversations.json"
_CLAUDE_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "claude" / "sample_conversations.json"
)
_GEMINI_FIXTURE = Path(__file__).parent.parent / "fixtures" / "gemini" / "sample_activity.json"
_GROK_FIXTURE = Path(__file__).parent.parent / "fixtures" / "grok" / "sample_conversations.json"

# ── Dict-backed pool stub ──────────────────────────────────────────
#
# Mirrors exactly the slice of DatabasePool the router exercises:
# ``pool.read()`` / ``pool.write()`` yielding a connection whose
# ``.execute(sql, params)`` routes "SELECT 1 FROM conversation_imports"
# and "INSERT ... INTO conversation_imports" against an in-memory set.


class _DictPoolConn:
    def __init__(self, hashes: set[str]) -> None:
        self._hashes = hashes
        self._last_sql = ""

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _DictPoolCursor:
        self._last_sql = sql.strip().lower()
        if self._last_sql.startswith("select 1 from conversation_imports"):
            # params[0] is the source_hash
            found = params and params[0] in self._hashes
            return _DictPoolCursor([1] if found else None)
        if self._last_sql.startswith("insert or ignore into conversation_imports"):
            # params[0] is the source_hash
            if params:
                self._hashes.add(params[0])
            return _DictPoolCursor(None)
        # Any other SQL — no-op cursor.
        return _DictPoolCursor(None)

    async def commit(self) -> None:
        pass


class _DictPoolCursor:
    def __init__(self, row: object) -> None:
        self._row = row

    async def fetchone(self) -> object:
        return self._row


class _DictPoolCtx:
    """Async context manager yielding a ``_DictPoolConn``."""

    def __init__(self, hashes: set[str]) -> None:
        self._hashes = hashes

    async def __aenter__(self) -> _DictPoolConn:
        return _DictPoolConn(self._hashes)

    async def __aexit__(self, *_: object) -> None:
        pass


class _DictPool:
    """Minimal DatabasePool stand-in for the conversation_imports path."""

    def __init__(self) -> None:
        self._hashes: set[str] = set()

    def read(self) -> _DictPoolCtx:
        return _DictPoolCtx(self._hashes)

    def write(self) -> _DictPoolCtx:
        return _DictPoolCtx(self._hashes)


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", tmp_path / "token")


@pytest.fixture()
def token(tmp_path: Path) -> str:
    t = secrets.token_urlsafe(32)
    (tmp_path / "token").write_text(t)
    return t


@pytest.fixture()
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client(token: str) -> TestClient:  # noqa: ARG001 — ensures token file exists
    return TestClient(create_app())


def _make_registry() -> tuple[MagicMock, list[dict[str, object]]]:
    """Build a registry whose resolves cover Brain / DB / LLM / Bridge."""
    captured_episodes: list[dict[str, object]] = []

    from sovyx.engine.types import ConceptId, EpisodeId

    mock_brain = AsyncMock()
    mock_brain.learn_concept = AsyncMock(
        side_effect=lambda **kw: ConceptId(f"concept-{kw['name']}"),
    )

    async def _encode(**kw: object) -> EpisodeId:
        captured_episodes.append(kw)
        return EpisodeId(f"episode-{len(captured_episodes)}")

    mock_brain.encode_episode = AsyncMock(side_effect=_encode)

    dict_pool = _DictPool()
    mock_db_manager = MagicMock()
    mock_db_manager.get_brain_pool = MagicMock(return_value=dict_pool)

    mock_bridge = MagicMock()
    mock_bridge.mind_id = "test-mind"

    from sovyx.llm.models import LLMResponse

    canned_summary = json.dumps(
        {
            "summary": "A test conversation.",
            "concepts": [
                {
                    "name": "test-concept",
                    "category": "fact",
                    "content": "A thing.",
                    "importance": 0.5,
                },
            ],
            "emotional_valence": 0.0,
            "emotional_arousal": 0.0,
            "importance": 0.5,
        }
    )
    mock_llm_router = AsyncMock()
    mock_llm_router.generate = AsyncMock(
        return_value=LLMResponse(
            content=canned_summary,
            model="fake",
            tokens_in=0,
            tokens_out=0,
            latency_ms=1,
            cost_usd=0.0,
            finish_reason="stop",
            provider="fake",
        ),
    )

    registry = MagicMock()

    async def _resolve(interface: type) -> object:
        from sovyx.brain.service import BrainService
        from sovyx.bridge.manager import BridgeManager
        from sovyx.llm.router import LLMRouter
        from sovyx.persistence.manager import DatabaseManager

        mapping: dict[type, object] = {
            BrainService: mock_brain,
            DatabaseManager: mock_db_manager,
            LLMRouter: mock_llm_router,
            BridgeManager: mock_bridge,
        }
        result = mapping.get(interface)
        if result is None:
            msg = f"Service not registered: {interface.__name__}"
            raise Exception(msg)  # noqa: TRY002
        return result

    registry.resolve = AsyncMock(side_effect=_resolve)
    registry.is_registered = MagicMock(return_value=True)

    registry._brain = mock_brain
    registry._pool = dict_pool
    registry._llm_router = mock_llm_router
    registry._captured_episodes = captured_episodes

    return registry, captured_episodes


def _wait_for_completion(
    client: TestClient,
    job_id: str,
    auth: dict[str, str],
    *,
    timeout_s: float = 10.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/api/import/{job_id}/progress", headers=auth)
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        if data["state"] in ("completed", "failed"):
            return data
        time.sleep(0.05)
    msg = f"Job did not finish in {timeout_s}s"
    raise AssertionError(msg)


# ── Auth tests ─────────────────────────────────────────────────────


class TestConversationImportAuth:
    def test_post_requires_auth(self, client: TestClient) -> None:
        resp = client.post(
            "/api/import/conversations",
            files={"file": ("x.json", b"[]")},
            data={"platform": "chatgpt"},
        )
        assert resp.status_code == 401  # noqa: PLR2004

    def test_progress_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/api/import/some-id/progress")
        assert resp.status_code == 401  # noqa: PLR2004


# ── Validation ─────────────────────────────────────────────────────


class TestConversationImportValidation:
    def test_missing_registry_returns_503(
        self,
        client: TestClient,
        auth: dict[str, str],
    ) -> None:
        resp = client.post(
            "/api/import/conversations",
            files={"file": ("x.json", _FIXTURE.read_bytes())},
            data={"platform": "chatgpt"},
            headers=auth,
        )
        assert resp.status_code == 503  # noqa: PLR2004

    def test_unknown_platform_returns_422(
        self,
        client: TestClient,
        auth: dict[str, str],
    ) -> None:
        registry, _ = _make_registry()
        client.app.state.registry = registry  # type: ignore[union-attr]
        resp = client.post(
            "/api/import/conversations",
            files={"file": ("x.json", b"[]")},
            data={"platform": "bogus"},
            headers=auth,
        )
        assert resp.status_code == 422  # noqa: PLR2004
        assert "not supported" in resp.json()["error"]

    def test_missing_file_field_returns_422(
        self,
        client: TestClient,
        auth: dict[str, str],
    ) -> None:
        registry, _ = _make_registry()
        client.app.state.registry = registry  # type: ignore[union-attr]
        resp = client.post(
            "/api/import/conversations",
            data={"platform": "chatgpt"},
            headers=auth,
        )
        assert resp.status_code == 422  # noqa: PLR2004

    def test_malformed_json_returns_422(
        self,
        client: TestClient,
        auth: dict[str, str],
    ) -> None:
        registry, _ = _make_registry()
        client.app.state.registry = registry  # type: ignore[union-attr]
        resp = client.post(
            "/api/import/conversations",
            files={"file": ("bad.json", b"{this isn't json")},
            data={"platform": "chatgpt"},
            headers=auth,
        )
        assert resp.status_code == 422  # noqa: PLR2004

    def test_claude_platform_accepted(
        self,
        client: TestClient,
        auth: dict[str, str],
    ) -> None:
        """Registry wiring smoke-test: ``platform=claude`` reaches the worker.

        The heavy end-to-end flow (progress polling, dedup, summary
        encoding) is exercised via the ChatGPT fixture and is fully
        platform-agnostic, so we don't duplicate it for every new
        platform — we just assert that the router accepts the new
        identifier and starts a job.
        """
        registry, _ = _make_registry()
        client.app.state.registry = registry  # type: ignore[union-attr]
        resp = client.post(
            "/api/import/conversations",
            files={"file": ("c.json", _CLAUDE_FIXTURE.read_bytes())},
            data={"platform": "claude"},
            headers=auth,
        )
        assert resp.status_code == 202  # noqa: PLR2004
        body = resp.json()
        assert body["platform"] == "claude"
        assert body["conversations_total"] == 3  # noqa: PLR2004
        _wait_for_completion(client, body["job_id"], auth)

    def test_gemini_platform_accepted(
        self,
        client: TestClient,
        auth: dict[str, str],
    ) -> None:
        """Registry wiring smoke-test: ``platform=gemini`` reaches the worker.

        Same rationale as ``test_claude_platform_accepted``: we only
        assert that the router accepts the new identifier and starts a
        job. The heavy end-to-end flow is exercised via ChatGPT and is
        platform-agnostic.
        """
        registry, _ = _make_registry()
        client.app.state.registry = registry  # type: ignore[union-attr]
        resp = client.post(
            "/api/import/conversations",
            files={"file": ("g.json", _GEMINI_FIXTURE.read_bytes())},
            data={"platform": "gemini"},
            headers=auth,
        )
        assert resp.status_code == 202  # noqa: PLR2004
        body = resp.json()
        assert body["platform"] == "gemini"
        # Fixture produces 4 conversations: Bard 2023-06-01, EN SQLite
        # 2024-10-20, PT Curitiba 2024-10-21 14:01, EN React 2024-10-21 17:45.
        assert body["conversations_total"] == 4  # noqa: PLR2004
        _wait_for_completion(client, body["job_id"], auth)

    def test_grok_platform_accepted(
        self,
        client: TestClient,
        auth: dict[str, str],
    ) -> None:
        """Registry wiring smoke-test: ``platform=grok`` reaches the worker."""
        registry, _ = _make_registry()
        client.app.state.registry = registry  # type: ignore[union-attr]
        resp = client.post(
            "/api/import/conversations",
            files={"file": ("gk.json", _GROK_FIXTURE.read_bytes())},
            data={"platform": "grok"},
            headers=auth,
        )
        assert resp.status_code == 202  # noqa: PLR2004
        body = resp.json()
        assert body["platform"] == "grok"
        assert body["conversations_total"] == 2  # noqa: PLR2004
        _wait_for_completion(client, body["job_id"], auth)

    def test_obsidian_platform_accepted(
        self,
        client: TestClient,
        auth: dict[str, str],
        tmp_path: Path,
    ) -> None:
        """Registry wiring smoke-test: ``platform=obsidian`` with a ZIP vault."""
        import zipfile as _zipfile

        vault_zip = tmp_path / "vault.zip"
        with _zipfile.ZipFile(vault_zip, "w", _zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "welcome.md",
                "---\ntags: [greeting]\n---\n# Welcome\nHi.",
            )
            zf.writestr(
                "topics/portuguese.md",
                "---\ntags: [language, learning]\n---\n# Portuguese\nSee [[Welcome]].\n",
            )
            zf.writestr(
                "topics/spanish.md",
                "# Spanish\nAnother language. See [[Portuguese]] and #language.",
            )

        registry, _ = _make_registry()
        client.app.state.registry = registry  # type: ignore[union-attr]
        resp = client.post(
            "/api/import/conversations",
            files={"file": ("vault.zip", vault_zip.read_bytes())},
            data={"platform": "obsidian"},
            headers=auth,
        )
        assert resp.status_code == 202  # noqa: PLR2004
        body = resp.json()
        assert body["platform"] == "obsidian"
        # 3 notes in the vault.
        assert body["conversations_total"] == 3  # noqa: PLR2004
        _wait_for_completion(client, body["job_id"], auth)


# ── Happy path + dedup ────────────────────────────────────────────


class TestConversationImportHappyPath:
    def test_end_to_end_chatgpt_import(
        self,
        client: TestClient,
        auth: dict[str, str],
    ) -> None:
        registry, captured = _make_registry()
        client.app.state.registry = registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/import/conversations",
            files={"file": ("c.json", _FIXTURE.read_bytes())},
            data={"platform": "chatgpt"},
            headers=auth,
        )
        assert resp.status_code == 202  # noqa: PLR2004
        start = resp.json()
        assert start["platform"] == "chatgpt"
        assert start["conversations_total"] == 3  # noqa: PLR2004
        job_id = start["job_id"]

        final = _wait_for_completion(client, job_id, auth)
        assert final["state"] == "completed"
        assert final["error"] is None
        assert final["conversations_processed"] == 3  # noqa: PLR2004
        assert final["conversations_skipped"] == 0
        assert final["episodes_created"] == 3  # noqa: PLR2004
        assert final["concepts_learned"] == 3  # noqa: PLR2004

        assert len(captured) == 3  # noqa: PLR2004
        assert all(ep["summary"] == "A test conversation." for ep in captured)

    def test_reimport_skips_all(
        self,
        client: TestClient,
        auth: dict[str, str],
    ) -> None:
        registry, _ = _make_registry()
        client.app.state.registry = registry  # type: ignore[union-attr]

        r1 = client.post(
            "/api/import/conversations",
            files={"file": ("c.json", _FIXTURE.read_bytes())},
            data={"platform": "chatgpt"},
            headers=auth,
        )
        first = _wait_for_completion(client, r1.json()["job_id"], auth)
        assert first["state"] == "completed"

        r2 = client.post(
            "/api/import/conversations",
            files={"file": ("c.json", _FIXTURE.read_bytes())},
            data={"platform": "chatgpt"},
            headers=auth,
        )
        second = _wait_for_completion(client, r2.json()["job_id"], auth)
        assert second["state"] == "completed"
        assert second["conversations_processed"] == 0
        assert second["conversations_skipped"] == 3  # noqa: PLR2004
        assert second["episodes_created"] == 0


# ── Progress endpoint ─────────────────────────────────────────────


class TestConversationImportProgress:
    def test_unknown_job_id_returns_404(
        self,
        client: TestClient,
        auth: dict[str, str],
    ) -> None:
        registry, _ = _make_registry()
        client.app.state.registry = registry  # type: ignore[union-attr]
        resp = client.get("/api/import/does-not-exist/progress", headers=auth)
        assert resp.status_code == 404  # noqa: PLR2004

    def test_progress_schema_has_all_fields(
        self,
        client: TestClient,
        auth: dict[str, str],
    ) -> None:
        registry, _ = _make_registry()
        client.app.state.registry = registry  # type: ignore[union-attr]

        r = client.post(
            "/api/import/conversations",
            files={"file": ("c.json", _FIXTURE.read_bytes())},
            data={"platform": "chatgpt"},
            headers=auth,
        )
        final = _wait_for_completion(client, r.json()["job_id"], auth)

        expected_keys = {
            "job_id",
            "platform",
            "state",
            "conversations_total",
            "conversations_processed",
            "conversations_skipped",
            "episodes_created",
            "concepts_learned",
            "warnings",
            "error",
            "elapsed_ms",
        }
        assert set(final.keys()) == expected_keys
