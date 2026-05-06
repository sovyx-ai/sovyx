"""CLI command: sovyx doctor — health checks for installation and voice stack.

Composes three doctor surfaces under one command:

* ``sovyx doctor`` — general installation health (disk, RAM, CPU,
  model files, config + online RPC checks against a running daemon).
* ``sovyx doctor voice`` — Voice Capture Health Lifecycle diagnostics
  per ADR §4.8. Runs the L5 pre-flight (the subset the CLI can drive
  standalone) and surfaces per-step results with a non-zero exit code
  equal to the number of failing steps. Adds two opt-in flags:
    * ``--fix`` -- apply safe remediations (Linux ALSA mixer reset).
    * ``--full-diag`` -- run the full bundled diag toolkit (8-12 min,
      interactive) and triage the result tarball in-process. Wires the
      :mod:`sovyx.voice.diagnostics` package introduced in T1.4 of
      MISSION-voice-self-calibrating-system-2026-05-05.
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
from typing import Any

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
from sovyx.voice.calibration import (
    ApplyError,
    CalibrationApplier,
    CalibrationEngine,
    CalibrationProfile,
    capture_fingerprint,
    capture_measurements,
)
from sovyx.voice.diagnostics import (
    DiagPrerequisiteError,
    DiagRunError,
    TriageResult,
    run_full_diag,
    triage_tarball,
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
    full_diag: bool = typer.Option(
        False,
        "--full-diag",
        help="Run the full bundled voice diagnostic toolkit "
        "(8-12 min, interactive — will ask you to speak in short "
        "windows) and triage the result in-process. Linux-only. "
        "Mutually exclusive with --fix; chain them in separate "
        "invocations if needed.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="With --full-diag: skip every operator-prompt window in "
        "the bash diag (cegamente captures whatever audio is on the "
        "mic at probe time). REQUIRED when stdin is not a TTY (CI, "
        "systemd, cron). Reduces forensic coverage; prefer interactive.",
    ),
    calibrate: bool = typer.Option(
        False,
        "--calibrate",
        help="Run the calibration engine (Layer 2 of the voice "
        "self-calibrating mission). Captures hardware fingerprint + "
        "mixer state + (optionally) full diag artifacts, evaluates "
        "all rules, and persists a signed CalibrationProfile to "
        "<data_dir>/<mind_id>/calibration.json. Linux-only. Mutually "
        "exclusive with --fix and --full-diag.",
    ),
    mind_id: str = typer.Option(
        "default",
        "--mind-id",
        help="With --calibrate: the mind whose calibration to compute "
        "(default: 'default'). The persisted profile lands at "
        "<data_dir>/<mind_id>/calibration.json.",
    ),
    explain: bool = typer.Option(
        False,
        "--explain",
        help="With --calibrate: render the rule trace (which rules "
        "fired + matched conditions + produced decisions). Use to "
        "audit calibration decisions before they apply.",
    ),
) -> None:
    """Voice Capture Health Lifecycle diagnostics (ADR §4.8 + v1.3 §4.4).

    Without ``--fix`` or ``--full-diag`` the command is diagnostic-only:
    it runs the standalone subset of L5 pre-flight (PortAudio host-API
    sanity + Linux ALSA mixer saturation) and returns the count of
    failing steps so CI pipelines can gate on voice readiness.

    With ``--fix`` the command becomes remediating: on a saturated
    Linux mixer it invokes :func:`apply_mixer_reset` to drive the
    over-driven controls down to their safe fractions, re-runs the
    preflight to confirm, and clears the boot marker file (L7) on
    success. Exit codes (see module-level constants) distinguish the
    outcomes so shell wrappers can branch.

    With ``--full-diag`` (Linux-only) the command runs the bundled
    forensic diagnostic toolkit end-to-end (8-12 min interactive),
    captures a multi-MB tarball with hardware/kernel/PipeWire/PortAudio
    snapshots + recorded audio, then runs the typed triage analyzer
    in-process and renders a verdict + the suggested fix command (if
    any). Mutually exclusive with ``--fix``: full-diag observes,
    ``--fix`` mutates; chain them in separate invocations.
    """
    if full_diag and fix:
        raise typer.BadParameter(
            "--full-diag and --fix are mutually exclusive. "
            "Run --full-diag first to observe, then --fix to remediate "
            "based on the verdict."
        )
    if calibrate and fix:
        raise typer.BadParameter(
            "--calibrate and --fix are mutually exclusive. "
            "--calibrate observes + decides; --fix mutates. "
            "Run --calibrate first to produce a profile, then --fix "
            "to apply the operator-level remediations it advises."
        )
    if calibrate and full_diag:
        raise typer.BadParameter(
            "--calibrate and --full-diag are mutually exclusive. "
            "--calibrate runs --full-diag internally + adds the "
            "fingerprint + measurer + engine + applier pipeline."
        )
    exit_code = _run_voice_doctor(
        output_json=output_json,
        device=device,
        fix=fix,
        yes=yes,
        dry_run=dry_run,
        card_index=card_index,
        full_diag=full_diag,
        non_interactive=non_interactive,
        calibrate=calibrate,
        mind_id=mind_id,
        explain=explain,
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
    full_diag: bool = False,
    non_interactive: bool = False,
    calibrate: bool = False,
    mind_id: str = "default",
    explain: bool = False,
) -> int:
    """Execute the voice doctor flow. Returns the desired exit code."""
    if calibrate:
        return _run_voice_calibrate(
            mind_id=mind_id,
            non_interactive=non_interactive,
            dry_run=dry_run,
            explain=explain,
        )
    if full_diag:
        return _run_voice_full_diag(non_interactive=non_interactive)

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

    if report.passed or not (
        _first_failure_is_saturation(report) or _first_failure_is_attenuation(report)
    ):
        console.print("\n[green]No saturation or attenuation detected — nothing to fix.[/green]")
        return EXIT_DOCTOR_OK

    return _apply_mixer_fix_flow(
        yes=yes,
        dry_run=dry_run,
        card_index=card_index,
    )


def _run_voice_full_diag(*, non_interactive: bool) -> int:
    """Execute the bundled forensic diag toolkit + in-process triage.

    Wires :func:`sovyx.voice.diagnostics.run_full_diag` (extract bash
    package data + interactive subprocess) to
    :func:`sovyx.voice.diagnostics.triage_tarball` (typed verdict),
    then renders the verdict via rich and surfaces the operator-facing
    fix command for the highest-confidence hypothesis.

    Returns the doctor-voice exit code:
        * EXIT_DOCTOR_OK on a clean run with verdict rendered.
        * EXIT_DOCTOR_UNSUPPORTED on non-Linux / missing bash.
        * EXIT_DOCTOR_USER_ABORTED on a non-TTY shell without
          ``--non-interactive`` (the diag would dead-lock waiting for
          speech prompts).
        * EXIT_DOCTOR_GENERIC_FAILURE on diag-script failure (rc!=0).
    """
    if not sys.stdin.isatty() and not non_interactive:
        console.print(
            "\n[red]--full-diag requires an interactive TTY for the speech-prompt "
            "windows.[/red]\n"
            "Pass [bold]--non-interactive[/bold] to bypass (reduces forensic coverage), "
            "or run from a terminal where stdin is a TTY."
        )
        return EXIT_DOCTOR_USER_ABORTED

    console.print(
        "\n[bold cyan]Running full voice diagnostic[/bold cyan] "
        "[dim](8-12 min, interactive — speak when prompted)[/dim]\n"
    )

    extra_args: tuple[str, ...] = ()
    if non_interactive:
        extra_args = ("--non-interactive",)

    try:
        diag_result = run_full_diag(extra_args=extra_args)
    except DiagPrerequisiteError as exc:
        console.print(f"\n[red]Voice diag prerequisites not met:[/red] {exc}")
        console.print(
            "\n[dim]On macOS or Windows, use [bold]sovyx doctor voice[/bold] "
            "(cross-platform health checks) instead.[/dim]"
        )
        return EXIT_DOCTOR_UNSUPPORTED
    except DiagRunError as exc:
        console.print(f"\n[red]Voice diag failed:[/red] {exc}")
        if exc.partial_output_dir is not None:
            # Print the label via rich (style markup), the path via raw
            # print() so it stays on a single line regardless of console
            # width. Rich's print() crops/folds at console.width even
            # with overflow=ignore + no_wrap=True; the path is a copy-
            # paste target for the operator and MUST stay contiguous.
            console.print("[dim]Partial output preserved at:[/dim]")
            print(f"  {exc.partial_output_dir}")
        return EXIT_DOCTOR_GENERIC_FAILURE

    console.print(
        f"\n[green]Diag completed[/green] in {diag_result.duration_s:.1f}s\n"
        f"[dim]Result tarball:[/dim] {diag_result.tarball_path}\n"
    )

    try:
        triage = triage_tarball(diag_result.tarball_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"\n[red]Triage failed:[/red] {exc}")
        return EXIT_DOCTOR_GENERIC_FAILURE

    _render_full_diag_verdict(triage)
    return EXIT_DOCTOR_OK


def _render_full_diag_verdict(result: TriageResult) -> None:
    """Render the triage verdict via rich + surface fix command for the winner."""
    summary_table = Table(
        title="Triage hypotheses",
        title_style="bold",
        show_lines=False,
    )
    summary_table.add_column("ID", style="cyan", no_wrap=True)
    summary_table.add_column("Title")
    summary_table.add_column("Confidence", justify="right")
    summary_table.add_column("", no_wrap=True)

    for h in result.hypotheses:
        if h.confidence < 0.05 and not h.evidence_for and not h.evidence_against:
            continue
        if h.confidence > 0.7:
            marker = "[red]●[/red]"
        elif h.confidence > 0.3:
            marker = "[yellow]●[/yellow]"
        else:
            marker = "[dim]○[/dim]"
        summary_table.add_row(
            h.hid.value,
            h.title,
            f"{h.confidence:.2f}",
            marker,
        )

    console.print(summary_table)

    winner = result.winner
    if winner is not None:
        console.print(
            f"\n[bold red]Highest-confidence hypothesis:[/bold red] "
            f"[bold]{winner.hid.value}[/bold] — {winner.title} "
            f"(confidence={winner.confidence:.2f})"
        )
        if winner.recommended_action:
            console.print(
                f"\n[bold green]Recommended action:[/bold green] {winner.recommended_action}"
            )
    else:
        console.print(
            "\n[green]No high-confidence hypothesis detected.[/green] "
            "[dim]Voice subsystem appears healthy.[/dim]"
        )

    console.print(
        f"\n[dim]Full markdown report: "
        f"[bold]python -m sovyx.voice.diagnostics.triage {result.tarball_root}[/bold] "
        f"--extract-dir[/dim]"
    )


def _run_voice_calibrate(
    *,
    mind_id: str,
    non_interactive: bool,
    dry_run: bool,
    explain: bool,
) -> int:
    """Execute the calibration engine end-to-end + persist the profile.

    Pipeline:
        1. Capture HardwareFingerprint (real local probes).
        2. Run the bundled forensic diag (8-12 min, interactive).
        3. Triage the resulting tarball in-process.
        4. Capture MeasurementSnapshot from mixer state + diag artifacts
           + triage cross-correlation.
        5. Run the CalibrationEngine (R10 + future rules).
        6. Apply (CalibrationApplier) unless --dry-run.
        7. Render verdict + advised_actions + (with --explain) rule trace.

    Returns the doctor-voice exit code:
        * EXIT_DOCTOR_OK on a clean run.
        * EXIT_DOCTOR_UNSUPPORTED on non-Linux / missing bash.
        * EXIT_DOCTOR_USER_ABORTED on a non-TTY shell without
          ``--non-interactive``.
        * EXIT_DOCTOR_GENERIC_FAILURE on any pipeline step failure.
    """
    if not sys.stdin.isatty() and not non_interactive:
        console.print(
            "\n[red]--calibrate runs --full-diag internally and requires an "
            "interactive TTY for the speech-prompt windows.[/red]\n"
            "Pass [bold]--non-interactive[/bold] to bypass (reduces forensic "
            "coverage), or run from a terminal where stdin is a TTY."
        )
        return EXIT_DOCTOR_USER_ABORTED

    console.print(
        "\n[bold cyan]Voice calibration[/bold cyan] "
        "[dim](capturing fingerprint + running diag + triaging + applying)[/dim]\n"
    )

    # Step 1: fingerprint.
    console.print("[dim](1/6) Capturing hardware fingerprint...[/dim]")
    fingerprint = capture_fingerprint()
    console.print(
        f"[dim]    audio_stack={fingerprint.audio_stack!r} "
        f"system={fingerprint.system_vendor!r} {fingerprint.system_product!r}[/dim]"
    )

    # Step 2: full diag.
    console.print("\n[dim](2/6) Running full diag (8-12 min, interactive)...[/dim]")
    extra_args: tuple[str, ...] = ()
    if non_interactive:
        extra_args = ("--non-interactive",)
    try:
        diag_result = run_full_diag(extra_args=extra_args)
    except DiagPrerequisiteError as exc:
        console.print(f"\n[red]Voice diag prerequisites not met:[/red] {exc}")
        return EXIT_DOCTOR_UNSUPPORTED
    except DiagRunError as exc:
        console.print(f"\n[red]Voice diag failed:[/red] {exc}")
        return EXIT_DOCTOR_GENERIC_FAILURE

    # Step 3: triage.
    console.print(
        f"\n[dim](3/6) Triaging tarball in-process[/dim] "
        f"[dim]({diag_result.tarball_path.name})[/dim]"
    )
    try:
        triage = triage_tarball(diag_result.tarball_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"\n[red]Triage failed:[/red] {exc}")
        return EXIT_DOCTOR_GENERIC_FAILURE

    # Step 4: measurements.
    console.print("\n[dim](4/6) Capturing measurements (mixer state + diag artifacts)...[/dim]")
    measurements = capture_measurements(
        diag_tarball_root=triage.tarball_root,
        triage_result=triage,
        duration_s=diag_result.duration_s,
    )
    console.print(
        f"[dim]    mixer_regime={measurements.mixer_attenuation_regime!r} "
        f"capture_pct={measurements.mixer_capture_pct} "
        f"boost_pct={measurements.mixer_boost_pct}[/dim]"
    )

    # Step 5: engine.
    console.print("\n[dim](5/6) Evaluating calibration engine...[/dim]")
    engine = CalibrationEngine()
    profile = engine.evaluate(
        mind_id=mind_id,
        fingerprint=fingerprint,
        measurements=measurements,
        triage_result=triage,
    )
    console.print(
        f"[dim]    {len(profile.decisions)} decision(s) emitted by "
        f"{len(profile.provenance)} rule(s)[/dim]"
    )

    # Step 6: apply (or dry-run).
    step_label = "Dry-run (no persistence)" if dry_run else "Applying + persisting"
    console.print(f"\n[dim](6/6) {step_label}...[/dim]")
    data_dir = Path.home() / ".sovyx"
    applier = CalibrationApplier(data_dir=data_dir)
    try:
        apply_result = applier.apply(profile, dry_run=dry_run)
    except ApplyError as exc:
        console.print(f"\n[red]Calibration apply failed:[/red] {exc}")
        return EXIT_DOCTOR_GENERIC_FAILURE

    _render_calibration_verdict(profile, apply_result, explain=explain)
    return EXIT_DOCTOR_OK


def _render_calibration_verdict(
    profile: CalibrationProfile,
    apply_result: object,  # ApplyResult; structural-typed to avoid TYPE_CHECKING dance
    *,
    explain: bool,
) -> None:
    """Render the calibration outcome to the operator: rule trace + advised actions."""
    # rich-rendered table of decisions.
    decisions_table = Table(
        title="Calibration decisions",
        title_style="bold",
        show_lines=False,
    )
    decisions_table.add_column("Rule", style="cyan", no_wrap=True)
    decisions_table.add_column("Op", no_wrap=True)
    decisions_table.add_column("Target")
    decisions_table.add_column("Confidence", justify="right")

    for d in profile.decisions:
        if d.confidence.value == "high":
            conf_marker = "[green]high[/green]"
        elif d.confidence.value == "medium":
            conf_marker = "[yellow]medium[/yellow]"
        elif d.confidence.value == "experimental":
            conf_marker = "[dim]experimental[/dim]"
        else:
            conf_marker = "low"
        decisions_table.add_row(d.rule_id, d.operation, d.target, conf_marker)

    if profile.decisions:
        console.print(decisions_table)
    else:
        console.print("\n[green]No calibration decisions needed.[/green]")

    # Advised actions: the operator-actionable next steps.
    advised = getattr(apply_result, "advised_actions", ())
    if advised:
        console.print("\n[bold green]Recommended actions:[/bold green]")
        for action in advised:
            print(f"  {action}")  # raw print so paths/commands stay contiguous

    # Explain mode: render the rule trace.
    if explain and profile.provenance:
        console.print("\n[bold]Rule trace ([dim]--explain[/dim]):[/bold]")
        for trace in profile.provenance:
            console.print(
                f"  [cyan]{trace.rule_id}[/cyan] "
                f"@v{trace.rule_version} "
                f"([{trace.confidence.value}]) "
                f"fired_at={trace.fired_at_utc}"
            )
            for cond in trace.matched_conditions:
                console.print(f"    matched: {cond}")
            for dec in trace.produced_decisions:
                console.print(f"    produced: {dec}")

    # Profile path footer.
    profile_path_attr = getattr(apply_result, "profile_path", None)
    if profile_path_attr is not None:
        dry_run_attr = getattr(apply_result, "dry_run", False)
        if dry_run_attr:
            console.print("\n[dim]Profile would be persisted to:[/dim]")
        else:
            console.print("\n[dim]Profile persisted to:[/dim]")
        print(f"  {profile_path_attr}")  # raw print to preserve path


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
    """Return True when step 9 reports the SATURATION regime as the blocker.

    Step 9 emits a single code (``LINUX_MIXER_SATURATED``) for both
    saturation and attenuation regimes — distinguished only by the
    ``details`` payload. When ``details.snapshots`` is present, this
    helper inspects ``saturation_warning`` per-card to disambiguate.
    For backward compatibility (callers/tests that build a step report
    without populating ``details.snapshots``), it falls back to treating
    the bare code as saturation — preserving the v0.21.2 contract.
    """
    from sovyx.voice.health import PreflightStepCode

    first = report.first_failure
    if first is None or first.code is not PreflightStepCode.LINUX_MIXER_SATURATED:
        return False
    details = first.details if isinstance(first.details, dict) else {}
    snapshots = details.get("snapshots")
    if not snapshots:
        # Legacy fallback — bare code historically implied saturation.
        return True
    return any(s.get("saturation_warning") is True for s in snapshots)


def _first_failure_is_attenuation(report: PreflightReport) -> bool:
    """Return True when step 9 reports the ATTENUATION regime as the blocker.

    Counterpart of :func:`_first_failure_is_saturation` — same step,
    same code, distinguished by ``details.snapshots[].attenuation_warning``.
    Returns False when ``details.snapshots`` is absent (legacy/test
    fixtures default to saturation semantics).
    """
    from sovyx.voice.health import PreflightStepCode

    first = report.first_failure
    if first is None or first.code is not PreflightStepCode.LINUX_MIXER_SATURATED:
        return False
    details = first.details if isinstance(first.details, dict) else {}
    snapshots = details.get("snapshots") or []
    return any(s.get("attenuation_warning") is True for s in snapshots)


def _is_capture_or_boost_name(name: str) -> bool:
    """Return True for ALSA mixer controls in the capture/boost path.

    Used by the attenuation fix flow to select which controls to lift.
    Mirrors the boost/capture pattern recognition in
    :mod:`sovyx.voice.health._linux_mixer_probe`.
    """
    lowered = name.lower()
    return "capture" in lowered or "boost" in lowered


def _apply_mixer_fix_flow(
    *,
    yes: bool,
    dry_run: bool,
    card_index: int | None,
) -> int:
    """Remediate a saturated OR attenuated ALSA mixer (``--fix`` path).

    Two regimes are handled:

    * **Saturation** (``saturation_warning=True``): boost/capture controls
      are clipping. :func:`apply_mixer_reset` reduces them to safe
      fractions (capture 0.5, boost 0.0 by default).
    * **Attenuation** (``attenuation_warning=True`` via
      :func:`_is_attenuated`): capture+boost are below the Silero VAD
      operating range. :func:`apply_mixer_boost_up` lifts them to safe
      midpoints (capture 0.75, boost 0.66 by default).

    A single card can only be in one regime at a time (saturation
    requires controls at ``max_raw``; attenuation requires boost at
    ``min_raw`` AND capture below 0.5 fraction — mutually exclusive).
    Multiple cards may exhibit different regimes simultaneously.

    See :func:`_run_voice_doctor` and the module-level exit-code
    constants for the outcome mapping. Subprocess errors degrade to
    :data:`EXIT_DOCTOR_APPLY_FAILED` with the ``amixer`` reason token
    surfaced so operators can read the failure mode straight from the
    exit-code-plus-stderr pair.
    """
    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health._linux_mixer_apply import (
        REASON_AMIXER_UNAVAILABLE,
        apply_mixer_boost_up,
        apply_mixer_reset,
    )
    from sovyx.voice.health._linux_mixer_check import _is_attenuated
    from sovyx.voice.health._linux_mixer_probe import enumerate_alsa_mixer_snapshots
    from sovyx.voice.health.bypass._strategy import BypassApplyError

    snapshots = enumerate_alsa_mixer_snapshots()
    if not snapshots:
        console.print(
            "\n[yellow]No ALSA cards enumerable — is ``alsa-utils`` installed?[/yellow]",
        )
        return EXIT_DOCTOR_UNSUPPORTED

    saturating = [s for s in snapshots if s.saturation_warning]
    attenuated = [s for s in snapshots if _is_attenuated(s)]
    if card_index is not None:
        saturating = [s for s in saturating if s.card_index == card_index]
        attenuated = [s for s in attenuated if s.card_index == card_index]
    if not saturating and not attenuated:
        msg = (
            f"No saturating or attenuated card matches --card-index={card_index}."
            if card_index is not None
            else "No saturating or attenuated cards found."
        )
        console.print(f"\n[yellow]{msg}[/yellow]")
        return EXIT_DOCTOR_OK

    # Render the remediation plan before taking any action. --dry-run
    # stops here; the interactive path re-uses the same summary for
    # the confirmation prompt.
    console.print("\n[bold]Planned mixer remediation:[/bold]")
    for card in saturating:
        risky = [c.name for c in card.controls if c.saturation_risk]
        console.print(
            f"  [dim]card {card.card_index}[/dim] {card.card_longname or card.card_id} — "
            f"[red]REDUCE[/red] [bold]{', '.join(risky) or '(none)'}[/bold] "
            f"(saturation; aggregated boost {card.aggregated_boost_db:.1f} dB)",
        )
    for card in attenuated:
        targets = [c for c in card.controls if _is_capture_or_boost_name(c.name)]
        console.print(
            f"  [dim]card {card.card_index}[/dim] {card.card_longname or card.card_id} — "
            f"[cyan]BOOST UP[/cyan] [bold]{', '.join(c.name for c in targets) or '(none)'}[/bold] "
            f"(attenuation; aggregated boost {card.aggregated_boost_db:.1f} dB)",
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
    # Saturation path — REDUCE.
    for card in saturating:
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
    # Attenuation path — BOOST UP.
    for card in attenuated:
        controls_to_boost = [c for c in card.controls if _is_capture_or_boost_name(c.name)]
        if not controls_to_boost:
            continue
        try:
            snapshot = asyncio.run(
                apply_mixer_boost_up(
                    card.card_index,
                    controls_to_boost,
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
                f"\n[red]Mixer boost-up failed on card {card.card_index}: "
                f"{exc.reason} ({exc}).[/red]",
            )
            return EXIT_DOCTOR_APPLY_FAILED
        for control_name, raw in snapshot.applied_controls:
            applied.append((card.card_index, (control_name, raw)))
        console.print(
            f"[green]✓[/green] card {card.card_index}: boosted "
            f"{len(snapshot.applied_controls)} control(s).",
        )

    if not applied:
        console.print("\n[yellow]No controls required mutation.[/yellow]")
        return EXIT_DOCTOR_OK

    # Re-run the preflight to confirm the fix actually cleared step 9
    # in either regime.
    verify = _run_voice_preflight()
    if _first_failure_is_saturation(verify) or _first_failure_is_attenuation(verify):
        console.print(
            "\n[red]Post-fix preflight still reports a mixer fault — the "
            "remediation did not reach a safe configuration.[/red]",
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


@doctor_app.command("platform")
def doctor_platform(
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the report as a single JSON object on stdout.",
    ),
) -> None:
    """Render the cross-OS platform-diagnostics report.

    Operator-side mirror of ``GET /api/voice/platform-diagnostics``
    that runs WITHOUT a daemon and prints structured per-OS state:

    * **Cross-platform** — microphone permission (granted / denied /
      unknown + remediation hint).
    * **Linux** — PipeWire / WirePlumber detection + ALSA UCM verb
      selection.
    * **Windows** — Audiosrv + AudioEndpointBuilder service state +
      recent ETW audio operational events.
    * **macOS** — HAL plug-in catalogue + Bluetooth audio profile +
      Hardened-Runtime mic-entitlement verifier.

    Probes are always run in parallel inside each branch via
    ``asyncio.gather``; per-probe failures collapse into structured
    notes — the command always exits ``0`` (this is a diagnostic, not
    a gate). Operators piping into ``jq`` should use ``--json``.
    """
    exit_code = asyncio.run(_run_platform_diagnostics(output_json=output_json))
    raise typer.Exit(exit_code)


async def _run_platform_diagnostics(*, output_json: bool) -> int:
    """Execute the per-OS detectors and render the result.

    Mirrors the dispatch logic in
    ``sovyx.dashboard.routes.voice_platform_diagnostics`` so the CLI
    and dashboard surfaces stay consistent. Probe failures are
    isolated per detector: a crash in one branch never takes out
    the rest.
    """
    from sovyx.voice.health._mic_permission import check_microphone_permission

    platform = sys.platform

    async def _safe(
        fn: Any,  # noqa: ANN401
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception:  # noqa: BLE001 — diagnostic isolation
            return None

    mic_task = _safe(check_microphone_permission)
    payload: dict[str, object] = {"platform": platform}

    if platform == "linux":
        from sovyx.voice.health._alsa_ucm import detect_ucm
        from sovyx.voice.health._pipewire import detect_pipewire

        mic_report, pw_report, ucm_report = await asyncio.gather(
            mic_task,
            _safe(detect_pipewire),
            _safe(detect_ucm, "0"),
        )
        payload["linux"] = {
            "pipewire": _serialise_dataclass_or_unknown(pw_report),
            "alsa_ucm": _serialise_dataclass_or_unknown(ucm_report),
        }
    elif platform == "win32":
        from sovyx.voice.health._windows_audio_service import (
            query_audio_service_status,
        )
        from sovyx.voice.health._windows_etw import query_audio_etw_events

        mic_report, svc_report, etw_report = await asyncio.gather(
            mic_task,
            _safe(query_audio_service_status),
            _safe(query_audio_etw_events),
        )
        payload["windows"] = {
            "audio_service": _serialise_dataclass_or_unknown(svc_report),
            "etw_audio_events": _serialise_etw_results(etw_report),
        }
    elif platform == "darwin":
        from sovyx.voice._bluetooth_profile_mac import (
            detect_bluetooth_audio_profile,
        )
        from sovyx.voice._codesign_verify_mac import verify_microphone_entitlement
        from sovyx.voice._hal_detector_mac import detect_hal_plugins

        mic_report, hal_report, bt_report, cs_report = await asyncio.gather(
            mic_task,
            _safe(detect_hal_plugins),
            _safe(detect_bluetooth_audio_profile),
            _safe(verify_microphone_entitlement),
        )
        payload["macos"] = {
            "hal_plugins": _serialise_dataclass_or_unknown(hal_report),
            "bluetooth": _serialise_dataclass_or_unknown(bt_report),
            "code_signing": _serialise_dataclass_or_unknown(cs_report),
        }
    else:
        payload["platform"] = "other"
        mic_report = await mic_task

    payload["mic_permission"] = _serialise_dataclass_or_unknown(mic_report)

    if output_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, default=str))
        return 0

    _render_platform_diagnostics_table(payload)
    return 0


def _serialise_dataclass_or_unknown(report: Any) -> dict[str, Any]:  # noqa: ANN401
    """Convert a detector report dataclass into a plain dict.

    Used by both the CLI rendering and the JSON output path. Returns
    a sentinel ``{"status": "unknown", "notes": ["probe returned None"]}``
    when the probe failed (reported as ``None`` from ``_safe``).

    ``Any`` is intentional: this helper is generic across N detector
    return types (PipeWireReport, UcmReport, EtwQueryResult, etc.)
    without forcing a Protocol hierarchy across modules.
    """
    if report is None:
        return {"status": "unknown", "notes": ["probe returned None"]}
    if dataclasses.is_dataclass(report) and not isinstance(report, type):
        coerced = _coerce_to_jsonable(dataclasses.asdict(report))
        if isinstance(coerced, dict):
            return coerced
    return {"value": str(report)}


def _serialise_etw_results(results: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """Special-case the ETW probe — it returns a tuple of results."""
    if results is None:
        return []
    out: list[dict[str, Any]] = []
    for r in results:
        if dataclasses.is_dataclass(r) and not isinstance(r, type):
            coerced = _coerce_to_jsonable(dataclasses.asdict(r))
            if isinstance(coerced, dict):
                out.append(coerced)
    return out


def _coerce_to_jsonable(obj: Any) -> Any:  # noqa: ANN401
    """Recursively convert StrEnum / tuple / set into JSON-safe types."""
    if isinstance(obj, dict):
        return {k: _coerce_to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_coerce_to_jsonable(v) for v in obj]
    # StrEnum values serialise as their string token via .value
    if hasattr(obj, "value") and isinstance(getattr(obj, "value", None), str):
        return obj.value
    return obj


def _render_platform_diagnostics_table(payload: dict[str, object]) -> None:
    """Render the platform diagnostics payload as Rich tables."""
    platform = payload.get("platform", "unknown")
    console.print(
        f"\n[bold]Sovyx Platform Diagnostics[/bold]  (platform=[cyan]{platform}[/cyan])\n",
    )

    mic = payload.get("mic_permission") or {}
    mic_status = mic.get("status", "unknown") if isinstance(mic, dict) else "unknown"
    mic_color = {
        "granted": "green",
        "denied": "red",
        "unknown": "yellow",
    }.get(str(mic_status), "yellow")
    console.print(
        f"  microphone permission : [{mic_color}]{mic_status}[/{mic_color}]",
    )
    if isinstance(mic, dict):
        hint = mic.get("remediation_hint") or ""
        if hint:
            console.print(f"    └─ hint: {hint}")

    for branch_name in ("linux", "windows", "macos"):
        branch = payload.get(branch_name)
        if not isinstance(branch, dict):
            continue
        console.print(f"\n[bold]{branch_name}[/bold]")
        for section, data in branch.items():
            console.print(f"  [bold cyan]{section}[/bold cyan]")
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in ("notes",) and isinstance(v, list) and v:
                        console.print(f"    {k}: {'; '.join(str(x) for x in v)}")
                    elif k in ("notes",):
                        continue
                    else:
                        console.print(f"    {k}: {v}")
            elif isinstance(data, list):
                console.print(f"    ({len(data)} entries)")
                for item in data[:3]:
                    if isinstance(item, dict) and "channel" in item:
                        n_events = len(item.get("events", []))
                        console.print(
                            f"    - {item['channel']}: {n_events} event(s)",
                        )


__all__ = ["doctor_app"]
