# Módulo: observability

## Objetivo

Fornecer a instrumentação operacional completa do Sovyx: logging estruturado, tracing distribuído, métricas, health checks, SLOs com burn rate e alertas. É a fundação para debugging em produção, SRE e detecção proativa de degradação.

## Responsabilidades

- **Logging estruturado** — `structlog` com JSON em arquivo e console colorido, context binding (mind_id, conversation_id, request_id) via `contextvars`, masking de campos sensíveis.
- **Tracing** — spans OpenTelemetry para o loop cognitivo (perceive → attend → think → act → reflect), LLM, brain search e context assembly.
- **Métricas** — 30+ instrumentos (counters, histograms, gauges) via OTel, exportáveis para Prometheus, OTLP ou in-memory.
- **Health checks** — 10 verificações cobrindo DB, disk, RAM, LLM provider, ports, modelos de voz, etc.
- **SLO monitoring** — 5 objetivos (brain search, response, uptime, error rate, cost/msg) com burn rate multi-window (Google SRE).
- **Alerting** — `AlertManager` com regras threshold e integração com event bus.
- **Prometheus exporter** — endpoint `/metrics` via dashboard.

## Arquitetura

```
observability/
  ├── logging.py     → setup_logging(), get_logger(), bind_request_context()
  ├── tracing.py     → SovyxTracer (wraps OTel Tracer)
  ├── metrics.py     → MetricsRegistry (MeterProvider + 30+ instrumentos)
  ├── health.py      → HealthRegistry + 10 checks (CheckStatus: green/yellow/red)
  ├── slo.py         → SLOMonitor (sliding window + burn rate multi-window)
  ├── alerts.py      → AlertManager (threshold rules + event bus)
  └── prometheus.py  → PrometheusExporter (/metrics endpoint)
```

Todos os módulos são **lazy / no-op safe**: se `setup_*()` não for chamado, `get_*()` retorna stubs que não fazem nada (útil em testes e bibliotecas).

## Código real (exemplos curtos)

**`src/sovyx/observability/logging.py`** — context binding assincrônico:

```python
def bind_request_context(
    *,
    mind_id: str = "",
    conversation_id: str = "",
    request_id: str | None = None,
    **extra: Any,
) -> None:
    if request_id is None:
        request_id = uuid.uuid4().hex[:12]
    bindings: dict[str, Any] = {"request_id": request_id}
    if mind_id:
        bindings["mind_id"] = mind_id
    if conversation_id:
        bindings["conversation_id"] = conversation_id
    structlog.contextvars.bind_contextvars(**bindings, **extra)
```

**`src/sovyx/observability/health.py`** — contrato de check:

```python
class CheckStatus(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"

@dataclasses.dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)
```

**`src/sovyx/observability/slo.py`** — objetivos SLO core (SPE-026 §6):

```python
# 1. Brain Search Latency: p95 < 100ms
# 2. Response Time: p95 < 3s
# 3. Uptime/Availability: > 99.5%
# 4. Error Rate: < 1%
# 5. Cost per Message: < $0.01

class AlertSeverity(StrEnum):
    NONE = "none"
    TICKET = "ticket"  # slow burn → ticket
    PAGE = "page"      # fast burn → acordar alguém
```

**`src/sovyx/observability/metrics.py`** — uso típico:

```python
from sovyx.observability.metrics import get_metrics

m = get_metrics()
m.messages_received.add(1, {"channel": "telegram"})

with m.measure_latency(m.llm_response_latency):
    response = await provider.generate(...)
```

## Specs-fonte

- **IMPL-015-OBSERVABILITY** — `BatchSpanProcessor`, gen_ai semantic conventions, SLO burn rate, logging JSON.
- **SPE-026-OBSERVABILITY-METRICS** — catálogo de 30+ métricas, naming Prometheus, 5 SLOs.

## Status de implementação

| Item | Status |
|---|---|
| `setup_logging()` com structlog (JSON + console) | Aligned |
| Context binding (mind_id, conversation_id, request_id) | Aligned |
| Masking de campos sensíveis | Aligned |
| OTel tracing com spans do loop cognitivo | Aligned |
| MetricsRegistry (30+ instrumentos) | Aligned |
| HealthRegistry (10 checks) | Aligned |
| SLO burn rate multi-window | Aligned |
| AlertManager (threshold + event bus) | Aligned |
| Prometheus exporter (`/metrics`) | Aligned |
| gen_ai semantic conventions OTel padrão | Partial — pode usar atributos custom em vez do namespace oficial `gen_ai.*` |

## Divergências

**gen_ai semantic conventions** — IMPL-015 adota as semantic conventions de OpenTelemetry para LLMs (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, etc.). O código atual em `tracing.py` adiciona atributos Sovyx-específicos (`provider`, `model`, `mind_id`) que podem não coincidir 1:1 com o padrão OTel. Risco baixo — dashboards custom funcionam; interoperabilidade com Grafana Cloud/OTel Collector pode exigir mapping.

## Anti-padrões conhecidos (CLAUDE.md)

- **Nunca usar `print()`** — sempre `from sovyx.observability.logging import get_logger`.
- **Nunca usar `logging.getLogger()` direto** — perde contexto e formatação.
- **`observability/__init__.py` usa `__getattr__` lazy** — nunca adicionar eager imports (ver anti-pattern #1 em CLAUDE.md).
- **`httpx` é suprimido para WARNING em `setup_logging()`** — se aparecer linha crua HTTP no console, `setup_logging()` não foi chamado (anti-pattern #6).
- **`LoggingConfig.console_format` (não `format`)** — campo renomeado em v0.5.24; YAML legado é auto-migrado; file handler SEMPRE escreve JSON (anti-pattern #3).
- **`log_file` resolvido por `EngineConfig.model_validator`** — nunca hardcode; `LoggingConfig.log_file` default é `None` e vira `data_dir/logs/sovyx.log` (anti-pattern #4).

## Dependências

- `structlog>=24` — logging estruturado.
- `opentelemetry-sdk`, `opentelemetry-api` — tracing e metrics.
- `opentelemetry-exporter-prometheus` — exporter.
- `psutil` — health checks (RAM, disk).

Consumidores: todos os módulos Sovyx usam `get_logger(__name__)`. O dashboard expõe `/api/health` e `/metrics`.

## Testes

- `tests/unit/observability/` — cobrem logging (masking, context), health checks (GREEN/YELLOW/RED), SLO windows, alert rules.
- Fixture de cleanup obrigatória para `RotatingFileHandler` (ver CLAUDE.md — `_clean_handlers`).
- Testes de SLO usam windows pequenos (ex: 10s) para evitar lentidão.

## Referências

- `src/sovyx/observability/logging.py` — structlog setup, context binding.
- `src/sovyx/observability/tracing.py` — SovyxTracer.
- `src/sovyx/observability/metrics.py` — MetricsRegistry.
- `src/sovyx/observability/health.py` — 10 health checks.
- `src/sovyx/observability/slo.py` — SLOMonitor, burn rate.
- `src/sovyx/observability/alerts.py` — AlertManager.
- `src/sovyx/observability/prometheus.py` — Prometheus exporter.
- IMPL-015-OBSERVABILITY — implementação detalhada.
- SPE-026-OBSERVABILITY-METRICS — catálogo de métricas.
- `docs/_meta/gap-inputs/analysis-B-services.md` §observability.
- `CLAUDE.md` §Anti-Patterns — regras #1, #3, #4, #6.
