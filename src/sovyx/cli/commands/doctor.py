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
    PreflightStepSpec,
    check_portaudio,
    default_step_names,
    run_preflight,
)
from sovyx.voice.health._linux_mixer_check import check_linux_mixer_sanity

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
) -> None:
    """Voice Capture Health Lifecycle diagnostics (ADR §4.8).

    Runs the subset of L5 pre-flight the CLI can drive without a
    daemon: PortAudio host-API sanity and default-device enumeration.
    Exit code equals the number of failed steps so CI pipelines can
    gate on voice readiness.
    """
    exit_code = _run_voice_doctor(output_json=output_json, device=device)
    raise typer.Exit(exit_code)


def _run_voice_doctor(*, output_json: bool, device: str | None) -> int:
    """Execute the voice doctor flow. Returns the desired exit code."""
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
        report = asyncio.run(run_preflight(steps=specs, stop_on_first_failure=False))

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
        return sum(1 for s in report.steps if not s.passed)

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
            f"in {report.total_duration_ms:.1f} ms.[/green]"
        )
    else:
        first = report.first_failure
        assert first is not None  # noqa: S101 — narrows type for reporting
        console.print(
            f"\n[red]{failure_count} of {len(report.steps)} step(s) failed. "
            f"First failure: step {first.step} ({first.code.value}).[/red]"
        )
        if first.hint:
            console.print(f"[dim]Hint:[/dim] {first.hint}")
    if device is not None:
        console.print(
            f"\n[dim]Note: --device {device!r} is informational in this "
            "release; cascade-level filtering ships with the L7 RPC surface.[/dim]"
        )
    return failure_count


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
