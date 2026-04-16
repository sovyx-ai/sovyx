# Module: persistence

## What it does

`sovyx.persistence` is the async SQLite layer that stores everything durable — brain concepts, episodes, relations, conversations, and system config. It provides a connection pool with WAL mode (one writer, N concurrent readers), a versioned migration runner, and optional `sqlite-vec` extension loading for vector similarity search.

## Key classes

| Name | Responsibility |
|---|---|
| `DatabasePool` | 1 writer + N reader connections on aiosqlite with WAL. |
| `DatabaseManager` | High-level lifecycle — initializes pools per Mind. |
| `MigrationRunner` | Forward-only schema versioning with SHA-256 checksum. |
| `Migration` | One migration step (version, description, sql_up, checksum). |
| `parse_db_datetime` | Timezone-aware datetime parsing from SQLite text columns. |

## Database pool

`DatabasePool` enforces a strict write-serialization / read-parallelism model:

- **Writer**: single `aiosqlite` connection. All writes go through `pool.write()` or `pool.transaction()`.
- **Readers**: N connections (default 3), dispatched round-robin. All reads go through `pool.read()`.
- **WAL mode**: write-ahead logging allows readers to proceed without blocking on the writer.

```python
async with pool.transaction() as conn:
    await conn.execute("INSERT INTO concepts ...")
    await conn.execute("INSERT INTO concept_embeddings ...")
    # auto-commit on exit, auto-rollback on exception

async with pool.read() as conn:
    cursor = await conn.execute("SELECT * FROM concepts WHERE mind_id = ?", (mind_id,))
    rows = await cursor.fetchall()
```

## Pragmas

Nine non-negotiable pragmas set at connection open:

| Pragma | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | 1 writer + N readers concurrently |
| `synchronous` | `NORMAL` | Adequate durability without fsync on every commit |
| `temp_store` | `MEMORY` | Temp tables in RAM |
| `foreign_keys` | `ON` | CASCADE deletes on relations |
| `busy_timeout` | `5000` | 5 s before SQLITE_BUSY |
| `wal_autocheckpoint` | `1000` | Periodic WAL → main DB transfer |
| `auto_vacuum` | `INCREMENTAL` | Reclaim space without full VACUUM |
| `cache_size` | configurable | Pages in memory |
| `mmap_size` | configurable | Memory-mapped reads |

## Schemas

Three schema modules under `persistence/schemas/`:

| Schema | Database | Migrations | Contents |
|---|---|---|---|
| `brain.py` | `brain.db` | 6 | concepts, episodes, relations, FTS5, sqlite-vec embeddings, consolidation_log, conversation_imports |
| `conversations.py` | `conversations.db` | — | message history, metadata |
| `system.py` | `system.db` | — | mind config, API keys, plugin state |

Each Mind gets its own `brain.db` and `conversations.db` under `~/.sovyx/<mind>/`.

## Migrations

`MigrationRunner` applies migrations forward-only. Each migration carries a SHA-256 checksum of its SQL; if the checksum doesn't match at runtime, the runner refuses to proceed (guards against silent edits).

```python
runner = MigrationRunner(pool)
await runner.initialize()
migrations = get_brain_migrations(has_sqlite_vec=pool.has_sqlite_vec)
applied = await runner.run_migrations(migrations)
```

Current brain migrations: 001 (core tables + FTS5), 002 (sqlite-vec virtual tables, conditional), 003 (canonical relation ordering), 004 (covering indices), 005 (conversation_imports dedup), 006 (PAD 3D emotional model).

## sqlite-vec

The pool attempts to load the `vec0` extension at startup. If unavailable, `pool.has_sqlite_vec` is `False` and all vector queries gracefully degrade to FTS5-only. Migration 002 (embedding virtual tables) is skipped when the extension is absent.

## Configuration

```yaml
# system.yaml
database:
  read_pool_size: 3
  cache_size: -2000        # negative = KiB (2 MB)
  mmap_size: 268435456     # 256 MB
```

## Roadmap

- Optional Redis caching layer for multi-instance deployments (v1.0).
- Read replica support for horizontally-scaled dashboards.

## See also

- Source: `src/sovyx/persistence/pool.py`, `src/sovyx/persistence/migrations.py`, `src/sovyx/persistence/schemas/`.
- Tests: `tests/unit/persistence/`.
- Related: [`brain`](./brain.md) (primary consumer), [`engine`](./engine.md) (bootstrap creates pools).
