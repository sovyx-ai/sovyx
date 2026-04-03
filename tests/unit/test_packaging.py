"""Tests for packaging configuration."""

from __future__ import annotations


class TestPackaging:
    """Validate packaging files exist and are correct."""

    def test_entry_point_importable(self) -> None:
        """CLI entry point module is importable."""
        from sovyx.cli.main import app

        assert app is not None

    def test_version_string(self) -> None:
        """Version is a valid semver string."""
        from sovyx import __version__

        parts = __version__.split(".")
        assert len(parts) == 3  # noqa: PLR2004
        assert all(p.isdigit() for p in parts)

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
