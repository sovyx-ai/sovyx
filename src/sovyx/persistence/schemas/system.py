"""System database schema migrations.

Defines tables for engine state, persons, and channel mappings.
"""

from __future__ import annotations

from sovyx.persistence.migrations import Migration

_MIGRATION_001_SQL = """\
-- Engine key-value state store
CREATE TABLE engine_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Persons (cross-channel identity)
CREATE TABLE persons (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    display_name TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Channel-to-person mappings
CREATE TABLE channel_mappings (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    channel_type TEXT NOT NULL,
    channel_user_id TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel_type, channel_user_id)
);
CREATE INDEX idx_mapping_person ON channel_mappings(person_id);
CREATE INDEX idx_mapping_channel ON channel_mappings(channel_type, channel_user_id);
"""

_MIGRATION_001 = Migration(
    version=1,
    description="engine state, persons, channel mappings",
    sql_up=_MIGRATION_001_SQL,
    checksum=Migration.compute_checksum(_MIGRATION_001_SQL),
)


def get_system_migrations() -> list[Migration]:
    """Return system database migrations.

    Returns:
        List of migrations for system.db.
    """
    return [_MIGRATION_001]
