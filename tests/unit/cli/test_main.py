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


class TestMindForget:
    """``sovyx mind forget`` — Phase 8 / T8.21 step 4."""

    @staticmethod
    def _report(**overrides: int) -> dict[str, object]:
        """Build a complete report dict for the mocked _run return."""
        defaults: dict[str, object] = {
            "mind_id": "aria",
            "concepts_purged": 5,
            "relations_purged": 3,
            "episodes_purged": 4,
            "concept_embeddings_purged": 5,
            "episode_embeddings_purged": 4,
            "conversation_imports_purged": 0,
            "consolidation_log_purged": 1,
            "conversations_purged": 2,
            "conversation_turns_purged": 6,
            "daily_stats_purged": 1,
            "consent_ledger_purged": 7,
            "total_brain_rows_purged": 22,
            "total_conversations_rows_purged": 8,
            "total_system_rows_purged": 1,
            "total_rows_purged": 31,
            "dry_run": False,
        }
        defaults.update(overrides)
        return defaults

    def test_forget_no_daemon(self) -> None:
        with patch("sovyx.cli.main._get_client") as mock:
            mock.return_value.is_daemon_running.return_value = False
            result = runner.invoke(app, ["mind", "forget", "aria"])
        assert result.exit_code == 1
        assert "Daemon not running" in result.stdout

    def test_forget_empty_mind_id_rejected(self) -> None:
        with patch("sovyx.cli.main._get_client") as mock:
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["mind", "forget", "  "])
        assert result.exit_code == 2  # noqa: PLR2004
        assert "non-empty" in result.stdout

    def test_forget_dry_run_skips_confirmation_prompt(self) -> None:
        """``--dry-run`` does not require confirmation — the operator
        is just previewing counts; no destructive action happens."""
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch(
                "sovyx.cli.main._run",
                return_value=self._report(dry_run=True),
            ),
        ):
            mock.return_value.is_daemon_running.return_value = True
            # No "yes" input piped — confirmation must NOT be prompted.
            result = runner.invoke(app, ["mind", "forget", "aria", "--dry-run"])
        assert result.exit_code == 0
        assert "would purge" in result.stdout
        assert "31 relational rows" in result.stdout

    def test_forget_yes_flag_skips_confirmation(self) -> None:
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value=self._report()),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["mind", "forget", "aria", "--yes"])
        assert result.exit_code == 0
        assert "purged" in result.stdout

    def test_forget_renders_per_table_breakdown(self) -> None:
        """Non-zero per-table counts surface in the output;
        zero-count tables are omitted to keep the output useful."""
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value=self._report()),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["mind", "forget", "aria", "--yes"])
        assert "concepts_purged" in result.stdout
        assert "5" in result.stdout
        # conversation_imports_purged is 0 in the default report —
        # MUST NOT surface (keeps the breakdown signal-dense).
        assert "conversation_imports_purged" not in result.stdout

    def test_forget_no_confirmation_aborts(self) -> None:
        """Without --yes / --dry-run, declining the prompt aborts
        with exit 1 + 'aborted' message."""
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run") as run_mock,
        ):
            mock.return_value.is_daemon_running.return_value = True
            # Pipe "n\n" to the confirmation prompt.
            result = runner.invoke(app, ["mind", "forget", "aria"], input="n\n")
        assert result.exit_code == 1
        assert "aborted" in result.stdout
        # The RPC was NEVER called.
        run_mock.assert_not_called()


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


class TestPreflightWarningSurface:
    """v1.3 §4.8 L7 — CLI commands read the marker file and surface warnings.

    ``status`` is the easiest command to exercise because its happy
    path is synchronous and fully mockable. ``start`` shares the same
    helper so testing one helper call + one command arm covers the
    contract.
    """

    def test_status_surfaces_preflight_warning_from_marker(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice.health import write_preflight_warnings_file

        # Anchor the marker file to tmp_path by pinning HOME so the
        # CLI helper resolves to our fixture rather than the dev's home.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows fallback
        (tmp_path / ".sovyx").mkdir()
        write_preflight_warnings_file(
            [
                {
                    "code": "linux_mixer_saturated",
                    "hint": "reset mic gain",
                },
            ],
            data_dir=tmp_path / ".sovyx",
        )
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value={"version": "0.1.0"}),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        # Rich renders the marker text onto stdout; assert on stable
        # tokens rather than the whole rendered block.
        assert "Voice preflight warning" in result.output
        assert "linux_mixer_saturated" in result.output
        assert "sovyx doctor voice --fix --yes" in result.output

    def test_status_no_marker_no_surface(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        with (
            patch("sovyx.cli.main._get_client") as mock,
            patch("sovyx.cli.main._run", return_value={"version": "0.1.0"}),
        ):
            mock.return_value.is_daemon_running.return_value = True
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "Voice preflight warning" not in result.output
