# Resource hygiene — operator guide

Mission H4 surfaces per-cohort resource cardinality on the
`self.health.snapshot` structured-log stream. This page documents the
operator-facing tools that consume that data.

## What's observable

Each `self.health.snapshot` record (emitted every
`observability.sampling.perf_hotpath_interval_seconds`, default 60 s)
now carries 28 canonical fields across these cohorts:

- **process** — `process.rss_bytes`, `process.vms_bytes`,
  `process.cpu_percent`, `process.num_threads`,
  `process.num_handles_or_fds`, `process.open_files_count`,
  `process.connections_count`.
- **asyncio** — `asyncio.task_count`, `asyncio.running_count`,
  `asyncio.pending_count`.
- **to_thread** — `to_thread.pool_size`, `to_thread.queue_depth`,
  `to_thread.max_workers`, `to_thread.dispatch_count_total`,
  `to_thread.dispatch_count_per_label`.
- **lock_dict** — `lock_dict.total_cardinality`,
  `lock_dict.per_owner`, `lock_dict.instance_count`.
- **onnx** — `onnx.session_count`, `onnx.session_labels`.
- **gc** — `gc.collections_by_gen`, `gc.objects_count`.
- **tracemalloc** — `tracemalloc.is_tracing`,
  `tracemalloc.current_kb`, `tracemalloc.peak_kb` (only meaningful
  when `observability.features.tracemalloc=True`).
- **exception_cohort** — `exception_cohort.retained_bytes_estimate`,
  `exception_cohort.distinct_group_id_count`,
  `exception_cohort.last_observation_monotonic`.

### Legacy alias during the LENIENT window

`system.rss_bytes` is preserved as a legacy alias of
`process.rss_bytes` during v0.49.15..v0.53.x. External dashboards /
log forwarders keyed on the legacy name keep working. The v0.54.0
STRICT flip drops the alias.

## API: `GET /api/engine/resources`

Returns a structured JSON envelope of the live snapshot:

```json
{
  "observed_at_unix": 1716143280.42,
  "cohorts": {
    "to_thread.pool_size": 4,
    "to_thread.dispatch_count_total": 142,
    "to_thread.dispatch_count_per_label": {
      "voice.vad.silero": 80,
      "brain.embedding": 62
    },
    "lock_dict.total_cardinality": 156,
    "lock_dict.per_owner": {
      "bridge.manager.conv_locks": 12,
      "voice.health.watchdog.lifecycle_locks": 144
    },
    "onnx.session_count": 4,
    "onnx.session_labels": [
      "brain.embedding",
      "voice.vad.silero",
      "voice.wake_word",
      "voice.tts.piper"
    ],
    "gc.collections_by_gen": [215, 7, 2],
    "tracemalloc.is_tracing": false
  },
  "canonical_field_count": 28,
  "legacy_alias_count": 1
}
```

The envelope is forward-additive (`extra="allow"`): Phase 1.D's
`ResourceCohortGovernor` introduces `cohort_governor.budget_state` +
`cohort_governor.circuit_breaker_engaged` + heap-snapshot manifest
fields without breaking older clients.

## CLI: `sovyx doctor resources`

```bash
# Render the live snapshot as a table grouped by cohort section.
sovyx doctor resources

# Filter to one section.
sovyx doctor resources --cohort to_thread

# JSON output for pipe consumers.
sovyx doctor resources --json | jq '."onnx.session_count"'
```

## Anomaly detection

`anomaly.memory_growth_spike` fires when `process.rss_bytes` grows by
> `observability.tuning.anomaly_memory_growth_pct` (default 10%)
within a `observability.tuning.anomaly_memory_growth_window_s` window
(default 300 s).

Pre-Mission H4 the detector was silently dead because the consumer
read a different field name than the producer emitted. v0.49.15 fixed
the drift; the detector now fires correctly on real RSS growth.

## Build-time guarantee (Quality Gate 15)

`scripts/dev/check_resource_hygiene_discipline.py` AST-scans every
`*.py` under `src/sovyx/` and enforces:

1. **Producer parity** — `logger.info("self.health.snapshot",
   **kwargs)` literal keys MUST appear in
   `_HEALTH_SNAPSHOT_FIELDS`.
2. **Consumer parity** — `event_dict.get(<literal>)` reads MUST
   reference an SSoT-registered field key or a registered
   `legacy_alias`.
3. **Construction-site pairing** — every `ort.InferenceSession(...)`
   under `src/sovyx/{voice,brain}/` MUST pair with
   `register_onnx_session(...)`; every `LRULockDict(...)` MUST pair
   with `register_lock_dict(...)`.
4. **Future migration tracking** — bare `asyncio.to_thread(...)`
   call sites are reported as informational (Phase 3 STRICT
   v0.54.0 requires either migration to `dispatch_to_thread` or
   an `# h4-allowlist:` annotation).

LENIENT in `verify_gates.sh` during v0.49.14..v0.53.x; STRICT in
`publish.yml` post-build verify; STRICT in `verify_gates.sh` at
v0.54.0.

## See also

- **Phase 1.D (v0.49.17 — pending):** `ResourceCohortGovernor` adds
  5 cohort budget verdicts (rss_growth_spike, thread_count_spike,
  lock_dict_cardinality_saturated, onnx_session_unexpected_count,
  exception_cohort_retention_high), heap-snapshot file capture with
  rotation, and a circuit-breaker for runaway cohorts.
- **Mission spec:**
  `docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md`
  (gitignored internal planning doc).
