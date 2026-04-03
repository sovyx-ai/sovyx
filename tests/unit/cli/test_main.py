"""Tests for sovyx.cli.main — CLI commands."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sovyx.cli.main import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


class TestVersion:
    """Version flag."""

    def test_version_flag(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.stdout

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
        assert "Aria" in result.stdout

    def test_init_idempotent(self, tmp_path: Path) -> None:
        with patch("sovyx.cli.main.Path.home", return_value=tmp_path):
            runner.invoke(app, ["init", "Test", "--quick"])
            result = runner.invoke(app, ["init", "Test", "--quick"])
        assert result.exit_code == 0
        assert "already exists" in result.stdout


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
            patch("sovyx.cli.main._get_client") as mock_client,
            patch("sovyx.cli.main.Path.home", return_value=tmp_path),
        ):
            mock_client.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "data_dir" in result.stdout


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
        checks = {"checks": {"sqlite": True, "brain": True}}
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
