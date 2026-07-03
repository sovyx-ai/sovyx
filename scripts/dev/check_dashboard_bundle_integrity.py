#!/usr/bin/env python3
"""Quality Gate 11 — dashboard bundle integrity enforcement.

Mission C5 §Phase 1.A §T1.2 / §T1.3 — anti-pattern #43 (static-asset
distribution contracts MUST be enforced at three independent observation
points: build-time AST scan, install-time runtime probe, AND runtime
composite-banner surface).

This checker runs :func:`sovyx.dashboard.scan_bundle_integrity` against
a target static dir (default: ``src/sovyx/dashboard/static/`` of the
checked-out tree) and exits non-zero on any verdict other than
``FULLY_PRESENT``. The same script is invoked from ``publish.yml`` against
the unpacked wheel post-``uv build``, so the integrity contract is
enforced at BOTH the developer-machine push AND the wheel artifact level.

Exit codes:
    0 — bundle is fully present (every referenced asset on disk)
    1 — bundle is partial, index missing, static dir missing, or
        legacy index without assets

Usage:

    uv run python scripts/dev/check_dashboard_bundle_integrity.py
    uv run python scripts/dev/check_dashboard_bundle_integrity.py \
        --static-dir /tmp/wheel-check/sovyx/dashboard/static
    uv run python scripts/dev/check_dashboard_bundle_integrity.py --json

Invoked from ``scripts/verify_gates.sh`` as Gate 11 (LENIENT in Phase
1.A v0.47.0; now STRICT-when-applicable — verify_gates.sh enforces when
a local bundle build is present and SKIPs when absent; full STRICT runs
in ``publish.yml`` where the bundle is always built).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_STATIC_DIR = _REPO_ROOT / "src" / "sovyx" / "dashboard" / "static"

# Allow this script to be invoked from a wheel-extraction tmp dir where
# the repo's ``src/sovyx`` package isn't on sys.path. Inject it.
_REPO_SRC = _REPO_ROOT / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from sovyx.dashboard._integrity import (  # noqa: E402 — sys.path inject above
    BundleVerdict,
    scan_bundle_integrity,
)


def _report_to_dict(report: object) -> dict[str, object]:
    """Serialize :class:`BundleIntegrityReport` to a JSON-safe dict."""
    return {
        "verdict": str(report.verdict.value),  # type: ignore[union-attr]
        "static_dir": str(report.static_dir.as_posix()),  # type: ignore[union-attr]
        "index_html_path": str(report.index_html_path.as_posix()),  # type: ignore[union-attr]
        "referenced_count": len(report.referenced_assets),  # type: ignore[union-attr]
        "missing_count": len(report.missing_assets),  # type: ignore[union-attr]
        "orphan_count": len(report.orphan_assets),  # type: ignore[union-attr]
        "missing_assets": list(report.missing_assets),  # type: ignore[union-attr]
        "orphan_assets": list(report.orphan_assets),  # type: ignore[union-attr]
        "scan_duration_ms": round(float(report.scan_duration_ms), 3),  # type: ignore[union-attr]
    }


def _print_human(report: object) -> None:  # noqa: ANN001 — runtime type
    verdict = report.verdict  # type: ignore[union-attr]
    if verdict is BundleVerdict.FULLY_PRESENT:
        ref_count = len(report.referenced_assets)  # type: ignore[union-attr]
        duration = report.scan_duration_ms  # type: ignore[union-attr]
        print(
            f"Quality Gate 11 — dashboard bundle integrity: "
            f"FULLY_PRESENT ({ref_count} references, {duration:.2f}ms).",
        )
        return

    print(
        "Quality Gate 11 — dashboard bundle integrity: FAILED.",
        file=sys.stderr,
    )
    print(
        f"  verdict     = {verdict.value}",  # type: ignore[union-attr]
        file=sys.stderr,
    )
    print(
        f"  static_dir  = {report.static_dir.as_posix()}",  # type: ignore[union-attr]
        file=sys.stderr,
    )
    print(
        f"  index_html  = {report.index_html_path.as_posix()}",  # type: ignore[union-attr]
        file=sys.stderr,
    )
    missing = list(report.missing_assets)  # type: ignore[union-attr]
    if missing:
        print(
            f"  missing ({len(missing)}):",
            file=sys.stderr,
        )
        for ref in missing[:20]:
            print(f"    ✗ {ref}", file=sys.stderr)
        if len(missing) > 20:
            print(
                f"    … (+{len(missing) - 20} more)",
                file=sys.stderr,
            )
    print(
        "\nRemediation:",
        file=sys.stderr,
    )
    if verdict is BundleVerdict.STATIC_DIR_MISSING:
        print(
            "  - Static directory is absent. Run 'npm run build' inside "
            "the dashboard/ workspace, or 'pipx reinstall sovyx' if running "
            "from an installed wheel.",
            file=sys.stderr,
        )
    elif verdict is BundleVerdict.INDEX_HTML_MISSING:
        print(
            "  - index.html is missing. Run 'npm run build' inside the "
            "dashboard/ workspace, or 'pipx reinstall sovyx' to recover a "
            "complete wheel.",
            file=sys.stderr,
        )
    elif verdict is BundleVerdict.LEGACY_INDEX_HTML_NO_ASSETS:
        print(
            "  - index.html exists but the assets/ directory is missing "
            "or empty. This is typically a stale or interrupted build. "
            "Run 'npm run build' inside dashboard/ to rebuild.",
            file=sys.stderr,
        )
    elif verdict is BundleVerdict.PARTIAL:
        print(
            "  - Some referenced chunks are absent on disk. Run 'npm run "
            "build' to refresh the bundle, OR 'pipx reinstall sovyx' if "
            "the wheel itself was published with a partial bundle.",
            file=sys.stderr,
        )
    print(
        "\nAnti-pattern #43 enforcement (Mission C5): the dashboard SPA "
        "bundle MUST be byte-complete in every release artifact. Allowlist "
        "a deliberate test-fixture mismatch with an inline "
        "'# c5-allowlist: <rationale>' comment in the controlling index.html.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quality Gate 11 — dashboard bundle integrity (Mission C5).",
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=_DEFAULT_STATIC_DIR,
        help="Path to the static directory to scan (default: %(default)s).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON report on stdout instead of human-readable output.",
    )
    args = parser.parse_args(argv)

    report = scan_bundle_integrity(args.static_dir)

    if args.json:
        print(json.dumps(_report_to_dict(report), indent=2, sort_keys=True))
    else:
        _print_human(report)

    return 0 if report.verdict is BundleVerdict.FULLY_PRESENT else 1


if __name__ == "__main__":
    sys.exit(main())
