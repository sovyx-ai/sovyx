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
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help=(
                "Skip the permission confirmation for local-directory installs "
                "(no-op for pip / git installs — those trust the source by design, "
                "matching apt / pip / brew industry pattern)."
            ),
        ),
    ] = False,
) -> None:
    """Install a plugin from a local directory, pip package, or git URL.

    The 3 install paths have asymmetric permission-prompt behaviour by
    design (verified 2026-05-02 against apt / pip / brew industry
    pattern):

    * **Local directory** — ``./my-plugin/``. The operator points at an
      arbitrary path; Sovyx loads ``plugin.yaml`` and prompts for
      every requested permission. ``--yes`` skips the prompt for
      automated CI/CD.

    * **pip package** — ``my-sovyx-plugin``. Trust derives from PyPI's
      package signing + the operator's explicit package choice, so
      no Sovyx-side permission prompt fires (matching ``pip install``
      itself). ``--yes`` is a no-op here. Plugin permissions enforced
      at runtime by the 5-layer sandbox.

    * **git+URL** — ``git+https://...``. Same trust model as pip:
      operator's explicit URL choice + git-side authentication is the
      gate. ``--yes`` is a no-op here. Permissions enforced at runtime.

    Trade-off: the asymmetry means a malicious *local directory* gets
    operator review, but a malicious *PyPI package* or *git URL*
    relies on operator vetting + the runtime sandbox. The 5-layer
    sandbox is designed to contain malicious plugins regardless of
    install path.
    """
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


# ── Validate ────────────────────────────────────────────────────────


@plugin_app.command("validate")
def plugin_validate(
    plugin_dir: Annotated[str, typer.Argument(help="Plugin directory to validate")],
) -> None:
    """Run quality gates on a plugin directory.

    Checks: manifest schema, AST security scan, test discovery.
    Exit 0 if all pass, exit 1 on any failure.
    """
    target = Path(plugin_dir)
    if not target.is_dir():
        console.print(f"[red]Not a directory: {plugin_dir}[/red]")
        raise typer.Exit(code=1)

    errors: list[str] = []
    warnings: list[str] = []

    # 1. Manifest check
    console.print("[bold]1. Manifest validation[/bold]")
    manifest_result = _validate_manifest(target)
    if manifest_result is None:
        console.print("   [red]✗ plugin.yaml not found or invalid[/red]")
        errors.append("Invalid manifest")
    else:
        console.print(f"   [green]✓ {manifest_result}[/green]")

    # 2. AST security scan
    console.print("[bold]2. Security scan[/bold]")
    findings = _validate_security(target)
    critical = [f for f in findings if f.get("severity") == "critical"]
    warns = [f for f in findings if f.get("severity") == "warning"]
    if critical:
        for f in critical:
            console.print(f"   [red]✗ {f['file']}:{f['line']} — {f['message']}[/red]")
            errors.append(f"{f['file']}:{f['line']}: {f['message']}")
    if warns:
        for f in warns:
            console.print(f"   [yellow]⚠ {f['file']}:{f['line']} — {f['message']}[/yellow]")
            warnings.append(f"{f['file']}:{f['line']}: {f['message']}")
    if not critical and not warns:
        console.print("   [green]✓ No security issues found[/green]")

    # 3. Test discovery
    console.print("[bold]3. Test discovery[/bold]")
    test_count = _discover_tests(target)
    if test_count == 0:
        console.print("   [yellow]⚠ No tests found[/yellow]")
        warnings.append("No tests found")
    else:
        console.print(f"   [green]✓ {test_count} test file(s) found[/green]")

    # 4. Python files parseable
    console.print("[bold]4. Syntax check[/bold]")
    syntax_errors = _check_syntax(target)
    if syntax_errors:
        for err in syntax_errors:
            console.print(f"   [red]✗ {err}[/red]")
            errors.append(err)
    else:
        py_count = len(list(target.rglob("*.py")))
        console.print(f"   [green]✓ {py_count} Python file(s) parse OK[/green]")

    # Summary
    console.print()
    if errors:
        console.print(
            f"[red bold]FAILED[/red bold] — {len(errors)} error(s), {len(warnings)} warning(s)"
        )
        raise typer.Exit(code=1)
    if warnings:
        console.print(
            f"[yellow bold]PASSED with warnings[/yellow bold] — {len(warnings)} warning(s)"
        )
    else:
        console.print("[green bold]PASSED[/green bold] — all quality gates clean")


def _validate_manifest(plugin_dir: Path) -> str | None:
    """Validate plugin.yaml. Returns summary string or None on failure."""
    try:
        from sovyx.plugins.manifest import load_manifest

        manifest = load_manifest(plugin_dir)
        return f"{manifest.name} v{manifest.version} ({len(manifest.tools)} tools)"
    except Exception:  # noqa: BLE001
        return None


def _validate_security(plugin_dir: Path) -> list[dict[str, object]]:
    """Run AST security scan. Returns list of finding dicts."""
    try:
        from sovyx.plugins.security import PluginSecurityScanner

        scanner = PluginSecurityScanner()
        findings = scanner.scan_directory(plugin_dir)
        return [
            {
                "severity": f.severity,
                "file": f.file,
                "line": f.line,
                "message": f.message,
            }
            for f in findings
        ]
    except Exception:  # noqa: BLE001
        return []


def _discover_tests(plugin_dir: Path) -> int:
    """Count test files in plugin directory."""
    tests_dir = plugin_dir / "tests"
    if not tests_dir.exists():
        # Also check for test files in root
        return len(list(plugin_dir.glob("test_*.py")))
    return len(list(tests_dir.rglob("test_*.py")))


def _check_syntax(plugin_dir: Path) -> list[str]:
    """Check all Python files parse correctly."""
    import ast as ast_mod

    errors: list[str] = []
    for py_file in plugin_dir.rglob("*.py"):
        try:
            ast_mod.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError as e:
            errors.append(f"{py_file.name}:{e.lineno}: {e.msg}")
    return errors


# ── Create / Scaffold ───────────────────────────────────────────────


@plugin_app.command("create")
def plugin_create(
    name: Annotated[str, typer.Argument(help="Plugin name (lowercase, hyphens ok)")],
    output_dir: Annotated[
        Path,
        typer.Option("--output", "-o", help="Output directory (default: current)"),
    ] = Path("."),
) -> None:
    """Scaffold a new plugin from template."""
    import re

    if not re.match(r"^[a-z][a-z0-9\-]*$", name):
        console.print("[red]Name must be lowercase, start with letter, hyphens ok.[/red]")
        raise typer.Exit(code=1)

    plugin_dir = output_dir / name
    if plugin_dir.exists():
        console.print(f"[red]Directory '{plugin_dir}' already exists.[/red]")
        raise typer.Exit(code=1)

    _scaffold_plugin(name, plugin_dir)
    console.print(f"[green]✓ Plugin '{name}' created at {plugin_dir}[/green]")
    console.print("\nNext steps:")
    console.print(f"  1. cd {plugin_dir}")
    console.print("  2. Edit plugin.py — add your @tool methods")
    console.print("  3. Edit plugin.yaml — declare permissions")
    console.print(f"  4. sovyx plugin install {plugin_dir}")


def _scaffold_plugin(name: str, target: Path) -> None:
    """Generate plugin scaffold files."""
    # Convert name to Python module name
    module_name = name.replace("-", "_")
    class_name = "".join(w.capitalize() for w in name.split("-")) + "Plugin"

    target.mkdir(parents=True)

    # __init__.py
    (target / "__init__.py").write_text(
        f'"""Sovyx plugin: {name}."""\n\n'
        f"from {module_name}.plugin import {class_name}\n\n"
        f'__all__ = ["{class_name}"]\n',
        encoding="utf-8",
    )

    # plugin.py
    (target / "plugin.py").write_text(
        f'"""Sovyx Plugin — {name}."""\n\n'
        "from __future__ import annotations\n\n"
        "from sovyx.plugins.sdk import ISovyxPlugin, tool\n\n\n"
        f"class {class_name}(ISovyxPlugin):\n"
        f'    """A Sovyx plugin for {name}."""\n\n'
        "    @property\n"
        "    def name(self) -> str:\n"
        f'        return "{name}"\n\n'
        "    @property\n"
        "    def version(self) -> str:\n"
        '        return "0.1.0"\n\n'
        "    @property\n"
        "    def description(self) -> str:\n"
        f'        return "{name} plugin for Sovyx."\n\n'
        '    @tool(description="Example tool — replace with your logic")\n'
        '    async def hello(self, who: str = "world") -> str:\n'
        '        """Say hello."""\n'
        '        return f"Hello, {who}!"\n',
        encoding="utf-8",
    )

    # plugin.yaml
    (target / "plugin.yaml").write_text(
        f"name: {name}\n"
        "version: 0.1.0\n"
        f"description: {name} plugin for Sovyx.\n"
        'author: ""\n'
        "license: MIT\n"
        'homepage: ""\n'
        "min_sovyx_version: 0.6.0\n"
        "\npermissions: []\n"
        "\nnetwork:\n"
        "  allowed_domains: []\n"
        "\ntools:\n"
        "  - name: hello\n"
        "    description: Example tool\n",
        encoding="utf-8",
    )

    # tests/
    tests_dir = target / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / f"test_{module_name}.py").write_text(
        f'"""Tests for {name} plugin."""\n\n'
        "import pytest\n\n"
        f"from {module_name}.plugin import {class_name}\n\n\n"
        f"class Test{class_name}:\n"
        f'    """Tests for {class_name}."""\n\n'
        "    @pytest.mark.anyio()\n"
        "    async def test_hello(self) -> None:\n"
        f"        plugin = {class_name}()\n"
        '        result = await plugin.hello(who="Sovyx")\n'
        '        assert "Sovyx" in result\n\n'
        "    def test_name(self) -> None:\n"
        f"        plugin = {class_name}()\n"
        f'        assert plugin.name == "{name}"\n\n'
        "    def test_version(self) -> None:\n"
        f"        plugin = {class_name}()\n"
        '        assert plugin.version == "0.1.0"\n',
        encoding="utf-8",
    )

    # README.md
    (target / "README.md").write_text(
        f"# {name}\n\n"
        f"A Sovyx plugin for {name}.\n\n"
        "## Installation\n\n"
        "```bash\n"
        f"sovyx plugin install ./{name}\n"
        "```\n\n"
        "## Usage\n\n"
        "The plugin provides the following tools:\n\n"
        "- `hello` — Example tool\n",
        encoding="utf-8",
    )

    # pyproject.toml
    (target / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=68.0"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        f'name = "sovyx-plugin-{name}"\n'
        'version = "0.1.0"\n'
        f'description = "{name} plugin for Sovyx"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = ["sovyx>=0.6.0"]\n\n'
        '[project.entry-points."sovyx.plugins"]\n'
        f'{name} = "{module_name}:{class_name}"\n',
        encoding="utf-8",
    )


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
