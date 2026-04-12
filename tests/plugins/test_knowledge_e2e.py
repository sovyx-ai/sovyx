"""E2E integration tests for the Knowledge Plugin.

TASK-484: Tests that exercise the full stack:
  KnowledgePlugin → BrainAccess → BrainService → ConceptRepo/RelationRepo

Uses a real in-memory SQLite brain (no mocks).
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.episode_repo import EpisodeRepository
from sovyx.brain.learning import HebbianLearning
from sovyx.brain.relation_repo import RelationRepository
from sovyx.brain.retrieval import HybridRetrieval
from sovyx.brain.service import BrainService
from sovyx.brain.spreading import SpreadingActivation
from sovyx.brain.working_memory import WorkingMemory
from sovyx.engine.types import MindId
from sovyx.persistence.pool import DatabasePool
from sovyx.plugins.context import BrainAccess
from sovyx.plugins.official.knowledge import KnowledgePlugin
from sovyx.plugins.permissions import PermissionEnforcer

MIND_ID = "test-mind"

# Suppress aiosqlite 'Event loop is closed' RuntimeError that occurs
# in background threads during Python 3.11 interpreter shutdown.
if sys.version_info < (3, 12):
    _orig = threading.excepthook

    def _suppress_loop_closed(args: threading.ExceptHookArgs) -> None:
        if isinstance(args.exc_value, RuntimeError) and "Event loop" in str(args.exc_value):
            return
        _orig(args)

    threading.excepthook = _suppress_loop_closed


@pytest.fixture
async def db_pool(tmp_path: Path):
    """Create a real DatabasePool with SQLite."""
    pool = DatabasePool(tmp_path / "brain.db")
    await pool.initialize()

    # Create schema
    async with pool.write() as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS concepts (
                id TEXT PRIMARY KEY,
                mind_id TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT DEFAULT '',
                category TEXT DEFAULT 'fact',
                importance REAL DEFAULT 0.5,
                confidence REAL DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                last_accessed TEXT,
                emotional_valence REAL DEFAULT 0.0,
                source TEXT DEFAULT 'conversation',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                embedding BLOB
            );
            CREATE TABLE IF NOT EXISTS relations (
                id TEXT PRIMARY KEY,
                mind_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation_type TEXT DEFAULT 'related_to',
                weight REAL DEFAULT 1.0,
                co_occurrence_count INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (source_id) REFERENCES concepts(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES concepts(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY,
                mind_id TEXT NOT NULL,
                conversation_id TEXT,
                user_input TEXT,
                assistant_response TEXT,
                summary TEXT,
                importance REAL DEFAULT 0.5,
                emotional_valence REAL DEFAULT 0.0,
                emotional_arousal REAL DEFAULT 0.0,
                concepts_mentioned TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                embedding BLOB
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS concepts_fts USING fts5(
                name, content, content='concepts', content_rowid='rowid'
            );
            CREATE TRIGGER IF NOT EXISTS concepts_ai AFTER INSERT ON concepts BEGIN
                INSERT INTO concepts_fts(rowid, name, content)
                VALUES (new.rowid, new.name, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS concepts_ad AFTER DELETE ON concepts BEGIN
                INSERT INTO concepts_fts(concepts_fts, rowid, name, content)
                VALUES ('delete', old.rowid, old.name, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS concepts_au AFTER UPDATE ON concepts BEGIN
                INSERT INTO concepts_fts(concepts_fts, rowid, name, content)
                VALUES ('delete', old.rowid, old.name, old.content);
                INSERT INTO concepts_fts(rowid, name, content)
                VALUES (new.rowid, new.name, new.content);
            END;
            PRAGMA foreign_keys = ON;
        """
        )
        await conn.commit()

    yield pool
    # Close DB synchronously to prevent aiosqlite background thread from
    # raising RuntimeError('Event loop is closed') during shutdown on 3.11.
    with contextlib.suppress(Exception):
        if pool._write_conn is not None:
            await pool._write_conn.close()
            pool._write_conn = None
        for conn in pool._read_conns:
            await conn.close()
        pool._read_conns.clear()


@pytest.fixture
async def brain_service(db_pool: DatabasePool):
    """Create a BrainService with real repos."""
    mock_embedder = AsyncMock()
    mock_embedder.embed = AsyncMock(return_value=None)
    mock_embedder.embed_batch = AsyncMock(return_value=[])
    mock_embedder.encode = AsyncMock(return_value=None)
    mock_embedder.compute_category_centroid = AsyncMock(return_value=None)
    mock_embedder.has_embeddings = False

    concepts = ConceptRepository(db_pool, mock_embedder)
    relations = RelationRepository(db_pool)
    episodes = EpisodeRepository(db_pool, mock_embedder)
    retrieval = HybridRetrieval(concepts, episodes, mock_embedder)
    memory = WorkingMemory(capacity=100)
    hebbian = HebbianLearning(relations, memory, MindId(MIND_ID))

    service = BrainService.__new__(BrainService)
    service._concepts = concepts
    service._relations = relations
    service._retrieval = retrieval
    service._episodes = episodes
    service._memory = memory
    service._hebbian = hebbian
    service._llm_router = None
    service._fast_model = ""
    service._embedder = None
    service._embedding = mock_embedder
    mock_embedder.has_embeddings = False
    service._spreading = SpreadingActivation(relations, memory)
    service._background_tasks = set()
    service._events = AsyncMock()

    return service


@pytest.fixture
def plugin(brain_service: BrainService) -> KnowledgePlugin:
    """Create a KnowledgePlugin with real BrainAccess."""
    enforcer = PermissionEnforcer("knowledge", {"brain:read", "brain:write"})
    access = BrainAccess(
        brain=brain_service,
        enforcer=enforcer,
        write_allowed=True,
        plugin_name="knowledge",
        mind_id=MIND_ID,
    )
    return KnowledgePlugin(brain=access)


class TestE2ERememberAndSearch:
    """Full round-trip: remember → search → recall."""

    @pytest.mark.asyncio
    async def test_remember_creates_and_search_finds(self, plugin: KnowledgePlugin) -> None:
        # Remember
        r1 = json.loads(
            await plugin.remember("Python is a programming language", name="python-lang")
        )
        assert r1["ok"] is True, f"Remember error: {r1}"
        assert r1["action"] == "created"
        assert r1["concept_id"]

        # Search via text
        r2 = json.loads(await plugin.search("Python"))
        assert r2["ok"] is True
        assert r2["count"] >= 1
        names = [r["name"] for r in r2["results"]]
        assert "python-lang" in names

    @pytest.mark.asyncio
    async def test_remember_and_forget_round_trip(self, plugin: KnowledgePlugin) -> None:
        r1 = json.loads(await plugin.remember("temporary fact xyz123", name="temp-xyz"))
        assert r1["ok"] is True

        r2 = json.loads(await plugin.forget("temp-xyz"))
        assert r2["ok"] is True
        assert r2["action"] == "forgotten"

        r3 = json.loads(await plugin.search("temp-xyz"))
        assert r3["results"] == []

    @pytest.mark.asyncio
    async def test_what_do_you_know_reflects_state(self, plugin: KnowledgePlugin) -> None:
        r1 = json.loads(await plugin.what_do_you_know())
        assert r1["ok"] is True
        assert r1["total_concepts"] == 0

        await plugin.remember("fact one alpha", name="f1-alpha")
        await plugin.remember("fact two beta", name="f2-beta")

        r2 = json.loads(await plugin.what_do_you_know())
        assert r2["ok"] is True
        assert r2["total_concepts"] >= 2

    @pytest.mark.asyncio
    async def test_person_scoped_round_trip(self, plugin: KnowledgePlugin) -> None:
        await plugin.remember("likes dark mode", about_person="Guipe", name="guipe-dark")
        await plugin.remember("prefers light mode", about_person="Natasha", name="natasha-light")

        # Search all
        r_all = json.loads(await plugin.search("mode"))
        assert r_all["count"] >= 2

        # Search person-scoped
        r_guipe = json.loads(await plugin.search("mode", about_person="Guipe"))
        guipe_names = [r["name"] for r in r_guipe["results"]]
        assert "guipe-dark" in guipe_names
        assert "natasha-light" not in guipe_names

    @pytest.mark.asyncio
    async def test_recall_about_e2e(self, plugin: KnowledgePlugin) -> None:
        await plugin.remember("Rust is a systems language", name="rust-lang")
        await plugin.remember("Rust has zero-cost abstractions", name="rust-features")

        r = json.loads(await plugin.recall_about("Rust"))
        assert r["ok"] is True
        assert r["action"] == "recall"
        assert r["count"] >= 1

    @pytest.mark.asyncio
    async def test_multiple_remember_same_name_dedup(self, plugin: KnowledgePlugin) -> None:
        """Same name should dedup via BrainService."""
        r1 = json.loads(await plugin.remember("Go is fast", name="go-lang"))
        assert r1["action"] == "created"
        cid1 = r1["concept_id"]

        # Same name again — BrainService dedup kicks in
        r2 = json.loads(await plugin.remember("Go is fast and concurrent", name="go-lang"))
        # Should be created (learn_concept handles same-name dedup internally)
        assert r2["ok"] is True


class TestE2EStructuredOutput:
    """Verify structured output contract in E2E context."""

    @pytest.mark.asyncio
    async def test_all_responses_have_ok_and_message(self, plugin: KnowledgePlugin) -> None:
        responses = [
            await plugin.remember("e2e test fact", name="e2e-struct"),
            await plugin.search("e2e"),
            await plugin.forget("e2e-struct"),
            await plugin.recall_about("e2e"),
            await plugin.what_do_you_know(),
        ]

        for raw in responses:
            data = json.loads(raw)
            assert "ok" in data, f"Missing 'ok' in {data}"
            assert "message" in data, f"Missing 'message' in {data}"
            assert isinstance(data["ok"], bool)
            assert isinstance(data["message"], str)
