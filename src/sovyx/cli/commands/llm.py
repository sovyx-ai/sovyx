"""CLI command: sovyx llm — provider health doctor + interactive setup wizard.

Mission C6 §T3.1.

Subcommands:

* ``sovyx llm doctor [--json]`` — run :func:`scan_llm_provider_health`
  against the live process env + a fresh Ollama ping, render the verdict
  + per-provider matrix + remediation hint. Exits 0 on ``FULLY_AVAILABLE``
  / ``PARTIAL_HEALTH``; 1 on degraded verdicts. Mirrors the existing
  ``sovyx dashboard doctor`` shape (Mission C5 §T3.3) for cross-mission
  consistency.

* ``sovyx llm health`` — alias for ``doctor`` (operator-facing language).

* ``sovyx llm setup`` — interactive wizard: prompts for provider choice
  → API key (hidden input for cloud) → validates via the shared
  ``cli._provider_setup_shared`` helpers → persists to
  ``<data_dir>/secrets.env``. ``--non-interactive --provider <name>
  --api-key <key>`` for CI / scripted onboarding.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from sovyx.cli._provider_setup_shared import (
    create_provider,
    persist_api_key,
    test_provider,
)
from sovyx.llm._provider_health import (
    DiscoveryVerdict,
    scan_llm_provider_health,
)
from sovyx.llm._provider_registry import LLMProviderKey

if TYPE_CHECKING:
    from sovyx.llm._provider_health import LLMRouterDiscoveryReport


console = Console()
llm_app = typer.Typer(help="LLM provider health + setup")


def resolve_mind_llm_defaults(
    mind_id: str | None,
    *,
    data_dir: Path | None = None,
) -> tuple[str, str]:
    """Resolve the target mind's ``(llm.default_provider, llm.default_model)``.

    DOCTOR-4 closure (AP #71 class) — pre-fix the CLI doctors passed
    ``default_provider=""``/``default_model=""`` to
    :func:`scan_llm_provider_health`, making the
    ``DEFAULT_MODEL_UNAVAILABLE`` and configured-default
    ``OLLAMA_UNREACHABLE`` verdicts structurally unreachable: a mind
    pinned to a nonexistent Ollama model still showed
    ``FULLY_AVAILABLE`` (reachability-only probe). This helper loads
    the real values from the mind's ``mind.yaml`` via the same
    :func:`~sovyx.engine._rpc_handlers._load_mind_config_best_effort`
    resolver the daemon RPC handlers use.

    Resolution semantics:

    * ``mind_id`` explicit → :func:`resolve_mind_id` semantics: an
      unknown mind raises ``typer.BadParameter`` LOUDLY (AP #48 —
      silently scanning with empty defaults would be a semantic lie).
    * ``mind_id`` omitted → best-effort: exactly one mind on disk
      resolves to it; zero or 2+ minds (ambiguous) degrade to
      ``("", "")`` so the scan keeps its pre-fix env-only behaviour.
    * Any config-load failure degrades to ``("", "")``.

    Args:
        mind_id: Raw ``--mind-id`` flag value (``None`` when omitted).
        data_dir: Override for tests / callers that already resolved
            it. ``None`` → ``EngineConfig().data_dir`` (env-driven),
            falling back to ``~/.sovyx``.

    Returns:
        ``(default_provider, default_model)`` — both ``""`` when no
        mind context could be resolved.
    """
    from sovyx.cli._mind_resolver import resolve_mind_id
    from sovyx.engine._rpc_handlers import _load_mind_config_best_effort
    from sovyx.engine.types import MindId

    if data_dir is None:
        try:
            from sovyx.engine.config import EngineConfig

            data_dir = EngineConfig().data_dir
        except Exception:  # noqa: BLE001 — best-effort resolution
            data_dir = Path.home() / ".sovyx"

    if mind_id is not None:
        # Explicit flag — fail loudly on typos (raises typer.BadParameter).
        resolved = resolve_mind_id(mind_id, data_dir)
    else:
        try:
            resolved = resolve_mind_id(None, data_dir)
        except typer.BadParameter:
            # 0 minds (fresh install) or 2+ minds (ambiguous without
            # --mind-id) — no single mind context; scan env-only.
            return "", ""

    config = _load_mind_config_best_effort(data_dir, MindId(str(resolved)))
    if config is None:
        return "", ""
    return config.llm.default_provider, config.llm.default_model


async def _gather_live_report(
    *,
    default_provider: str = "",
    default_model: str = "",
) -> LLMRouterDiscoveryReport:
    """Run the scanner against live os.environ + a fresh Ollama ping.

    No I/O outside the Ollama ping (anti-pattern #14 — bounded with the
    provider's 2-second internal timeout).

    Args:
        default_provider: The target mind's ``llm.default_provider``
            (from :func:`resolve_mind_llm_defaults`). ``""`` disables
            the default-model verdicts — pre-DOCTOR-4 this was
            hardcoded ``""``, starving the verdict machinery.
        default_model: The target mind's ``llm.default_model``.
    """
    import os

    from sovyx.llm.providers.ollama import OllamaProvider

    ollama = OllamaProvider()
    await ollama.ping()
    ollama_models: tuple[str, ...] = ()
    if ollama.is_available:
        try:
            ollama_models = tuple(await ollama.list_models())
        except Exception:  # noqa: BLE001
            ollama_models = ()

    return scan_llm_provider_health(
        env=os.environ,
        ollama_ping_result=ollama.is_available,
        ollama_models=ollama_models if ollama.is_available else None,
        default_provider=default_provider,
        default_model=default_model,
    )


def _report_to_json_dict(report: LLMRouterDiscoveryReport) -> dict[str, object]:
    return {
        "verdict": report.verdict.value,
        "configured_count": report.configured_count,
        "available_count": report.available_count,
        "default_provider": report.default_provider,
        "default_model": report.default_model,
        "scan_duration_ms": round(report.scan_duration_ms, 3),
        "per_provider": [
            {
                "name": entry.name,
                "env_var": entry.env_var,
                "is_cloud": entry.is_cloud,
                "configured": entry.configured,
                "reachable": entry.reachable,
                "key_valid": entry.key_valid,
                "failure_reason": entry.failure_reason,
            }
            for entry in report.per_provider
        ],
    }


_VERDICT_COLOR: dict[DiscoveryVerdict, str] = {
    DiscoveryVerdict.FULLY_AVAILABLE: "green",
    DiscoveryVerdict.PARTIAL_HEALTH: "yellow",
    DiscoveryVerdict.OLLAMA_NO_MODELS: "yellow",
    DiscoveryVerdict.OLLAMA_UNREACHABLE: "yellow",
    DiscoveryVerdict.DEFAULT_MODEL_UNAVAILABLE: "yellow",
    DiscoveryVerdict.CLOUD_KEY_INVALID: "red",
    DiscoveryVerdict.ALL_PROVIDERS_UNHEALTHY: "red",
    DiscoveryVerdict.NO_PROVIDER_CONFIGURED: "red",
}


_VERDICT_REMEDIATION: dict[DiscoveryVerdict, str] = {
    DiscoveryVerdict.FULLY_AVAILABLE: "",
    DiscoveryVerdict.PARTIAL_HEALTH: (
        "Some providers are degraded but routing continues. Check the per-"
        "provider matrix and address the unhealthy entries."
    ),
    DiscoveryVerdict.NO_PROVIDER_CONFIGURED: (
        "No LLM provider is configured. Run 'sovyx llm setup' to onboard a "
        "cloud key OR install Ollama (https://ollama.ai) for a local fallback."
    ),
    DiscoveryVerdict.OLLAMA_UNREACHABLE: (
        "Ollama was previously configured as default but the daemon is not "
        "reachable. Start it with 'ollama serve' (or restart the service)."
    ),
    DiscoveryVerdict.OLLAMA_NO_MODELS: (
        "Ollama is running but has no models installed. Pull one with "
        "'ollama pull llama3.1' (or any model name from https://ollama.ai/library)."
    ),
    DiscoveryVerdict.CLOUD_KEY_INVALID: (
        "Every configured cloud key failed validation. Open the dashboard "
        "settings to rotate the keys, or re-run 'sovyx llm setup'."
    ),
    DiscoveryVerdict.ALL_PROVIDERS_UNHEALTHY: (
        "Every configured provider is currently unreachable. Check network "
        "connectivity + the dashboard provider-settings 'Test connection' button."
    ),
    DiscoveryVerdict.DEFAULT_MODEL_UNAVAILABLE: (
        "The configured default model cannot be served by any available "
        "provider. Update mind.yaml or open the dashboard settings."
    ),
}


def _print_doctor_report(report: LLMRouterDiscoveryReport) -> None:
    color = _VERDICT_COLOR[report.verdict]
    console.print()
    console.print("[bold]Sovyx LLM — provider health[/bold]")
    if report.verdict is DiscoveryVerdict.FULLY_AVAILABLE:
        console.print(
            f"  [green]✓[/green]  FULLY_AVAILABLE  "
            f"[dim]({report.available_count} provider(s) available, "
            f"{report.scan_duration_ms:.1f}ms)[/dim]",
        )
    else:
        console.print(
            f"  [bold {color}]{report.verdict.value.upper()}[/bold {color}]  "
            f"[dim](configured={report.configured_count}, "
            f"available={report.available_count})[/dim]",
        )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Env var")
    table.add_column("Configured")
    table.add_column("Reachable")
    table.add_column("Failure")
    for entry in report.per_provider:
        configured = "[green]yes[/green]" if entry.configured else "[dim]no[/dim]"
        if entry.reachable is True:
            reachable = "[green]yes[/green]"
        elif entry.reachable is False:
            reachable = "[red]no[/red]"
        else:
            reachable = "[dim]unprobed[/dim]"
        failure = entry.failure_reason or ""
        table.add_row(
            entry.name,
            entry.env_var or "[dim](local)[/dim]",
            configured,
            reachable,
            failure,
        )
    console.print(table)

    remediation = _VERDICT_REMEDIATION[report.verdict]
    if remediation:
        console.print(f"\n  [dim]{remediation}[/dim]")
    console.print()


@llm_app.command("doctor")
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
    mind_id: str | None = typer.Option(
        None,
        "--mind-id",
        help="Mind whose llm.default_provider/default_model the scan "
        "checks (DEFAULT_MODEL_UNAVAILABLE / configured-default "
        "verdicts). Default: auto-detected when exactly one mind "
        "exists under <data_dir>/; with zero or multiple minds the "
        "scan runs env-only (no default-model check).",
    ),
) -> None:
    """Verify LLM provider health (Mission C6 §T3.1).

    Runs :func:`scan_llm_provider_health` against the live process env +
    a fresh Ollama ping + the target mind's configured
    ``llm.default_provider``/``default_model`` (DOCTOR-4 — pre-fix the
    defaults were hardcoded ``""``, so a mind pinned to a nonexistent
    model still reported FULLY_AVAILABLE), prints the verdict +
    per-provider matrix + remediation hint, and exits with code 1 on
    any non-healthy verdict.

    Use ``--json`` for machine-readable output (e.g.
    ``sovyx llm doctor --json | jq '.verdict'``).
    """
    default_provider, default_model = resolve_mind_llm_defaults(mind_id)
    report = asyncio.run(
        _gather_live_report(
            default_provider=default_provider,
            default_model=default_model,
        ),
    )
    if json_output:
        console.print_json(json.dumps(_report_to_json_dict(report), sort_keys=True))
    else:
        _print_doctor_report(report)
    if report.verdict not in (
        DiscoveryVerdict.FULLY_AVAILABLE,
        DiscoveryVerdict.PARTIAL_HEALTH,
    ):
        raise typer.Exit(1)


@llm_app.command("health")
def health(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
    mind_id: str | None = typer.Option(
        None,
        "--mind-id",
        help="Mind whose llm defaults the scan checks (see `sovyx llm doctor --help`).",
    ),
) -> None:
    """Alias for ``sovyx llm doctor`` (operator-facing language)."""
    doctor(json_output=json_output, mind_id=mind_id)


def _provider_choice_prompt() -> LLMProviderKey:
    console.print("\n[bold]Choose an LLM provider:[/bold]")
    members = list(LLMProviderKey)
    for idx, key in enumerate(members, start=1):
        marker = "[dim](local)[/dim]" if not key.is_cloud else f"[dim]({key.env_var})[/dim]"
        console.print(f"  [{idx}] {key.value} {marker}")
    while True:
        choice = typer.prompt("Provider number", default="1").strip()
        try:
            idx = int(choice)
            if 1 <= idx <= len(members):
                return members[idx - 1]
        except ValueError:
            pass
        console.print(f"[red]Invalid choice — pick 1..{len(members)}[/red]")


def _setup_cloud_provider(
    provider_key: LLMProviderKey,
    api_key: str | None,
    *,
    data_dir: Path,
    interactive: bool,
) -> int:
    """Validate + persist a cloud-provider API key. Returns process exit code."""
    if not api_key and interactive:
        api_key = typer.prompt(
            f"API key for {provider_key.value}",
            hide_input=True,
        ).strip()
    if not api_key:
        console.print(
            f"[red]✗ {provider_key.value} requires an API key.[/red] "
            "Use --api-key in --non-interactive mode.",
        )
        return 2

    provider_instance = create_provider(provider_key.value, api_key)
    if provider_instance is None:
        console.print(
            f"[red]✗ Failed to instantiate provider '{provider_key.value}'.[/red]",
        )
        return 1

    console.print("[dim]Probing the provider...[/dim]")
    ok, message = asyncio.run(test_provider(provider_instance))
    if not ok:
        console.print(f"[red]✗ Validation failed:[/red] {message}")
        return 1

    secrets_path = persist_api_key(data_dir, provider_key.env_var, api_key)
    console.print(
        f"[green]✓ {provider_key.value} configured.[/green] Key persisted to {secrets_path}.",
    )
    console.print(
        "[dim]Run 'sovyx start' to load the new key, or restart the daemon "
        "if it is already running.[/dim]",
    )
    return 0


def _setup_ollama() -> int:
    """Ollama setup — no key required. Just verify the daemon + list models."""
    from sovyx.llm.providers.ollama import OllamaProvider

    ollama = OllamaProvider()
    console.print("[dim]Pinging Ollama daemon...[/dim]")
    asyncio.run(ollama.ping())
    if not ollama.is_available:
        console.print(
            "[red]✗ Ollama is not reachable.[/red] "
            "Install from https://ollama.ai and run 'ollama serve'.",
        )
        return 1
    try:
        models = asyncio.run(ollama.list_models())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ Ollama reachable but list_models failed:[/red] {exc}")
        return 1
    if not models:
        console.print(
            "[yellow]⚠ Ollama is reachable but has no models installed.[/yellow] "
            "Pull one with 'ollama pull llama3.1' (or any model name from "
            "https://ollama.ai/library).",
        )
        return 1
    console.print(
        f"[green]✓ Ollama is reachable with {len(models)} model(s).[/green]",
    )
    for m in models[:10]:
        console.print(f"  [dim]•[/dim] {m}")
    if len(models) > 10:
        console.print(f"  [dim]… (+{len(models) - 10} more)[/dim]")
    return 0


@llm_app.command("setup")
def setup(
    provider: str | None = typer.Option(
        None,
        "--provider",
        "-p",
        help="Provider canonical name (e.g. 'anthropic', 'ollama').",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        "-k",
        help="API key (cloud providers only). Required when --non-interactive.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Fail-fast on missing inputs instead of prompting (CI / scripted use).",
    ),
    data_dir: Path | None = typer.Option(  # noqa: B008 — typer requires call in default
        None,
        "--data-dir",
        help="Engine data directory (where secrets.env is written). Defaults to ~/.sovyx.",
    ),
) -> None:
    """Interactive (or scripted) LLM provider setup (Mission C6 §T3.1).

    Walks the operator through choosing a provider, entering the API
    key, validating the key against the provider's API, and persisting
    the env-var to ``<data_dir>/secrets.env`` with ``0o600`` permissions.

    Use ``--non-interactive --provider <name> --api-key <key>`` to script
    the setup in CI or automated tooling.
    """
    interactive = not non_interactive
    resolved_data_dir = data_dir if data_dir is not None else Path.home() / ".sovyx"

    if provider:
        try:
            provider_key = LLMProviderKey(provider)
        except ValueError as exc:
            valid = ", ".join(key.value for key in LLMProviderKey)
            console.print(
                f"[red]✗ Unknown provider '{provider}'.[/red] Valid: {valid}",
            )
            raise typer.Exit(2) from exc
    elif interactive:
        provider_key = _provider_choice_prompt()
    else:
        console.print(
            "[red]✗ --provider is required in --non-interactive mode.[/red]",
        )
        raise typer.Exit(2)

    if provider_key is LLMProviderKey.OLLAMA:
        exit_code = _setup_ollama()
    else:
        exit_code = _setup_cloud_provider(
            provider_key,
            api_key,
            data_dir=resolved_data_dir,
            interactive=interactive,
        )
    if exit_code != 0:
        raise typer.Exit(exit_code)
