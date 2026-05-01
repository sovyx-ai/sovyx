"""``sovyx voice`` — operator commands for voice-data lifecycle (Phase 7 / T7.39).

Subcommands:

    sovyx voice forget --user-id=USER_ID
        Right-to-erasure (GDPR Art. 17 / LGPD Art. 18 VI). Purges
        every record in the ConsentLedger that matches ``user_id``
        and writes a tombstone DELETE record so the audit trail
        survives the deletion. The tombstone is the ONLY record
        remaining for the user; everything else is gone.

    sovyx voice history --user-id=USER_ID
        Right-of-access (GDPR Art. 15 / LGPD Art. 18 I). Lists
        every privacy-relevant voice action recorded for the user
        (wake / listen / transcribe / store / share / delete).
        Output is a chronological JSONL dump for direct piping to
        ``jq`` or external auditing tools.

The ConsentLedger lives at ``<data_dir>/voice/consent.jsonl`` by
default; the CLI resolves the path via :class:`EngineConfig` so it
reads/writes the same file the daemon writes to.

Phase 7 / T7.39 — operator-actionable surface for the consent
ledger that voice/_consent_ledger.py already implements. This CLI
is the deliberate complement to the dashboard endpoint shipping
in the same task; either path produces identical effects (the
ledger's atomic JSONL writes guarantee no race between the two).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

if TYPE_CHECKING:
    from sovyx.voice._consent_ledger import ConsentLedger

console = Console()

voice_app = typer.Typer(name="voice", help="Voice-data lifecycle commands (GDPR / LGPD)")


def _resolve_ledger_path() -> Path:
    """Resolve the active ConsentLedger path from EngineConfig.

    Mirrors the pattern used by ``sovyx audit`` + ``sovyx logs``:
    pulls ``data_dir`` from the live :class:`EngineConfig` so the CLI
    inspects the same file the daemon writes to. Fallback to
    ``~/.sovyx/voice/consent.jsonl`` when config loading fails
    (pre-init, corrupted YAML, etc.).
    """
    try:
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415

        config = EngineConfig()
        return config.data_dir / "voice" / "consent.jsonl"
    except Exception:  # noqa: BLE001 — fall back gracefully
        return Path.home() / ".sovyx" / "voice" / "consent.jsonl"


def _open_ledger(path: Path) -> ConsentLedger:
    """Construct a ConsentLedger at the resolved path.

    The ledger handles missing files itself (treats the absent file
    as an empty ledger), so the CLI doesn't need to pre-check
    ``path.exists()``. Callers can check the resulting
    ``len(history(...))`` to detect "user has no records".
    """
    from sovyx.voice._consent_ledger import ConsentLedger  # noqa: PLC0415

    return ConsentLedger(path=path)


@voice_app.command("forget")
def forget(
    user_id: str = typer.Option(
        ...,
        "--user-id",
        help=(
            "Stable opaque identifier for the user (caller hashes the real "
            "name before passing). Empty string is rejected — would match "
            "every empty-id record."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation prompt (scripted use).",
    ),
) -> None:
    """Purge every ConsentLedger record for ``user_id``.

    GDPR Article 17 (Right to Erasure) + LGPD Art. 18 VI. The ledger
    rewrites the active segment in-place dropping every line whose
    user_id matches; rotated segments are walked + rewritten too so
    the deletion is comprehensive. A single ``DELETE`` tombstone is
    appended so the audit trail survives the erasure (the tombstone
    is the only remaining trace of that user).

    Idempotent — running twice on the same user_id is safe; the
    second call finds no records to purge but still writes a fresh
    tombstone.
    """
    if not user_id.strip():
        console.print("[red]error:[/red] --user-id must be a non-empty string")
        raise typer.Exit(code=2)

    path = _resolve_ledger_path()

    if not yes:
        confirm = typer.confirm(
            f"This will permanently delete every voice-data record for "
            f"user_id={user_id!r} from {path}. Continue?",
            default=False,
        )
        if not confirm:
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(code=1)

    ledger = _open_ledger(path)
    purged = ledger.forget(user_id=user_id)
    console.print(
        f"[green]forget complete[/green]: purged [bold]{purged}[/bold] records "
        f"for user_id={user_id!r}; tombstone DELETE record written to {path}"
    )


@voice_app.command("history")
def history(
    user_id: str = typer.Option(
        ...,
        "--user-id",
        help="Stable opaque identifier for the user.",
    ),
) -> None:
    """List every ConsentLedger record for ``user_id`` as JSONL.

    GDPR Article 15 (Right of Access) + LGPD Art. 18 I. Output is
    one JSON object per line (newline-delimited JSON), chronological
    order, suitable for piping to ``jq`` or saving to file:

        sovyx voice history --user-id=u-12345 > history.jsonl
        sovyx voice history --user-id=u-12345 | jq '.action'

    The output writes to stdout via plain ``print`` (not Rich) so
    redirection / piping produces clean machine-readable JSON.
    """
    if not user_id.strip():
        console.print("[red]error:[/red] --user-id must be a non-empty string")
        raise typer.Exit(code=2)

    path = _resolve_ledger_path()
    ledger = _open_ledger(path)
    records = ledger.history(user_id=user_id)
    if not records:
        console.print(
            f"[yellow]no records[/yellow] for user_id={user_id!r} at {path}",
        )
        return
    for record in records:
        # Use print (not console) so stdout redirection produces
        # clean JSONL without Rich colour escapes.
        sys.stdout.write(record.to_jsonl_line() + "\n")
    sys.stdout.flush()
