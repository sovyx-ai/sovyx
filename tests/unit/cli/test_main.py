"""Tests for sovyx.cli.main — CLI commands."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sovyx import __version__
from sovyx.cli.main import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


class TestVersion:
    """Version flag."""

    def test_version_flag(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.stdout

    def test_version_short(self) -> None:
        result = runner.invoke(app, ["-v"])
        assert result.exit_code == 0
        assert "sovyx" in result.stdout


class TestInit:
    """Init command."""

    def test_init_creates_files(self, tmp_path: Path) -> None:
        with patch("sovyx.cli.main.Path.home", return_value=tmp_path):
            result = runner.invoke(app, ["init", "TestMind", "--quick"])
        assert result.exit_code == 0
        assert "initialized" in result.stdout

    def test_init_default_name(self, tmp_path: Path) -> None:
        with patch("sovyx.cli.main.Path.home", return_value=tmp_path):
            result = runner.invoke(app, ["init", "--quick"])
        assert result.exit_code == 0
        assert "Sovyx" in result.stdout

    def test_init_idempotent(self, tmp_path: Path) -> None:
        with patch("sovyx.cli.main.Path.home", return_value=tmp_path):
            runner.invoke(app, ["init", "Test", "--quick"])
            result = runner.invoke(app, ["init", "Test", "--quick"])
        assert result.exit_code == 0
        assert "already exists" in result.stdout

    def test_init_creates_logs_dir(self, tmp_path: Path) -> None:
        with patch("sovyx.cli.main.Path.home", return_value=tmp_path):
            result = runner.invoke(app, ["init", "Test", "--quick"])
        assert result.exit_code == 0
        logs_dir = tmp_path / ".sovyx" / "logs"
        assert logs_dir.is_dir()
        assert "logs" in result.stdout


class TestStop:
    """Stop command."""

    def test_stop_no_daemon(self) -> None:
        with patch("sovyx.cli.main._get_client") as mock:
            mock.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["stop"])
        assert result.exit_code == 1
        assert "not running" in result.stdout


class TestStatus:
    """Status command."""

    def test_status_no_daemon(self) -> None:
        with patch("sovyx.cli.main._get_client") as mock:
            mock.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 1
        assert "not running" in result.stdout


class TestDoctor:
    """Doctor command."""

    def test_doctor_offline(self, tmp_path: Path) -> None:
        with (
            patch("sovyx.cli.commands.doctor.DaemonClient") as mock_client,
            patch("sovyx.cli.commands.doctor.Path.home", return_value=tmp_path),
        ):
            mock_client.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        # New doctor uses Rich Table — check for offline check names
        assert "Disk Space" in result.stdout or "offline" in result.stdout.lower()

    def test_doctor_offline_json(self, tmp_path: Path) -> None:
        with (
            patch("sovyx.cli.commands.doctor.DaemonClient") as mock_client,
            patch("sovyx.cli.commands.doctor.Path.home", return_value=tmp_path),
        ):
            mock_client.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0
        import json

        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) >= 4  # At least 4 offline checks + config
        names = {item["name"] for item in data}
        assert "Disk Space" in names
        assert "RAM" in names
        assert "CPU" in names


class TestBrainCommands:
    """Brain subcommands."""

    def test_search_no_daemon(self) -> None:
        with patch("sovyx.cli.main._get_client") as mock:
            mock.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["brain", "search", "test"])
        assert result.exit_code == 1

    def test_stats_no_daemon(self) -> None:
        with patch("sovyx.cli.main._get_client") as mock:
            mock.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["brain", "stats"])
        assert result.exit_code == 1


class TestMindCommands:
    """Mind subcommands."""

    def test_list_no_daemon(self) -> None:
        with patch("sovyx.cli.main._get_client") as mock:
            mock.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["mind", "list"])
        assert result.exit_code == 1

    def test_status_no_daemon(self) -> None:
        with patch("sovyx.cli.main._get_client") as mock:
            mock.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["mind", "status"])
        assert result.exit_code == 1


@pytest.mark.skip(
    reason="deadlock on CI — leaks async resources that hang next test's collection; tracked separately"
)
class TestWithDaemon:
    """Commands that connect to daemon."""

    def test_stop_success(self) -> None:
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value="ok"),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0

    def test_status_success(self) -> None:
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value={"version": "0.1.0"}),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0

    def test_doctor_online(self) -> None:
        checks = {
            "checks": {
                "sqlite": {"status": "green", "message": "ok", "metadata": None},
                "brain": {"status": "green", "message": "indexed"},
            }
        }
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value=checks),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0

    def test_doctor_online_legacy_bool(self) -> None:
        """Backward compat: daemon returns simple bool checks."""
        checks = {"checks": {"sqlite": True, "brain": False}}
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value=checks),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0

    def test_brain_search_success(self) -> None:
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value=["concept1"]),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["brain", "search", "test"])
        assert result.exit_code == 0

    def test_brain_stats_success(self) -> None:
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value={"concepts": 10}),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["brain", "stats"])
        assert result.exit_code == 0

    def test_mind_list_success(self) -> None:
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value=["aria"]),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["mind", "list"])
        assert result.exit_code == 0

    def test_mind_status_success(self) -> None:
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value={"name": "aria"}),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["mind", "status", "aria"])
        assert result.exit_code == 0

    def test_start_already_running(self) -> None:
        with patch("sovyx.cli.main._get_client") as mock:
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["start"])
        assert result.exit_code == 1
        assert "already running" in result.stdout
