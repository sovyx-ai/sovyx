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

_MIGRATION_003_SQL = """\
-- Mission C4 §Phase 3 — operator acknowledgement state for the
-- composite degraded banner. Survives daemon restart so the operator
-- does NOT see a re-surfaced banner after a refresh / multi-tab
-- session that already acked the same condition.
--
-- Cardinality bounded by the cross-axis degraded reason count
-- (typically ≤ 8 active reasons per session). PK is the canonical
-- reason token so re-acking the same reason upserts in place.
--
-- TTL semantics: an ack is "active" when ``acked_at_ts + ttl_sec >
-- strftime('%s','now')``. Expired acks are surfaced via the
-- ``voice.degraded_banner.resurfaced`` event by the Phase 3 TTL
-- re-surface scheduler at :mod:`sovyx.engine._ack_resurface_scheduler`,
-- then removed from this table on the next prune cycle.
--
-- ``operator_id`` is best-effort identification derived from the
-- dashboard auth token hash; empty string when unidentifiable. NOT
-- a credential — purely audit-trail-grade.
--
-- ``metadata`` is JSON-encoded axis-specific context captured at ack
-- time (e.g. the candidates_unreachable list for the voice axis).
CREATE TABLE IF NOT EXISTS operator_acks (
    reason       TEXT    NOT NULL PRIMARY KEY,
    acked_at_ts  INTEGER NOT NULL,
    ttl_sec      INTEGER NOT NULL,
    operator_id  TEXT    NOT NULL DEFAULT '',
    metadata     TEXT    NOT NULL DEFAULT '{}'
);
"""

_MIGRATION_003 = Migration(
    version=3,
    description="operator_acks — composite degraded banner ack persistence (Mission C4 Phase 3)",
    sql_up=_MIGRATION_003_SQL,
    checksum=Migration.compute_checksum(_MIGRATION_003_SQL),
)


def get_system_migrations() -> list[Migration]:
    """Return system database migrations.

    Returns:
        List of migrations for system.db.
    """
    return [_MIGRATION_001, _MIGRATION_002, _MIGRATION_003]
