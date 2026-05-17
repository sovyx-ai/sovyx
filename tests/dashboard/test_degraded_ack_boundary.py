"""Boundary tests for POST /api/voice/degraded/ack + ack-state
enrichment on GET /api/engine/degraded.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§Phase 3 §T3.3 + §T3.4 + §9.1 row "POST /api/voice/degraded/ack round-trip".

Quality Gate 8 round-trip discipline: every Model.model_validate call
at the route boundary has a paired test. Phase 3 added AckRequestBody
+ AckResponse + the GET-side ack-state enrichment.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.routes.engine_degraded import (
    AckRequestBody,
    AckResponse,
)
from sovyx.dashboard.server import create_app
from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    reset_default_degraded_store,
)
from sovyx.engine._operator_acks_store import OperatorAcksStore
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.system import get_system_migrations
from tests.dashboard._boundary_helpers import assert_boundary_accepts

_TOKEN = "test-token-c4-ack"


@pytest.fixture(autouse=True)
def _reset_store() -> Generator[None, None, None]:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


@pytest.fixture()
async def acks_store(tmp_path: Path) -> AsyncGenerator[OperatorAcksStore, None]:
    db_path = tmp_path / "system.db"
    pool = DatabasePool(db_path, read_pool_size=1)
    await pool.initialize()
    migrations = get_system_migrations()
    async with pool.write() as conn:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS _schema (
                 version INTEGER PRIMARY KEY,
                 description TEXT NOT NULL,
                 checksum TEXT NOT NULL,
                 applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                 duration_ms INTEGER
               )""",
        )
        await conn.commit()
        for m in migrations:
            await conn.executescript(m.sql_up)
            await conn.execute(
                "INSERT OR IGNORE INTO _schema (version, description, checksum) VALUES (?, ?, ?)",
                (m.version, m.description, m.checksum),
            )
            await conn.commit()
    yield OperatorAcksStore(pool)
    await pool.close()


def _seed_voice_axis() -> None:
    store = get_default_degraded_store()
    _now = time.monotonic()
    store.record(
        DegradedEntry(
            axis="voice",
            reason="failover_ladder_exhausted",
            severity="error",
            title_token="degraded.voice.ladderExhausted.title",
            body_token="degraded.voice.ladderExhausted.body",
            action_chips=(),
            metadata={},
            first_observed_monotonic=_now,
            last_observed_monotonic=_now,
            occurrence_count=1,
        ),
    )


class TestAckRequestBodyBoundary:
    def test_composite_reason_with_default_ttl(self) -> None:
        assert_boundary_accepts(
            AckRequestBody,
            helper_factory=lambda: {"reason": "composite"},
            field_assertions={"reason": "composite", "ttl_sec": None},
        )

    def test_explicit_ttl_round_trips(self) -> None:
        assert_boundary_accepts(
            AckRequestBody,
            helper_factory=lambda: {
                "reason": "voice.failover_ladder_exhausted",
                "ttl_sec": 7200,
            },
            field_assertions={
                "reason": "voice.failover_ladder_exhausted",
                "ttl_sec": 7200,
            },
        )

    def test_metadata_round_trips(self) -> None:
        assert_boundary_accepts(
            AckRequestBody,
            helper_factory=lambda: {
                "reason": "composite",
                "metadata": {"source": "operator", "tab_id": "abc123"},
            },
        )

    def test_future_field_passes_through(self) -> None:
        """Forward-additive: future request fields land via extra-allow."""
        assert_boundary_accepts(
            AckRequestBody,
            helper_factory=lambda: {
                "reason": "composite",
                "future_field": "tolerated",
            },
            field_assertions={"reason": "composite"},
        )


class TestAckResponseBoundary:
    def test_success_shape(self) -> None:
        assert_boundary_accepts(
            AckResponse,
            helper_factory=lambda: {
                "ok": True,
                "reasons_acked": ["voice.failover_ladder_exhausted"],
                "acked_at_ts": 1700000000,
                "ttl_sec": 3600,
                "ttl_remaining_sec": 3600,
            },
            field_assertions={
                "ok": True,
                "ttl_sec": 3600,
            },
        )

    def test_no_active_axes_returns_ok_false(self) -> None:
        """When operator clicks ack but no axis is currently active
        (race vs. server-side recovery), the endpoint returns
        ok=False so the frontend skips its optimistic dismiss."""
        assert_boundary_accepts(
            AckResponse,
            helper_factory=lambda: {
                "ok": False,
                "reasons_acked": [],
                "acked_at_ts": 1700000000,
                "ttl_sec": 3600,
                "ttl_remaining_sec": 3600,
            },
            field_assertions={"ok": False, "reasons_acked": []},
        )


class TestAckEndpointE2E:
    def test_unauthenticated_post_returns_401(self) -> None:
        app = create_app(token=_TOKEN)
        client = TestClient(app)
        response = client.post("/api/engine/degraded/ack", json={"reason": "composite"})
        assert response.status_code == 401

    def test_ttl_sec_below_minimum_returns_422(self) -> None:
        app = create_app(token=_TOKEN)
        client = TestClient(
            app,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        response = client.post(
            "/api/engine/degraded/ack",
            json={"reason": "composite", "ttl_sec": 30},
        )
        assert response.status_code == 422

    def test_ttl_sec_above_maximum_returns_422(self) -> None:
        app = create_app(token=_TOKEN)
        client = TestClient(
            app,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        response = client.post(
            "/api/engine/degraded/ack",
            json={"reason": "composite", "ttl_sec": 100000},
        )
        assert response.status_code == 422

    def test_ttl_sec_at_minimum_boundary_passes(self) -> None:
        """ADR-D9 bounds [60, 86400] are INCLUSIVE on both sides."""
        app = create_app(token=_TOKEN)
        client = TestClient(
            app,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        # 60 = minimum bound; expect either 200 (no axes → ok=False)
        # or 503 (no store registered in this minimal test app). Both
        # are valid; 422 would indicate the bounds check rejected
        # a valid value.
        response = client.post(
            "/api/engine/degraded/ack",
            json={"reason": "composite", "ttl_sec": 60},
        )
        assert response.status_code in (200, 503)

    def test_ttl_sec_at_maximum_boundary_passes(self) -> None:
        app = create_app(token=_TOKEN)
        client = TestClient(
            app,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        response = client.post(
            "/api/engine/degraded/ack",
            json={"reason": "composite", "ttl_sec": 86400},
        )
        assert response.status_code in (200, 503)

    def test_post_without_store_returns_503(self) -> None:
        """Pre-Phase-3 host (no OperatorAcksStore registered) returns
        503 — frontend can detect + fallback to client-side dismiss."""
        app = create_app(token=_TOKEN)
        client = TestClient(
            app,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        _seed_voice_axis()
        response = client.post(
            "/api/engine/degraded/ack",
            json={"reason": "composite"},
        )
        # No registry/store in this minimal app → 503
        assert response.status_code == 503
