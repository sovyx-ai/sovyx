# Módulo: persistence

## Objetivo

Camada de persistência assíncrona baseada em SQLite, responsável por todo armazenamento durável do Sovyx: banco global `system.db`, bancos por Mind (`brain.db`, `conversations.db`), migrações versionadas e integração com a extensão `sqlite-vec` para busca vetorial.

## Responsabilidades

- **Pooling assíncrono** — 1 conexão *writer* + N conexões *readers* por banco, usando `aiosqlite` e WAL.
- **Pragmas non-negotiable** — aplica os 9 PRAGMAs do ADR-004 §2.3 em toda conexão aberta.
- **Extensões** — carrega `vec0` (sqlite-vec) com fallback silencioso se indisponível.
- **Isolamento por Mind** — cada Mind possui seu próprio `brain.db` e `conversations.db` (DB-per-Mind).
- **Migrações SemVer** — forward-only, idempotentes, checksummed (SHA-256).
- **Lifecycle** — `DatabaseManager` implementa `Lifecycle` (start/stop) e participa do bootstrap do Engine.
- **Shutdown ordenado** — readers fecham primeiro, depois `PRAGMA wal_checkpoint(TRUNCATE)`, writer por último.

## Arquitetura

```
DatabaseManager (lifecycle)
  ├── system_pool   → data_dir/system.db              (global)
  ├── brain_pools   → data_dir/minds/<id>/brain.db    (por Mind)
  └── conv_pools    → data_dir/minds/<id>/convs.db    (por Mind)

DatabasePool
  ├── 1 write conn  (serializado via asyncio.Lock)
  ├── N read conns  (round-robin, sem lock — WAL permite leitura paralela)
  └── extensions    (vec0 carregado em todas as conexões)

MigrationRunner
  └── _schema table { version, description, checksum, applied_at, duration_ms }
```

### 9 Pragmas Non-Negotiable (ADR-004 §2.3)

| Pragma | Valor | Motivo |
|---|---|---|
| `journal_mode` | WAL | Permite 1 writer + N readers concorrentes |
| `synchronous` | NORMAL | Durabilidade adequada, melhor throughput que FULL |
| `temp_store` | MEMORY | Sem I/O para tabelas temporárias |
| `foreign_keys` | ON | Integridade referencial ativa |
| `busy_timeout` | 5000 | 5s antes de SQLITE_BUSY |
| `wal_autocheckpoint` | 1000 | Checkpoint automático a cada 1000 frames |
| `auto_vacuum` | INCREMENTAL | Compactação progressiva sem bloquear |
| `cache_size` | (configurável) | Páginas em memória |
| `mmap_size` | (configurável) | Memory-map para reads rápidos |

## Código real (exemplos curtos)

**`src/sovyx/persistence/pool.py`** — pragmas e pool 1W+NR:

```python
_DEFAULT_PRAGMAS: dict[str, str | int] = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "temp_store": "MEMORY",
    "foreign_keys": "ON",
    "busy_timeout": 5000,
    "wal_autocheckpoint": 1000,
    "auto_vacuum": "INCREMENTAL",
}

class DatabasePool:
    async def initialize(self) -> None:
        self._write_conn = await aiosqlite.connect(str(self._db_path))
        await self._setup_connection(self._write_conn)
        for _ in range(self._read_pool_size):
            conn = await aiosqlite.connect(str(self._db_path))
            await self._setup_connection(conn)
            self._read_conns.append(conn)
```

**`src/sovyx/persistence/pool.py`** — carregamento da extensão `vec0`:

```python
async def _load_extensions(self, conn: aiosqlite.Connection) -> None:
    for name in self._extension_names:
        ext_path = self._find_extension_path(name)
        if ext_path is None:
            logger.warning("extension_not_found", extension=name)
            continue
        await conn.enable_load_extension(True)
        await conn.load_extension(ext_path)
        await conn.enable_load_extension(False)
        if name == "vec0":
            self._has_sqlite_vec = True
```

**`src/sovyx/persistence/migrations.py`** — migração com checksum:

```python
@dataclasses.dataclass(frozen=True)
class Migration:
    version: int
    description: str
    sql_up: str
    checksum: str

    @staticmethod
    def compute_checksum(sql: str) -> str:
        return hashlib.sha256(sql.encode()).hexdigest()
```

**Shutdown ordenado em `close()`**:

```python
# 1. readers primeiro (liberam shared locks)
for conn in self._read_conns:
    await conn.close()
# 2. checkpoint WAL antes de fechar o writer
await self._write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
# 3. writer por último
await self._write_conn.close()
```

## Specs-fonte

- **ADR-004-DATABASE-STACK** — escolha SQLite + WAL + sqlite-vec; 9 pragmas obrigatórios; menciona Redis caching (ainda não implementado).
- **SPE-005-PERSISTENCE-LAYER** — contrato de transações, DB-per-Mind, migrations, pool sizing.

## Status de implementação

| Item | Status |
|---|---|
| Pool assíncrono 1W+NR (WAL) | Aligned |
| 9 pragmas ADR-004 | Aligned |
| Carregamento `vec0` + fallback | Aligned |
| DB-per-Mind | Aligned |
| MigrationRunner (checksum + forward-only) | Aligned |
| Shutdown ordenado (readers → checkpoint → writer) | Aligned |
| Vector search (SELECT usando vec0) | Partial — extensão carrega, mas queries MATCH vec não visíveis no scan |
| Redis caching layer | Not Implemented — mencionado em ADR-004, sem código |

## Divergências

**Vector search queries ausentes** — `ADR-004` decide usar `sqlite-vec` para busca semântica, e `pool.py` carrega `vec0` com sucesso. Porém o scan dos 9 arquivos não encontra SQL do tipo `SELECT ... FROM vec_table WHERE embedding MATCH ?`. Hipótese: queries vivem em `brain/retrieval.py` ou `brain/embedding.py` (não inspecionados nesta fase). Ação: auditar chamadores de `get_brain_pool()`.

**Redis caching layer** — ADR-004 lista Redis como camada opcional de cache para embeddings quentes. Não há nenhum arquivo `redis_cache.py` ou similar em `persistence/`. Tratado como feature v1.0 no roadmap.

## Dependências

- `aiosqlite` — driver async SQLite.
- `sqlite-vec` (opcional, via pip) — extensão de busca vetorial.
- `sovyx.engine.errors.DatabaseConnectionError`, `MigrationError`.
- `sovyx.engine.config.EngineConfig.database` — `data_dir`, `read_pool_size`.
- `sovyx.engine.events.EventBus` — emite eventos de lifecycle.

Consumidores diretos:

- `brain/` (todos os serviços de memória).
- `bridge/` (conversations).
- `cognitive/` (perception log, reflection).
- `cloud/backup.py` (VACUUM INTO para snapshots).

## Testes

- `tests/unit/persistence/` — pool, migrations, pragmas, extensão fallback.
- Testes cobrem: inicialização, checkpoint no close, rejeição de migração com checksum inválido, round-robin de readers, serialização de writes.
- Usar fixture `tmp_path` para DBs isolados; nunca compartilhar entre testes.

## Public API reference

### Public API

| Classe | Descrição |
|---|---|
| `DatabaseManager` | Gerencia todos os bancos do Sovyx (system + per-mind brain/conversations); lifecycle. |
| `DatabasePool` | Pool async 1 writer + N readers sobre aiosqlite + WAL + extensões opcionais. |
| `MigrationRunner` | Executa schema migrations com verificação de integridade (SHA-256 checksum). |
| `Migration` | Uma única schema migration (version, description, sql_up, checksum). |

## Referências

- `src/sovyx/persistence/pool.py` — `DatabasePool`, pragmas, extensões.
- `src/sovyx/persistence/manager.py` — `DatabaseManager` (lifecycle).
- `src/sovyx/persistence/migrations.py` — `Migration`, `MigrationRunner`.
- `src/sovyx/persistence/schemas/{system,brain,conversations}.py` — DDL.
- `src/sovyx/persistence/datetime_utils.py` — serialização ISO-8601.
- ADR-004-DATABASE-STACK — decisão arquitetural dos 9 pragmas.
- SPE-005-PERSISTENCE-LAYER — contrato de transações.
- `docs/_meta/gap-inputs/analysis-B-services.md` §persistence — gaps identificados.
- `docs/_meta/gap-analysis.md` §persistence — resumo consolidado.
