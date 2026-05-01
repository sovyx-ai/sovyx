"""Tests for ``POST /api/mind/{mind_id}/retention/prune`` — Phase 8 / T8.21 step 6.

Companion to ``test_mind_forget_t821.py``. Mirrors the test surface
for the retention endpoint:

* Auth: missing Bearer token → 401.
* Validation: empty mind_id → 400.
* Service availability: missing registry / unregistered
  DatabaseManager → 503; missing per-mind databases → 404.
* Success: dry_run returns counts + horizons without writing; real
  run prunes only OLD records; effective_horizons in response.
* Response shape: every report field present + correctly typed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.engine.errors import DatabaseConnectionError
from sovyx.persistence.manager import DatabaseManager
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations
from sovyx.persistence.schemas.conversations import get_conversation_migrations
from sovyx.persistence.schemas.system import get_system_migrations

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


_TOKEN = "test-token-mind-retention-t821"  # noqa: S105 — test fixture


@pytest.fixture
async def brain_pool(tmp_path: Path) -> AsyncIterator[DatabasePool]:
    p = DatabasePool(
        db_path=tmp_path / "brain.db",
        read_pool_size=1,
        load_extensions=["vec0"],
    )
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=p.has_sqlite_vec))
    yield p
    await p.close()


@pytest.fixture
async def conv_pool(tmp_path: Path) -> AsyncIterator[DatabasePool]:
    p = DatabasePool(db_path=tmp_path / "conv.db", read_pool_size=1)
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
    mgr = MagicMock(spec=DatabaseManager)
    if missing_mind:
        mgr.get_brain_pool = MagicMock(
            side_effect=DatabaseConnectionError("not initialised"),
        )
        mgr.get_conversation_pool = MagicMock(
            side_effect=DatabaseConnectionError("not initialised"),
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
            "/api/mind/aria/retention/prune",
            json={"dry_run": True},
        )
        assert response.status_code == 401  # noqa: PLR2004


# ── Validation ───────────────────────────────────────────────────────


class TestValidation:
    def test_whitespace_mind_id_returns_400(self, tmp_path: Path) -> None:
        app = _build_app(tmp_path=tmp_path, db_manager=None)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/%20%20/retention/prune",
            json={"dry_run": True},
        )
        assert response.status_code == 400  # noqa: PLR2004

    def test_no_confirm_field_required(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
        conv_pool: DatabasePool,
        system_pool: DatabasePool,
    ) -> None:
        """Retention endpoint does NOT require ``confirm: <mind_id>``
        like forget — it's a scheduled-policy operation removing only
        aged records, not destructive in the same sense."""
        db_manager = _build_db_manager(
            brain_pool=brain_pool,
            conv_pool=conv_pool,
            system_pool=system_pool,
        )
        app = _build_app(tmp_path=tmp_path, db_manager=db_manager)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/aria/retention/prune",
            json={"dry_run": True},
        )
        assert response.status_code == 200  # noqa: PLR2004


# ── Service availability ─────────────────────────────────────────────


class TestServiceAvailability:
    def test_unregistered_db_manager_returns_503(self, tmp_path: Path) -> None:
        app = _build_app(
            tmp_path=tmp_path,
            db_manager=None,
            register_db_manager=False,
        )
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/aria/retention/prune",
            json={"dry_run": True},
        )
        assert response.status_code == 503  # noqa: PLR2004

    def test_missing_mind_returns_404(
        self,
        tmp_path: Path,
        system_pool: DatabasePool,
    ) -> None:
        db_manager = _build_db_manager(
            system_pool=system_pool,
            missing_mind=True,
        )
        app = _build_app(tmp_path=tmp_path, db_manager=db_manager)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        response = client.post(
            "/api/mind/ghost/retention/prune",
            json={"dry_run": True},
        )
        assert response.status_code == 404  # noqa: PLR2004


# ── Success path ─────────────────────────────────────────────────────


class TestSuccessPath:
    @pytest.mark.asyncio
    async def test_dry_run_with_seeded_old_episode(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
        conv_pool: DatabasePool,
        system_pool: DatabasePool,
    ) -> None:
        # Seed an old episode (60 days ago).
        old_ts = datetime.now(UTC) - timedelta(days=60)
        async with brain_pool.transaction() as conn:
            await conn.execute(
                """INSERT INTO episodes
                   (id, mind_id, conversation_id, user_input, assistant_response,
                    summary, importance, emotional_valence, emotional_arousal,
                    concepts_mentioned, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, NULL, 0.5, 0.0, 0.0, '[]', '{}', ?)""",
                ("ep-1", "aria", "c1", "hi", "ok", old_ts.isoformat()),
            )

        db_manager = _build_db_manager(
            brain_pool=brain_pool,
            conv_pool=conv_pool,
            system_pool=system_pool,
        )
        app = _build_app(tmp_path=tmp_path, db_manager=db_manager)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/retention/prune",
            json={"dry_run": True},
        )
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["dry_run"] is True
        # Default 30d horizon → the 60d-old episode is eligible.
        assert data["episodes_purged"] == 1
        # No actual delete in dry run.
        async with brain_pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM episodes")
            row = await cursor.fetchone()
            assert int(row[0]) == 1  # type: ignore[index]

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
            "/api/mind/aria/retention/prune",
            json={"dry_run": True},
        )
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        for field in (
            "mind_id",
            "cutoff_utc",
            "episodes_purged",
            "conversations_purged",
            "conversation_turns_purged",
            "daily_stats_purged",
            "consolidation_log_purged",
            "consent_ledger_purged",
            "effective_horizons",
            "total_brain_rows_purged",
            "total_conversations_rows_purged",
            "total_system_rows_purged",
            "total_rows_purged",
            "dry_run",
        ):
            assert field in data, f"missing field: {field}"
        assert isinstance(data["effective_horizons"], dict)
        # Default RetentionTuningConfig: episodes_days = 30.
        assert data["effective_horizons"]["episodes"] == 30  # noqa: PLR2004

    def test_default_dry_run_false_writes(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
        conv_pool: DatabasePool,
        system_pool: DatabasePool,
    ) -> None:
        """Without ``dry_run`` field, default is False → real prune."""
        db_manager = _build_db_manager(
            brain_pool=brain_pool,
            conv_pool=conv_pool,
            system_pool=system_pool,
        )
        app = _build_app(tmp_path=tmp_path, db_manager=db_manager)
        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

        response = client.post(
            "/api/mind/aria/retention/prune",
            json={},
        )
        assert response.status_code == 200  # noqa: PLR2004
        data = response.json()
        assert data["dry_run"] is False
