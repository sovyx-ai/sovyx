"""sovyx plugin — manage plugins (install, list, info, enable, disable, remove).

Usage::

    sovyx plugin list                       # list all plugins
    sovyx plugin info weather               # detailed plugin info
    sovyx plugin install ./my-plugin        # install from local dir
    sovyx plugin install git+https://...    # install from git URL
    sovyx plugin install some-plugin        # install from pip
    sovyx plugin enable weather             # enable a disabled plugin
    sovyx plugin disable weather            # disable a plugin
    sovyx plugin remove weather             # remove a plugin

Ref: SPE-008 Appendix C.6
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

console = Console()

plugin_app = typer.Typer(
    name="plugin",
    help="Plugin management commands.",
    no_args_is_help=True,
)


def _plugins_dir() -> Path:
    """Get the plugins directory."""
    return Path.home() / ".sovyx" / "plugins"


def _mind_yaml_path() -> Path:
    """Get the default mind.yaml path."""
    return Path.home() / ".sovyx" / "mind.yaml"


# ── List ────────────────────────────────────────────────────────────


@plugin_app.command("list")
def plugin_list() -> None:
    """List all installed plugins with status and tool count."""
    plugins_dir = _plugins_dir()
    if not plugins_dir.exists():
        console.print("[dim]No plugins installed.[/dim]")
        return

    table = Table(title="Installed Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Version", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Tools", justify="right")
    table.add_column("Description")

    found = False
    for plugin_dir in sorted(plugins_dir.iterdir()):
        if not plugin_dir.is_dir():
            continue

        manifest = _load_manifest_safe(plugin_dir)
        if manifest is None:
            continue

        found = True
        name = _str(manifest.get("name"), plugin_dir.name)
        status = _get_plugin_status(name)
        tools_count = len(_list(manifest.get("tools")))
        table.add_row(
            name,
            _str(manifest.get("version"), "?"),
            status,
            str(tools_count),
            _str(manifest.get("description")),
        )

    if not found:
        console.print("[dim]No plugins installed.[/dim]")
        return

    console.print(table)


# ── Info ────────────────────────────────────────────────────────────


@plugin_app.command("info")
def plugin_info(
    name: Annotated[str, typer.Argument(help="Plugin name")],
) -> None:
    """Show detailed information about a plugin."""
    plugin_dir = _plugins_dir() / name
    if not plugin_dir.exists():
        console.print(f"[red]Plugin '{name}' not found.[/red]")
        raise typer.Exit(code=1)

    manifest = _load_manifest_safe(plugin_dir)
    if manifest is None:
        console.print(f"[red]Invalid plugin.yaml in '{name}'.[/red]")
        raise typer.Exit(code=1)

    display_name = _str(manifest.get("name"), name)
    console.print(f"\n[bold cyan]{display_name}[/bold cyan]")
    console.print(f"  Version:     {_str(manifest.get('version'), '?')}")
    console.print(f"  Description: {_str(manifest.get('description'), '-')}")
    console.print(f"  Author:      {_str(manifest.get('author'), '-')}")
    console.print(f"  License:     {_str(manifest.get('license'), '-')}")
    console.print(f"  Homepage:    {_str(manifest.get('homepage'), '-')}")
    console.print(f"  Status:      {_get_plugin_status(name)}")

    # Permissions
    perms = _list(manifest.get("permissions"))
    if perms:
        console.print(f"\n  [bold]Permissions ({len(perms)}):[/bold]")
        for p in perms:
            console.print(f"    • {p}")

    # Tools
    tools = _list(manifest.get("tools"))
    if tools:
        console.print(f"\n  [bold]Tools ({len(tools)}):[/bold]")
        for t in tools:
            if isinstance(t, dict):
                desc = _str(t.get("description"))
                console.print(f"    • {_str(t.get('name'), '?')}: {desc}")

    # Dependencies
    deps = _list(manifest.get("depends"))
    if deps:
        console.print(f"\n  [bold]Dependencies ({len(deps)}):[/bold]")
        for d in deps:
            if isinstance(d, dict):
                console.print(f"    • {_str(d.get('name'), '?')} {_str(d.get('version'))}")

    console.print()


# ── Install ─────────────────────────────────────────────────────────


@plugin_app.command("install")
def plugin_install(
    source: Annotated[str, typer.Argument(help="Local path, pip package, or git+URL")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip permission confirmation")] = False,
) -> None:
    """Install a plugin from a local directory, pip package, or git URL."""
    if source.startswith("git+"):
        _install_from_git(source, yes=yes)
    elif Path(source).is_dir():
        _install_from_local(Path(source), yes=yes)
    else:
        _install_from_pip(source, yes=yes)


def _install_from_local(source_dir: Path, *, yes: bool = False) -> None:
    """Install plugin from a local directory."""
    manifest = _load_manifest_safe(source_dir)
    if manifest is None:
        console.print("[red]No valid plugin.yaml found.[/red]")
        raise typer.Exit(code=1)

    name = _str(manifest.get("name"), source_dir.name)
    perms = _list(manifest.get("permissions"))

    if perms and not yes:
        console.print(f"\n[bold yellow]Plugin '{name}' requests permissions:[/bold yellow]")
        for p in perms:
            console.print(f"  • {p}")
        if not typer.confirm("\nApprove and install?"):
            console.print("[dim]Installation cancelled.[/dim]")
            raise typer.Exit(code=0)

    target = _plugins_dir() / str(name)
    if target.exists():
        console.print(f"[yellow]Plugin '{name}' already installed. Replacing...[/yellow]")
        shutil.rmtree(target)

    shutil.copytree(source_dir, target)
    console.print(f"[green]✓ Plugin '{name}' installed from local directory.[/green]")


def _install_from_pip(package: str, *, yes: bool = False) -> None:
    """Install plugin via pip."""
    console.print(f"[dim]Installing '{package}' via pip...[/dim]")
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pip", "install", package, "--quiet"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]pip install failed: {result.stderr.strip()}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]✓ Package '{package}' installed via pip.[/green]")
    console.print("[dim]Plugin will be auto-discovered on next 'sovyx start'.[/dim]")


def _install_from_git(url: str, *, yes: bool = False) -> None:
    """Install plugin from a git URL."""
    # Strip git+ prefix for pip
    pip_url = url
    console.print(f"[dim]Installing from {url}...[/dim]")
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pip", "install", pip_url, "--quiet"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]git install failed: {result.stderr.strip()}[/red]")
        raise typer.Exit(code=1)
    console.print("[green]✓ Plugin installed from git.[/green]")
    console.print("[dim]Plugin will be auto-discovered on next 'sovyx start'.[/dim]")


# ── Enable / Disable ───────────────────────────────────────────────


@plugin_app.command("enable")
def plugin_enable(
    name: Annotated[str, typer.Argument(help="Plugin name to enable")],
) -> None:
    """Enable a disabled plugin."""
    updated = _update_mind_yaml_plugins(name, enable=True)
    if updated:
        console.print(f"[green]✓ Plugin '{name}' enabled.[/green]")
    else:
        console.print(f"[yellow]Plugin '{name}' was already enabled.[/yellow]")


@plugin_app.command("disable")
def plugin_disable(
    name: Annotated[str, typer.Argument(help="Plugin name to disable")],
) -> None:
    """Disable a plugin (remains installed but won't load)."""
    updated = _update_mind_yaml_plugins(name, enable=False)
    if updated:
        console.print(f"[yellow]✓ Plugin '{name}' disabled.[/yellow]")
    else:
        console.print(f"[yellow]Plugin '{name}' was already disabled.[/yellow]")


# ── Remove ──────────────────────────────────────────────────────────


@plugin_app.command("remove")
def plugin_remove(
    name: Annotated[str, typer.Argument(help="Plugin name to remove")],
) -> None:
    """Remove an installed plugin."""
    plugin_dir = _plugins_dir() / name
    if not plugin_dir.exists():
        console.print(f"[red]Plugin '{name}' not found.[/red]")
        raise typer.Exit(code=1)

    shutil.rmtree(plugin_dir)
    # Also remove from disabled list if present
    _update_mind_yaml_plugins(name, remove=True)
    console.print(f"[green]✓ Plugin '{name}' removed.[/green]")


# ── Helpers ─────────────────────────────────────────────────────────


def _load_manifest_safe(plugin_dir: Path) -> dict[str, object] | None:
    """Load plugin.yaml as a dict, or None on failure."""
    from typing import Any

    import yaml

    manifest_path = plugin_dir / "plugin.yaml"
    if not manifest_path.exists():
        return None
    try:
        data: Any = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _str(val: object, default: str = "") -> str:
    """Safely convert manifest value to string."""
    return str(val) if val is not None else default


def _list(val: object) -> list[object]:
    """Safely convert manifest value to list."""
    return list(val) if isinstance(val, list) else []


def _get_plugin_status(name: str) -> str:
    """Get plugin status from mind.yaml."""
    import yaml

    path = _mind_yaml_path()
    if not path.exists():
        return "enabled"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return "enabled"
        plugins = data.get("plugins", {})
        if not isinstance(plugins, dict):
            return "enabled"
        disabled = plugins.get("disabled", [])
        if name in disabled:
            return "disabled"
        config = plugins.get("plugins_config", {})
        if isinstance(config, dict) and name in config:
            entry = config[name]
            if isinstance(entry, dict) and not entry.get("enabled", True):
                return "disabled"
        return "enabled"
    except Exception:  # noqa: BLE001
        return "enabled"


def _update_mind_yaml_plugins(
    name: str,
    *,
    enable: bool = False,
    remove: bool = False,
) -> bool:
    """Update plugins.disabled in mind.yaml.

    Returns True if a change was made.
    """
    import yaml

    path = _mind_yaml_path()
    if not path.exists():
        if enable or remove:
            return False
        # Create minimal mind.yaml with disabled plugin
        data: dict[str, object] = {
            "name": "default",
            "plugins": {"disabled": [name]},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.dump(data, default_flow_style=False),
            encoding="utf-8",
        )
        return True

    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            return False
    except Exception:  # noqa: BLE001
        return False

    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        return False

    disabled: list[str] = plugins.setdefault("disabled", [])
    if not isinstance(disabled, list):
        disabled = []
        plugins["disabled"] = disabled

    changed = False

    if remove or enable:
        if name in disabled:
            disabled.remove(name)
            changed = True
    else:
        # disable
        if name not in disabled:
            disabled.append(name)
            changed = True

    if changed:
        path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    return changed
