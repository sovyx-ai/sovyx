"""CLI command: sovyx dashboard — show dashboard access info + integrity doctor."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from sovyx.dashboard import STATIC_DIR
from sovyx.dashboard._integrity import (
    BundleIntegrityReport,
    BundleVerdict,
    scan_bundle_integrity,
)
from sovyx.dashboard.server import TOKEN_FILE

console = Console()
dashboard_app = typer.Typer(help="Dashboard management")


@dashboard_app.callback(invoke_without_command=True)
def dashboard_info(
    ctx: typer.Context,
    show_token: bool = typer.Option(False, "--token", "-t", help="Show the auth token"),
) -> None:
    """Show dashboard access information."""
    if ctx.invoked_subcommand is not None:
        return

    # Read config for host/port (defaults)
    host = "127.0.0.1"
    port = 7777

    config_path = Path.home() / ".sovyx" / "system.yaml"
    if config_path.exists():
        try:
            import yaml

            with config_path.open() as f:
                cfg = yaml.safe_load(f) or {}
            api_cfg = cfg.get("api", {})
            host = api_cfg.get("host", host)
            port = api_cfg.get("port", port)
        except Exception:  # noqa: BLE001  # nosec B110 — best-effort config read; defaults are safe
            pass

    url = f"http://{host}:{port}"

    console.print()
    console.print("[bold]Sovyx Dashboard[/bold]")
    console.print()
    console.print(f"  URL:   [link={url}]{url}[/link]")

    if show_token:
        if TOKEN_FILE.exists():
            token = TOKEN_FILE.read_text().strip()
            console.print(f"  Token: [dim]{token}[/dim]")
        else:
            console.print("  Token: [yellow]Not generated yet (start sovyx first)[/yellow]")
    else:
        console.print("  Token: [dim]use --token to reveal[/dim]")

    console.print()
    console.print("[dim]Start the daemon with 'sovyx start' to access the dashboard.[/dim]")
    console.print()


def _report_to_json_dict(report: BundleIntegrityReport) -> dict[str, object]:
    """JSON-safe serialization of :class:`BundleIntegrityReport`."""
    return {
        "verdict": str(report.verdict.value),
        "static_dir": str(report.static_dir.as_posix()),
        "index_html_path": str(report.index_html_path.as_posix()),
        "referenced_count": len(report.referenced_assets),
        "missing_count": len(report.missing_assets),
        "orphan_count": len(report.orphan_assets),
        "missing_assets": list(report.missing_assets),
        "orphan_assets": list(report.orphan_assets),
        "scan_duration_ms": round(float(report.scan_duration_ms), 3),
    }


def _print_doctor_report(report: BundleIntegrityReport) -> None:
    """Human-readable rendering of the integrity report.

    Mission C5 §T3.3 — mirrors the C4 ``_render_voice_degraded_banner_surface``
    structure: bold section header + severity-colored verdict line +
    bulleted missing list + actionable remediation hint.
    """
    console.print()
    console.print("[bold]Sovyx Dashboard — bundle integrity[/bold]")
    if report.verdict is BundleVerdict.FULLY_PRESENT:
        console.print(
            f"  [green]✓[/green]  FULLY_PRESENT  "
            f"[dim]({len(report.referenced_assets)} refs, "
            f"{report.scan_duration_ms:.1f}ms)[/dim]",
        )
        console.print()
        return

    severity_color = "red" if report.verdict is not BundleVerdict.PARTIAL else "yellow"
    console.print(
        f"  [bold {severity_color}]{report.verdict.value.upper()}[/bold {severity_color}]  "
        f"[dim]static_dir={report.static_dir}[/dim]",
    )
    missing = list(report.missing_assets)
    if missing:
        console.print(f"\n  [bold]Missing chunks ({len(missing)}):[/bold]")
        for ref in missing[:20]:
            console.print(f"    [dim]✗[/dim] {ref}")
        if len(missing) > 20:
            console.print(f"    [dim]… (+{len(missing) - 20} more)[/dim]")
    console.print()
    if report.verdict is BundleVerdict.PARTIAL:
        console.print(
            "  [dim]Some referenced chunks are absent on disk. Run 'pipx "
            "reinstall sovyx' to restore the bundle, OR 'npm run build' if "
            "developing from a checkout.[/dim]",
        )
    elif report.verdict is BundleVerdict.INDEX_HTML_MISSING:
        console.print(
            "  [dim]The dashboard entry point is missing. Run 'pipx "
            "reinstall sovyx' to recover a complete wheel, OR 'npm run "
            "build' if developing.[/dim]",
        )
    elif report.verdict is BundleVerdict.STATIC_DIR_MISSING:
        console.print(
            "  [dim]The static directory is absent entirely. Run 'pipx "
            "reinstall sovyx', OR 'npm run build' if developing.[/dim]",
        )
    elif report.verdict is BundleVerdict.LEGACY_INDEX_HTML_NO_ASSETS:
        console.print(
            "  [dim]The entry point exists but the assets directory is "
            "empty (typically a stale or interrupted build). Run 'npm "
            "run build' in the dashboard/ checkout.[/dim]",
        )
    console.print()


@dashboard_app.command("doctor")
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Verify the dashboard SPA bundle integrity (Mission C5 §T3.3).

    Runs the integrity scanner against the installed static directory,
    prints a verdict + missing list + remediation hint, and exits with
    code 1 on any non-``FULLY_PRESENT`` verdict.

    Use ``--json`` to emit the report as JSON for piping to tools (e.g.
    ``sovyx dashboard doctor --json | jq '.verdict'``).
    """
    report = scan_bundle_integrity(STATIC_DIR)
    if json_output:
        console.print_json(json.dumps(_report_to_json_dict(report), sort_keys=True))
    else:
        _print_doctor_report(report)
    if report.verdict is not BundleVerdict.FULLY_PRESENT:
        raise typer.Exit(1)
