# Log Schema Catalog

JSON-Schema Draft 2020-12 definitions for every canonical log event the
Sovyx daemon emits. Ships with the package so:

* `scripts/check_log_schemas.py` (CI gate, P11.2) can load it via
  `importlib.resources.files("sovyx.observability.log_schema")`,
* `KNOWN_EVENTS` in `sovyx.observability.schema` (P11.3) seeds itself
  from the catalog,
* downstream consumers that pip-install Sovyx get the contract for
  free.

## Conventions

* **One file per event**, named `<event_name>.json` (e.g.
  `voice.vad.frame.json`). The filename IS the canonical event name.
* **Envelope fields are always required** —
  `timestamp, level, logger, event, schema_version, process_id, host,
  sovyx_version, sequence_no` — and `event` is pinned to the file's
  event name via `"const"`.
* **Saga fields** (`saga_id, cause_id, span_id`) are documented but
  optional; emit sites inside a `@trace_saga` decorator add them
  automatically.
* `additionalProperties: true` so a phase can ship a new field on an
  emit site before this schema catches up — the gate stays
  forward-compatible.

## Updating

Schemas are **generated**, not hand-edited. Edit the table in
`scripts/_gen_log_schemas.py` then re-run:

```bash
uv run python scripts/_gen_log_schemas.py
```

A diff in `*.json` without a matching diff in the generator means
someone bypassed the source of truth — reject in review.
