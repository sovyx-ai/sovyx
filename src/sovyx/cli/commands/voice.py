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


# ── Phase 8 / T8.13 — wake-word training ────────────────────────────


def _resolve_training_root() -> Path:
    """Resolve the wake-word-training root directory.

    Mirror of :func:`_resolve_ledger_path`: pulls ``data_dir`` from
    the live :class:`EngineConfig` so the CLI uses the same
    location the daemon writes to.
    """
    try:
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415

        config = EngineConfig()
        return config.data_dir / "wake_word_training"
    except Exception:  # noqa: BLE001 — fall back gracefully
        return Path.home() / ".sovyx" / "wake_word_training"


def _slugify_for_filesystem(text: str) -> str:
    """ASCII-fold + alnum-only normalisation for job-id derivation.

    Mirrors the synthesizer's filename sanitisation so a job's id
    matches its job-dir name regardless of the operator's wake-word
    diacritics.
    """
    import unicodedata  # noqa: PLC0415

    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    return "".join(c if (c.isascii() and c.isalnum()) else "_" for c in folded)[:48]


def _attempt_hot_reload(mind_id: str, output_path: Path) -> None:
    """Try to hot-reload the trained model into the running daemon.

    Phase 8 / T8.15 — calls the ``wake_word.register_mind`` RPC
    (commit `96f8abe`) so the operator does not have to restart the
    daemon. Best-effort: every failure mode falls through to the
    operator-restart path with a clear remediation hint, NEVER
    aborts the CLI exit code (training succeeded — the model is on
    disk; hot-reload is a convenience, not a correctness gate).

    Failure modes (each renders a yellow hint, returns cleanly):
      * Daemon not running → next-restart pickup.
      * Daemon RPC error (voice disabled, single-mind mode, etc.) →
        surface the daemon-side message; restart still works.
      * Unexpected exception (network race, malformed response) →
        surface the exception text; restart still works.

    Args:
        mind_id: ``--mind-id`` from the operator's CLI invocation.
        output_path: The trained ``.onnx`` path (from the orchestrator
            final state, never empty when this is called).
    """
    import asyncio  # noqa: PLC0415

    from sovyx.cli.rpc_client import DaemonClient  # noqa: PLC0415
    from sovyx.engine.errors import ChannelConnectionError  # noqa: PLC0415

    client = DaemonClient()
    if not client.is_daemon_running():
        console.print(
            "  [yellow]Daemon not running[/yellow] — restart the daemon "
            "to activate the new model "
            f"(loaded from [dim]{output_path}[/dim] on next boot).",
        )
        return

    try:
        result = asyncio.run(
            client.call(
                "wake_word.register_mind",
                {"mind_id": mind_id, "model_path": str(output_path)},
            ),
        )
    except ChannelConnectionError as exc:
        # Surface the daemon-side message verbatim — it carries the
        # remediation hint (single-mind mode, voice disabled, etc.).
        console.print(
            f"  [yellow]Hot-reload via daemon failed:[/yellow] {exc}\n"
            "  Restart the daemon to pick up the new model.",
        )
        return
    except Exception as exc:  # noqa: BLE001 — operator-readable boundary
        console.print(
            f"  [yellow]Hot-reload error:[/yellow] {exc}\n"
            "  Restart the daemon to pick up the new model.",
        )
        return

    if isinstance(result, dict) and result.get("hot_reload_succeeded"):
        console.print(
            f"  [green]✓ Hot-reloaded[/green] into mind "
            f"[cyan]{mind_id!r}[/cyan] — wake-word detection is "
            "live with the new model (no restart needed).",
        )
    else:
        console.print(
            f"  [yellow]Daemon returned unexpected response:[/yellow] "
            f"{result!r}\n"
            "  Restart the daemon to be safe.",
        )


@voice_app.command("train-wake-word")
def train_wake_word(
    wake_word: str = typer.Argument(
        ...,
        help="Wake word to train (e.g. 'Lúcia', 'Jhonatan').",
    ),
    mind_id: str = typer.Option(
        "",
        "--mind-id",
        help=(
            "Owning mind. Empty = global / unattached training. "
            "When set, the trained ONNX is hot-reloaded into the "
            "daemon's WakeWordRouter for that mind on success."
        ),
    ),
    language: str = typer.Option(
        "en-US",
        "--language",
        help="BCP-47 tag (e.g. pt-BR, es-ES, fr-FR). Drives Kokoro G2P + backend phoneme tables.",
    ),
    target_samples: int = typer.Option(
        200,
        "--target-samples",
        help=(
            "Positive samples to synthesise. 200 is the conservative "
            "minimum for reasonable accuracy per OpenWakeWord docs; "
            "operators with fewer compute hours can lower, but "
            "accuracy degrades sub-100."
        ),
        min=10,
        max=2000,
    ),
    negatives_dir: str = typer.Option(
        "",
        "--negatives-dir",
        help=(
            "Directory containing non-wake-word WAV files. "
            "REQUIRED — backend training needs at least one negative "
            "sample to learn the discrimination boundary. Default "
            "empty = error with operator hint."
        ),
    ),
    output: str = typer.Option(
        "",
        "--output",
        help=(
            "Where to write the trained .onnx. Default: "
            "<data_dir>/wake_word_models/pretrained/<wake_word>.onnx"
        ),
    ),
    voices: str = typer.Option(
        "",
        "--voices",
        help=(
            "Comma-separated Kokoro voice IDs for synthesis (e.g. "
            "'af_heart,am_adam'). Empty = synthesizer's default 8-voice "
            "catalogue."
        ),
    ),
    variants: str = typer.Option(
        "",
        "--variants",
        help=("Comma-separated phrases to synthesise. Empty = [wake_word, 'hey '+wake_word]."),
    ),
) -> None:
    """Train a custom wake-word ONNX model.

    Runs the full pipeline:

    1. **Synthesise** ``target_samples`` positive samples via Kokoro
       TTS at varied voices + speeds.
    2. **Train** an ONNX model via the registered trainer backend
       (operators install ``sovyx[wake-training]`` extras + register
       the backend at boot — see ``sovyx.voice.wake_word_training``
       package docs).
    3. **Hot-reload** the trained model into the daemon's
       ``WakeWordRouter`` on success (when ``--mind-id`` is set
       AND the daemon is running).

    Cancellation: press Ctrl+C OR (from a separate shell)
    ``touch <data_dir>/wake_word_training/<job_id>/.cancel``.
    The orchestrator polls before each synthesis sample + at the
    backend's checkpoint boundaries.

    Phase 8 / T8.13 + T8.14 + T8.15.
    """
    if not wake_word.strip():
        console.print("[red]error:[/red] wake_word must be non-empty")
        raise typer.Exit(code=2)
    if not negatives_dir.strip():
        console.print(
            "[red]error:[/red] --negatives-dir is required. Provide a "
            "directory with non-wake-word .wav files (recordings of "
            "yourself talking, common-voice samples, ambient audio).",
        )
        raise typer.Exit(code=2)

    # Resolve trainer backend FIRST so we fail fast with operator
    # guidance when the extras aren't installed — saves the operator
    # from waiting for synthesis only to hit the missing-backend
    # error after 5 minutes.
    try:
        from sovyx.voice.wake_word_training import resolve_default_backend  # noqa: PLC0415

        backend = resolve_default_backend()
    except Exception as exc:  # noqa: BLE001 — operator-readable hint
        console.print(f"[red]Trainer backend unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from None

    console.print(f"[cyan]Trainer backend:[/cyan] [bold]{backend.name}[/bold]")

    # Build job directory under the canonical training root.
    training_root = _resolve_training_root()
    job_id = _slugify_for_filesystem(wake_word)
    # Reject all-underscore (or empty) job-ids — those mean every
    # character was non-ASCII / non-alphanumeric, leaving the
    # operator with an opaque "__" job dir that's hard to manage
    # AND likely indicates the wake word will fail downstream
    # synthesis (Kokoro G2P needs at least some Latin-script content
    # for its default voice catalogue).
    if not any(c.isascii() and c.isalnum() for c in job_id):
        console.print(
            "[red]error:[/red] wake_word produced no ASCII alphanumeric "
            "characters after fold (e.g. Chinese-only / Cyrillic-only "
            "input). Use ASCII characters or romanise the name "
            "(e.g. 'Ni hao' instead of '你好').",
        )
        raise typer.Exit(code=2)
    job_dir = training_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Resolve output path.
    if output.strip():
        output_path = Path(output)
    else:
        # Default to the pretrained-pool layout so the operator's
        # daemon picks it up via PretrainedModelRegistry on next
        # boot (or hot-reload via on_complete callback when the
        # daemon is running).
        try:
            from sovyx.engine.config import EngineConfig  # noqa: PLC0415

            data_dir = EngineConfig().data_dir
        except Exception:  # noqa: BLE001
            data_dir = Path.home() / ".sovyx"
        output_path = data_dir / "wake_word_models" / "pretrained" / f"{job_id}.onnx"

    # Build TrainingRequest.
    voice_tuple: tuple[str, ...] = tuple(v.strip() for v in voices.split(",") if v.strip())
    if variants.strip():
        variant_tuple: tuple[str, ...] = tuple(v.strip() for v in variants.split(",") if v.strip())
    else:
        variant_tuple = (wake_word, f"hey {wake_word}")

    from sovyx.voice.wake_word_training import (  # noqa: PLC0415
        TrainingRequest,
    )

    request = TrainingRequest(
        wake_word=wake_word,
        mind_id=mind_id,
        language=language,
        target_positive_samples=target_samples,
        synthesizer_voices=voice_tuple,
        synthesizer_variants=variant_tuple,
        negative_samples_dir=Path(negatives_dir),
        output_path=output_path,
    )

    console.print(
        f"[bold]Training job[/bold] for [cyan]{wake_word!r}[/cyan] → [dim]{output_path}[/dim]",
    )
    console.print(f"  job_dir:       [dim]{job_dir}[/dim]")
    console.print(f"  language:      [cyan]{language}[/cyan]")
    console.print(f"  target_samples: [cyan]{target_samples}[/cyan]")
    console.print(f"  negatives_dir: [dim]{negatives_dir}[/dim]")
    if mind_id:
        console.print(
            f"  hot-reload to mind: [cyan]{mind_id}[/cyan] (if daemon running)",
        )

    # Build orchestrator.
    from sovyx.voice.tts_kokoro import KokoroTTS  # noqa: PLC0415
    from sovyx.voice.wake_word_training import (  # noqa: PLC0415
        KokoroSampleSynthesizer,
        ProgressTracker,
        TrainingOrchestrator,
        TrainingStatus,
    )

    # Kokoro requires a model directory; resolve from the same
    # data_dir EngineConfig uses.
    try:
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415

        kokoro_model_dir = EngineConfig().data_dir / "models" / "voice"
    except Exception:  # noqa: BLE001
        kokoro_model_dir = Path.home() / ".sovyx" / "models" / "voice"
    kokoro = KokoroTTS(model_dir=kokoro_model_dir)
    synthesizer = KokoroSampleSynthesizer(tts=kokoro)
    progress_tracker = ProgressTracker(job_dir / "progress.jsonl")

    orchestrator = TrainingOrchestrator(
        synthesizer=synthesizer,
        backend=backend,
        progress_tracker=progress_tracker,
    )

    # Run. Ctrl+C → KeyboardInterrupt → translate to filesystem
    # cancel signal so the orchestrator's transition is CANCELLED
    # (not FAILED). Without this hook, the operator's Ctrl+C would
    # propagate as a bare KeyboardInterrupt + the orchestrator never
    # got a chance to write CANCELLED to JSONL.
    import asyncio  # noqa: PLC0415

    cancel_path = job_dir / ".cancel"
    try:
        final_state = asyncio.run(orchestrator.run(request, job_dir=job_dir))
    except KeyboardInterrupt:
        cancel_path.touch()
        console.print("\n[yellow]Cancellation signalled.[/yellow] Run again to resume.")
        raise typer.Exit(code=130) from None  # 130 = standard SIGINT exit code

    if final_state.status is TrainingStatus.COMPLETE:
        console.print(
            f"[green]✓ Training complete:[/green] [bold]{final_state.output_path}[/bold]",
        )
        console.print(
            f"  duration_actual:  [dim]{final_state.completed_at}[/dim]",
        )
        if mind_id and final_state.output_path:
            _attempt_hot_reload(mind_id, Path(final_state.output_path))
        elif mind_id:
            console.print(
                "  [yellow]No output path recorded — hot-reload skipped.[/yellow]",
            )
        else:
            console.print(
                "  [dim]Trained without --mind-id; daemon will pick up "
                "this model from the pretrained pool on next restart.[/dim]",
            )
    elif final_state.status is TrainingStatus.CANCELLED:
        console.print(
            f"[yellow]Cancelled:[/yellow] {final_state.message}",
        )
        raise typer.Exit(code=1)
    elif final_state.status is TrainingStatus.FAILED:
        console.print(
            f"[red]✗ Failed:[/red] {final_state.error_summary}",
        )
        raise typer.Exit(code=1)
    else:
        console.print(
            f"[red]Unexpected non-terminal state:[/red] {final_state.status.value}",
        )
        raise typer.Exit(code=1)
