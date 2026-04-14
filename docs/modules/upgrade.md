# Módulo: upgrade

## Objetivo

Gerencia operações de ciclo de vida do Sovyx que vão além do runtime normal: diagnóstico completo (`sovyx doctor`), migração de schema com SemVer, import/export de Minds no formato **SMF (Sovyx Memory Format)** e arquivos `.sovyx-mind` (ZIP), backup + rollback e blue-green upgrade. É a porta de entrada para GDPR Art. 20 (data portability) e para migração de usuários vindos de outros assistentes.

**Estado atual: ~53% completo.** Infra de migração/backup pronta; importers de conversas (ChatGPT, Claude, Gemini, Obsidian) não foram implementados.

## Responsabilidades

- **Doctor** — 10+ diagnósticos: integridade DB, schema, consistência brain, config, disk, memory, model files, portas, versão Python, deps.
- **Schema migrations** — SemVer, forward-only, idempotente, runner dedicado em `schema.py` (complementa o runner de `persistence/migrations.py` com suporte multi-banco).
- **MindImporter** — carrega SMF (diretório) ou `.sovyx-mind` (ZIP) em uma Mind existente ou nova.
- **Exporter** — gera SMF para backup manual / transferência entre instâncias.
- **BackupManager** — snapshots com rollback on failure durante upgrade.
- **BlueGreen** — estratégia de upgrade zero-downtime (blue ativo → green novo → swap).

## Arquitetura

```
upgrade/
  ├── doctor.py         Doctor (10+ DiagnosticResult com PASS/WARN/FAIL)
  ├── schema.py         MigrationRunner + UpgradeMigration (SemVer)
  ├── importer.py       MindImporter (SMF + .sovyx-mind ZIP)
  ├── exporter.py       Exporter (SMF output)
  ├── backup_manager.py BackupManager (pre-upgrade snapshot + rollback)
  ├── blue_green.py     Blue-Green deployment
  └── migrations/       migrations versionadas
```

## Código real (exemplos curtos)

**`src/sovyx/upgrade/doctor.py`** — resultado estruturado:

```python
class DiagnosticStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"

@dataclasses.dataclass(frozen=True)
class DiagnosticResult:
    check: str                   # "db_integrity", "disk_space", ...
    status: DiagnosticStatus
    message: str
    fix_suggestion: str | None = None
    details: dict[str, Any] | None = None
```

**`src/sovyx/upgrade/importer.py`** — formatos e contagens:

```python
@dataclass
class ImportInfo:
    mind_id: str
    source_format: str           # "smf" | "sovyx-mind"
    concepts_imported: int = 0
    episodes_imported: int = 0
    relations_imported: int = 0
    migrations_applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

class ImportValidationError(MigrationError):
    """Raised when import data fails validation."""
```

**Fluxo típico de `sovyx doctor`**:

```python
# CLI → Doctor.run_all() → list[DiagnosticResult]
# Console output com Rich (PASS=green, WARN=yellow, FAIL=red)
# Com --json: serializa list[DiagnosticResult] para stdout
```

## Specs-fonte

- **SPE-028-UPGRADE-MIGRATION** — migrations, doctor, SMF format, blue-green.
- **IMPL-SUP-015-IMPORTS-INTERMIND-PAGINATION** — importers (ChatGPT/Claude/Gemini/Obsidian), InterMindBridge, CursorPagination, SMFExporter GDPR.

## Status de implementação

| Item | Status |
|---|---|
| Doctor (10+ checks: DB, schema, disk, RAM, ports, Python, deps) | Aligned |
| Schema migrations (SemVer, forward-only) | Aligned |
| MindImporter SMF (diretório) | Aligned |
| MindImporter `.sovyx-mind` (ZIP) | Aligned |
| Exporter SMF (básico) | Aligned |
| BackupManager (pre-upgrade + rollback) | Aligned |
| BlueGreen upgrade | Aligned |
| ChatGPTImporter (conversations.json tree) | Not Implemented |
| ClaudeImporter | Not Implemented |
| GeminiImporter | Not Implemented |
| ObsidianImporter (markdown + wikilinks) | Not Implemented |
| InterMindBridge (multi-instance sync) | Not Implemented |
| CursorPagination (REST API) | Not Implemented |
| SMFExporter completo (GDPR Art. 20) | Not Implemented |

## Divergências

**Conversation importers (IMPL-SUP-015 / UPG-007, UPG-008, UPG-009) não implementados** — os três formatos proprietários de export (ChatGPT `conversations.json` tree, Claude, Gemini) exigem parsers específicos para reconstruir o grafo de conversas e mapear para `Episode` + `Concept`. **Impacto comercial: bloqueia onboarding de novos usuários** migrando de concorrentes (gap-analysis Top 10 #3).

**ObsidianImporter não implementado (UPG-010)** — markdown com wikilinks (`[[Link]]`), frontmatter YAML, hierarquia de folders. Relevante para usuários power-user de notes.

**InterMindBridge (MMD-009) não implementado** — sync multi-instância (ex: dois Sovyx rodando em laptop + servidor convergem Brain). Feature complexa, deferida para v0.6+.

**CursorPagination (API-014) não implementado** — paginação baseada em cursor opaco para REST API listagens (conversations, episodes). Hoje usa LIMIT/OFFSET (quebra sob mutação).

**SMFExporter completo (PER-013 / GDPR Art. 20) incompleto** — exporter atual cobre export SMF básico, mas **não garante completude GDPR** (todos os dados pessoais, em formato estruturado machine-readable, incluindo metadata de processamento). Requer auditoria legal antes de produção em EU.

## Dependências

- `yaml` — SMF manifests.
- `zipfile` (stdlib) — `.sovyx-mind` archives.
- `sovyx.persistence.pool.DatabasePool` — executa migrations.
- `sovyx.persistence.manager.DatabaseManager` — cria pools para Minds importadas.
- `sovyx.engine.errors.MigrationError` — base para `ImportValidationError`.
- `sovyx.observability.logging` — todos os arquivos.

Consumidores:

- `sovyx.cli.main` — comandos `sovyx doctor`, `sovyx mind import`, `sovyx mind export`.
- `sovyx.engine.bootstrap` — `MigrationRunner` em startup.

## Testes

- `tests/unit/upgrade/` — doctor checks, schema migrations (order, idempotência, checksum), import roundtrip SMF.
- `tests/integration/upgrade/` — import→export→import verifica que Brain é preservado.
- Blue-green e BackupManager precisam de fixtures com DB real (`tmp_path`) por natureza.

## Referências

- `src/sovyx/upgrade/doctor.py` — Doctor, DiagnosticResult.
- `src/sovyx/upgrade/schema.py` — UpgradeMigration, MigrationRunner.
- `src/sovyx/upgrade/importer.py` — MindImporter, ImportValidationError.
- `src/sovyx/upgrade/exporter.py` — SMF exporter.
- `src/sovyx/upgrade/backup_manager.py` — pre-upgrade snapshot + rollback.
- `src/sovyx/upgrade/blue_green.py` — blue-green deploy.
- `src/sovyx/upgrade/migrations/` — migrations versionadas.
- SPE-028-UPGRADE-MIGRATION — spec de migrations e doctor.
- IMPL-SUP-015-IMPORTS-INTERMIND-PAGINATION — importers, InterMind, cursor, GDPR.
- `docs/_meta/gap-inputs/analysis-C-integration.md` §upgrade — 53% completion.
- `docs/_meta/gap-analysis.md` Top 10 #3.
