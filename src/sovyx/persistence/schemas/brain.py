"""Brain database schema migrations.

Defines tables for concepts (neocortex), episodes (hippocampus),
relations (synapses), FTS5 search, and optional sqlite-vec embeddings.
"""

from __future__ import annotations

from sovyx.persistence.migrations import Migration

# ── Migration 001: Core tables + FTS5 ──────────────────────────────────────

_MIGRATION_001_SQL = """\
-- Concepts (Neocortex — semantic memory)
CREATE TABLE concepts (
    id TEXT PRIMARY KEY,
    mind_id TEXT NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'fact',
    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.5,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed TIMESTAMP,
    emotional_valence REAL NOT NULL DEFAULT 0.0,
    source TEXT NOT NULL DEFAULT 'conversation',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_concepts_mind ON concepts(mind_id);
CREATE INDEX idx_concepts_category ON concepts(mind_id, category);
CREATE INDEX idx_concepts_importance ON concepts(mind_id, importance DESC);
CREATE INDEX idx_concepts_accessed ON concepts(mind_id, last_accessed DESC);

-- Episodes (Hippocampus — episodic memory)
CREATE TABLE episodes (
    id TEXT PRIMARY KEY,
    mind_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_input TEXT NOT NULL,
    assistant_response TEXT NOT NULL,
    summary TEXT,
    importance REAL NOT NULL DEFAULT 0.5,
    emotional_valence REAL NOT NULL DEFAULT 0.0,
    emotional_arousal REAL NOT NULL DEFAULT 0.0,
    concepts_mentioned TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_episodes_mind ON episodes(mind_id);
CREATE INDEX idx_episodes_conversation ON episodes(mind_id, conversation_id);
CREATE INDEX idx_episodes_importance ON episodes(mind_id, importance DESC);
CREATE INDEX idx_episodes_created ON episodes(mind_id, created_at DESC);

-- Relations (Synapses — concept graph)
CREATE TABLE relations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT 'related_to',
    weight REAL NOT NULL DEFAULT 0.5,
    co_occurrence_count INTEGER NOT NULL DEFAULT 1,
    last_activated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, target_id, relation_type)
);
CREATE INDEX idx_relations_source ON relations(source_id);
CREATE INDEX idx_relations_target ON relations(target_id);
CREATE INDEX idx_relations_weight ON relations(weight DESC);

-- FTS5 for text search
CREATE VIRTUAL TABLE concepts_fts USING fts5(
    name, content, category,
    content='concepts',
    content_rowid='rowid'
);

-- Triggers to keep FTS5 in sync
CREATE TRIGGER concepts_fts_insert AFTER INSERT ON concepts BEGIN
    INSERT INTO concepts_fts(rowid, name, content, category)
    VALUES (new.rowid, new.name, new.content, new.category);
END;
CREATE TRIGGER concepts_fts_delete AFTER DELETE ON concepts BEGIN
    INSERT INTO concepts_fts(concepts_fts, rowid, name, content, category)
    VALUES ('delete', old.rowid, old.name, old.content, old.category);
END;
CREATE TRIGGER concepts_fts_update AFTER UPDATE ON concepts BEGIN
    INSERT INTO concepts_fts(concepts_fts, rowid, name, content, category)
    VALUES ('delete', old.rowid, old.name, old.content, old.category);
    INSERT INTO concepts_fts(rowid, name, content, category)
    VALUES (new.rowid, new.name, new.content, new.category);
END;

-- Consolidation log
CREATE TABLE consolidation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mind_id TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    concepts_created INTEGER NOT NULL DEFAULT 0,
    concepts_pruned INTEGER NOT NULL DEFAULT 0,
    relations_strengthened INTEGER NOT NULL DEFAULT 0,
    relations_pruned INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER
);
"""

# ── Migration 002: sqlite-vec embeddings ────────────────────────────────────

_MIGRATION_002_SQL = """\
-- Vector tables for embedding search (requires sqlite-vec extension)
-- 384 dimensions = E5-small-v2
CREATE VIRTUAL TABLE concept_embeddings USING vec0(
    concept_id TEXT PRIMARY KEY,
    embedding FLOAT[384]
);

CREATE VIRTUAL TABLE episode_embeddings USING vec0(
    episode_id TEXT PRIMARY KEY,
    embedding FLOAT[384]
);
"""

# ── Pre-computed checksums ──────────────────────────────────────────────────

_MIGRATION_001 = Migration(
    version=1,
    description="brain core tables, indexes, FTS5, triggers",
    sql_up=_MIGRATION_001_SQL,
    checksum=Migration.compute_checksum(_MIGRATION_001_SQL),
)

_MIGRATION_002 = Migration(
    version=2,
    description="sqlite-vec embedding tables (E5-small-v2, 384d)",
    sql_up=_MIGRATION_002_SQL,
    checksum=Migration.compute_checksum(_MIGRATION_002_SQL),
)


def get_brain_migrations(*, has_sqlite_vec: bool = True) -> list[Migration]:
    """Return brain database migrations.

    Migration 002 (sqlite-vec virtual tables) is only included when
    has_sqlite_vec=True. Without the extension loaded, the CREATE
    VIRTUAL TABLE USING vec0(...) SQL would fail.

    If sqlite-vec is installed later, the next restart detects that
    migration 002 hasn't been applied and applies it automatically.

    Args:
        has_sqlite_vec: Whether the sqlite-vec extension is available.

    Returns:
        List of migrations to apply.
    """
    migrations = [_MIGRATION_001]
    if has_sqlite_vec:
        migrations.append(_MIGRATION_002)
    return migrations
