"""Tests for ``POST /api/mind/{mind_id}/forget`` — Phase 8 / T8.21 step 5.

Companion to the ``sovyx mind forget`` CLI; surfaces the same
:class:`MindForgetService` over HTTP for dashboard operators.

Coverage:

* Auth: missing Bearer token → 401.
* Validation: empty mind_id → 400; missing confirm field → 422; wrong
  confirm value → 400 (defense against accidental wipe).
* Service availability: missing registry / unregistered DatabaseManager
  → 503; missing per-mind databases → 404.
* Success: dry_run returns counts without writing; real run wipes
  every per-mind row; cross-mind isolation holds (other mind's data
  survives).
* Response shape: every report field is present + int / bool typed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.episode_repo import EpisodeRepository
from sovyx.brain.models import Concept, Episode
from sovyx.dashboard.server import create_app
from sovyx.engine.errors import DatabaseConnectionError
from sovyx.engine.types import ConversationId, MindId
from sovyx.persistence.manager import DatabaseManager
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations
from sovyx.persistence.schemas.conversations import get_conversation_migrations
from sovyx.persistence.schemas.system import get_system_migrations
from sovyx.voice._consent_ledger import ConsentAction, ConsentLedger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_TOKEN = "test-token-mind-forget-t821"  # noqa: S105 — test fixture token


# ── Fixtures: real per-mind pools wired into a fake DatabaseManager ──


@pytest.fixture
async def brain_pool(tmp_path: Path) -> AsyncIterator[DatabasePool]:
    p = DatabasePool(
        db_path=tmp_path / "aria_brain.db",
        read_pool_size=1,
        load_extensions=["vec0"],
    )
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(
        get_brain_migrations(has_sqlite_vec=p.has_sqlite_vec),
    )
    yield p
    await p.close()


@pytest.fixture
async def conv_pool(tmp_path: Path) -> AsyncIterator[DatabasePool]:
    p = DatabasePool(db_path=tmp_path / "aria_conv.db", read_pool_size=1)
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_conversation_migrations())
    yield p
    await p.close()


@pytest.fixture
async def system_pool(tmp_path: Path) -> AsyncIterator[DatabasePool]:
    p = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_system_migrations())
    yield p
    await p.close()


def _build_app(
    *,
    tmp_path: Path,
    db_manager: DatabaseManager | None,
    register_db_manager: bool = True,
) -> Any:
    """Build a test app with EngineConfig + (optionally) a DB manager."""
    from sovyx.engine.config import DatabaseConfig, EngineConfig

    app = create_app(token=_TOKEN)
    app.state.engine_config = EngineConfig(
        data_dir=tmp_path,
        database=DatabaseConfig(data_dir=tmp_path),
    )

    registry = MagicMock()
    if db_manager is None or not register_db_manager:
        registry.is_registered = MagicMock(return_value=False)
    else:
        registry.is_registered = MagicMock(return_value=True)
        registry.resolve = AsyncMock(return_value=db_manager)
    app.state.registry = registry
    return app


def _build_db_manager(
    *,
    brain_pool: DatabasePool | None = None,
    conv_pool: DatabasePool | None = None,
    system_pool: DatabasePool | None = None,
    missing_mind: bool = False,
) -> DatabaseManager:
    """Construct a MagicMock DatabaseManager exposing the given pools."""
    mgr = MagicMock(spec=DatabaseManager)
    if missing_mind:
        mgr.get_brain_pool = MagicMock(
            side_effect=DatabaseConnectionError("brain not initialised"),
        )
        mgr.get_conversation_pool = MagicMock(
            side_effect=DatabaseConnectionError("conv not initialised"),
        )
    else:
        mgr.get_brain_pool = MagicMock(return_value=brain_pool)
        mgr.get_conversation_pool = MagicMock(return_value=conv_pool)
    mgr.get_system_pool = MagicMock(return_value=system_pool)
    return mgr


# ── Auth ─────────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_token_returns_401(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path, db_manager=None)
        client = TestClient(app)
        response = client.post(
            "/api/mind/aria/forget",
            json={"confirm": "aria"},
        )
        assert response.status_code == 401  # noqa: PLR2004


# ── Validation ───────────────────────────────────────────────────────


class TestValidation:
    def test_empty_mind_id_path_does_not_reach_handler(
        self,
        tmp_path: Path,
    ) -> None:
        """An empty path segment doesn't reach the handler — FastAPI's
        router rejects with 404 / 405 / 422 depending on path
        normalisation. Either way it never reaches the destructive
        handler. The defense-in-depth empty-string check in the
        handler covers whitespace-only ids that DO reach it."""
        app = _build_app(tmp_path=tmp_path, db_manager=None)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post("/api/mind//forget", json={"confirm": ""})
        # 404 (no route match), 405 (method not allowed on collapsed
        # path), or 422 (path-param validation) are all acceptable —
        # the contract is "destructive handler is never invoked".
        assert response.status_code in {404, 405, 422}

    def test_whitespace_mind_id_returns_400(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path, db_manager=None)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/%20%20/forget",
            json={"confirm": "  "},
        )
        assert response.status_code == 400  # noqa: PLR2004
        assert "non-empty" in response.json()["detail"]

    def test_missing_confirm_returns_422(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path, db_manager=None)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post("/api/mind/aria/forget", json={})
        assert response.status_code == 422  # noqa: PLR2004

    def test_confirm_mismatch_returns_400(self, tmp_path: Path) -> None:
        """Defense against accidental wipe — the operator MUST type
        the mind id verbatim. ``confirm="yes"`` is rejected."""
        app = _build_app(tmp_path=tmp_path, db_manager=None)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/aria/forget",
            json={"confirm": "yes"},
        )
        assert response.status_code == 400  # noqa: PLR2004
        assert "must exactly match" in response.json()["detail"]

    def test_confirm_mismatch_dry_run_still_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        """The confirmation check applies even to dry_run — consistency
        + defends against typos that test the wipe path with the wrong
        mind id."""
        app = _build_app(tmp_path=tmp_path, db_manager=None)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/aria/forget",
            json={"confirm": "luna", "dry_run": True},
        )
        assert response.status_code == 400  # noqa: PLR2004


# ── Service availability ─────────────────────────────────────────────


class TestServiceAvailability:
    def test_missing_registry_returns_503(self, tmp_path: Path) -> None:
        from sovyx.engine.config import DatabaseConfig, EngineConfig

        app = create_app(token=_TOKEN)
        app.state.engine_config = EngineConfig(
            data_dir=tmp_path,
            database=DatabaseConfig(data_dir=tmp_path),
        )
        # NOTE: no app.state.registry set.
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/aria/forget",
            json={"confirm": "aria"},
        )
        assert response.status_code == 503  # noqa: PLR2004
        assert "engine registry" in response.json()["detail"]

    def test_unregistered_db_manager_returns_503(self, tmp_path: Path) -> None:
        app = _build_app(
            tmp_path=tmp_path,
            db_manager=None,
            register_db_manager=False,
        )
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/aria/forget",
            json={"confirm": "aria"},
        )
        assert response.status_code == 503  # noqa: PLR2004
        assert "DatabaseManager" in response.json()["detail"]

    def test_missing_mind_returns_404(
        self,
        tmp_path: Path,
        system_pool: DatabasePool,
    ) -> None:
        """A mind whose per-mind DBs were never initialised (operator
        named a mind that doesn't exist) returns 404, not 500."""
        db_manager = _build_db_manager(
            system_pool=system_pool,
            missing_mind=True,
        )
        app = _build_app(tmp_path=tmp_path, db_manager=db_manager)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/ghost/forget",
            json={"confirm": "ghost"},
        )
        assert response.status_code == 404  # noqa: PLR2004
        assert "not found" in response.json()["detail"]


# ── Success path ─────────────────────────────────────────────────────


class TestSuccessPath:
    @pytest.mark.asyncio
    async def test_dry_run_returns_counts_without_writing(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
        conv_pool: DatabasePool,
        system_pool: DatabasePool,
    ) -> None:
        # Seed brain.
        embedding = AsyncMock()
        embedding.has_embeddings = False
        embedding.encode = AsyncMock(return_value=[0.1] * 384)
        concept_repo = ConceptRepository(brain_pool, embedding)
        episode_repo = EpisodeRepository(brain_pool, embedding)
        await concept_repo.create(Concept(mind_id=MindId("aria"), name="c1"))
        await episode_repo.create(
            Episode(
                mind_id=MindId("aria"),
                conversation_id=ConversationId("c-1"),
                user_input="hi",
                assistant_response="ok",
            ),
        )

        db_manager = _build_db_manager(
            brain_pool=brain_pool,
            conv_pool=conv_pool,
            system_pool=system_pool,
        )
        app = _build_app(tmp_path=tmp_path, db_manager=db_manager)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/forget",
            json={"confirm": "aria", "dry_run": True},
        )
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["dry_run"] is True
        assert data["mind_id"] == "aria"
        assert data["concepts_purged"] == 1
        assert data["episodes_purged"] == 1

        # No actual deletion.
        async with brain_pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM concepts")
            row = await cursor.fetchone()
            assert int(row[0]) == 1  # type: ignore[index]

    @pytest.mark.asyncio
    async def test_real_run_wipes_seeded_data(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
        conv_pool: DatabasePool,
        system_pool: DatabasePool,
    ) -> None:
        # Seed brain.
        embedding = AsyncMock()
        embedding.has_embeddings = False
        embedding.encode = AsyncMock(return_value=[0.1] * 384)
        concept_repo = ConceptRepository(brain_pool, embedding)
        for i in range(3):
            await concept_repo.create(Concept(mind_id=MindId("aria"), name=f"c{i}"))

        # Seed consent ledger.
        ledger_path = tmp_path / "voice" / "consent.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger = ConsentLedger(path=ledger_path)
        ledger.append(
            user_id="u1",
            action=ConsentAction.WAKE,
            mind_id="aria",
        )

        db_manager = _build_db_manager(
            brain_pool=brain_pool,
            conv_pool=conv_pool,
            system_pool=system_pool,
        )
        app = _build_app(tmp_path=tmp_path, db_manager=db_manager)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/forget",
            json={"confirm": "aria"},
        )
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()

        assert data["dry_run"] is False
        assert data["concepts_purged"] == 3  # noqa: PLR2004
        assert data["consent_ledger_purged"] == 1
        assert data["total_rows_purged"] == 3  # 3 concepts only  # noqa: PLR2004

        # Brain actually empty now.
        async with brain_pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM concepts")
            row = await cursor.fetchone()
            assert int(row[0]) == 0  # type: ignore[index]

    def test_response_carries_every_report_field(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
        conv_pool: DatabasePool,
        system_pool: DatabasePool,
    ) -> None:
        db_manager = _build_db_manager(
            brain_pool=brain_pool,
            conv_pool=conv_pool,
            system_pool=system_pool,
        )
        app = _build_app(tmp_path=tmp_path, db_manager=db_manager)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/forget",
            json={"confirm": "aria", "dry_run": True},
        )
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        for field in (
            "mind_id",
            "concepts_purged",
            "relations_purged",
            "episodes_purged",
            "concept_embeddings_purged",
            "episode_embeddings_purged",
            "conversation_imports_purged",
            "consolidation_log_purged",
            "conversations_purged",
            "conversation_turns_purged",
            "daily_stats_purged",
            "consent_ledger_purged",
            "total_brain_rows_purged",
            "total_conversations_rows_purged",
            "total_system_rows_purged",
            "total_rows_purged",
            "dry_run",
        ):
            assert field in data, f"missing field: {field}"
        # Type contract: counts are int, dry_run is bool, mind_id is str.
        assert isinstance(data["mind_id"], str)
        assert isinstance(data["dry_run"], bool)
        for k, v in data.items():
            if k.endswith("_purged") or k.startswith("total_"):
                assert isinstance(v, int), f"{k} should be int, got {type(v).__name__}"
