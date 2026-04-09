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
-- Covering indices for weighted neighbor queries (spreading activation, consolidation)
CREATE INDEX IF NOT EXISTS idx_relations_source_weight ON relations(source_id, weight DESC);
CREATE INDEX IF NOT EXISTS idx_relations_target_weight ON relations(target_id, weight DESC);

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

# ── Migration 003: Canonical relation ordering ─────────────────────────────

_MIGRATION_003_SQL = """\
-- Merge bidirectional duplicate relations into canonical order.
-- Canonical: source_id = min(source_id, target_id) by string comparison.
--
-- Strategy:
--   1. Identify pairs where both A→B and B→A exist (non-canonical = B→A where B > A).
--   2. For each pair: merge co_occurrence (sum), weight (max) into canonical row.
--   3. Delete non-canonical rows.
--   4. Flip remaining non-canonical rows that have no canonical counterpart.

-- Step 1+2: Merge duplicates — add co_occurrence and take max weight
-- from the non-canonical row into the canonical row.
UPDATE relations
SET co_occurrence_count = co_occurrence_count + (
        SELECT r2.co_occurrence_count
        FROM relations r2
        WHERE r2.source_id = relations.target_id
          AND r2.target_id = relations.source_id
          AND r2.relation_type = relations.relation_type
    ),
    weight = MAX(weight, (
        SELECT r2.weight
        FROM relations r2
        WHERE r2.source_id = relations.target_id
          AND r2.target_id = relations.source_id
          AND r2.relation_type = relations.relation_type
    ))
WHERE source_id < target_id
  AND EXISTS (
      SELECT 1 FROM relations r2
      WHERE r2.source_id = relations.target_id
        AND r2.target_id = relations.source_id
        AND r2.relation_type = relations.relation_type
  );

-- Step 3: Delete the non-canonical half of merged duplicates.
DELETE FROM relations
WHERE source_id > target_id
  AND EXISTS (
      SELECT 1 FROM relations r2
      WHERE r2.source_id = relations.target_id
        AND r2.target_id = relations.source_id
        AND r2.relation_type = relations.relation_type
  );

-- Step 4: Flip remaining non-canonical rows (no canonical counterpart).
-- SQLite doesn't support UPDATE with self-join on same table easily,
-- so we use a temp table approach.
CREATE TEMPORARY TABLE _flip_relations AS
SELECT id, target_id AS new_source, source_id AS new_target
FROM relations
WHERE source_id > target_id;

UPDATE relations
SET source_id = (SELECT new_source FROM _flip_relations WHERE _flip_relations.id = relations.id),
    target_id = (SELECT new_target FROM _flip_relations WHERE _flip_relations.id = relations.id)
WHERE id IN (SELECT id FROM _flip_relations);

DROP TABLE IF EXISTS _flip_relations;
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

_MIGRATION_003 = Migration(
    version=3,
    description="canonical relation ordering — merge bidirectional duplicates",
    sql_up=_MIGRATION_003_SQL,
    checksum=Migration.compute_checksum(_MIGRATION_003_SQL),
)


_MIGRATION_004_SQL = """\
-- Covering indices for weighted neighbor queries.
-- Speeds up spreading activation and degree centrality computation
-- by allowing index-only scans on (source/target, weight) pairs.
CREATE INDEX IF NOT EXISTS idx_relations_source_weight ON relations(source_id, weight DESC);
CREATE INDEX IF NOT EXISTS idx_relations_target_weight ON relations(target_id, weight DESC);
"""

_MIGRATION_004 = Migration(
    version=4,
    description="covering indices for weighted relation queries",
    sql_up=_MIGRATION_004_SQL,
    checksum=Migration.compute_checksum(_MIGRATION_004_SQL),
)


def get_brain_migrations(*, has_sqlite_vec: bool = True) -> list[Migration]:
    """Return brain database migrations.

    Migration 002 (sqlite-vec virtual tables) is only included when
    has_sqlite_vec=True. Without the extension loaded, the CREATE
    VIRTUAL TABLE USING vec0(...) SQL would fail.

    If sqlite-vec is installed later, the next restart detects that
    migration 002 hasn't been applied and applies it automatically.

    Migration 003 (canonical relation ordering) is always included —
    it merges bidirectional duplicate relations and flips non-canonical
    rows so that ``source_id < target_id`` (string comparison).

    Args:
        has_sqlite_vec: Whether the sqlite-vec extension is available.

    Returns:
        List of migrations to apply.
    """
    migrations = [_MIGRATION_001]
    if has_sqlite_vec:
        migrations.append(_MIGRATION_002)
    migrations.append(_MIGRATION_003)
    migrations.append(_MIGRATION_004)
    return migrations
