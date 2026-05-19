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
import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from sovyx.cli._mind_resolver import resolve_mind_id
from sovyx.cli.rpc_client import DaemonClient
from sovyx.engine._rpc_handlers import _load_mind_config_best_effort
from sovyx.engine.config import EngineConfig
from sovyx.engine.registry import ServiceRegistry
from sovyx.observability.health import (
    CheckResult,
    CheckStatus,
    HealthRegistry,
    create_offline_registry,
)
from sovyx.observability.logging import get_logger
from sovyx.voice.calibration import (
    ApplyError,
    ApplyResult,
    CalibrationApplier,
    CalibrationEngine,
    CalibrationProfile,
    CalibrationProfileLoadError,
    CalibrationProfileRollbackError,
    capture_fingerprint,
    capture_measurements,
    inspect_migrated_profile_dict,
    load_calibration_profile,
    profile_path,
    resolve_active_mic_card,
    rollback_calibration_profile,
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

EXIT_DOCTOR_VOICE_NOT_CONFIGURED = 6
"""Phase 5.T5.1 — ``--calibrate`` invoked non-interactively against a mind
whose ``voice_input_device_name`` is empty. Distinct from
:data:`EXIT_DOCTOR_UNSUPPORTED` (platform mismatch — operator can't
remediate by configuring the mic) because the operator CAN remediate by
running ``sovyx voice setup`` or by passing ``--input-device 'NAME'``
inline. Shell wrappers can branch on this code to surface a
configuration-prompt UI instead of treating it as a platform error."""

console = Console()
logger = get_logger(__name__)
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
        help="Run an automatic 8-12 minute hardware tune-up. Detects "
        "your audio setup, identifies any mic/mixer issues, and applies "
        "safe fixes. Saves the result so future runs replay the cached "
        "profile in seconds. Linux-only (uses bash diag tools). "
        "Mutually exclusive with --fix and --full-diag. "
        "Prereq: a mic must be configured for the target mind via "
        "`sovyx voice setup` (or the dashboard). On an interactive shell "
        "the setup picker runs inline when the mic is unconfigured; "
        "non-interactive shells must pre-configure via "
        "`sovyx voice setup --non-interactive --input-device 'NAME'` "
        "OR pass `--input-device 'NAME'` to this command inline.",
    ),
    mind_id: str | None = typer.Option(
        None,
        "--mind-id",
        help="With --calibrate: the mind whose calibration to compute. "
        "Default: auto-detected when exactly one mind exists under "
        "<data_dir>/. When multiple minds exist, --mind-id is required "
        "and the command errors with the available-minds list. The "
        "persisted profile lands at <data_dir>/<mind_id>/calibration.json.",
    ),
    explain: bool = typer.Option(
        False,
        "--explain",
        help="With --calibrate: also show WHICH detection rules fired "
        "and why. Useful to audit calibration decisions; the default "
        "verdict shows the operator-relevant summary only.",
    ),
    show: bool = typer.Option(
        False,
        "--show",
        help="With --calibrate: read-only — display the last saved "
        "calibration profile for --mind-id without running a new "
        "tune-up. Pairs with --explain to also show the rule trace.",
    ),
    rollback: bool = typer.Option(
        False,
        "--rollback",
        help="With --calibrate: restore the most-recent prior "
        "calibration. Walks the .bak.{1,2,3} multi-generation chain — "
        "up to 3 prior calibrations are retained, so you can roll back "
        "repeatedly if a re-calibration didn't help. Each --rollback "
        "consumes one generation; re-run --calibrate to repopulate the "
        "chain after exhaustion. Refuses to restore a malformed backup.",
    ),
    surgical: bool = typer.Option(
        False,
        "--surgical",
        help="With --calibrate or --full-diag: fast mode (~30s instead of "
        "8-12min) for re-runs when you've already calibrated once. Skips "
        "the speech-capture windows + interactive prompts. Use only if "
        "you're sure your hardware hasn't changed since the prior run; "
        "full mode is the default and is recommended for first calibration.",
    ),
    signing_key: Path | None = typer.Option(  # noqa: B008 -- typer Options canonical pattern
        None,
        "--signing-key",
        help="ADVANCED (developers only): cryptographically sign the "
        "persisted calibration profile with an Ed25519 private key. "
        "Most users do NOT need this — calibration works without it "
        "in the default LENIENT loader mode. Generate the dev key via "
        "`scripts/dev/generate_calibration_signing_key.py`.",
    ),
    evaluate_rules: bool = typer.Option(
        False,
        "--evaluate-rules",
        help="With --calibrate: preview which detection rules WOULD fire "
        "on your hardware without running the full 8-12 min tune-up or "
        "applying any changes. Useful for triage before committing to "
        "a real calibration run.",
    ),
    inspect_migration: bool = typer.Option(
        False,
        "--inspect-migration",
        help="With --calibrate: read-only — print the calibration profile "
        "dict AFTER walking the schema-migration chain to the runtime's "
        "current schema version. Useful at schema bump time to preview "
        "the post-migration shape without committing to it. Skips the "
        "signature gate; the dict is not a fully-validated profile. "
        "Mutually exclusive with --show / --rollback / --evaluate-rules.",
    ),
    input_device: str | None = typer.Option(
        None,
        "--input-device",
        help="Phase 5.T5.2 escape hatch — with --calibrate + "
        "--non-interactive, invoke `sovyx voice setup` inline against "
        "this device specifier (substring matched, case-insensitive, "
        "OR enumeration index) BEFORE the prereq gate fires. Persists "
        "the mic choice to mind.yaml and continues the calibrate "
        "pipeline. Use in CI / systemd scripts that need to onboard a "
        "fresh mind without a separate `voice setup` step. No-op when "
        "the mic is already configured OR when running interactively "
        "(the inline picker runs in that case).",
    ),
    reason_filter: str | None = typer.Option(
        None,
        "--reason-filter",
        help="Mission C1 §T2.4 + Mission H3 §T3.2: filter the 'Voice — "
        "quarantined endpoints' section to entries whose SSoT-resolved "
        "(or legacy fallback) reason matches the given value. "
        "Canonical values: 'apo_degraded' (capture-side DSP); "
        "'vad_frontend_dead' (Silero LSTM corruption); 'format_mismatch' "
        "(frame shape mismatch); 'driver_silent' (working stream / zero "
        "RMS); 'capture_dead' (substrate fully silent, Mission H3 new); "
        "'kernel_invalidated' (IAudioClient wedged); 'unclassified' "
        "(Mission H3 taxonomy fallback). Default: show every "
        "quarantined endpoint with its reason class + remediation hint. "
        "Empty quarantine renders an empty section regardless of the "
        "filter.",
    ),
) -> None:
    """Voice subsystem health checks + auto-fix tools.

    Without ``--fix`` or ``--full-diag`` the command is read-only:
    it runs the basic audio pre-flight (PortAudio sanity + Linux mixer
    saturation check) and returns the count of failing steps so CI
    pipelines can gate on voice readiness.

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
    if (show or rollback or inspect_migration) and not calibrate:
        raise typer.BadParameter(
            "--show, --rollback, and --inspect-migration require "
            "--calibrate. They operate on the per-mind "
            "<data_dir>/<mind_id>/calibration.json (+ .bak chain) "
            "files which only exist after at least one --calibrate run."
        )
    # Read-only inspect modes form a closed-enum mutex set: each one
    # is a distinct operator intent (show=current state, rollback=revert,
    # evaluate_rules=preview-without-running, inspect_migration=preview
    # post-migration shape). Enforce single-intent at the flag-parse
    # boundary so the dispatcher in `_run_voice_doctor` doesn't have
    # to disambiguate downstream.
    _read_only_modes_set = sum([show, rollback, evaluate_rules, inspect_migration])
    if _read_only_modes_set > 1:
        raise typer.BadParameter(
            "--show, --rollback, --evaluate-rules, and "
            "--inspect-migration are mutually exclusive — each is a "
            "distinct read-only inspection mode. Pick one per "
            "invocation; chain them in separate commands if you need "
            "more than one."
        )
    # rc.6 (Agent 2 A.5): fail-fast on a missing --signing-key path so an
    # operator typo doesn't waste 8-12 min of diag runtime + then silently
    # degrade to unsigned. Per Mission §0 promise 5 (LENIENT/STRICT real
    # crypto): the operator who passed --signing-key explicitly intended
    # signed output; a missing-path on a signed-intent run MUST surface
    # at flag-parse time, not deep in `_persistence.py:319` after the diag.
    #
    # rc.7 (Agent 2 NEW.1): extend the fail-fast to ALSO validate the key
    # format (PEM parseable + Ed25519 algorithm). Pre-rc.7 a malformed
    # PEM or non-Ed25519 key (e.g. RSA) passed `is_file()` + ran the
    # 8-12 min diag + landed UNSIGNED with the only forensic surface
    # being a structlog WARN. Now `_load_private_signing_key()` runs
    # at flag-parse time (cost <1ms) and converts its RuntimeError
    # into a Click BadParameter with the underlying reason.
    if signing_key is not None:
        if not signing_key.is_file():
            raise typer.BadParameter(
                f"--signing-key path does not exist: {signing_key}\n"
                f"Pass an existing PEM-encoded Ed25519 private key file. "
                f"Generate via `scripts/dev/generate_calibration_signing_key.py` "
                f"(dev-only); production rotation per docs/contributing/voice-kb-rotation.md.",
                param_hint="--signing-key",
            )
        # rc.8/rc.9 cosmetic: scope the symbol import to this conditional
        # branch. Note: `cryptography` itself ALREADY loads transitively
        # at module import via the voice subsystem's eager imports —
        # empirical trace (rc.9) shows the FIRST trigger is
        # `sovyx.voice.health._mixer_kb._signing` (mixer KB profile
        # signing), with `sovyx.voice.calibration._persistence` and
        # the calibration trust-store path resolving cryptography
        # modules transitively. A cold `import sovyx.cli.commands.doctor`
        # pulls in 30+ cryptography modules regardless. So this is NOT
        # a startup-time deferral — it's a name-scoping convention
        # that keeps `_load_private_signing_key` private to the
        # validation block. A future rc.X+ that wants to actually
        # defer cryptography would have to refactor every
        # voice/health/_mixer_kb/_signing + calibration/_persistence
        # eager import, which has wider blast radius (every load-path
        # test depends on those eager imports being available).
        from sovyx.voice.calibration._persistence import (  # noqa: PLC0415
            _load_private_signing_key,
        )

        try:
            _load_private_signing_key(signing_key)
        except RuntimeError as exc:
            raise typer.BadParameter(
                f"--signing-key validation failed: {exc}\n"
                f"Generate a valid Ed25519 PEM via "
                f"`scripts/dev/generate_calibration_signing_key.py` "
                f"(dev-only); production rotation per "
                f"docs/contributing/voice-kb-rotation.md.",
                param_hint="--signing-key",
            ) from exc
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
        show=show,
        rollback=rollback,
        surgical=surgical,
        signing_key=signing_key,
        evaluate_rules=evaluate_rules,
        inspect_migration=inspect_migration,
        input_device=input_device,
        reason_filter=reason_filter,
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
    mind_id: str | None = None,
    explain: bool = False,
    show: bool = False,
    rollback: bool = False,
    surgical: bool = False,
    signing_key: Path | None = None,
    evaluate_rules: bool = False,
    inspect_migration: bool = False,
    input_device: str | None = None,
    reason_filter: str | None = None,
) -> int:
    """Execute the voice doctor flow. Returns the desired exit code.

    When ``calibrate`` is true, ``mind_id`` is resolved via
    :func:`sovyx.cli._mind_resolver.resolve_mind_id` BEFORE dispatching
    to any calibrate sub-handler — closes anti-pattern #35 at the CLI
    boundary by replacing the legacy literal ``"default"`` sentinel
    with an explicit filesystem-validated mind id (or an actionable
    :class:`typer.BadParameter` when the value cannot be resolved).
    """
    # Resolve mind_id once for the calibrate dispatch chain. resolve_mind_id
    # raises typer.BadParameter on any unresolvable input (missing mind,
    # ambiguous, zero minds, empty string) and otherwise returns a MindId
    # (NewType[str]) whose <data_dir>/<mind_id>/mind.yaml exists on disk.
    # Branches below are exclusive — at most one fires per invocation —
    # so the resolver runs at most once, with a typed-narrow local that
    # downstream str-typed parameters accept cleanly.
    resolved_mind_id: str
    if calibrate:
        resolved_mind_id = resolve_mind_id(mind_id, Path.home() / ".sovyx")
        if show:
            return _run_voice_calibrate_show(mind_id=resolved_mind_id, explain=explain)
        if rollback:
            return _run_voice_calibrate_rollback(mind_id=resolved_mind_id)
        if evaluate_rules:
            return _run_voice_calibrate_evaluate_rules(mind_id=resolved_mind_id, explain=explain)
        if inspect_migration:
            return _run_voice_calibrate_inspect_migration(mind_id=resolved_mind_id)
        return _run_voice_calibrate(
            mind_id=resolved_mind_id,
            non_interactive=non_interactive,
            dry_run=dry_run,
            explain=explain,
            surgical=surgical,
            signing_key=signing_key,
            input_device=input_device,
        )
    if full_diag:
        return _run_voice_full_diag(non_interactive=non_interactive, surgical=surgical)

    report = _run_voice_preflight()
    _render_voice_report(report, output_json=output_json, device=device)

    # Mission C1 §T2.4 — surface the quarantine inventory + verdict-
    # derived reason class + remediation hint per entry. Greenfield
    # surface (the pre-mission doctor had ZERO quarantine awareness);
    # always renders even when empty so operators can verify that no
    # endpoint is silently quarantined under their nose.
    _render_voice_quarantine_surface(
        output_json=output_json,
        reason_filter=reason_filter,
    )

    # Mission C3 §T2.11 — surface the runtime failover-history ring.
    # Greenfield observability for the loop-in-place ladder iteration
    # (v0.45.2). Operators can now triage why a ladder exhausted from
    # the CLI without parsing structured logs.
    _render_voice_failover_history_surface(output_json=output_json)

    # Mission C4 §Phase 3 §T3.6 — surface the cross-axis degraded
    # banner state. Mirrors the dashboard's composite banner so
    # CLI-only operators see (a) which axes are currently degraded,
    # (b) the composite severity (warn / error / critical), (c) the
    # per-axis action chips the dashboard would render.
    _render_voice_degraded_banner_surface(output_json=output_json)

    # Mission C5 §T3.4 — surface dashboard bundle integrity alongside
    # the voice surfaces. Pure read; never blocks the doctor exit path.
    _render_dashboard_integrity_surface(output_json=output_json)

    # Mission C6 §T3.2 — surface LLM provider health alongside the voice
    # + dashboard surfaces. Pure read; never blocks the doctor exit path.
    _render_llm_health_surface(output_json=output_json)

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


_SURGICAL_ONLY_LAYERS = "A,C,D,E,J"


def _surgical_extra_args(*, non_interactive: bool, surgical: bool) -> tuple[str, ...]:
    """Build the bash diag extra_args tuple for the CLI.

    Combines `--non-interactive` (when stdin is non-TTY or operator
    opts in) with `--only A,C,D,E,J --skip-captures --skip-guardian
    --skip-operator-prompts` when `surgical=True`. The surgical layers
    cover hardware probe + ALSA + PipeWire + PortAudio + latency
    budget; the skip flags cut speech captures + the Temporal Guardian
    background follower + interactive prompts so the run lands at
    ~30s instead of the default 8-12 min.
    """
    args: list[str] = []
    if non_interactive:
        args.append("--non-interactive")
    if surgical:
        args.extend(
            [
                "--only",
                _SURGICAL_ONLY_LAYERS,
                "--skip-captures",
                "--skip-guardian",
                "--skip-operator-prompts",
            ]
        )
    return tuple(args)


def _run_voice_full_diag(*, non_interactive: bool, surgical: bool = False) -> int:
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

    extra_args = _surgical_extra_args(non_interactive=non_interactive, surgical=surgical)

    try:
        diag_result = run_full_diag(extra_args=extra_args, trigger="cli")
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
    surgical: bool = False,
    signing_key: Path | None = None,
    input_device: str | None = None,
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

    # Phase 5.T5.1 — voice prereq gate (STRICT v0.40.0).
    #
    # Detect the operator's "voice never configured" sentinel BEFORE
    # the 8-12 min diag pipeline runs:
    #
    # * Interactive shell -> auto-invoke ``run_voice_setup`` inline so
    #   the mic gets persisted to mind.yaml, then re-load mind_config
    #   and continue with the now-populated value.
    # * Non-interactive   -> hard error with
    #   :data:`EXIT_DOCTOR_VOICE_NOT_CONFIGURED` + structured ERROR log
    #   ``voice.calibrate.prereq_strict`` + a red operator banner listing
    #   THREE remediation paths (interactive setup / non-interactive
    #   setup / inline ``--input-device``). The v0.39.x LENIENT branch
    #   (WARN + heuristic fallback) is GONE — one-minor-cycle staged-
    #   adoption deprecation window closed at v0.40.0 ship.
    #
    # The mind_id passed in is already resolver-validated (Phase 1.T1.2)
    # so the load is guaranteed to find a mind.yaml on disk. A None
    # return from ``_load_mind_config_best_effort`` would indicate a
    # malformed YAML — fall through silently (the existing measurer
    # path tolerates it; out of scope per Mission §2 D5).
    from sovyx.engine.types import MindId as _MindId  # noqa: PLC0415

    data_dir = Path.home() / ".sovyx"
    prereq_mind_config = _load_mind_config_best_effort(data_dir, _MindId(mind_id))
    prereq_device_unset = (
        prereq_mind_config is not None
        and not (prereq_mind_config.voice_input_device_name or "").strip()
    )

    # Phase 5.T5.2 — ``--input-device`` escape hatch for non-interactive
    # operators with an unconfigured mic. Persists the choice via the
    # same ``run_voice_setup`` flow the dashboard + ``sovyx voice setup``
    # CLI command use, then re-loads ``prereq_mind_config`` so the gate
    # below sees the populated value and falls through silently. Lets
    # one-line CI scripts onboard a fresh mind with a single command
    # instead of chaining ``voice setup`` + ``calibrate``. No-op when
    # the mic is already configured OR the operator is on a TTY (the
    # inline picker handles that path).
    if prereq_device_unset and non_interactive and input_device:
        try:
            from sovyx.cli.commands.voice_setup import (  # noqa: PLC0415
                VoiceSetupError as _VoiceSetupError,
            )
            from sovyx.cli.commands.voice_setup import (  # noqa: PLC0415
                run_voice_setup as _run_voice_setup,
            )
            from sovyx.engine.types import MindId as _MindIdEscape  # noqa: PLC0415

            asyncio.run(
                _run_voice_setup(
                    mind_id=_MindIdEscape(mind_id),
                    data_dir=data_dir,
                    input_device=input_device,
                    non_interactive=True,
                )
            )
        except _VoiceSetupError as exc:
            console.print(
                f"\n[red]--input-device setup failed:[/red] {exc}\n"
                f"[dim]Re-run with a different --input-device value or "
                f"run `sovyx voice setup --mind-id {mind_id}` interactively.[/dim]"
            )
            return EXIT_DOCTOR_GENERIC_FAILURE
        # Re-load mind_config so the gate below sees the now-populated
        # voice_input_device_name.
        prereq_mind_config = _load_mind_config_best_effort(data_dir, _MindId(mind_id))
        prereq_device_unset = False
    if prereq_device_unset:
        if non_interactive:
            logger.error(
                "voice.calibrate.prereq_strict",
                mind_id=mind_id,
                action_required=(
                    "voice_input_device_name is empty for this mind. "
                    "Configure the mic via ONE of: "
                    "(a) `sovyx voice setup --mind-id <X>` (interactive), "
                    "(b) `sovyx voice setup --mind-id <X> --input-device 'NAME' "
                    "--non-interactive`, "
                    "(c) re-run this command with `--input-device 'NAME'` inline."
                ),
            )
            console.print(
                f"\n[red]ERROR:[/red] [bold]voice_input_device_name is empty"
                f"[/bold] for mind [bold]{mind_id}[/bold].\n\n"
                f"[bold]Remediation — pick one:[/bold]\n"
                f"  • Interactive:  [cyan]sovyx voice setup --mind-id {mind_id}"
                f"[/cyan]\n"
                f"  • Scripted:     [cyan]sovyx voice setup --mind-id {mind_id}"
                f" --input-device 'NAME' --non-interactive[/cyan]\n"
                f"  • Inline:       [cyan]sovyx doctor voice --calibrate"
                f" --input-device 'NAME' --non-interactive[/cyan]\n"
            )
            return EXIT_DOCTOR_VOICE_NOT_CONFIGURED
        console.print(
            f"\n[cyan]Voice is not yet configured for mind "
            f"[bold]{mind_id}[/bold]. Let's set up your mic first.[/cyan]\n"
        )
        try:
            from sovyx.cli.commands.voice_setup import (  # noqa: PLC0415
                VoiceSetupError,
                run_voice_setup,
            )
            from sovyx.engine.types import MindId  # noqa: PLC0415

            asyncio.run(
                run_voice_setup(
                    mind_id=MindId(mind_id),
                    data_dir=data_dir,
                    input_device=None,
                    non_interactive=False,
                )
            )
        except VoiceSetupError as exc:
            console.print(
                f"\n[red]Voice setup failed:[/red] {exc}\n"
                f"[dim]Aborting calibrate. Re-run `sovyx voice setup` "
                f"manually, then retry `sovyx doctor voice --calibrate`.[/dim]"
            )
            return EXIT_DOCTOR_GENERIC_FAILURE

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

    # Step 2: full diag (or surgical ~30s if opted-in).
    if surgical:
        console.print("\n[dim](2/6) Running surgical diag (~30s, --only A,C,D,E,J)...[/dim]")
    else:
        console.print("\n[dim](2/6) Running full diag (8-12 min, interactive)...[/dim]")
    extra_args = _surgical_extra_args(non_interactive=non_interactive, surgical=surgical)
    try:
        diag_result = run_full_diag(extra_args=extra_args, trigger="cli")
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
    # v0.31.5 LE-1 closure: resolve operator's active mic card so the
    # measurer probes THAT card (not hardcoded card 0). Pre-v0.31.5
    # operators with a USB headset on card 2 had R10's input data
    # always come from card 1 — the bug class operator hit on
    # 2026-05-08. ``mind_config`` may be None for headless CLI
    # invocations without mind context; the resolver returns None
    # defensively in that case + the measurer falls back to card 0.
    from sovyx.engine.types import MindId  # noqa: PLC0415

    mind_config = _load_mind_config_best_effort(
        Path.home() / ".sovyx",
        MindId(mind_id),
    )
    active_mic_card_index = resolve_active_mic_card(mind_config=mind_config)
    measurements = capture_measurements(
        diag_tarball_root=triage.tarball_root,
        triage_result=triage,
        duration_s=diag_result.duration_s,
        active_mic_card_index=active_mic_card_index,
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
    # v0.31.5 LE-1 closure: pass ``active_mic_card_index`` so
    # ``_apply_linux_mixer`` prefers the operator's active card
    # instead of ``candidates[0]``. Same resolver used above for
    # ``capture_measurements``.
    applier = CalibrationApplier(
        data_dir=data_dir,
        mind_yaml_path=data_dir / mind_id / "mind.yaml",
        signing_key_path=signing_key,
        active_mic_card_index=active_mic_card_index,
    )
    try:
        # CalibrationApplier.apply is async (P1+; runs handlers via
        # asyncio.to_thread). The CLI command is sync; use asyncio.run
        # to drive it. ``allow_medium=True`` mirrors the operator's
        # explicit ``--yes`` posture for ``sovyx doctor voice
        # --calibrate`` (the calibrate flow is opt-in by definition).
        # asyncio is module-level imported (line 31); the previous local
        # import was redundant + caused F823 when the Phase 4 prereq
        # gate also called asyncio.run earlier in the function.
        apply_result = asyncio.run(applier.apply(profile, dry_run=dry_run, allow_medium=True))
    except ApplyError as exc:
        console.print(f"\n[red]Calibration apply failed:[/red] {exc}")
        return EXIT_DOCTOR_GENERIC_FAILURE

    _render_calibration_verdict(profile, apply_result, explain=explain)
    return EXIT_DOCTOR_OK


def _run_voice_calibrate_show(*, mind_id: str, explain: bool) -> int:
    """Render the LAST persisted calibration profile (read-only).

    Loads ``<data_dir>/<mind_id>/calibration.json`` via
    :func:`load_calibration_profile` (LENIENT mode) and renders the
    same verdict block ``--calibrate`` produces. No diag, no engine,
    no mutation -- pure inspection.

    Returns:
        * EXIT_DOCTOR_OK on a clean render.
        * EXIT_DOCTOR_GENERIC_FAILURE when the profile cannot be
          loaded (missing, malformed, schema mismatch).
    """
    data_dir = Path.home() / ".sovyx"
    target = profile_path(data_dir=data_dir, mind_id=mind_id)
    console.print(f"\n[bold cyan]Voice calibration[/bold cyan] [dim](showing {target})[/dim]\n")
    try:
        profile = load_calibration_profile(data_dir=data_dir, mind_id=mind_id)
    except CalibrationProfileLoadError as exc:
        console.print(f"\n[red]Cannot load calibration profile:[/red] {exc}")
        return EXIT_DOCTOR_GENERIC_FAILURE

    advised = tuple(str(d.value) for d in profile.decisions if d.operation == "advise")
    inspect_result = ApplyResult(
        profile_path=target,
        applied_decisions=profile.applicable_decisions,
        skipped_decisions=tuple(
            d for d in profile.decisions if d not in profile.applicable_decisions
        ),
        advised_actions=advised,
        dry_run=True,
    )
    _render_calibration_verdict(profile, inspect_result, explain=explain)
    return EXIT_DOCTOR_OK


def _run_voice_calibrate_evaluate_rules(*, mind_id: str, explain: bool) -> int:
    """Dry-eval the calibration engine without running the diag or applying.

    P6 (v0.30.34) — Mission §10.2 #14. Captures the hardware
    fingerprint + builds a measurement snapshot from the mixer probe
    + invokes :class:`CalibrationEngine` with no triage input. Renders
    the rule trace + advised actions WITHOUT a tarball, triage, or
    apply — useful for triaging "would R30 fire on this hardware?"
    without paying the 8-12 min full-diag cost.

    Returns:
        * EXIT_DOCTOR_OK on a clean evaluation + render.
        * EXIT_DOCTOR_UNSUPPORTED on non-Linux hosts (fingerprint
          + measurer probes are Linux-specific: dmidecode + amixer +
          /sys/class/sound/* paths).
        * EXIT_DOCTOR_GENERIC_FAILURE on fingerprint capture failure
          (rare; mostly Linux-only mixer probe + dmidecode dependency).
    """
    # rc.6 (Agent 2 A.3): fail-fast on non-Linux hosts so the operator
    # gets a friendly message instead of a Python exception from the
    # Linux-only fingerprint + amixer probes. Mission §0 promises 5/6
    # are Linux-scoped; this flag inherits the same scope.
    if sys.platform != "linux":
        console.print(
            f"\n[red]--evaluate-rules is Linux-only[/red] (current platform: "
            f"{sys.platform!r}).\n"
            "The calibration engine probes Linux-specific surfaces "
            "(dmidecode, amixer, /sys/class/sound/*) which are not "
            "available on this host.\n"
            "On Windows / macOS, use `sovyx doctor voice` (cross-platform "
            "health checks) instead."
        )
        return EXIT_DOCTOR_UNSUPPORTED

    from sovyx.voice.calibration import (  # noqa: PLC0415 -- lazy import
        CalibrationEngine,
        capture_fingerprint,
    )
    from sovyx.voice.calibration._measurer import capture_measurements

    console.print(
        f"\n[bold cyan]Voice calibration rule evaluation[/bold cyan] "
        f"[dim](mind_id={mind_id}, dry-eval, no apply)[/dim]\n"
    )
    console.print("[dim](1/3) Capturing hardware fingerprint...[/dim]")
    try:
        fingerprint = capture_fingerprint()
    except Exception as exc:  # noqa: BLE001 -- broad to cover platform probes
        console.print(f"\n[red]Fingerprint capture failed:[/red] {exc}")
        return EXIT_DOCTOR_GENERIC_FAILURE

    console.print(
        f"[dim]    {fingerprint.system_vendor} {fingerprint.system_product} "
        f"({fingerprint.audio_stack})[/dim]"
    )

    console.print("[dim](2/3) Capturing measurement snapshot from probes...[/dim]")
    # Without a triage tarball, capture_measurements falls through to
    # the probe-only path (mixer state + null winner_hid). Engine sees
    # no triage_result so triage-gated rules (R10) won't fire — but
    # measurement-driven rules will, which is the point.
    # v0.31.5 LE-1: pass operator's active mic card so the measurer
    # probes the correct card (not hardcoded card 0). Resolver returns
    # None defensively when the operator hasn't completed the setup
    # wizard yet — preserves legacy card-0 behaviour.
    from sovyx.engine.types import MindId as _MindIdAlias  # noqa: PLC0415

    mind_config_for_dryeval = _load_mind_config_best_effort(
        Path.home() / ".sovyx",
        _MindIdAlias(mind_id),
    )
    active_mic_card_index = resolve_active_mic_card(mind_config=mind_config_for_dryeval)
    try:
        measurements = capture_measurements(
            diag_tarball_root=None,
            triage_result=None,
            duration_s=0.0,
            active_mic_card_index=active_mic_card_index,
        )
    except Exception as exc:  # noqa: BLE001 -- mixer probe rare-failure path
        console.print(f"\n[red]Measurement capture failed:[/red] {exc}")
        return EXIT_DOCTOR_GENERIC_FAILURE

    console.print("[dim](3/3) Evaluating rules...[/dim]")
    engine = CalibrationEngine()
    profile = engine.evaluate(
        mind_id=mind_id,
        fingerprint=fingerprint,
        measurements=measurements,
        triage_result=None,
    )

    console.print(
        f"[dim]    {len(profile.decisions)} decision(s) emitted by "
        f"{len(profile.provenance)} rule(s)[/dim]"
    )
    # Reuse the standard verdict renderer; pass a dummy ApplyResult
    # with dry_run=True so the operator sees the partition without
    # confusion that anything was applied.
    inspect_result = ApplyResult(
        profile_path=Path("/dev/null"),  # dry-eval: no path
        applied_decisions=profile.applicable_decisions,
        skipped_decisions=tuple(
            d for d in profile.decisions if d not in profile.applicable_decisions
        ),
        advised_actions=tuple(str(d.value) for d in profile.decisions if d.operation == "advise"),
        dry_run=True,
    )
    _render_calibration_verdict(profile, inspect_result, explain=explain)
    return EXIT_DOCTOR_OK


def _run_voice_calibrate_rollback(*, mind_id: str) -> int:
    """Restore the most-recent prior calibration profile (multi-generation chain).

    Walks the ``calibration.json.bak.{1,2,3}`` chain (rc.12 multi-
    generation backup). Each invocation consumes one generation:
    ``.bak.1`` becomes the canonical profile; ``.bak.2`` shifts to
    ``.bak.1``; ``.bak.3`` shifts to ``.bak.2``. Operator can roll
    back up to 3 prior calibrations in a row before the chain
    exhausts; once empty, ``--calibrate`` repopulates.

    Refuses to restore a malformed backup (validates JSON + schema
    BEFORE the swap), so the operator's voice config is never left
    pointing at a corrupt profile.

    Returns:
        * EXIT_DOCTOR_OK on successful rollback + render.
        * EXIT_DOCTOR_GENERIC_FAILURE when the chain is exhausted, or
          the backup is malformed (rollback refuses to restore corrupt
          state).
    """
    # Local import keeps the persistence dependency surface narrow at
    # the module level — only the rollback path needs the backup-listing
    # helper, and the rest of doctor.py operates without it.
    from sovyx.voice.calibration._persistence import (  # noqa: PLC0415
        list_calibration_backups,
    )

    data_dir = Path.home() / ".sovyx"
    console.print(
        f"\n[bold cyan]Voice calibration rollback[/bold cyan] [dim](mind_id={mind_id})[/dim]\n"
    )
    try:
        restored = rollback_calibration_profile(data_dir=data_dir, mind_id=mind_id)
    except CalibrationProfileRollbackError as exc:
        console.print(f"\n[red]Rollback failed:[/red] {exc}")
        return EXIT_DOCTOR_GENERIC_FAILURE

    remaining = len(list_calibration_backups(data_dir=data_dir, mind_id=mind_id))
    console.print(f"[green]Restored prior profile to[/green] {restored}")
    console.print(
        f"[dim]Backup chain: {remaining} generation"
        f"{'' if remaining == 1 else 's'} remaining "
        f"(re-run --calibrate to repopulate)[/dim]\n"
    )
    # Render the restored profile so the operator confirms what's now active.
    return _run_voice_calibrate_show(mind_id=mind_id, explain=False)


def _run_voice_calibrate_inspect_migration(*, mind_id: str) -> int:
    """Print the migrated calibration profile dict (post-schema-walk).

    Operator inspection mode for schema bumps: reads the raw JSON,
    walks the migration registry to bring the dict to the runtime's
    current ``CALIBRATION_PROFILE_SCHEMA_VERSION``, and emits the
    result to stdout (pretty-printed). Skips signature verification +
    profile dataclass construction, so the dict is suitable for
    diffing against the on-disk file but is NOT a fully-validated
    profile.

    Useful at schema-bump time: an operator running v0.31.x with a
    profile written under schema_version=1 can preview the v2 shape
    BEFORE upgrading by running this command on a Sovyx that supports
    v2. The output is exactly what ``load_calibration_profile`` would
    feed into ``_profile_from_dict`` had the load completed.

    Returns:
        * EXIT_DOCTOR_OK on success — migrated dict printed.
        * EXIT_DOCTOR_GENERIC_FAILURE when the file is missing,
          malformed, or the migration chain refuses (CalibrationProfile
          MigrationError subclasses CalibrationProfileLoadError).
    """
    data_dir = Path.home() / ".sovyx"
    console.print(
        f"\n[bold cyan]Voice calibration migration inspection[/bold cyan] "
        f"[dim](mind_id={mind_id})[/dim]\n"
    )
    try:
        migrated = inspect_migrated_profile_dict(data_dir=data_dir, mind_id=mind_id)
    except CalibrationProfileLoadError as exc:
        console.print(f"\n[red]Inspection failed:[/red] {exc}")
        return EXIT_DOCTOR_GENERIC_FAILURE

    rendered = json.dumps(migrated, indent=2, sort_keys=True, ensure_ascii=False)
    # Plain stdout (NOT console.print) so operators can pipe the output
    # into ``jq`` or ``diff``. Rich-rendering would inject ANSI escapes
    # that break shell pipelines.
    sys.stdout.write(rendered)
    sys.stdout.write("\n")
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

    # rc.7 (NEW.2/NEW.3) + rc.10 (Agent 2 fix #2): surface signing
    # status from apply_result.signed (disk-side truth) + suppress the
    # unsigned banner from the default path so non-technical operators
    # don't see scary "(LENIENT-loadable; STRICT rejects); pass
    # --signing-key" hints that punt them at a dev-only flag.
    #
    # Three branches:
    # * signed=True → green ✓ banner (operator's signing intent worked)
    # * signed=False AND signed_intent=True → yellow warning (operator
    #   passed --signing-key but signing failed mid-write — actionable)
    # * signed=False AND signed_intent=False → SILENT (default path;
    #   unsigned is the expected normal case for non-technical users)
    # * signed=None → dry_run; render nothing
    signed_status = getattr(apply_result, "signed", None)
    signed_intent = getattr(apply_result, "signed_intent", None)
    if signed_status is True:
        console.print(
            "\n[green]✓[/green] Profile is [bold green]signed[/bold green] "
            "(Ed25519). Loadable in STRICT mode."
        )
    elif signed_status is False and signed_intent is True:
        # Operator wanted signing but it failed — surface the actionable
        # warning so they know to investigate. This path is reachable
        # only when --signing-key was passed AND its load succeeded at
        # flag-parse (per rc.7 NEW.1 PEM fail-fast) AND signing failed
        # later in the persistence layer (rare; disk error or race).
        console.print(
            "\n[yellow][!][/yellow] Profile [bold yellow]could not be signed[/bold yellow] "
            "despite --signing-key being passed. The profile was persisted "
            "unsigned. Check $data_dir/logs/sovyx.log for "
            "voice.calibration.profile.signing_failed events."
        )
    # All other branches (default unsigned path, dry_run): render nothing.
    # Non-technical operators on the default path see a clean verdict
    # without dev-only flag suggestions.

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


def _render_voice_quarantine_surface(
    *,
    output_json: bool,
    reason_filter: str | None,
) -> None:
    """Mission C1 §T2.4 — render the "Voice — quarantined endpoints" section.

    Greenfield: pre-mission ``sovyx doctor voice`` had zero quarantine
    awareness; this helper queries the process-local
    :class:`EndpointQuarantine` (shared with the cascade + watchdog
    rechecker) and renders one row per live entry. Each row carries:

    * The friendly device name + endpoint GUID.
    * The verdict-derived reason class (``apo_degraded`` /
      ``vad_frontend_dead`` / ``format_mismatch`` / ``driver_silent``)
      with a graceful fallback to the legacy ``reason`` field for
      pre-mission entries.
    * The operator-facing remediation hint from
      :func:`diagnosis_user_remediation` (the same single-source-of-
      truth dict the dashboard's service-health card consumes).
    * Seconds-until-expiry so operators can correlate with the TTL knob.

    ``reason_filter`` (when non-empty) drops entries whose
    ``derived_reason`` / legacy ``reason`` doesn't match. Empty filter
    OR empty quarantine renders an empty section — silence is OK here,
    "no entries" is the typical (healthy) state.

    JSON mode (``output_json=True``) skips this surface entirely; the
    preflight JSON shape is consumed by external monitors that already
    have the dashboard quarantine endpoint, so duplicating the surface
    in the JSON envelope would just bloat the wire.
    """
    if output_json:
        return
    from sovyx.voice.health._quarantine import get_default_quarantine
    from sovyx.voice.health._user_remediation import diagnosis_user_remediation

    quarantine = get_default_quarantine()
    entries = quarantine.snapshot()
    if reason_filter:
        filter_value = reason_filter.strip()
        entries = tuple(
            entry
            for entry in entries
            if (entry.resolved_reason or entry.derived_reason or entry.reason) == filter_value
        )

    console.print("\n[bold]Voice — quarantined endpoints[/bold]")
    if not entries:
        if reason_filter:
            console.print(f"[dim]No quarantined endpoints match reason {reason_filter!r}.[/dim]")
        else:
            console.print("[dim]No endpoints in quarantine.[/dim]")
        return

    now = time.monotonic()
    for entry in entries:
        # Mission H3 §T3.2 — H3-canonical field-chain fallback.
        reason_key = entry.resolved_reason or entry.derived_reason or entry.reason or "unknown"
        seconds_left = max(0.0, entry.expires_at_monotonic - now)
        friendly = entry.device_friendly_name or entry.endpoint_guid or "(unknown)"
        console.print(f"  • [bold]{friendly}[/bold]  [dim](reason: {reason_key})[/dim]")
        hint = diagnosis_user_remediation(reason_key)
        if hint:
            console.print(f"    [dim]{hint}[/dim]")
        console.print(
            f"    [dim]Endpoint: {entry.endpoint_guid or '—'}  "
            f"Host API: {entry.host_api or '—'}  "
            f"Recheck in: {int(seconds_left)}s[/dim]"
        )


def _render_voice_failover_history_surface(
    *,
    output_json: bool,
    limit: int = 8,
) -> None:
    """Mission C3 §T2.11 — render the "Voice — failover history" section.

    Surfaces the most recent ``limit`` ladder runs from the process-local
    :class:`sovyx.voice.health._failover_history.FailoverHistoryRing`
    (populated by ``_try_runtime_failover`` per ladder complete).

    JSON mode skips this surface entirely (operators using JSON output
    consume the ``/api/voice/health/failover-history`` endpoint
    directly).

    Renders gracefully on a fresh-boot daemon where no ladder has yet
    run — prints a single ``[dim]No failover ladder has run yet.[/dim]``
    line.

    Args:
        output_json: When True, the surface is suppressed.
        limit: Max number of ladder runs to render. Default 8 covers
            the most recent operator-session timeframe without flooding
            the terminal.
    """
    if output_json:
        return
    try:
        from sovyx.voice.health._failover_history import get_default_failover_history
    except Exception as exc:  # noqa: BLE001 — observability-only surface
        console.print(
            f"[dim]Voice — failover history: unavailable ({exc}).[/dim]",
        )
        return

    ring = get_default_failover_history()
    entries = ring.entries()[:limit]

    console.print("\n[bold]Voice — failover history[/bold]")
    if not entries:
        console.print("[dim]No failover ladder has run yet on this daemon process.[/dim]")
        return

    for entry in entries:
        verdict_color = {
            "succeeded": "green",
            "exhausted": "red",
            "in_progress": "yellow",
        }.get(entry.verdict, "white")
        elapsed = f"{entry.elapsed_ms}ms" if entry.elapsed_ms is not None else "—"
        console.print(
            f"  • [bold]{entry.ladder_id}[/bold]  "
            f"[{verdict_color}]{entry.verdict}[/{verdict_color}]  "
            f"[dim](candidates={entry.candidates_tried}, elapsed={elapsed})[/dim]",
        )
        if entry.from_endpoint:
            console.print(f"    [dim]From: {entry.from_endpoint}[/dim]")
        for candidate in entry.candidates:
            cand_color = {
                "succeeded": "green",
                "failed": "red",
                "skipped": "yellow",
            }.get(candidate.verdict, "white")
            cand_elapsed = f"{candidate.elapsed_ms}ms" if candidate.elapsed_ms is not None else "—"
            extra = ""
            if candidate.error_class:
                extra = f" [dim]error_class={candidate.error_class}[/dim]"
            elif candidate.skipped_reason:
                extra = f" [dim]reason={candidate.skipped_reason}[/dim]"
            console.print(
                f"    {candidate.index}. "
                f"[{cand_color}]{candidate.verdict}[/{cand_color}]  "
                f"{candidate.target_endpoint}  [dim]({cand_elapsed})[/dim]" + extra,
            )


def _render_voice_degraded_banner_surface(
    *,
    output_json: bool,
) -> None:
    """Mission C4 §Phase 3 §T3.6 — render the "Voice — degraded banner" section.

    Surfaces the cross-axis :class:`EngineDegradedStore` snapshot +
    current operator-ack state from
    :class:`OperatorAcksStore` (when registered — Phase 3+ daemon).
    Pairs with the dashboard's composite banner so CLI-only operators
    get the same picture as the React surface.

    JSON mode skips this surface entirely (operators using JSON output
    consume ``/api/engine/degraded`` directly).

    Renders gracefully on a healthy daemon (no degraded axes) — prints
    a single ``[dim]No degraded axes.[/dim]`` line.

    Args:
        output_json: When True, the surface is suppressed.
    """
    if output_json:
        return
    try:
        from sovyx.engine._degraded_store import get_default_degraded_store
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[dim]Voice — degraded banner: unavailable ({exc}).[/dim]",
        )
        return

    entries = get_default_degraded_store().snapshot()
    console.print("\n[bold]Voice — degraded banner[/bold]")
    if not entries:
        console.print("[dim]No degraded axes.[/dim]")
        return

    # Severity counter (composite-severity per ADR-D6)
    distinct_axes = sorted({e.axis for e in entries})
    if len(distinct_axes) >= 3:
        composite_color = "red"
        composite_label = "CRITICAL"
    elif len(distinct_axes) == 2:
        composite_color = "red"
        composite_label = "ERROR"
    else:
        composite_color = "yellow"
        composite_label = "WARN"
    console.print(
        f"  [bold {composite_color}]{composite_label}[/bold {composite_color}]  "
        f"[dim]({len(distinct_axes)} axis(es) degraded: "
        f"{', '.join(distinct_axes)})[/dim]",
    )

    for entry in entries:
        sev_color = {
            "warn": "yellow",
            "error": "red",
            "critical": "red",
        }.get(entry.severity, "white")
        console.print(
            f"  • [bold]{entry.axis}[/bold]  "
            f"[{sev_color}]{entry.severity}[/{sev_color}]  "
            f"[dim]reason={entry.reason}[/dim]",
        )
        # Chips (operator-actionable next steps)
        for chip in entry.action_chips:
            console.print(
                f"    → [dim]{chip.action}[/dim]: {chip.target}",
            )


def _render_dashboard_integrity_surface(
    *,
    output_json: bool,
) -> None:
    """Mission C5 §T3.4 — render the "Dashboard — bundle integrity" section.

    Mirrors :func:`_render_voice_degraded_banner_surface` shape so the
    aggregate ``sovyx doctor`` (no args) renders this section alongside
    the voice quarantine / failover / degraded banner surfaces.

    JSON mode skips this surface entirely (operators using JSON output
    consume ``sovyx dashboard doctor --json`` directly).

    Renders gracefully on a healthy install — prints
    ``[green]✓[/green]  FULLY_PRESENT`` with reference + duration stats.

    Args:
        output_json: When True, the surface is suppressed.
    """
    if output_json:
        return
    try:
        from sovyx.dashboard import STATIC_DIR
        from sovyx.dashboard._integrity import BundleVerdict, scan_bundle_integrity
    except Exception as exc:  # noqa: BLE001 — observability-only surface
        console.print(
            f"[dim]Dashboard — bundle integrity: unavailable ({exc}).[/dim]",
        )
        return

    report = scan_bundle_integrity(STATIC_DIR)
    console.print("\n[bold]Dashboard — bundle integrity[/bold]")
    if report.verdict is BundleVerdict.FULLY_PRESENT:
        console.print(
            f"  [green]✓[/green]  FULLY_PRESENT  "
            f"[dim]({len(report.referenced_assets)} refs, "
            f"{report.scan_duration_ms:.1f}ms)[/dim]",
        )
        return

    severity_color = "yellow" if report.verdict is BundleVerdict.PARTIAL else "red"
    console.print(
        f"  [bold {severity_color}]{report.verdict.value.upper()}[/bold {severity_color}]  "
        f"[dim]static_dir={report.static_dir}[/dim]",
    )
    missing = list(report.missing_assets)
    if missing:
        sample_size = min(5, len(missing))
        for ref in missing[:sample_size]:
            console.print(f"    [dim]✗[/dim] {ref}")
        if len(missing) > sample_size:
            console.print(f"    [dim]… (+{len(missing) - sample_size} more)[/dim]")
    console.print(
        "  [dim]Run 'sovyx dashboard doctor' for the full report, or "
        "'pipx reinstall sovyx' to repair.[/dim]",
    )


def _render_llm_health_surface(*, output_json: bool) -> None:
    """Mission C6 §T3.2 — surface LLM provider health in aggregate ``sovyx doctor``.

    Pure read. Never blocks the doctor exit path. JSON mode is a no-op
    here because the aggregate ``sovyx doctor --json`` output already
    summarizes the voice surfaces; the LLM detail lives at
    ``sovyx llm doctor --json``.
    """
    if output_json:
        return
    try:
        import asyncio  # noqa: PLC0415 — lazy to keep doctor cold-path light
        import os  # noqa: PLC0415

        from sovyx.llm._provider_health import (  # noqa: PLC0415
            DiscoveryVerdict,
            scan_llm_provider_health,
        )
        from sovyx.llm.providers.ollama import OllamaProvider  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        console.print(f"[dim]LLM — provider health: unavailable ({exc}).[/dim]")
        return

    try:
        ollama = OllamaProvider()
        asyncio.run(ollama.ping())
        ollama_models: tuple[str, ...] = ()
        if ollama.is_available:
            try:
                ollama_models = tuple(asyncio.run(ollama.list_models()))
            except Exception:  # noqa: BLE001
                ollama_models = ()
        report = scan_llm_provider_health(
            env=os.environ,
            ollama_ping_result=ollama.is_available,
            ollama_models=ollama_models if ollama.is_available else None,
            default_provider="",
            default_model="",
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[dim]LLM — provider health: scan failed ({exc}).[/dim]")
        return

    console.print("\n[bold]LLM — provider health[/bold]")
    if report.verdict is DiscoveryVerdict.FULLY_AVAILABLE:
        console.print(
            f"  [green]✓[/green]  FULLY_AVAILABLE  "
            f"[dim]({report.available_count} provider(s), "
            f"{report.scan_duration_ms:.1f}ms)[/dim]",
        )
        return

    severity_color = {
        DiscoveryVerdict.NO_PROVIDER_CONFIGURED: "red",
        DiscoveryVerdict.ALL_PROVIDERS_UNHEALTHY: "red",
        DiscoveryVerdict.CLOUD_KEY_INVALID: "yellow",
        DiscoveryVerdict.DEFAULT_MODEL_UNAVAILABLE: "yellow",
        DiscoveryVerdict.OLLAMA_UNREACHABLE: "yellow",
        DiscoveryVerdict.OLLAMA_NO_MODELS: "yellow",
        DiscoveryVerdict.PARTIAL_HEALTH: "yellow",
    }.get(report.verdict, "yellow")
    console.print(
        f"  [bold {severity_color}]{report.verdict.value.upper()}[/bold {severity_color}]  "
        f"[dim](configured={report.configured_count}, "
        f"available={report.available_count})[/dim]",
    )
    failures = [
        (entry.name, entry.failure_reason)
        for entry in report.per_provider
        if entry.configured and entry.failure_reason
    ]
    for name, reason in failures[:5]:
        console.print(f"    [dim]✗[/dim] {name}: {reason}")
    console.print("  [dim]Run 'sovyx llm doctor' for the full per-provider matrix.[/dim]")


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


@doctor_app.command("voice_capture_apo")
@doctor_app.command("voice_capture_integrity")
def doctor_voice_capture_integrity(
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the DiagnosticResult as a single JSON object on stdout.",
    ),
) -> None:
    """Scan capture-chain integrity (Windows Voice Clarity APO + cross-platform).

    Mission H2 §T3.7 — renamed from ``voice_capture_apo`` to
    ``voice_capture_integrity``; the legacy ``voice_capture_apo``
    subcommand alias is preserved through v0.51.0 STRICT per ADR-D14
    so operator docs + bash diag scripts grepping the legacy name
    continue to resolve.

    Phase 5.C v0.32.6 — surfaces the existing
    :func:`sovyx.upgrade.doctor._check_voice_capture_apo` check as a
    standalone Typer subcommand. Multiple operator-facing docs
    (``faq.md``, ``modules/voice-troubleshooting-windows.md``,
    ``modules/voice.md``) and the bundled bash diag script
    (``voice/diagnostics/_bash/lib/G_sovyx.sh``) have referenced this
    subcommand syntax since v0.21.1; the underlying check existed but
    no Typer wire-up was ever added.

    Same backing function as the ``voice_capture_apo`` row in the
    default ``sovyx doctor`` output — running this subcommand directly
    skips the other 11 checks and prints just the result.

    Always exits ``0`` on non-Windows platforms (the check returns
    ``PASS`` with a "skipped" message — capture APOs are a Windows-only
    failure mode; Linux + macOS capture-chain integrity surfaces
    through the voice pipeline's bypass coordinator at runtime). On
    Windows, exit code reflects the severity:

    * ``0`` — ``PASS`` (no Voice Clarity APO active OR bypass armed)
    * ``1`` — ``WARN`` (Voice Clarity APO active without armed bypass,
      OR scan failed). Treat as actionable: the printed
      ``fix_suggestion`` lists the env-var to set.
    """
    from sovyx.upgrade.doctor import DiagnosticStatus, _check_voice_capture_apo

    result = _check_voice_capture_apo()

    if output_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        console = Console()
        status_style = {
            DiagnosticStatus.PASS: "green",
            DiagnosticStatus.WARN: "yellow",
            DiagnosticStatus.FAIL: "red",
        }[result.status]
        marker = {
            DiagnosticStatus.PASS: "✓",
            DiagnosticStatus.WARN: "⚠",
            DiagnosticStatus.FAIL: "✗",
        }[result.status]
        console.print(
            f"[bold {status_style}]{marker} {result.check}: {result.status.value.upper()}[/]"
        )
        console.print(f"  {result.message}")
        if result.fix_suggestion:
            console.print(f"  [dim]fix:[/] {result.fix_suggestion}")
        if result.details:
            # Per-endpoint scan + bypass_status — render selectively
            # rather than dumping the full dict (operators piping into
            # jq want --json).
            endpoints = result.details.get("endpoints")
            if isinstance(endpoints, list) and endpoints:
                console.print("  endpoints:")
                for ep in endpoints:
                    if isinstance(ep, dict):
                        name = ep.get("endpoint_name") or ep.get("endpoint_id") or "?"
                        active = ep.get("voice_clarity_active", False)
                        # Mission H2 §T3.7 — render platform-neutral
                        # marker on non-Windows; on Windows the
                        # "APO active" verbiage is canonical (Voice
                        # Clarity IS APO).
                        if sys.platform == "win32":
                            marker = "⚠ APO active" if active else "✓ clean"
                        else:
                            marker = "⚠ capture-chain processing active" if active else "✓ clean"
                        console.print(f"    - {name!s} ({marker})")
            bypass_status = result.details.get("bypass_status")
            if isinstance(bypass_status, list) and bypass_status:
                console.print("  bypass_status:")
                for tier in bypass_status:
                    if isinstance(tier, dict):
                        name = tier.get("name", "?")
                        enabled = tier.get("enabled", False)
                        console.print(f"    - {name}: {'ON' if enabled else 'off'}")

    raise typer.Exit(0 if result.status == DiagnosticStatus.PASS else 1)


# Mission H2 §T3.7 — legacy function name preserved for backwards-compat
# tests that imported it. The decorator stack above registers both
# subcommand names; this alias preserves import-path stability.
doctor_voice_capture_apo = doctor_voice_capture_integrity


@doctor_app.command("piper_locale_match")
def doctor_piper_locale_match(
    language: str | None = typer.Option(
        None,
        "--language",
        "-l",
        help="BCP-47 locale to check (e.g. 'pt-BR', 'en-US'). When "
        "omitted, falls back to 'en-US' so the probe is runnable "
        "without an active mind context.",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the DiagnosticResult as a single JSON object on stdout.",
    ),
) -> None:
    """Check whether a locale has a curated Piper voice (F2-M03↑ §3.F).

    Surfaces gaps between the mind's spoken language and Sovyx's
    curated Piper voice catalog. Useful before installing a mind for
    a new locale, or after an upgrade that may have changed catalog
    coverage.

    LENIENT default per ``feedback_staged_adoption``:

    * ``0`` — ``PASS`` (catalog hit; the factory will download +
      load the locale-matched voice).
    * ``1`` — ``WARN`` (no curated voice; the factory falls back to
      ``tuning.piper_default_voice`` — English. Voice still works
      but in the wrong language.).

    STRICT promotion (WARN → FAIL) is deferred to a follow-up cycle
    gated on operator telemetry — see the TODO in
    :func:`sovyx.upgrade.doctor._check_piper_locale_match`.
    """
    from sovyx.upgrade.doctor import DiagnosticStatus, _check_piper_locale_match

    result = _check_piper_locale_match(language=language)

    if output_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        console = Console()
        status_style = {
            DiagnosticStatus.PASS: "green",
            DiagnosticStatus.WARN: "yellow",
            DiagnosticStatus.FAIL: "red",
        }[result.status]
        marker = {
            DiagnosticStatus.PASS: "✓",
            DiagnosticStatus.WARN: "⚠",
            DiagnosticStatus.FAIL: "✗",
        }[result.status]
        console.print(
            f"[bold {status_style}]{marker} {result.check}: {result.status.value.upper()}[/]",
        )
        console.print(f"  {result.message}")
        if result.fix_suggestion:
            console.print(f"  [dim]fix:[/] {result.fix_suggestion}")
        if result.details:
            voice = result.details.get("piper_voice")
            if voice:
                console.print(f"  [dim]voice:[/] {voice}")

    raise typer.Exit(0 if result.status == DiagnosticStatus.PASS else 1)


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


@doctor_app.command("resources")
def doctor_resources(
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the resource snapshot as a JSON object on stdout.",
    ),
    cohort: str | None = typer.Option(
        None,
        "--cohort",
        help="Filter the table to one cohort section "
        "(process / asyncio / to_thread / lock_dict / onnx / gc / "
        "tracemalloc / exception_cohort).",
    ),
) -> None:
    """Mission H4 §T3.2 — render the engine resource-cohort snapshot.

    Operator-facing surface for the Phase 1.A + 1.B in-process
    :class:`ResourceRegistry`. Renders the same fields that fire on
    every ``self.health.snapshot`` log record, but as a one-shot
    structured table — operators no longer need to grep raw JSON Lines
    to inspect ONNX session counts, LRULockDict cardinality, or
    asyncio.to_thread dispatch totals.

    Use ``--cohort to_thread`` to scope to one section; use ``--json``
    for piped consumption (Grafana exporters, CI checks).

    Returns 0 unconditionally — diagnostic-only; the Phase 1.D
    ResourceCohortGovernor will introduce non-zero exit semantics
    when a cohort breaches budget.
    """
    from sovyx.observability._resource_registry import (
        _HEALTH_SNAPSHOT_FIELDS,
        get_default_resource_registry,
    )

    fields = get_default_resource_registry().snapshot_fields()

    if output_json:
        # Stable shape for piped consumers: dotted-key dict mirroring
        # the structured-log envelope.
        print(json.dumps(fields, indent=2, default=str))
        return

    console = Console()
    sections: dict[str, list[tuple[str, object]]] = {}
    for key, value in fields.items():
        spec = _HEALTH_SNAPSHOT_FIELDS.get(key)
        section = spec.section if spec else "other"
        sections.setdefault(section, []).append((key, value))

    if cohort is not None and cohort not in sections:
        console.print(
            f"[yellow]No fields registered under cohort '{cohort}'.[/]"
            f"  Known cohorts: {', '.join(sorted(sections))}",
        )
        return

    for section, entries in sorted(sections.items()):
        if cohort is not None and section != cohort:
            continue
        table = Table(
            title=f"Engine resources — {section}",
            show_lines=False,
            show_header=True,
        )
        table.add_column("field", min_width=30)
        table.add_column("value")
        for key, value in sorted(entries):
            table.add_row(key, str(value))
        console.print(table)


__all__ = ["doctor_app"]
