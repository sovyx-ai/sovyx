"""CI gate — validate Sovyx log entries against the canonical schema catalog.

Two modes, picked by ``--log-file`` and ``--synth``:

1. **File mode** (``--log-file PATH`` or ``data/logs/sovyx.log`` exists):
   read up to ``--max-entries`` JSONL records from disk and validate each
   against ``sovyx.observability.log_schema.<event>.json``. Fails on:

   * any JSON parse error,
   * an entry whose ``event`` is in the catalog but fails schema validation,
   * a structurally malformed envelope (missing one of the nine
     required envelope fields).

   Unknown events (``event`` absent from the catalog) emit a warning and
   keep going — the catalog has ``additionalProperties: true`` so a phase
   can ship a new field on an emit site before this schema catches up.

2. **Synth mode** (default fallback when no log file is present, or
   forced via ``--synth``): for each cataloged schema, builds a minimal
   valid entry from the schema's required-fields list and validates it.
   This proves:

   * every JSON file in the catalog is parseable and a valid Draft 2020-12
     schema,
   * the ``event`` const inside each schema matches its filename
     (rejects rename drift),
   * the schema's required envelope fields are exactly the nine in
     :data:`sovyx.observability.schema.ENVELOPE_FIELDS` (rejects envelope
     drift),
   * the schema validates a payload built strictly from its own
     ``properties`` table (rejects type/required mismatches between the
     ``properties`` and ``required`` lists).

Wired into ``.github/workflows/ci.yml`` as the ``schema-gate`` job.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from sovyx.observability import log_schema as catalog
from sovyx.observability.schema import ENVELOPE_FIELDS, SCHEMA_VERSION

DEFAULT_LOG_FILE = Path("data/logs/sovyx.log")
DEFAULT_MAX_ENTRIES = 5000

EXIT_OK = 0
EXIT_FAIL = 1


def _envelope_payload() -> dict[str, Any]:
    """Minimal envelope dict that satisfies every cataloged schema.

    Synth mode reuses this for every event; file mode never calls it.
    """
    return {
        "timestamp": "2026-04-20T00:00:00.000Z",
        "level": "INFO",
        "logger": "sovyx.synth",
        "schema_version": SCHEMA_VERSION,
        "process_id": 1,
        "host": "synth",
        "sovyx_version": "0.0.0-synth",
        "sequence_no": 0,
    }


def _example_value(prop_schema: dict[str, Any]) -> Any:  # noqa: ANN401
    """Build a placeholder value matching ``prop_schema``'s declared type.

    Only handles the type tokens the generator emits today. Adding a new
    type to ``scripts/_gen_log_schemas.py`` requires extending this map.
    """
    if "const" in prop_schema:
        return prop_schema["const"]
    if "enum" in prop_schema:
        return prop_schema["enum"][0]

    declared = prop_schema.get("type")
    if isinstance(declared, list):
        # Nullable union — pick the non-null variant for concreteness.
        for candidate in declared:
            if candidate != "null":
                declared = candidate
                break
        else:
            return None

    if declared == "string":
        if prop_schema.get("format") == "date-time":
            return "2026-04-20T00:00:00.000Z"
        return "x"
    if declared == "integer":
        return int(prop_schema.get("minimum", 0))
    if declared == "number":
        return 0.0
    if declared == "boolean":
        return False
    if declared == "array":
        items = prop_schema.get("items", {})
        return [_example_value(items)]
    if declared == "object":
        return {}
    if declared == "null":
        return None
    raise ValueError(f"unsupported schema type token: {declared!r}")


def _build_synth_entry(event_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Construct a minimal entry that should validate against ``schema``."""
    entry: dict[str, Any] = _envelope_payload()
    entry["event"] = event_name

    properties = schema.get("properties", {})
    for field in schema.get("required", []):
        if field in entry:
            continue
        prop_schema = properties.get(field)
        if prop_schema is None:
            raise ValueError(
                f"{event_name}: required field {field!r} has no entry in 'properties'"
            )
        entry[field] = _example_value(prop_schema)
    return entry


def _check_schema_self_consistency(event_name: str, schema: dict[str, Any]) -> list[str]:
    """Return a list of structural problems with ``schema`` (empty = clean)."""
    problems: list[str] = []

    # Schema must itself be valid Draft 2020-12.
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.exceptions.SchemaError as exc:
        problems.append(f"invalid Draft 2020-12 schema: {exc.message}")
        return problems

    event_const = schema.get("properties", {}).get("event", {}).get("const")
    if event_const != event_name:
        problems.append(
            f"event const mismatch: filename={event_name!r}, "
            f"properties.event.const={event_const!r}"
        )

    required = set(schema.get("required", []))
    missing_envelope = ENVELOPE_FIELDS - required
    if missing_envelope:
        problems.append(f"required[] missing envelope fields: {sorted(missing_envelope)}")

    if schema.get("additionalProperties") is not True:
        problems.append(
            "additionalProperties must be true for forward-compat (found: {!r})".format(
                schema.get("additionalProperties")
            )
        )

    return problems


def run_synth_mode(verbose: bool = False) -> int:
    """Validate every cataloged schema against its own minimal example."""
    names = catalog.event_names()
    if not names:
        print("ERROR: log_schema catalog is empty — nothing to validate", file=sys.stderr)
        return EXIT_FAIL

    print(f"synth mode: validating {len(names)} cataloged event(s)")
    failures = 0
    for name in names:
        schema = catalog.load(name)

        problems = _check_schema_self_consistency(name, schema)
        if problems:
            failures += 1
            for problem in problems:
                print(f"FAIL [{name}] {problem}", file=sys.stderr)
            continue

        try:
            entry = _build_synth_entry(name, schema)
        except ValueError as exc:
            failures += 1
            print(f"FAIL [{name}] could not build synth entry: {exc}", file=sys.stderr)
            continue

        try:
            jsonschema.validate(entry, schema)
        except jsonschema.exceptions.ValidationError as exc:
            failures += 1
            print(f"FAIL [{name}] synth entry rejected: {exc.message}", file=sys.stderr)
            continue

        if verbose:
            print(f"  ok  {name}")

    if failures:
        print(f"\n{failures} schema(s) failed self-consistency", file=sys.stderr)
        return EXIT_FAIL
    print(f"all {len(names)} schemas self-consistent")
    return EXIT_OK


def run_file_mode(log_file: Path, max_entries: int, verbose: bool = False) -> int:
    """Validate JSONL entries from ``log_file`` against the catalog."""
    if not log_file.exists():
        print(f"ERROR: log file not found: {log_file}", file=sys.stderr)
        return EXIT_FAIL

    known_events = set(catalog.event_names())
    print(f"file mode: validating up to {max_entries} entries from {log_file}")
    print(f"  catalog covers {len(known_events)} known event(s)")

    validated = 0
    parse_failures = 0
    schema_failures = 0
    envelope_failures = 0
    unknown_events: dict[str, int] = {}

    with log_file.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            if validated + parse_failures + schema_failures + envelope_failures >= max_entries:
                break
            line = raw.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_failures += 1
                print(
                    f"FAIL [{log_file.name}:{lineno}] json parse: {exc.msg}",
                    file=sys.stderr,
                )
                continue

            missing = ENVELOPE_FIELDS - entry.keys()
            if missing:
                envelope_failures += 1
                print(
                    f"FAIL [{log_file.name}:{lineno}] envelope missing: {sorted(missing)}",
                    file=sys.stderr,
                )
                continue

            event_name = entry.get("event", "")
            if event_name not in known_events:
                unknown_events[event_name] = unknown_events.get(event_name, 0) + 1
                continue

            schema = catalog.load(event_name)
            try:
                jsonschema.validate(entry, schema)
            except jsonschema.exceptions.ValidationError as exc:
                schema_failures += 1
                print(
                    f"FAIL [{log_file.name}:{lineno}] event={event_name!r}: {exc.message}",
                    file=sys.stderr,
                )
                continue

            validated += 1
            if verbose:
                print(f"  ok  {log_file.name}:{lineno} event={event_name}")

    print()
    print(f"validated:         {validated}")
    print(f"parse failures:    {parse_failures}")
    print(f"envelope failures: {envelope_failures}")
    print(f"schema failures:   {schema_failures}")
    if unknown_events:
        print(
            f"unknown events:    {sum(unknown_events.values())} entries across "
            f"{len(unknown_events)} name(s)"
        )
        for name, count in sorted(unknown_events.items(), key=lambda kv: -kv[1]):
            print(f"  WARN unknown event {name!r}: {count} entries")

    failures = parse_failures + envelope_failures + schema_failures
    return EXIT_FAIL if failures else EXIT_OK


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Sovyx log entries against the canonical schema catalog.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help=(
            "Path to a JSONL log file to validate. "
            f"Defaults to {DEFAULT_LOG_FILE} when present; "
            "falls back to synth mode otherwise."
        ),
    )
    parser.add_argument(
        "--max-entries",
        type=int,
        default=DEFAULT_MAX_ENTRIES,
        help=f"Cap on entries validated in file mode (default: {DEFAULT_MAX_ENTRIES}).",
    )
    parser.add_argument(
        "--synth",
        action="store_true",
        help=(
            "Skip the log file and validate every catalog schema against its own minimal example."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print one line per validated entry.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.synth:
        return run_synth_mode(verbose=args.verbose)

    log_file = args.log_file if args.log_file is not None else DEFAULT_LOG_FILE
    if not log_file.exists():
        if args.log_file is not None:
            # User explicitly named a file; missing is a hard error.
            print(f"ERROR: --log-file {log_file} does not exist", file=sys.stderr)
            return EXIT_FAIL
        print(f"no log file at {log_file} — falling back to synth mode")
        return run_synth_mode(verbose=args.verbose)

    return run_file_mode(log_file, args.max_entries, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
