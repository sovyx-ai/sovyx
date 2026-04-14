# Módulo: plugins

## Objetivo

Sistema de extensão do Sovyx: descoberta, carregamento, sandboxing, permissões e execução de *plugins* de terceiros. Cada plugin expõe *tools* (funções LLM-callable) que o Mind pode invocar via function calling. O módulo é **o maior do projeto** — 19 arquivos, 9860 LOC, 32 documentos de spec — porque concentra a superfície de ataque e a *API pública* do ecossistema.

## Responsabilidades

- **Descoberta e ciclo de vida** — `PluginManager` carrega, resolve dependências (topological sort), inicializa, monitora saúde e desabilita plugins que falham consecutivamente.
- **Segurança estática (AST scanner)** — `PluginSecurityScanner` analisa o código no install/validate time procurando imports/calls/atributos bloqueados.
- **Segurança runtime (`ImportGuard`)** — `sys.meta_path` hook que bloqueia `__import__()` e `importlib` durante a execução do plugin.
- **Sandbox filesystem** — `SandboxedFsAccess`: operações escopadas ao `data_dir`, resolução de symlinks antes de checar path, limites de 50 MB/arquivo e 500 MB/plugin.
- **Sandbox HTTP** — `SandboxedHttpAccess`: rate limiting, allowlist de domínios, timeout.
- **Permissões capability-based** — 13 permissões (brain/event/network/fs/scheduler/vault/proactive) no modelo Deno (`--allow-*`). O número **18** em IMPL-012 refere-se a *escape vectors* do threat model (vetores de fuga analisados), não permissions concedidas.
- **SDK** — `ISovyxPlugin` ABC, decorator `@tool`, `ToolDefinition` com JSON Schema.
- **Contexto por plugin** — `PluginContext` injeta apenas acessos aprovados (brain, event bus, fs, http, scheduler, vault).
- **Plugins oficiais** — calculator, financial_math, knowledge, weather, web_intelligence.

## Arquitetura — 7 camadas (v1 implementa 0-4)

```
Layer 0  Manifest validation     (manifest.py)
Layer 1  Static AST scan         (security.py — BLOCKED_IMPORTS / CALLS / ATTRIBUTES)
Layer 2  Runtime ImportGuard     (security.py — sys.meta_path hook)
Layer 3  Permission enforcer     (permissions.py — Deno-style capability)
Layer 4  Sandbox FS + HTTP       (sandbox_fs.py, sandbox_http.py)
--- v1 cutoff ---------------------------------------------------------
Layer 5  seccomp-BPF (Linux)     NOT IMPLEMENTED — v2
Layer 6  Namespaces (mnt/PID/user) NOT IMPLEMENTED — v2
Layer 7  macOS Seatbelt profile  NOT IMPLEMENTED — v2
+ Subprocess IPC                  NOT IMPLEMENTED — v2
```

## Código real (exemplos curtos)

**`src/sovyx/plugins/security.py`** — AST scanner com imports bloqueados:

```python
class PluginSecurityScanner:
    BLOCKED_IMPORTS: frozenset[str] = frozenset({
        "os", "subprocess", "shutil", "sys", "importlib",
        "ctypes", "pickle", "marshal", "code", "codeop",
        "compileall", "multiprocessing", "threading",
        "signal", "resource", "socket",
    })
```

**`src/sovyx/plugins/permissions.py`** — modelo capability-based:

```python
class Permission(enum.StrEnum):
    BRAIN_READ = "brain:read"
    BRAIN_WRITE = "brain:write"
    EVENT_SUBSCRIBE = "event:subscribe"
    EVENT_EMIT = "event:emit"
    NETWORK_LOCAL = "network:local"
    NETWORK_INTERNET = "network:internet"
    FS_READ = "fs:read"
    FS_WRITE = "fs:write"
    SCHEDULER_READ = "scheduler:read"
    SCHEDULER_WRITE = "scheduler:write"
    VAULT_READ = "vault:read"
    VAULT_WRITE = "vault:write"
    PROACTIVE = "proactive"
```

**`src/sovyx/plugins/sandbox_fs.py`** — limites duros:

```python
_MAX_FILE_BYTES = 50 * 1024 * 1024    # 50 MB por arquivo
_MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB por plugin

class SandboxedFsAccess:
    """Filesystem scoped ao data_dir. Symlinks resolvidos ANTES do path check."""
    async def write(self, path: str, data: str | bytes) -> None: ...
    async def read(self, path: str) -> bytes: ...
```

**`src/sovyx/plugins/manager.py`** — auto-disable em falhas consecutivas:

```python
_DEFAULT_TOOL_TIMEOUT_S = 30.0
_MAX_CONSECUTIVE_FAILURES = 5

@dataclasses.dataclass
class _PluginHealth:
    consecutive_failures: int = 0
    disabled: bool = False
    last_error: str = ""
    active_tasks: int = 0
```

**`src/sovyx/plugins/sdk.py`** — ToolDefinition:

```python
@dataclasses.dataclass(frozen=True)
class ToolDefinition:
    name: str                  # "weather.get_weather"
    description: str           # visível para o LLM
    parameters: dict[str, Any] # JSON Schema
    requires_confirmation: bool = False
    timeout_seconds: int = 30
    handler: Callable[..., Any] | None = None
```

## Plugins oficiais

| Plugin | Tools | Permissões |
|---|---|---|
| `calculator` | `evaluate`, `convert_unit` | (nenhuma — pure) |
| `financial_math` | `compound_interest`, `npv`, `irr`, `amortization` | (nenhuma) |
| `knowledge` | `search`, `summarize` | `brain:read` |
| `weather` | `get_weather`, `get_forecast` | `network:internet`, `fs:read/write` |
| `web_intelligence` | `fetch_url`, `extract_content` | `network:internet` |

## Specs-fonte

- **IMPL-012-PLUGIN-SANDBOX** — 7 camadas, 18 vetores de escape mapeados, seccomp.
- **SPE-008-PLUGIN-*** (12 variantes) — SDK, registry, review CI, governance, marketplace.

## Status de implementação

| Item | Status |
|---|---|
| Sandbox v1 (layers 0-4) | Aligned — completo e seguro |
| AST scanner (BLOCKED_IMPORTS/CALLS/ATTRIBUTES) | Aligned |
| ImportGuard runtime (`sys.meta_path`) | Aligned |
| Sandbox FS (50 MB/arq, 500 MB total, symlink check) | Aligned |
| Sandbox HTTP (rate limit) | Aligned |
| Permission enforcer (13 tipos) | Aligned |
| Plugin state machine + health + auto-disable | Aligned |
| SDK `@tool` decorator + ToolDefinition | Aligned |
| 5 plugins oficiais | Aligned |
| Layer 5 — seccomp-BPF (Linux) | Not Implemented — v2 |
| Layer 6 — Namespaces (mnt/PID/user) | Not Implemented — v2 |
| Layer 7 — macOS Seatbelt | Not Implemented — v2 |
| Subprocess IPC protocol | Not Implemented — v2 |
| Zero-downtime update/rollback | Partial |

## Divergências

**v2 kernel isolation deferido intencionalmente** — IMPL-012 especifica sandbox em 3 camadas de isolamento de kernel (seccomp-BPF no Linux, namespaces Linux, Seatbelt no macOS) mais um protocolo IPC para rodar plugins em subprocess. Decisão: v0.5/v1.0 é in-process, confiando no AST scanner + ImportGuard + permissions + FS/HTTP sandbox. Aceitável porque o atacante teria que:

1. passar pelo scanner AST (install time),
2. passar pelo ImportGuard (runtime), e
3. conseguir escalonar privilégios dentro do processo Python.

v2 traz isolamento kernel-level para plugins de *marketplace* (não-oficiais). Roadmap: v1.0.

**Zero-downtime update/rollback parcial** — reload de plugin existe (`hot_reload.py`), mas o rollback para versão anterior em caso de falha pós-reload não está completamente coberto. Hoje o fluxo seguro é disable → install nova versão → enable.

## Dependências

- `sovyx.observability.logging` — todos os arquivos.
- `sovyx.brain.service.BrainService` — via `PluginContext.brain`.
- `sovyx.engine.events.EventBus` — via `PluginContext.events`.
- `sovyx.llm.models.ToolResult` — tipo de retorno de tools.
- Bibliotecas terceiras: `httpx` (sandbox_http), nenhum async FS dedicado (uso de `asyncio.to_thread` para I/O síncrono).

## Testes

- `tests/unit/plugins/` — AST scanner (BLOCKED_IMPORTS), ImportGuard runtime, sandbox FS (symlink escape, traversal, limites), sandbox HTTP (rate limit), PermissionEnforcer, lifecycle (auto-disable após 5 falhas).
- `tests/security/plugins/` — vetores de escape do IMPL-012.
- Plugins oficiais têm próprio test suite.
- Regra crítica (CLAUDE.md anti-pattern #2): **nunca** injetar módulos falsos via `sys.modules` — poisona a suíte toda. Usar DI ou monkeypatch escopado.

## Public API reference

### Public API
| Classe | Descrição |
|---|---|
| `PluginManager` | Orquestrador: descoberta, dependências, lifecycle, health, auto-disable. |
| `ISovyxPlugin` | ABC — contrato mínimo de um plugin (metadata, lifecycle, tools). |
| `ToolDefinition` | Schema de uma tool LLM-callable (name, description, JSON Schema params, handler). |
| `tool` | Decorator `@tool(...)` que registra método como ToolDefinition. |
| `PluginContext` | Injetor de acessos aprovados (brain, events, fs, http, scheduler, vault). |
| `BrainAccess` | Handle permissionado para Brain (read/write controlado por capabilities). |
| `EventBusAccess` | Handle permissionado para EventBus (subscribe/emit controlado). |
| `Permission` | StrEnum — 13 capabilities (brain/event/network/fs/scheduler/vault/proactive). |
| `PermissionEnforcer` | Valida capabilities declaradas vs. chamadas de runtime. |
| `PluginSecurityScanner` | AST scanner install-time — BLOCKED_IMPORTS / CALLS / ATTRIBUTES. |
| `ImportGuard` | `sys.meta_path` hook runtime — bloqueia `__import__` e `importlib`. |
| `SandboxedFsAccess` | FS escopado ao data_dir — symlink resolv, 50 MB/arq, 500 MB total. |
| `SandboxedHttpClient` | HTTP com rate limit, allowlist de domínios e timeout. |
| `PluginFileWatcher` | Watcher hot-reload para diretórios de plugins instalados. |
| `PluginStateTracker` | State machine do lifecycle (discovered → loaded → running → disabled). |
| `PluginState` | StrEnum — estados do lifecycle. |
| `LoadedPlugin` | Registro runtime do plugin (instância, manifest, tools, health). |
| `SecurityFinding` | Finding do AST scanner (kind, line, details). |
| `MockPluginContext` | Helper de teste para plugins (context injection sem engine). |

### Errors
| Exception | Quando é raised |
|---|---|
| `PluginError` | Base para erros do plugin system. |
| `PluginDisabledError` | Tentativa de invocar tool de plugin desabilitado. |
| `PermissionDeniedError` | Capability não declarada em uso runtime. |
| `PluginAutoDisabledError` | Plugin excedeu `_MAX_CONSECUTIVE_FAILURES` (5). |
| `InvalidTransitionError` | Transição ilegal na state machine do lifecycle. |
| `ManifestError` | `plugin.yaml` inválido ou schema violado. |

### Events
| Event | Payload / trigger |
|---|---|
| `PluginStateChanged` | Transição de estado — plugin_name, from_state, to_state, error_message. |
| `PluginLoaded` | Plugin carregado e pronto — plugin_name, plugin_version, tools_count. |
| `PluginUnloaded` | Plugin descarregado — plugin_name, reason. |
| `PluginToolExecuted` | Tool executada — plugin_name, tool_name, success, duration_ms, error_message. |
| `PluginAutoDisabled` | Auto-disable após falhas — plugin_name, consecutive_failures, last_error. |

### Configuration
| Config | Campo/Finalidade |
|---|---|
| `PluginManifest` | Schema pydantic do `plugin.yaml` (nome, versão, permissions, tools, events, deps). |
| `NetworkConfig` | Allowlist de domínios + rate limit para acesso HTTP. |
| `PluginDependency` | Dependência entre plugins (name + version constraint). |
| `EventsConfig` | Declaração de subscribe/emit (usado pelo PermissionEnforcer). |
| `EventDeclaration` | Schema de um evento declarado (nome + payload). |
| `ToolDeclaration` | Declaração de tool no manifest (name, description, parameters). |

## Referências

- `src/sovyx/plugins/manager.py` — PluginManager, lifecycle, health.
- `src/sovyx/plugins/security.py` — AST scanner + ImportGuard.
- `src/sovyx/plugins/permissions.py` — Permission enum, PermissionEnforcer.
- `src/sovyx/plugins/sandbox_fs.py` — filesystem scoped.
- `src/sovyx/plugins/sandbox_http.py` — HTTP scoped + rate limit.
- `src/sovyx/plugins/sdk.py` — ISovyxPlugin, @tool, ToolDefinition.
- `src/sovyx/plugins/context.py` — PluginContext (injeção de acessos).
- `src/sovyx/plugins/manifest.py` — plugin.yaml parser.
- `src/sovyx/plugins/lifecycle.py` — state machine.
- `src/sovyx/plugins/hot_reload.py` — reload/update.
- `src/sovyx/plugins/events.py` — eventos do plugin manager.
- `src/sovyx/plugins/testing.py` — helpers de teste.
- `src/sovyx/plugins/official/{calculator,financial_math,knowledge,weather,web_intelligence}.py`.
- IMPL-012-PLUGIN-SANDBOX — 7 camadas, 18 vetores de escape.
- SPE-008-PLUGIN-* — SDK, registry, governance.
- `docs/_meta/gap-inputs/analysis-B-services.md` §plugins.
- `CLAUDE.md` §Anti-Patterns — regras #2 (sys.modules), #11 (patch string path).
