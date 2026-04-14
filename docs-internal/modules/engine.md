# Módulo: engine

## Objetivo

O pacote `sovyx.engine` é o kernel do Sovereign Minds Engine: orquestra o ciclo
de vida do processo daemon, provê o container de injeção de dependências,
o event bus tipado que alimenta todos os subsistemas, health checks, graceful
degradation, e o canal RPC local usado pelo CLI e pelo dashboard. Nenhum outro
módulo importa daemons externos — todo wiring passa pelo bootstrap em camadas
definido aqui.

## Responsabilidades

- Carregar `EngineConfig` (pydantic-settings, prefixo `SOVYX_`, `__` para
  nesting) e resolver paths (`data_dir`, `log_file`).
- Manter o `ServiceRegistry` como única fonte de verdade para singletons e
  factories de serviços (DI custom, ~150 LOC).
- Publicar eventos imutáveis no `EventBus` (11 classes de evento cobrindo
  engine, cognitive, brain e bridge).
- Gerenciar o ciclo de vida do daemon: PidLock, start → run → shutdown em
  ordem reversa de inicialização.
- Expor health checks (10) e degradation states para CLI (`sovyx doctor`) e
  dashboard.
- Servir o protocolo RPC local (Unix socket / named pipe) que CLI e dashboard
  consomem para inspeção/controle em runtime.

## Arquitetura

`engine/bootstrap.py` define a ordem canônica (SPE-001 §init_order):

- Layer 0: `EngineConfig` + `setup_logging` + channel.env.
- Layer 1: `EventBus` → `DatabaseManager` (pools + migrations) → `MindManager`.
- Layer 2 (por mente): Brain (repositórios + embedding + spreading + retrieval)
  → PersonalityEngine → ContextAssembler → LLMRouter → CognitiveLoop
  → BridgeManager → Channels.

Tudo é registrado no `ServiceRegistry`. Shutdown percorre `_init_order` em
sentido reverso, chamando `.shutdown()` (sync ou async) se existir.

Eventos são `@dataclasses.dataclass(frozen=True)`, herdando de `Event` com
`event_id` (uuid4), `timestamp` (UTC) e `correlation_id`. O `EventBus`
propaga `correlation_id` para o contexto do logger (structlog) antes de
despachar handlers. Falha em um handler é logada, mas os demais continuam —
error isolation explícita.

O padrão de DI é deliberadamente simples: nenhum reflection, nenhum scope
hierárquico. `register_singleton(interface, factory)` cria lazy; 
`register_instance(interface, obj)` grava ready. Chaves usam
`f"{module}.{qualname}"` para sobreviver a reimports sob pytest-xdist.

## Código real

```python
# src/sovyx/engine/registry.py:29-38
class ServiceRegistry:
    """Lightweight DI container (~100 LOC).

    Two registration modes:
    - register_singleton(interface, factory): lazy instantiation on first resolve()
    - register_instance(interface, instance): ready instance, resolve() returns it

    Shutdown: reverse init order. Calls .shutdown() if it exists.
    """
```

```python
# src/sovyx/engine/events.py:40-56 — base event + category
@dataclasses.dataclass(frozen=True)
class Event:
    event_id: str = dataclasses.field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = dataclasses.field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str = ""

    @property
    def category(self) -> EventCategory:
        raise NotImplementedError
```

```python
# src/sovyx/engine/events.py:311-322 — emit com isolation
async def emit(self, event: Event) -> None:
    handlers = self._handlers.get(type(event), [])
    if event.correlation_id:
        set_correlation_id(event.correlation_id)
    for handler in handlers:
        try:
            await handler(event)
        except Exception:
            logger.error("event_handler_error", exc_info=True)
```

```python
# src/sovyx/engine/bootstrap.py:52-68 — entrypoint de bootstrap
async def bootstrap(
    engine_config: EngineConfig,
    mind_configs: Sequence[MindConfig],
) -> ServiceRegistry:
    """Initialize all services in dependency order.
    Layer 0: config+logging; Layer 1: EventBus, DB, MindManager;
    Layer 2: per-mind services.
    """
```

## Specs-fonte

- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-SPE-001-ENGINE-CORE.md`
  — DI container (~200 linhas planejadas), lifecycle, init_order.
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/adrs/SOVYX-BKD-ADR-007-EVENT-ARCHITECTURE.md`
  — decisão do in-process async event bus, trade-offs vs broker externo.
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/adrs/SOVYX-BKD-ADR-008-LOCAL-FIRST.md`
  — restrição "zero external deps default", motivação do daemon standalone.

## Status de implementação

### Implementado conforme spec

- `ServiceRegistry` (`registry.py`, 149 LOC): `register_singleton`,
  `register_instance`, `resolve`, `is_registered`, `shutdown_all` em ordem
  reversa.
- `EventBus` (`events.py`, 349 LOC) com 13 event classes: `EngineStarted`,
  `EngineStopping`, `ServiceHealthChanged`, `PerceptionReceived`,
  `ThinkCompleted`, `ResponseSent`, `ConceptCreated`, `EpisodeEncoded`,
  `ConceptContradicted`, `ConceptForgotten`, `ConsolidationCompleted`,
  `ChannelConnected`, `ChannelDisconnected`.
- `LifecycleManager` (`lifecycle.py`) com `PidLock` para evitar double-start.
- `MindManager` (`bootstrap.py:21-49`): v0.1 single-mind, interface pronta
  para multi-mind.
- Bootstrap em camadas (`bootstrap.py:52-572`): ordem e cleanup em reverse
  sobre `_closables` on partial failure.
- `EngineConfig` (`config.py`, 265 LOC): resolve `data_dir/logs/sovyx.log`
  via model_validator. Prefixo env `SOVYX_`, delimitador `__`.
- `Errors` (`errors.py`, 292 LOC): hierarquia com `SovyxError` base,
  `ServiceNotRegisteredError`, `CognitiveError`, `PerceptionError`,
  `CostLimitExceededError`, `ProviderUnavailableError`, etc.

### Implementado sem doc (feature extra)

- `HealthChecker` (`health.py`, 263 LOC): 10 checks — SQLite writable,
  sqlite-vec loaded, embedding model, event bus, brain, LLM provider,
  Telegram, disk > 100MB, RSS < 85%, event loop lag < 100ms. Consumido por
  `sovyx doctor`. Coexiste com `observability.health.HealthRegistry` que é
  usado pelo endpoint `/api/health` do dashboard.
- `DegradationManager` (`degradation.py`, 170 LOC): estados HEALTHY /
  DEGRADED / CRITICAL por componente, matriz de fallbacks (sqlite-vec →
  FTS5-only; todos providers down → template response; disk < 100MB →
  read-only warning; OOM → consolidation prune).
- `DaemonRPCServer` (`rpc_server.py`, 119 LOC) + protocol
  (`rpc_protocol.py`, 75 LOC): RPC local (socket UNIX em POSIX, named pipe
  em Windows) usado pelo CLI (`sovyx stop`, `sovyx status`) e
  opcionalmente pelo dashboard.

### Gaps

- 0 gaps significativos. O que a spec descreve de forma declarativa está
  implementado; o extra (health/degradation/RPC) excede a spec.

## Divergências [DIVERGENCE]

- `ServiceRegistry` tem ~150 LOC; SPE-001 §3.2 previa ~200 LOC. A redução
  vem de não suportar scope hierárquico — decisão consciente pela
  simplicidade (KISS), sem perda de feature necessária.

## Dependências

- **Bibliotecas externas**: `pydantic`, `pydantic-settings`, `structlog`.
- **Módulos Sovyx internos usados**:
  `sovyx.observability.logging` (get_logger, set_correlation_id,
  setup_logging), `sovyx.persistence.manager.DatabaseManager`,
  `sovyx.brain.*`, `sovyx.cognitive.*`, `sovyx.llm.*`, `sovyx.mind.*`,
  `sovyx.bridge.*`, `sovyx.dashboard.status` (counters).

## Testes

- `tests/unit/engine/` — ServiceRegistry, EventBus, Config, Errors, Lifecycle.
- `tests/integration/test_bootstrap.py` — ordem de inicialização e cleanup
  on partial failure.
- `tests/unit/engine/test_events.py` — error isolation, correlation_id
  propagation.
- Padrão: `pytest.raises(Exception) as exc_info` + assert em
  `type(exc_info.value).__name__` para evitar xdist class-identity
  (Anti-pattern #8).

## Public API reference

### Public API

| Classe | Descrição |
|---|---|
| `MindManager` | Gerencia Minds dentro do engine; v0.1 single-mind, interface pronta para multi-mind. |
| `ServiceRegistry` | Container DI lightweight (~100 LOC) com singleton/instance e shutdown em ordem reversa. |
| `EventBus` | Event bus async in-process que propaga eventos imutáveis e isola falhas por handler. |
| `PidLock` | Gerenciamento de PID file com detecção de stale locks (evita double-start). |
| `LifecycleManager` | Orquestra startup e shutdown graceful do daemon com sinais e ordem reversa. |
| `HealthChecker` | Roda 10 health checks consumidos por `sovyx doctor` (SQLite, vec, embedding, LLM, etc.). |
| `DegradationManager` | Centraliza fallback chains e monitora estados HEALTHY/DEGRADED/CRITICAL por componente. |
| `DaemonRPCServer` | Servidor JSON-RPC 2.0 via Unix socket / named pipe para CLI e dashboard. |

### Errors

| Exception | Quando é raised |
|---|---|
| `SovyxError` | Base de todas as exceções do Sovyx. |
| `EngineError` | Erro na infra do kernel. |
| `BootstrapError` | Falha ao bootstrapar o engine (DI, service init). |
| `ShutdownError` | Erro durante shutdown graceful. |
| `ServiceNotRegisteredError` | Serviço solicitado não encontrado no registry. |
| `LifecycleError` | Erro em lifecycle do daemon (PID lock, sinais). |
| `HealthCheckError` | Um ou mais health checks falharam. |
| `ConfigError` | Erro ao carregar ou validar configuração. |
| `ConfigNotFoundError` | Arquivo de configuração não encontrado. |
| `ConfigValidationError` | Valores de config falharam na validação. |

### Events

| Event | Payload / trigger |
|---|---|
| `EngineStarted` | Engine concluiu startup. |
| `EngineStopping` | Engine iniciou shutdown. |
| `ServiceHealthChanged` | Status de saúde de um serviço mudou. |
| `PerceptionReceived` | Nova perception entrou no cognitive loop. |
| `ThinkCompleted` | Fase think concluiu uma chamada LLM (modelo, tokens, custo, latência). |
| `ResponseSent` | Response entregue por um canal. |
| `ConceptCreated` | Novo concept armazenado na memória. |
| `EpisodeEncoded` | Episode codificado na memória. |
| `ConceptContradicted` | Conteúdo contradiz concept existente. |
| `ConceptForgotten` | Concept removido do brain. |
| `ConsolidationCompleted` | Ciclo de consolidação concluído (merged/pruned/duration). |
| `ChannelConnected` | Canal de comunicação conectou. |
| `ChannelDisconnected` | Canal de comunicação desconectou. |

### Configuration

| Config | Campo/Finalidade |
|---|---|
| `LoggingConfig` | Configuração de structured logging (level, console_format, file handler). |
| `DatabaseConfig` | Configuração SQLite (pool size, WAL, extensões). |
| `TelemetryConfig` | Opt-in/out de telemetria. |
| `RelayConfig` | Configuração do relay server. |
| `APIConfig` | Configuração do endpoint REST. |
| `HardwareConfig` | Detecção de tier de hardware para voice. |
| `LLMProviderConfig` | Config de um provider individual (api_key, base_url, models). |
| `LLMDefaultsConfig` | Defaults engine-level de LLM; `MindConfig.llm` sobrescreve por mente. |
| `SocketConfig` | Path do socket Unix / named pipe para RPC. |
| `EngineConfig` | Configuração global do daemon (pydantic-settings, prefixo `SOVYX_`). |

## Referências

- Code: `src/sovyx/engine/bootstrap.py`, `src/sovyx/engine/registry.py`,
  `src/sovyx/engine/events.py`, `src/sovyx/engine/health.py`,
  `src/sovyx/engine/degradation.py`, `src/sovyx/engine/rpc_server.py`,
  `src/sovyx/engine/config.py`, `src/sovyx/engine/lifecycle.py`,
  `src/sovyx/engine/errors.py`, `src/sovyx/engine/types.py`,
  `src/sovyx/engine/protocols.py`.
- Specs: `SOVYX-BKD-SPE-001-ENGINE-CORE.md`, `SOVYX-BKD-ADR-007-EVENT-ARCHITECTURE.md`,
  `SOVYX-BKD-ADR-008-LOCAL-FIRST.md`.
- Gap analysis: `docs/_meta/gap-inputs/analysis-A-core.md` §engine.
