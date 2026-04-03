"""Tests for sovyx package initialization."""

from __future__ import annotations

import re
import subprocess
import sys


def test_version_exists() -> None:
    """Package exposes __version__."""
    from sovyx import __version__

    assert __version__ == "0.1.0"


def test_version_is_semver() -> None:
    """Version follows semantic versioning."""
    from sovyx import __version__

    assert re.match(r"^\d+\.\d+\.\d+$", __version__)


def test_main_module_prints_version() -> None:
    """python -m sovyx prints version string."""
    result = subprocess.run(
        [sys.executable, "-m", "sovyx"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "Sovyx v0.1.0" in result.stdout


def test_package_is_typed() -> None:
    """Package has py.typed marker (PEP 561)."""
    import importlib.resources as resources

    ref = resources.files("sovyx").joinpath("py.typed")
    assert ref.is_file()  # type: ignore[union-attr]


def test_main_function() -> None:
    """__main__.main() prints version."""
    from sovyx.__main__ import main

    # main() prints to stdout — just verify it doesn't raise
    main()


def test_cli_app_exists() -> None:
    """CLI app is a Typer instance."""
    from sovyx.cli.main import app

    assert app is not None
    assert app.info.name == "sovyx"


def test_cli_version_flag() -> None:
    """CLI --version flag shows version."""
    from typer.testing import CliRunner

    from sovyx.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "Sovyx v0.1.0" in result.output


def test_cli_no_args_shows_help() -> None:
    """CLI with no args shows help text."""
    from typer.testing import CliRunner

    from sovyx.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, [])
    # no_args_is_help returns exit code 0 or 2 depending on Typer/Click version
    assert "Sovereign Minds Engine" in result.output


def test_cli_without_version_flag() -> None:
    """CLI callback without --version does not print version."""
    from sovyx.cli.main import main

    # version=False → function should return without printing version
    main(version=False)


def test_subpackages_importable() -> None:
    """All subpackages are importable."""
    import sovyx.brain
    import sovyx.bridge
    import sovyx.bridge.channels
    import sovyx.cli
    import sovyx.cli.commands
    import sovyx.cognitive
    import sovyx.context
    import sovyx.engine
    import sovyx.llm
    import sovyx.mind
    import sovyx.observability
    import sovyx.persistence

    # If we got here, all imports succeeded
    assert sovyx.brain is not None
    assert sovyx.bridge is not None
    assert sovyx.bridge.channels is not None
    assert sovyx.cli is not None
    assert sovyx.cli.commands is not None
    assert sovyx.cognitive is not None
    assert sovyx.context is not None
    assert sovyx.engine is not None
    assert sovyx.llm is not None
    assert sovyx.mind is not None
    assert sovyx.observability is not None
    assert sovyx.persistence is not None
