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

    # py.typed must exist in the package
    ref = resources.files("sovyx").joinpath("py.typed")
    assert ref.is_file()  # type: ignore[union-attr]


def test_subpackages_importable() -> None:
    """All subpackages are importable."""
    import sovyx.engine
    import sovyx.mind
    import sovyx.cognitive
    import sovyx.brain
    import sovyx.persistence
    import sovyx.context
    import sovyx.llm
    import sovyx.bridge
    import sovyx.bridge.channels
    import sovyx.cli
    import sovyx.cli.commands
    import sovyx.observability

    # If we got here, all imports succeeded
    assert sovyx.engine is not None
    assert sovyx.mind is not None
    assert sovyx.cognitive is not None
    assert sovyx.brain is not None
    assert sovyx.persistence is not None
    assert sovyx.context is not None
    assert sovyx.llm is not None
    assert sovyx.bridge is not None
    assert sovyx.bridge.channels is not None
    assert sovyx.cli is not None
    assert sovyx.cli.commands is not None
    assert sovyx.observability is not None
