"""Conversation database schema migrations.

Defines tables for conversations, turns, and FTS5 search on turn content.
"""

from __future__ import annotations

from sovyx.persistence.migrations import Migration

_MIGRATION_001_SQL = """\
-- Conversations
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    mind_id TEXT NOT NULL,
    person_id TEXT,
    channel TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_message_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    message_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_conv_mind ON conversations(mind_id);
CREATE INDEX idx_conv_status ON conversations(mind_id, status);
CREATE INDEX idx_conv_last ON conversations(mind_id, last_message_at DESC);

-- Conversation turns
CREATE TABLE conversation_turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tokens INTEGER,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_turns_conv ON conversation_turns(conversation_id, created_at);

-- FTS5 for turn content search
CREATE VIRTUAL TABLE turns_fts USING fts5(
    content,
    content='conversation_turns',
    content_rowid='rowid'
);

-- FTS5 sync triggers
CREATE TRIGGER turns_ai AFTER INSERT ON conversation_turns BEGIN
    INSERT INTO turns_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER turns_ad AFTER DELETE ON conversation_turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, content)
    VALUES('delete', old.rowid, old.content);
END;
CREATE TRIGGER turns_au AFTER UPDATE ON conversation_turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, content)
    VALUES('delete', old.rowid, old.content);
    INSERT INTO turns_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""

_MIGRATION_001 = Migration(
    version=1,
    description="conversations, turns, FTS5 with sync triggers",
    sql_up=_MIGRATION_001_SQL,
    checksum=Migration.compute_checksum(_MIGRATION_001_SQL),
)


def get_conversation_migrations() -> list[Migration]:
    """Return conversation database migrations.

    Returns:
        List of migrations for conversations.db.
    """
    return [_MIGRATION_001]
