"""CLI command: sovyx doctor — health checks for installation and voice stack.

Composes three doctor surfaces under one command:

* ``sovyx doctor`` — general installation health (disk, RAM, CPU,
  model files, config + online RPC checks against a running daemon).
* ``sovyx doctor voice`` — Voice Capture Health Lifecycle diagnostics
  per ADR §4.8. Runs the L5 pre-flight (the subset the CLI can drive
  standalone) and surfaces per-step results with a non-zero exit code
  equal to the number of failing steps.
* ``sovyx doctor cascade`` — invokes :func:`run_startup_cascade` in
  operator mode (no daemon boot), captures the log slice by ``saga_id``,
  and renders it as a human-readable timeline. This is the
  IMPL-OBSERVABILITY-001 §15 replacement for the legacy ``.ps1``
  forensic scripts: the same data, structured, OS-agnostic, and
  reproducible from any operator shell.

The voice surface is intentionally standalone — it does NOT require a
running daemon. Daemon-dependent bits (ComboStore fast-path, live
device default-change watcher, TTS open probe against the configured
engine) land in the follow-up that wires the L7 backend RPC.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sovyx.cli.rpc_client import DaemonClient
from sovyx.engine.config import EngineConfig
from sovyx.engine.registry import ServiceRegistry
from sovyx.observability.health import (
    CheckResult,
    CheckStatus,
    HealthRegistry,
    create_offline_registry,
)
from sovyx.voice.health import (
    PreflightReport,
    PreflightStepSpec,
    check_portaudio,
    clear_preflight_warnings_file,
    default_step_names,
    run_preflight,
)
from sovyx.voice.health._linux_mixer_check import check_linux_mixer_sanity

# v1.3 §4.4 L5b — ``doctor voice --fix`` semantic exit codes. The
# ``--fix`` flow steers into these instead of returning the failing
# step count, so CI / shell wrappers can branch on the specific
# outcome. The non-``--fix`` path preserves the v0.21.2 contract of
# returning the number of failing steps.
EXIT_DOCTOR_OK = 0
"""No saturation, or --fix succeeded, or --dry-run printed the plan."""

EXIT_DOCTOR_GENERIC_FAILURE = 1
"""Reserved for non-fix paths (preserves existing behaviour for callers)."""

EXIT_DOCTOR_SATURATED_NOT_FIXED = 2
"""Saturation detected but --fix was not requested."""

EXIT_DOCTOR_APPLY_FAILED = 3
"""--fix attempted but apply_mixer_reset failed, or re-probe still saturated."""

EXIT_DOCTOR_USER_ABORTED = 4
"""--fix aborted: non-TTY shell without --yes, or interactive prompt rejected."""

EXIT_DOCTOR_UNSUPPORTED = 5
"""--fix requested on non-Linux, or ``amixer`` is not on PATH."""

console = Console()
doctor_app = typer.Typer(
    name="doctor",
    help="Health checks for the Sovyx installation and voice stack.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@doctor_app.callback()
def doctor(
    ctx: typer.Context,
    output_json: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
) -> None:
    """Run health checks on the Sovyx installation.

    Offline checks (always available): disk, RAM, CPU, model files, config.
    Online checks (daemon required): database, brain, LLM, channels,
    consolidation, cost budget.
    """
    if ctx.invoked_subcommand is not None:
        return
    _run_general_doctor(output_json=output_json)


def _run_general_doctor(*, output_json: bool) -> None:
    """Execute the general installation health check."""
    results: list[CheckResult] = []

    offline = create_offline_registry()
    offline_results = asyncio.run(offline.run_all(timeout=10.0))
    results.extend(offline_results)

    data_dir = Path.home() / ".sovyx"
    config_file = data_dir / "system.yaml"
    if config_file.exists():
        results.append(
            CheckResult(
                name="Config",
                status=CheckStatus.GREEN,
                message=f"system.yaml found ({config_file.stat().st_size} bytes)",
            )
        )
    else:
        results.append(
            CheckResult(
                name="Config",
                status=CheckStatus.YELLOW,
                message="system.yaml not found (using defaults)",
            )
        )

    client = DaemonClient()
    daemon_running = client.is_daemon_running()

    if daemon_running:
        try:
            rpc_result = asyncio.run(client.call("doctor"))
            if isinstance(rpc_result, dict):
                for name, check_data in rpc_result.get("checks", {}).items():
                    if isinstance(check_data, dict):
                        status_str = check_data.get("status", "green")
                        status = CheckStatus(status_str)
                        results.append(
                            CheckResult(
                                name=name,
                                status=status,
                                message=check_data.get("message", ""),
                                metadata=check_data.get("metadata") or {},
                            )
                        )
                    else:
                        ok = bool(check_data)
                        results.append(
                            CheckResult(
                                name=name,
                                status=CheckStatus.GREEN if ok else CheckStatus.RED,
                                message="ok" if ok else "failed",
                            )
                        )
        except Exception as exc:  # noqa: BLE001 — CLI boundary — renders RPC failure to doctor table; pragma: no cover
            results.append(
                CheckResult(
                    name="Daemon RPC",
                    status=CheckStatus.RED,
                    message=f"RPC call failed: {exc}",
                )
            )

    if output_json:
        json_out = [dataclasses.asdict(r) for r in results]
        for item in json_out:
            item["status"] = item["status"].value
        typer.echo(json.dumps(json_out, indent=2))
        return

    status_icon = {
        CheckStatus.GREEN: "[green]OK[/green]",
        CheckStatus.YELLOW: "[yellow]WARN[/yellow]",
        CheckStatus.RED: "[red]FAIL[/red]",
    }
    table = Table(title="Sovyx Health Check", show_lines=False)
    table.add_column("", width=5)
    table.add_column("Check", min_width=20)
    table.add_column("Status", min_width=10)
    table.add_column("Message")
    for r in results:
        icon = status_icon.get(r.status, "?")
        status_style = {
            CheckStatus.GREEN: "green",
            CheckStatus.YELLOW: "yellow",
            CheckStatus.RED: "red",
        }.get(r.status, "white")
        table.add_row(
            icon,
            r.name,
            f"[{status_style}]{r.status.value}[/{status_style}]",
            r.message,
        )
    console.print(table)
    if not daemon_running:
        console.print(
            "\n[yellow]Daemon not running — showing offline checks only.[/yellow]"
            "\n[dim]Start the daemon for full health checks "
            "(database, brain, LLM, channels).[/dim]"
        )

    overall = HealthRegistry().summary(results)
    greens = sum(1 for r in results if r.status == CheckStatus.GREEN)
    yellows = sum(1 for r in results if r.status == CheckStatus.YELLOW)
    reds = sum(1 for r in results if r.status == CheckStatus.RED)
    total = len(results)
    overall_style = {
        CheckStatus.GREEN: "green",
        CheckStatus.YELLOW: "yellow",
        CheckStatus.RED: "red",
    }.get(overall, "white")
    console.print(
        f"\n[{overall_style} bold]{overall.value.upper()}[/{overall_style} bold] — "
        f"[bold]{greens}[/bold]/{total} passed"
        + (f", [yellow]{yellows} warnings[/yellow]" if yellows else "")
        + (f", [red]{reds} critical[/red]" if reds else "")
    )


@doctor_app.command("voice")
def doctor_voice(
    output_json: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
    device: str | None = typer.Option(
        None,
        "--device",
        help="Restrict checks to one endpoint by GUID or friendly name. "
        "(Currently informational — reserved for --fix / --reset / --aggressive.)",
    ),
    fix: bool = typer.Option(
        False,
        "--fix",
        help="Apply safe remediations for detected issues — currently "
        "resets saturated ALSA mixer controls (Capture + Internal Mic "
        "Boost) to a known-safe fraction. Linux-only.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation on --fix. REQUIRED when "
        "stdin is not a TTY (systemd unit, cron job, CI).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="With --fix: print the planned mixer changes without "
        "mutating anything. Useful for auditing remediation scope.",
    ),
    card_index: int | None = typer.Option(
        None,
        "--card-index",
        help="With --fix: restrict the reset to one ALSA card index "
        "(from /proc/asound/cards). Default resets every saturated card.",
    ),
) -> None:
    """Voice Capture Health Lifecycle diagnostics (ADR §4.8 + v1.3 §4.4).

    Without ``--fix`` the command is diagnostic-only: it runs the
    standalone subset of L5 pre-flight (PortAudio host-API sanity +
    Linux ALSA mixer saturation) and returns the count of failing
    steps so CI pipelines can gate on voice readiness.

    With ``--fix`` the command becomes remediating: on a saturated
    Linux mixer it invokes :func:`apply_mixer_reset` to drive the
    over-driven controls down to their safe fractions, re-runs the
    preflight to confirm, and clears the boot marker file (L7) on
    success. Exit codes (see module-level constants) distinguish the
    outcomes so shell wrappers can branch.
    """
    exit_code = _run_voice_doctor(
        output_json=output_json,
        device=device,
        fix=fix,
        yes=yes,
        dry_run=dry_run,
        card_index=card_index,
    )
    raise typer.Exit(exit_code)


def _run_voice_doctor(
    *,
    output_json: bool,
    device: str | None,
    fix: bool = False,
    yes: bool = False,
    dry_run: bool = False,
    card_index: int | None = None,
) -> int:
    """Execute the voice doctor flow. Returns the desired exit code."""
    report = _run_voice_preflight()
    _render_voice_report(report, output_json=output_json, device=device)

    failure_count = sum(1 for s in report.steps if not s.passed)

    # Non-fix path: preserve v0.21.2 contract — exit code equals the
    # number of failing steps so existing CI scripts keep working.
    if not fix:
        if failure_count and _first_failure_is_saturation(report):
            # Keep behaviour stable but let callers distinguish the
            # "saturation present, fix available" case via stderr
            # without changing the numeric contract.
            return failure_count
        return failure_count

    # ── --fix path — semantic exit codes ────────────────────────────
    if sys.platform != "linux":
        console.print(
            f"\n[yellow]--fix is Linux-only; nothing to apply on {sys.platform}.[/yellow]",
        )
        return EXIT_DOCTOR_UNSUPPORTED

    if report.passed or not _first_failure_is_saturation(report):
        console.print("\n[green]No saturation detected — nothing to fix.[/green]")
        return EXIT_DOCTOR_OK

    return _apply_mixer_fix_flow(
        yes=yes,
        dry_run=dry_run,
        card_index=card_index,
    )


def _run_voice_preflight() -> PreflightReport:
    """Shared step 4 + step 9 preflight runner.

    Extracted from :func:`_run_voice_doctor` so ``--fix`` can re-run
    the exact same specs after the apply path without duplicating the
    spec list.
    """
    names = default_step_names()
    portaudio_name, portaudio_code = names[4]
    mixer_name, mixer_code = names[9]
    specs = [
        PreflightStepSpec(
            step=4,
            name=portaudio_name,
            code=portaudio_code,
            check=check_portaudio(),
        ),
        PreflightStepSpec(
            step=9,
            name=mixer_name,
            code=mixer_code,
            check=check_linux_mixer_sanity(),
        ),
    ]
    # structlog's default PrintLoggerFactory writes to stdout, which would
    # corrupt --json output and clutter the Rich table. Redirect any
    # stdout writes during preflight to stderr so the report alone owns stdout.
    with contextlib.redirect_stdout(sys.stderr):
        return asyncio.run(run_preflight(steps=specs, stop_on_first_failure=False))


def _render_voice_report(
    report: PreflightReport,
    *,
    output_json: bool,
    device: str | None,
) -> None:
    """Render a :class:`PreflightReport` as JSON or a Rich table."""
    if output_json:
        payload = {
            "passed": report.passed,
            "steps_run": len(report.steps),
            "first_failure_code": (
                report.first_failure.code.value if report.first_failure is not None else None
            ),
            "total_duration_ms": round(report.total_duration_ms, 1),
            "device_filter": device,
            "steps": [
                {
                    "step": s.step,
                    "name": s.name,
                    "code": s.code.value,
                    "passed": s.passed,
                    "hint": s.hint,
                    "duration_ms": round(s.duration_ms, 1),
                    "details": dict(s.details),
                }
                for s in report.steps
            ],
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    table = Table(
        title="Sovyx Voice Doctor — L5 Pre-flight",
        show_lines=False,
    )
    table.add_column("", width=5)
    table.add_column("Step", width=5, justify="right")
    table.add_column("Name", min_width=20)
    table.add_column("Code", min_width=16)
    table.add_column("Duration", min_width=10, justify="right")
    table.add_column("Hint / Details")

    for s in report.steps:
        icon = "[green]OK[/green]" if s.passed else "[red]FAIL[/red]"
        duration = f"{s.duration_ms:.1f} ms"
        detail_text = s.hint or _format_details(s.details)
        table.add_row(icon, str(s.step), s.name, s.code.value, duration, detail_text)

    console.print(table)

    failure_count = sum(1 for s in report.steps if not s.passed)
    if report.passed:
        console.print(
            f"\n[green]All {len(report.steps)} step(s) passed "
            f"in {report.total_duration_ms:.1f} ms.[/green]",
        )
    else:
        first = report.first_failure
        assert first is not None  # noqa: S101 — narrows type for reporting
        console.print(
            f"\n[red]{failure_count} of {len(report.steps)} step(s) failed. "
            f"First failure: step {first.step} ({first.code.value}).[/red]",
        )
        if first.hint:
            console.print(f"[dim]Hint:[/dim] {first.hint}")
    if device is not None:
        console.print(
            f"\n[dim]Note: --device {device!r} is informational in this "
            "release; cascade-level filtering ships with the L7 RPC surface.[/dim]",
        )


def _first_failure_is_saturation(report: PreflightReport) -> bool:
    """Return True when step 9 (linux_mixer_saturated) is the blocker."""
    from sovyx.voice.health import PreflightStepCode

    first = report.first_failure
    return first is not None and first.code is PreflightStepCode.LINUX_MIXER_SATURATED


def _apply_mixer_fix_flow(
    *,
    yes: bool,
    dry_run: bool,
    card_index: int | None,
) -> int:
    """Remediate a saturated ALSA mixer (``--fix`` path).

    See :func:`_run_voice_doctor` and the module-level exit-code
    constants for the outcome mapping. Subprocess errors degrade to
    :data:`EXIT_DOCTOR_APPLY_FAILED` with the ``amixer`` reason token
    surfaced so operators can read the failure mode straight from the
    exit-code-plus-stderr pair.
    """
    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health._linux_mixer_apply import (
        REASON_AMIXER_UNAVAILABLE,
        apply_mixer_reset,
    )
    from sovyx.voice.health._linux_mixer_probe import enumerate_alsa_mixer_snapshots
    from sovyx.voice.health.bypass._strategy import BypassApplyError

    snapshots = enumerate_alsa_mixer_snapshots()
    if not snapshots:
        console.print(
            "\n[yellow]No ALSA cards enumerable — is ``alsa-utils`` installed?[/yellow]",
        )
        return EXIT_DOCTOR_UNSUPPORTED

    targets = [s for s in snapshots if s.saturation_warning]
    if card_index is not None:
        targets = [s for s in targets if s.card_index == card_index]
    if not targets:
        console.print(
            f"\n[yellow]No saturating card matches --card-index={card_index}.[/yellow]"
            if card_index is not None
            else "\n[yellow]No saturating cards found.[/yellow]",
        )
        return EXIT_DOCTOR_OK

    # Render the remediation plan before taking any action. --dry-run
    # stops here; the interactive path re-uses the same summary for
    # the confirmation prompt.
    console.print("\n[bold]Planned mixer remediation:[/bold]")
    for card in targets:
        risky = [c.name for c in card.controls if c.saturation_risk]
        console.print(
            f"  [dim]card {card.card_index}[/dim] {card.card_longname or card.card_id} — "
            f"reset [bold]{', '.join(risky) or '(none)'}[/bold] "
            f"(aggregated boost {card.aggregated_boost_db:.1f} dB)",
        )

    if dry_run:
        console.print("\n[dim]--dry-run: no changes applied.[/dim]")
        return EXIT_DOCTOR_OK

    if not yes:
        if not sys.stdin.isatty():
            console.print(
                "\n[red]--fix without --yes requires an interactive TTY; "
                "re-run with --yes in non-interactive shells.[/red]",
            )
            return EXIT_DOCTOR_USER_ABORTED
        try:
            confirmed = typer.confirm(
                "Apply these mixer changes?",
                default=False,
            )
        except typer.Abort:
            console.print("\n[yellow]Aborted by user.[/yellow]")
            return EXIT_DOCTOR_USER_ABORTED
        if not confirmed:
            console.print("\n[yellow]Aborted by user.[/yellow]")
            return EXIT_DOCTOR_USER_ABORTED

    tuning = VoiceTuningConfig()
    applied: list[tuple[int, tuple[str, int]]] = []
    for card in targets:
        controls_to_reset = [c for c in card.controls if c.saturation_risk]
        if not controls_to_reset:
            continue
        try:
            snapshot = asyncio.run(
                apply_mixer_reset(
                    card.card_index,
                    controls_to_reset,
                    tuning=tuning,
                ),
            )
        except BypassApplyError as exc:
            if exc.reason == REASON_AMIXER_UNAVAILABLE:
                console.print(
                    "\n[red]amixer is not on PATH — install ``alsa-utils`` to enable --fix.[/red]",
                )
                return EXIT_DOCTOR_UNSUPPORTED
            console.print(
                f"\n[red]Mixer reset failed on card {card.card_index}: "
                f"{exc.reason} ({exc}).[/red]",
            )
            return EXIT_DOCTOR_APPLY_FAILED
        for control_name, raw in snapshot.applied_controls:
            applied.append((card.card_index, (control_name, raw)))
        console.print(
            f"[green]✓[/green] card {card.card_index}: reset "
            f"{len(snapshot.applied_controls)} control(s).",
        )

    if not applied:
        console.print("\n[yellow]No controls required mutation.[/yellow]")
        return EXIT_DOCTOR_OK

    # Re-run the preflight to confirm the fix actually cleared step 9.
    verify = _run_voice_preflight()
    if _first_failure_is_saturation(verify):
        console.print(
            "\n[red]Post-fix preflight still reports saturation — the "
            "mixer reset did not reach a safe configuration.[/red]",
        )
        return EXIT_DOCTOR_APPLY_FAILED

    # v1.3 §4.7.7 — clear the marker so subsequent ``sovyx start`` /
    # ``sovyx status`` no longer surface a warning the user just
    # remediated. File removal is best-effort: a race with another
    # process deleting it first is harmless.
    with contextlib.suppress(OSError):
        clear_preflight_warnings_file()
    console.print(
        "\n[green]Mixer remediated successfully; preflight warning marker cleared.[/green]",
    )
    return EXIT_DOCTOR_OK


def _format_details(details: object) -> str:
    """Render the details mapping as a short one-line string."""
    if not isinstance(details, dict) or not details:
        return ""
    parts = [f"{k}={v}" for k, v in details.items()]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# `sovyx doctor cascade` — startup self-diagnosis in operator mode.
# ---------------------------------------------------------------------------


class _CascadeCapture(logging.Handler):
    """Capture every record emitted during the cascade.

    The cascade emits via structlog, which after envelope processing
    forwards records to stdlib logging. The handler stores raw records
    so the renderer can pull saga_id / event / level / extras without
    re-parsing JSON.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@doctor_app.command("cascade")
def doctor_cascade(
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Emit captured cascade as JSONL on stdout (operators piping into jq).",
    ),
) -> None:
    """Run the startup self-diagnosis cascade and render it.

    Pivots the legacy ``.ps1`` forensic scripts onto the structured
    cascade defined in :mod:`sovyx.observability.self_diagnosis`. No
    daemon boot is required — this command is the operator entry point
    for "what does Sovyx see on this machine right now".

    Exit code is the number of WARNING-or-higher records emitted during
    the cascade so CI / monitoring pipelines can gate on a clean boot.
    """
    exit_code = _run_cascade(output_json=output_json)
    raise typer.Exit(exit_code)


@doctor_app.command("linux_session_manager_grab")
def doctor_linux_session_manager_grab(
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the report as a single JSON object on stdout.",
    ),
) -> None:
    """Detect whether another audio client holds the capture hardware.

    Introduced by ``voice-linux-cascade-root-fix`` T10. Answers the
    support question "why does 'Enable voice' keep saying my mic is
    busy?" by calling :func:`sovyx.voice._session_manager_detector.detect_session_manager_grab`
    and rendering its verdict.

    Exit codes:

    * ``0`` — detector confirmed no grab (``has_grab=False``).
    * ``1`` — detector confirmed a grab (``has_grab=True``). The mic
      is held by another app; the printed process list names the
      culprit.
    * ``2`` — detector was inconclusive (``has_grab=None``). Neither
      pactl nor the /proc scan produced a confident answer. Treat as
      "unknown" — the production cascade is still free to attempt.

    Linux-only; on Windows / macOS the command exits ``2`` with a
    "not applicable" message.
    """
    exit_code = asyncio.run(_run_linux_session_manager_grab(output_json=output_json))
    raise typer.Exit(exit_code)


async def _run_linux_session_manager_grab(*, output_json: bool) -> int:
    """Execute the detector and render the result."""
    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice._session_manager_detector import detect_session_manager_grab

    tuning = VoiceTuningConfig()
    report = await detect_session_manager_grab(tuning=tuning)

    if output_json:
        payload = {
            "has_grab": report.has_grab,
            "detection_method": report.detection_method,
            "grabbing_processes": [dataclasses.asdict(p) for p in report.grabbing_processes],
            "evidence": report.evidence,
        }
        print(json.dumps(payload, ensure_ascii=False))
    else:
        console = Console()
        if report.has_grab is True:
            console.print(
                "[bold yellow]⚠ Capture hardware is held by another client.[/]",
            )
        elif report.has_grab is False:
            console.print("[bold green]✓ Capture hardware is free.[/]")
        else:
            console.print(
                "[bold blue]ℹ Detector could not determine grab state[/]"
                " (pactl missing and/or /proc scan timed out).",
            )
        console.print(f"  method    = {report.detection_method}")
        if report.grabbing_processes:
            console.print("  processes =")
            for proc in report.grabbing_processes:
                label = f"{proc.name}" if proc.name else "(unknown)"
                console.print(f"    - pid={proc.pid} name={label!r}")
        if report.evidence:
            console.print(f"  evidence  = {report.evidence}")

    if report.has_grab is True:
        return 1
    if report.has_grab is False:
        return 0
    return 2


def _run_cascade(*, output_json: bool) -> int:
    """Execute the cascade with a temporary capture handler attached."""
    from sovyx.observability.logging import setup_logging
    from sovyx.observability.self_diagnosis import run_startup_cascade

    config = EngineConfig()
    setup_logging(config.log, config.observability, data_dir=config.data_dir)

    registry = ServiceRegistry()
    registry.register_instance(EngineConfig, config)

    capture = _CascadeCapture()
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        # The cascade itself sets up the saga; capture every record
        # emitted while it runs (the saga_scope binds saga_id into the
        # contextvars, which envelope processors copy into each record's
        # extra fields).
        with contextlib.redirect_stdout(sys.stderr):
            asyncio.run(run_startup_cascade(config, registry, None))
    finally:
        root.removeHandler(capture)

    if output_json:
        for record in capture.records:
            payload = {
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "event": record.getMessage(),
                "saga_id": getattr(record, "saga_id", None),
            }
            typer.echo(json.dumps(payload, ensure_ascii=False))
        return sum(1 for r in capture.records if r.levelno >= logging.WARNING)

    table = Table(title="Sovyx Doctor — Startup Cascade", show_lines=False)
    table.add_column("", width=5)
    table.add_column("Step / Event", min_width=28)
    table.add_column("Logger", min_width=22)
    table.add_column("Saga", width=18)
    for record in capture.records:
        icon = {
            logging.DEBUG: "[dim]·[/dim]",
            logging.INFO: "[green]OK[/green]",
            logging.WARNING: "[yellow]WARN[/yellow]",
            logging.ERROR: "[red]FAIL[/red]",
            logging.CRITICAL: "[red bold]CRIT[/red bold]",
        }.get(record.levelno, "?")
        saga_id = getattr(record, "saga_id", "") or ""
        table.add_row(icon, record.getMessage(), record.name, saga_id)
    console.print(table)

    warnings = sum(1 for r in capture.records if r.levelno == logging.WARNING)
    errors = sum(1 for r in capture.records if r.levelno >= logging.ERROR)
    total = len(capture.records)
    summary_style = "red" if errors else ("yellow" if warnings else "green")
    console.print(
        f"\n[{summary_style} bold]{total}[/{summary_style} bold] cascade events captured"
        + (f", [yellow]{warnings} warnings[/yellow]" if warnings else "")
        + (f", [red]{errors} errors[/red]" if errors else "")
    )
    return errors + warnings


__all__ = ["doctor_app"]
