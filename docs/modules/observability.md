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

10 probes run on demand (`GET /api/health`) or periodically:

| Check | GREEN | YELLOW | RED |
|---|---|---|---|
| Disk space | ≥ 1 GB free | 500 MB–1 GB | < 500 MB |
| RAM | ≥ 512 MB available | 256–512 MB | < 256 MB |
| CPU | < 80% | 80–95% | ≥ 95% |
| Database | writable + readers ok | slow (>100 ms) | unreachable |
| Brain indexed | concepts in FTS5 | stale index | no index |
| LLM reachable | ≥1 provider responds | all slow | none respond |
| Models loaded | embeddings ready | loading | failed |
| Channels | ≥1 channel connected | degraded | none |
| Consolidation | ran within interval | overdue | never ran |
| Cost budget | < 80% daily budget | 80–95% | > 95% exhausted |

## SLOs

5 core Service-Level Objectives with Google SRE multi-window burn-rate alerting:

| SLO | Target |
|---|---|
| Brain search latency | p95 < 100 ms |
| Response time | p95 < 3 s |
| Uptime | > 99.5% |
| Error rate | < 1% |
| Cost per message | < $0.01 |

All 5 SLOs share the same standard multi-window burn-rate alert rules
(Google SRE Workbook): fast burn 1 h / 5 m (PAGE), medium burn
6 h / 30 m (PAGE), slow burn 3 d / 6 h (TICKET).

## Metrics

Prometheus-compatible metrics exported at `/metrics`:

| Metric | Type | Labels |
|---|---|---|
| `sovyx_llm_calls_total` | Counter | `provider`, `model` |
| `sovyx_llm_tokens_total` | Counter | `direction`, `provider` |
| `sovyx_llm_cost_usd_total` | Counter | `provider` |
| `sovyx_llm_latency_milliseconds` | Histogram | `provider` |
| `sovyx_cognitive_latency_milliseconds` | Histogram | — |
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

## Resource hygiene (Mission H4)

The observability subsystem includes a per-cohort resource registry +
budget governor that closes the v0.43.1 forensic-audit §H4 gap (silent
+1.1 GB RSS / +105 thread spike with no operator-visible attribution).

### `self.health.snapshot` field taxonomy

Every snapshot record carries 37 canonical `FieldSpec` fields
organized into 8 cohort sections (SSoT:
`_resource_registry.py::_HEALTH_SNAPSHOT_FIELDS`; 9 of the 37
dual-emit a legacy alias during the LENIENT window):

- **process** (12 fields) — `process.rss_bytes`, `process.vms_bytes`,
  `process.cpu_percent`, `process.num_threads`,
  `process.num_handles_or_fds`, `process.open_files_count`,
  `process.open_files_status`, `process.connections_count`,
  `process.connections_status`, `process.memory_percent`,
  `process.cpu_times_user_s`, `process.cpu_times_system_s`.
- **asyncio** (5 fields) — `asyncio.task_count`,
  `asyncio.not_done_count` (legacy alias `asyncio.running_count`),
  `asyncio.awaiting_count` (legacy alias `asyncio.pending_count`),
  `asyncio.all_task_names` (legacy alias
  `asyncio.current_running_task_name`),
  `asyncio.default_executor_state` (dict).
- **to_thread** (5 fields) — `to_thread.pool_size_at_last_dispatch`,
  `to_thread.max_workers_at_last_dispatch`,
  `to_thread.queue_depth_at_last_dispatch` (legacy aliases drop the
  `_at_last_dispatch` suffix; the retired `to_thread.active_workers`
  shim stays dual-emitted until v0.55.0),
  `to_thread.dispatch_count_total`,
  `to_thread.dispatch_count_per_label`.
- **lock_dict** — `lock_dict.total_cardinality`,
  `lock_dict.per_owner`, `lock_dict.instance_count`.
- **onnx** — `onnx.session_count`, `onnx.session_labels`.
- **gc** — `gc.collections_by_gen`, `gc.objects_count`.
- **tracemalloc** — `tracemalloc.is_tracing`,
  `tracemalloc.current_kb`, `tracemalloc.peak_kb`.
- **exception_cohort** (5 fields) —
  `exception_cohort.cumulative_retained_bytes_since_start` (legacy
  alias `exception_cohort.retained_bytes_estimate`),
  `exception_cohort.cumulative_distinct_group_id_count` (legacy alias
  `exception_cohort.distinct_group_id_count`),
  `exception_cohort.window_retained_bytes`,
  `exception_cohort.window_distinct_group_id_count`,
  `exception_cohort.last_observation_monotonic`.

The legacy `system.rss_bytes` alias is dual-emitted alongside
`process.rss_bytes` during the v0.49.15..v0.53.x LENIENT window for
backward compatibility (drops at v0.54.0 STRICT).

### `ResourceCohortGovernor` budget verdicts

Every `self.health.snapshot` tick the governor evaluates 5 cohort
budgets (rss_growth, thread_count, lock_dict_cardinality, onnx_session,
exception_cohort). A BUDGET_EXCEEDED verdict emits a structured WARN
`engine.resources.cohort_budget_exceeded` + records into the C4
`EngineDegradedStore` under `axis="engine_resources"` so the existing
`<DegradedBanner>` renders the cohort automatically.

### OTel counters

Two counters export to the OTLP exporter when enabled:

- `sovyx.voice.health.resource_snapshot_emission` (labels: `final`).
- `sovyx.voice.health.cohort_budget_exceeded` (labels: `cohort`,
  `severity`).

### Operator surfaces

- `GET /api/engine/resources` — JSON envelope of the live snapshot.
- `sovyx doctor resources` — cohort-grouped table render. Flags:
  `--json` / `--cohort <name>` / `--explain <field>`.

### Build-time guarantee (Quality Gate 15)

`scripts/dev/check_resource_hygiene_discipline.py` AST-scans for
producer ↔ consumer field-name parity + construction-site pairing
for ONNX sessions + LRULockDict instances. LENIENT during
v0.49.14..v0.53.x; STRICT in `publish.yml`; STRICT in
`verify_gates.sh` at v0.54.0.

## See also

- Source: `src/sovyx/observability/`.
- Tests: `tests/unit/observability/`.
- Related: [`engine`](./engine.md) (HealthChecker wired at bootstrap), [`dashboard`](./dashboard.md) (`/api/status` and `/metrics` endpoints), [`llm`](./llm.md) (LLM metrics labelled by provider).
- Resource hygiene playbook: [`../operations/resource-hygiene.md`](../operations/resource-hygiene.md).
