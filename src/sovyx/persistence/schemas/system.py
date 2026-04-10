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

_MIGRATION_002_SQL = """\
-- Daily usage statistics for historical tracking.
-- One row per day per mind. Populated at day boundary by CostGuard
-- and DashboardCounters. Survives daemon restarts (unlike in-memory
-- counters). Queried by GET /api/stats/history.
CREATE TABLE IF NOT EXISTS daily_stats (
    date             TEXT    NOT NULL,
    mind_id          TEXT    NOT NULL DEFAULT 'aria',
    messages         INTEGER NOT NULL DEFAULT 0,
    llm_calls        INTEGER NOT NULL DEFAULT 0,
    tokens           INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL    NOT NULL DEFAULT 0.0,
    cost_by_provider TEXT    NOT NULL DEFAULT '{}',
    cost_by_model    TEXT    NOT NULL DEFAULT '{}',
    conversations    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, mind_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date);
"""

_MIGRATION_002 = Migration(
    version=2,
    description="daily usage stats for historical tracking",
    sql_up=_MIGRATION_002_SQL,
    checksum=Migration.compute_checksum(_MIGRATION_002_SQL),
)


def get_system_migrations() -> list[Migration]:
    """Return system database migrations.

    Returns:
        List of migrations for system.db.
    """
    return [_MIGRATION_001, _MIGRATION_002]
