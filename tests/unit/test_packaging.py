"""Tests for packaging configuration."""

from __future__ import annotations


class TestPackaging:
    """Validate packaging files exist and are correct."""

    def test_entry_point_importable(self) -> None:
        """CLI entry point module is importable."""
        from sovyx.cli.main import app

        assert app is not None

    def test_version_string(self) -> None:
        """Version is a valid semver string (or PEP 440 prerelease)."""
        import re

        from sovyx import __version__

        # Accept ``X.Y.Z`` (release) or ``X.Y.Zrc<N>`` (PEP 440 RC).
        # uv/pip normalize ``X.Y.Z-rc.N`` from pyproject.toml to
        # ``X.Y.ZrcN`` at metadata read time per PEP 440 §3.3.
        assert re.match(r"^\d+\.\d+\.\d+(?:rc\d+)?$", __version__), __version__

    def test_dockerfile_exists(self) -> None:
        """Dockerfile is present in repo root."""
        from pathlib import Path

        assert (Path(__file__).parents[2] / "Dockerfile").exists()

    def test_docker_compose_exists(self) -> None:
        """docker-compose.yml is present in repo root."""
        from pathlib import Path

        assert (Path(__file__).parents[2] / "docker-compose.yml").exists()

    def test_systemd_unit_exists(self) -> None:
        """systemd unit file is present."""
        from pathlib import Path

        assert (Path(__file__).parents[2] / "sovyx.service").exists()

    def test_install_script_exists(self) -> None:
        """Install script is present and executable."""
        import os
        from pathlib import Path

        script = Path(__file__).parents[2] / "scripts" / "install.sh"
        assert script.exists()
        assert os.access(script, os.X_OK)
