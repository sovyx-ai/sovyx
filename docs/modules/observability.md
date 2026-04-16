# Module: observability

## What it does

`sovyx.observability` provides the full instrumentation stack: structured logging (structlog + JSON), distributed tracing (OpenTelemetry), metrics (30+ OTel instruments), health checks (10 probes), SLO monitoring with burn-rate alerting (Google SRE pattern), and a Prometheus `/metrics` exporter.

## Key classes

| Name | Responsibility |
|---|---|
| `get_logger(__name__)` | Structured logger factory — the ONLY way to log in Sovyx. |
| `SovyxTracer` | OTel tracing wrapper — spans for cognitive phases, LLM calls, brain search. |
| `MetricsRegistry` | 30+ OTel counters, histograms, gauges (LLM, cognitive, brain, voice). |
| `HealthRegistry` | Runs 10 health checks, reports GREEN / YELLOW / RED per check. |
| `SLOMonitor` | 5 core SLOs with multi-window burn-rate alerting. |
| `AlertManager` | Threshold rules, event bus integration. |
| `PrometheusExporter` | `/metrics` endpoint on the dashboard. |
| `SecretMasker` | Redacts sensitive fields (api_key, token, password) from logs. |

## Logging

All logging flows through `sovyx.observability.logging.get_logger`:

```python
from sovyx.observability.logging import get_logger
logger = get_logger(__name__)

logger.info("concept_created", concept_id=str(cid), name=name)
```

Never `print()` or `logging.getLogger()` directly.

- **Console**: colorized text or JSON (configurable via `log.console_format`).
- **File**: always JSON at `<data_dir>/logs/sovyx.log` (RotatingFileHandler).
- **Context binding**: `mind_id`, `conversation_id`, `request_id` propagated automatically.
- **Secret masking**: fields matching `api_key`, `token`, `password`, `secret` are redacted.

## Health checks

10 probes run on demand (`GET /api/status`) or periodically:

| Check | GREEN | YELLOW | RED |
|---|---|---|---|
| Disk space | > 500 MB free | 100–500 MB | < 100 MB |
| RAM | RSS < 80% available | 80–90% | > 90% |
| Database | writable + readers ok | slow (>100 ms) | unreachable |
| Brain indexed | concepts in FTS5 | stale index | no index |
| LLM reachable | ≥1 provider responds | all slow | none respond |
| Models loaded | embeddings ready | loading | failed |
| Channels | ≥1 channel connected | degraded | none |
| Consolidation | ran within interval | overdue | never ran |
| Cost budget | < 80% daily budget | 80–95% | > 95% exhausted |
| Event loop lag | < 100 ms | 100–500 ms | > 500 ms |

## SLOs

5 core Service-Level Objectives with Google SRE multi-window burn-rate alerting:

| SLO | Target | Window |
|---|---|---|
| Brain search latency | p95 < 100 ms | 5 min / 1 h |
| Response time | p95 < 3 s | 5 min / 1 h |
| Uptime | > 99.5% | 1 h / 24 h |
| Error rate | < 1% | 5 min / 1 h |
| Cost per message | < $0.01 | 1 h / 24 h |

## Metrics

Prometheus-compatible metrics exported at `/metrics`:

| Metric | Type | Labels |
|---|---|---|
| `sovyx_llm_calls_total` | Counter | `provider`, `model` |
| `sovyx_llm_tokens_total` | Counter | `direction`, `provider` |
| `sovyx_llm_cost_usd_total` | Counter | `provider` |
| `sovyx_llm_response_latency` | Histogram | `provider` |
| `sovyx_cognitive_loop_latency` | Histogram | — |
| `sovyx_messages_processed_total` | Counter | `mind_id` |
| `sovyx_errors_total` | Counter | `error_type`, `module` |

## Configuration

```yaml
# system.yaml
log:
  level: INFO                    # DEBUG | INFO | WARNING | ERROR
  console_format: text           # text | json (file is always JSON)
  log_file: null                 # resolved to <data_dir>/logs/sovyx.log
```

## Roadmap

- BatchSpanProcessor (currently SimpleSpanProcessor for dev comfort).
- OpenTelemetry `gen_ai.*` semantic conventions for LLM spans.

## See also

- Source: `src/sovyx/observability/`.
- Tests: `tests/unit/observability/`.
- Related: [`engine`](./engine.md) (HealthChecker wired at bootstrap), [`dashboard`](./dashboard.md) (`/api/status` and `/metrics` endpoints), [`llm`](./llm.md) (LLM metrics labelled by provider).
