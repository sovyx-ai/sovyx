"""``sovyx audit`` — operator commands for the tamper-evident audit log.

Subcommands:

    sovyx audit verify-chain
        Walk every audit log file (current + rotated backups) and
        replay each hash chain via :func:`sovyx.observability.tamper.verify_chain`.
        Reports per-file ``OK`` / ``BROKEN at index N`` so an operator
        can pinpoint corruption to a single rotation generation.

    sovyx audit verify-chain --since=2026-04-01
        Same as above but only inspects files whose mtime falls on or
        after the given date. Useful when you only need to attest the
        last 30 days of audit history (compliance window).

    sovyx audit verify-chain --path=PATH
        Verify a specific file regardless of its name pattern. Useful
        when audit files have been moved into a long-term archive
        directory by an external retention job.

Implements §27.4 of IMPL-OBSERVABILITY-001 ("audit-of-auditor"). Boot-
time and rotation-time verification are wired automatically; this CLI
covers the third trigger ("on demand") so an operator can run an
attestation pass without restarting the daemon or waiting for the next
rotation.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from collections.abc import Iterable

console = Console()

audit_app = typer.Typer(name="audit", help="Tamper-evident audit log commands")


def _resolve_default_audit_dir() -> Path:
    """Resolve the audit directory from EngineConfig.

    Mirrors :func:`sovyx.cli.commands.logs._resolve_default_log_file`:
    pulls ``data_dir`` from the live :class:`EngineConfig` so the CLI
    inspects the same files the daemon writes to. Falls back to the
    canonical ``~/.sovyx/audit`` when config loading fails (pre-init,
    corrupted YAML).
    """
    try:
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415

        config = EngineConfig()
        return config.data_dir / "audit"
    except Exception:  # noqa: BLE001 — fall back gracefully on any config load failure.
        return Path.home() / ".sovyx" / "audit"


def _parse_since(value: str | None) -> datetime | None:
    """Parse ``--since=YYYY-MM-DD`` into a UTC ``datetime`` or ``None``.

    Naive inputs are treated as UTC midnight. ``None`` disables the
    filter (verify every file we find).
    """
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"Invalid --since value '{value}'. Use ISO format: 2026-04-01"
        raise typer.BadParameter(msg) from exc


def _collect_chain_files(
    audit_dir: Path,
    *,
    explicit_path: Path | None,
    since: datetime | None,
) -> list[Path]:
    """Return the list of audit chain files to verify, sorted by mtime.

    When ``explicit_path`` is given, return only that file (even if it
    sits outside ``audit_dir``). Otherwise glob ``audit.jsonl*`` so
    both the current file and rotated backups (``audit.jsonl.1``, …)
    are picked up. ``since`` applies a wall-clock mtime filter so an
    operator running a 30-day attestation doesn't have to walk archived
    backups outside the compliance window.
    """
    if explicit_path is not None:
        return [explicit_path]
    if not audit_dir.is_dir():
        return []
    candidates = sorted(audit_dir.glob("audit.jsonl*"), key=lambda p: p.stat().st_mtime)
    if since is None:
        return candidates
    cutoff = since.timestamp()
    return [p for p in candidates if p.stat().st_mtime >= cutoff]


def _verify_one(path: Path) -> tuple[bool, int, int | None]:
    """Verify *path*'s hash chain.

    Returns ``(intact, entries, broken_at)``. ``broken_at`` is the
    zero-based line index of the first broken record, or ``None`` if
    the chain is intact. A missing or unreadable file degrades to
    ``(False, 0, None)`` so the operator sees the failure in the
    table without the command itself crashing.
    """
    from sovyx.observability.tamper import verify_chain  # noqa: PLC0415

    try:
        with path.open("r", encoding="utf-8") as fh:
            entries = sum(1 for line in fh if line.strip())
    except OSError:
        return (False, 0, None)
    try:
        intact, idx = verify_chain(path)
    except (OSError, ValueError):
        return (False, entries, None)
    return (intact, entries, None if intact else idx)


def _render_results(results: Iterable[tuple[Path, bool, int, int | None]]) -> bool:
    """Print the results table and return overall pass/fail.

    Returns ``True`` only when every inspected file is intact. The
    caller maps that to the process exit code so CI / cron consumers
    can act on a single boolean instead of parsing the table.
    """
    table = Table(title="Audit chain verification", show_lines=False)
    table.add_column("File", style="cyan", no_wrap=False)
    table.add_column("Entries", justify="right")
    table.add_column("Status")
    table.add_column("Broken at", justify="right")

    all_ok = True
    for path, intact, entries, broken_at in results:
        status = "[green]OK[/green]" if intact else "[red]BROKEN[/red]"
        broken_repr = "—" if broken_at is None else str(broken_at)
        if not intact:
            all_ok = False
        table.add_row(str(path), str(entries), status, broken_repr)

    console.print(table)
    return all_ok


@audit_app.command("verify-chain")
def verify_chain_command(
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only verify files modified on or after this ISO date (e.g. 2026-04-01)",
    ),
    path: Path | None = typer.Option(  # noqa: B008
        None,
        "--path",
        help="Verify a specific file instead of the default audit/ directory",
    ),
    audit_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--audit-dir",
        help="Override the audit directory (defaults to <data_dir>/audit)",
    ),
) -> None:
    """Verify the hash chain of every audit log file (§27.4).

    Boot and rotation checks happen automatically when ``tamper_chain``
    is enabled; this command is the operator-driven third trigger. Use
    it before submitting an audit log to a compliance reviewer or
    after restoring from backup.

    Exit code is ``0`` when every file's chain is intact, ``1``
    otherwise. The render also writes the result to stdout as a Rich
    table; for machine-readable output, run ``sovyx logs`` against
    ``audit.chain.verified`` events instead.
    """
    parsed_since = _parse_since(since)
    base_dir = audit_dir if audit_dir is not None else _resolve_default_audit_dir()
    files = _collect_chain_files(base_dir, explicit_path=path, since=parsed_since)

    if not files:
        console.print(f"[yellow]No audit chain files found under {base_dir}[/yellow]")
        raise typer.Exit(code=0)

    results = [(p, *_verify_one(p)) for p in files]
    all_ok = _render_results(results)
    raise typer.Exit(code=0 if all_ok else 1)


__all__ = ["audit_app"]
