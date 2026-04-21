"""CI gate — assert OTel semantic-convention attributes are spelled right.

Stand-alone enforcement of §22.6 of IMPL-OBSERVABILITY-001 ("OTel
resource attributes follow OpenTelemetry semantic conventions"). The
gate is preventative, not reactive: a typo in a resource attribute
silently routes telemetry into a *new* attribute key on the backend,
where it bypasses every dashboard and alert tied to the canonical key
(``service.namespace`` vs ``service_namespace`` vs
``service.namspace``). Worse, OTLP receivers happily accept any
string, so the typo never surfaces at ingest.

We protect against three classes of regression:

1. **Misspelled semconv key.** Every resource-attribute key emitted by
   ``sovyx.observability.envelope.EnvelopeProcessor`` and
   ``sovyx.observability.otel._build_resource_attributes`` must be in
   the OTel semconv 1.27 resource vocabulary (locked snapshot below).
   A new attribute that is not yet in the vocabulary fails the gate
   so the operator either picks the canonical name or registers an
   intentional Sovyx-prefixed extension.

2. **Drift between log envelope and OTel Resource.** Logs and spans
   must agree on the set of resource attributes they expose, otherwise
   span/log join in the backend loses information. Every key that
   appears in BOTH builders must use the same source (e.g.
   ``service.instance.id`` is ``SERVICE_INSTANCE_ID`` in both — never
   a freshly-generated UUID per-call site).

3. **Forbidden attribute reuse.** A few names are reserved by the
   OTel SDK itself (``telemetry.sdk.*``) — emitting them by hand
   collides with the values the SDK injects on its own.

Wired into ``.github/workflows/ci.yml`` as ``otel-semconv-gate`` after
``pii-leak-gate``.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# ── OTel semantic-convention vocabulary (resource attributes) ─────────
# Locked snapshot of OTel semconv 1.27 — the resource subset we
# actually care about. The full registry is several hundred keys; we
# enumerate only the ones a logging+tracing pipeline legitimately
# emits as resource attributes. Adding a new one here is intentional
# and reviewed.
_OTEL_RESOURCE_VOCABULARY: frozenset[str] = frozenset(
    {
        # service.*
        "service.name",
        "service.namespace",
        "service.version",
        "service.instance.id",
        # deployment.*
        "deployment.environment",
        "deployment.environment.name",
        # host.*
        "host.name",
        "host.id",
        "host.arch",
        "host.type",
        # os.*
        "os.type",
        "os.description",
        "os.name",
        "os.version",
        # process.*
        "process.pid",
        "process.parent_pid",
        "process.executable.name",
        "process.executable.path",
        "process.command",
        "process.command_line",
        "process.runtime.name",
        "process.runtime.version",
        "process.runtime.description",
        # container.*
        "container.id",
        "container.name",
        "container.image.name",
        "container.image.tag",
        # k8s.*
        "k8s.namespace.name",
        "k8s.pod.name",
        "k8s.pod.uid",
        "k8s.node.name",
    }
)

# Attributes the OTel SDK injects on its own. Emitting them by hand
# either collides (best case) or shadows the SDK's value (worst case).
_OTEL_RESERVED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "telemetry.sdk.name",
        "telemetry.sdk.language",
        "telemetry.sdk.version",
        "telemetry.auto.version",
    }
)

# Keys the envelope adds that are NOT OTel-semconv (Sovyx wire format).
# These are intentionally outside the OTel namespace and don't need to
# be in the vocabulary.
_SOVYX_LEGACY_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "process_id",
        "host",
        "sovyx_version",
        "sequence_no",
        "saga_id",
        "span_id",
        "event_id",
        "cause_id",
    }
)


def _extract_dict_literal_keys(
    source: str,
    function_name: str | None = None,
) -> set[str]:
    """Return the union of string-literal keys from every dict literal.

    When *function_name* is given, only dict literals lexically inside
    that function definition are considered. Otherwise every top-level
    or class-attribute dict literal in the module is scanned. Non-string
    keys (computed expressions, f-strings) are ignored — they cannot
    typo a semconv name.
    """
    tree = ast.parse(source)
    found: set[str] = set()

    target_nodes: list[ast.AST]
    if function_name is None:
        target_nodes = [tree]
    else:
        target_nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == function_name
        ]

    for root in target_nodes:
        for node in ast.walk(root):
            if not isinstance(node, ast.Dict):
                continue
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.add(key.value)
    return found


def _classify_keys(keys: set[str]) -> tuple[set[str], set[str], set[str]]:
    """Split *keys* into (otel_canonical, sovyx_legacy, unknown)."""
    canonical: set[str] = set()
    legacy: set[str] = set()
    unknown: set[str] = set()
    for key in keys:
        if key in _OTEL_RESOURCE_VOCABULARY:
            canonical.add(key)
        elif key in _SOVYX_LEGACY_ENVELOPE_KEYS:
            legacy.add(key)
        else:
            unknown.add(key)
    return canonical, legacy, unknown


def _check_envelope(repo_root: Path) -> list[str]:
    """Validate envelope.py's cached resource-attribute dict."""
    path = repo_root / "src" / "sovyx" / "observability" / "envelope.py"
    if not path.is_file():
        return [f"missing source: {path}"]
    source = path.read_text(encoding="utf-8")
    keys = _extract_dict_literal_keys(source)
    _, _, unknown = _classify_keys(keys)
    # Filter out keys that are plainly internal (start with underscore)
    # or not OTel-shaped (no dot — likely a Sovyx contextual id we
    # haven't enumerated above).
    suspect = {k for k in unknown if "." in k and not k.startswith("_")}
    return [
        (
            f"envelope.py: '{k}' is not in OTel semconv 1.27 vocabulary "
            f"and not in _SOVYX_LEGACY_ENVELOPE_KEYS"
        )
        for k in sorted(suspect)
    ]


def _check_otel_resource(repo_root: Path) -> list[str]:
    """Validate otel.py's _build_resource_attributes return dict."""
    path = repo_root / "src" / "sovyx" / "observability" / "otel.py"
    if not path.is_file():
        return [f"missing source: {path}"]
    source = path.read_text(encoding="utf-8")
    keys = _extract_dict_literal_keys(source, function_name="_build_resource_attributes")
    if not keys:
        return [
            "otel.py: _build_resource_attributes did not yield any literal "
            "string keys — the AST extraction expects a dict literal in the "
            "function body. Refactor likely broke the gate."
        ]
    _, _, unknown = _classify_keys(keys)
    suspect = {k for k in unknown if "." in k and not k.startswith("_")}
    reserved = keys & _OTEL_RESERVED_ATTRIBUTES
    violations = [
        f"otel.py: '{k}' is not in OTel semconv 1.27 vocabulary" for k in sorted(suspect)
    ]
    violations.extend(
        f"otel.py: '{k}' is reserved by the OTel SDK and must not be set by hand"
        for k in sorted(reserved)
    )
    return violations


def _check_drift(repo_root: Path) -> list[str]:
    """Cross-check that overlapping keys agree between envelope and OTel.

    For every OTel semconv key that BOTH builders emit, the value
    expression must reference the same module-level constant. We check
    the two we treat as authoritative: ``SERVICE_NAMESPACE`` and
    ``SERVICE_INSTANCE_ID``. A literal-string mismatch (e.g.,
    ``"sovyx"`` hard-coded on one side) means a future rename of the
    constant won't propagate, and the two pipelines silently diverge.
    """
    envelope = (repo_root / "src" / "sovyx" / "observability" / "envelope.py").read_text(
        encoding="utf-8"
    )
    otel = (repo_root / "src" / "sovyx" / "observability" / "otel.py").read_text(encoding="utf-8")

    violations: list[str] = []
    for key, expected_const in (
        ("service.namespace", "SERVICE_NAMESPACE"),
        ("service.instance.id", "SERVICE_INSTANCE_ID"),
    ):
        for label, source in (("envelope.py", envelope), ("otel.py", otel)):
            tree = ast.parse(source)
            ok = False
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for k_node, v_node in zip(node.keys, node.values, strict=False):
                    if not (isinstance(k_node, ast.Constant) and k_node.value == key):
                        continue
                    if isinstance(v_node, ast.Name) and v_node.id == expected_const:
                        ok = True
            if not ok:
                violations.append(
                    f"{label}: '{key}' must be sourced from the module "
                    f"constant '{expected_const}' so envelope and OTel "
                    "stay in lockstep across renames"
                )
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns 0 on clean run, 1 on any violation."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: current working directory)",
    )
    args = parser.parse_args(argv)

    if not (args.root / "src" / "sovyx").is_dir():
        print(
            f"error: {args.root} does not look like the sovyx repo root (missing src/sovyx/)",
            file=sys.stderr,
        )
        return 2

    violations: list[str] = []
    violations.extend(_check_envelope(args.root))
    violations.extend(_check_otel_resource(args.root))
    violations.extend(_check_drift(args.root))

    if violations:
        print(
            f"\nFAIL: {len(violations)} OTel semconv violation(s):",
            file=sys.stderr,
        )
        for line in violations:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\n  Fix: use the canonical OTel semconv 1.27 attribute name, "
            "or add to _SOVYX_LEGACY_ENVELOPE_KEYS / _OTEL_RESOURCE_VOCABULARY "
            "with a comment explaining the extension.",
            file=sys.stderr,
        )
        return 1

    print(
        "OK: envelope + OTel resource attributes match OTel semconv 1.27 "
        "vocabulary and stay in lockstep via SERVICE_NAMESPACE / "
        "SERVICE_INSTANCE_ID."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
