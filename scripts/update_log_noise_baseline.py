"""Refresh ``benchmarks/log_noise_baseline.json`` after an intentional bump.

Stand-alone counterpart to :mod:`scripts.check_log_noise`. The gate
asserts that the cataloged-event distribution emitted by
``tests/regression/synthetic_workload.py`` has not drifted; this script
*re-captures* that distribution after a phase explicitly added new
events.

Use is gated behind ``--justify`` (free-text, mandatory) so the
re-baseline is always paired with a one-line rationale that lands in
the commit body and the PR description. A baseline bump that lacks
justification is indistinguishable from "I silenced a regression I
didn't understand," so we refuse to write the file without it.

Workflow:

  1. Phase ships a new emit site (e.g. Phase 7 LLM telemetry adds
     ``llm.request.start`` × 1, ``llm.request.end`` × 1 per saga).
  2. Author runs ``check_log_noise.py`` locally; gate fails with the
     exact deltas.
  3. Author runs::

         uv run python scripts/update_log_noise_baseline.py \\
             --justify "Phase 7 LLM telemetry adds 2 events/saga"

  4. Commit body cites the justify text. CI re-runs the gate against
     the new baseline → green.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # noqa: S404 — controlled subprocess of our own workload.
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

_DEFAULT_BASELINE: Path = Path("benchmarks/log_noise_baseline.json")
_DEFAULT_SCHEMA_VERSION: str = "1.0.0"


def _run_workload(*, repo_root: Path) -> list[dict[str, Any]]:
    """Spawn the workload module, return parsed JSONL entries."""
    with tempfile.TemporaryDirectory(prefix="sovyx-noise-baseline-") as tmp:
        tmp_path = Path(tmp)
        out_path = tmp_path / "workload.log"
        proc = subprocess.run(  # noqa: S603 — module path is repo-controlled.
            [
                sys.executable,
                "-m",
                "tests.regression.synthetic_workload",
                "--out",
                str(out_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        if proc.returncode != 0:
            msg = (
                f"workload exited with status {proc.returncode}\n"
                f"--- stdout ---\n{proc.stdout}\n"
                f"--- stderr ---\n{proc.stderr}"
            )
            raise RuntimeError(msg)
        if not out_path.is_file():
            msg = f"workload did not produce {out_path}"
            raise FileNotFoundError(msg)
        entries: list[dict[str, Any]] = []
        for line in out_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
        return entries


def _count_by_event_logger(entries: list[dict[str, Any]]) -> dict[str, int]:
    """Return ``{"event|logger": count}`` ordered for stable diffs."""
    counter: Counter[tuple[str, str]] = Counter()
    for entry in entries:
        event = str(entry.get("event", ""))
        logger = str(entry.get("logger", ""))
        if not event:
            continue
        counter[(event, logger)] += 1
    return {f"{event}|{logger}": count for (event, logger), count in sorted(counter.items())}


def main(argv: list[str] | None = None) -> int:
    """CLI entry — write a fresh baseline and print a one-line summary."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--justify",
        type=str,
        required=True,
        help="One-line rationale for the bump (recorded in the JSON metadata)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: current working directory)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_BASELINE,
        help=f"Baseline file to write (default: {_DEFAULT_BASELINE})",
    )
    args = parser.parse_args(argv)

    if not (args.root / "tests" / "regression" / "synthetic_workload.py").is_file():
        print(
            f"error: {args.root} does not look like the sovyx repo "
            "(missing tests/regression/synthetic_workload.py)",
            file=sys.stderr,
        )
        return 2

    if not args.justify.strip():
        print("error: --justify must not be empty", file=sys.stderr)
        return 2

    entries = _run_workload(repo_root=args.root)
    by_event_logger = _count_by_event_logger(entries)
    total = sum(by_event_logger.values())

    payload: dict[str, Any] = {
        "schema_version": _DEFAULT_SCHEMA_VERSION,
        "justification": args.justify.strip(),
        "total": total,
        "by_event_logger": by_event_logger,
    }
    out_path = args.out if args.out.is_absolute() else args.root / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(
        f"OK: wrote baseline ({total} entries, "
        f"{len(by_event_logger)} unique event|logger) to {out_path}"
    )
    print(f"  Justification: {args.justify.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
