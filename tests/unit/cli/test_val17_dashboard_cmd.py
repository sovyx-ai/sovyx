"""VAL-17: CLI dashboard command coverage + pragma audit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


runner = CliRunner()


class TestDashboardInfo:
    def test_default_output(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dashboard info shows URL and token hint."""
        from sovyx.cli.commands.dashboard import dashboard_app

        monkeypatch.setattr("sovyx.cli.commands.dashboard.TOKEN_FILE", tmp_path / "token")
        monkeypatch.setattr(
            "sovyx.cli.commands.dashboard.Path.home", staticmethod(lambda: tmp_path)
        )

        result = runner.invoke(dashboard_app)
        assert result.exit_code == 0
        assert "Dashboard" in result.output
        assert "127.0.0.1" in result.output
        assert "use --token" in result.output

    def test_show_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--token flag reveals the token."""
        from sovyx.cli.commands.dashboard import dashboard_app

        token_file = tmp_path / "token"
        token_file.write_text("test-secret-token-123")
        monkeypatch.setattr("sovyx.cli.commands.dashboard.TOKEN_FILE", token_file)

        result = runner.invoke(dashboard_app, ["--token"])
        assert result.exit_code == 0
        assert "test-secret-token-123" in result.output

    def test_show_token_no_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--token with missing file shows warning."""
        from sovyx.cli.commands.dashboard import dashboard_app

        monkeypatch.setattr("sovyx.cli.commands.dashboard.TOKEN_FILE", tmp_path / "notoken")

        result = runner.invoke(dashboard_app, ["--token"])
        assert result.exit_code == 0
        assert "Not generated" in result.output

    def test_reads_system_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reads host/port from system.yaml if present."""
        from sovyx.cli.commands.dashboard import dashboard_app

        config_dir = tmp_path / ".sovyx"
        config_dir.mkdir()
        (config_dir / "system.yaml").write_text("api:\n  host: '0.0.0.0'\n  port: 9999\n")

        monkeypatch.setattr("sovyx.cli.commands.dashboard.TOKEN_FILE", tmp_path / "token")
        monkeypatch.setattr(
            "sovyx.cli.commands.dashboard.Path.home", staticmethod(lambda: tmp_path)
        )

        result = runner.invoke(dashboard_app)
        assert result.exit_code == 0
        assert "0.0.0.0" in result.output
        assert "9999" in result.output

    def test_bad_system_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bad system.yaml falls back to defaults."""
        from sovyx.cli.commands.dashboard import dashboard_app

        config_dir = tmp_path / ".sovyx"
        config_dir.mkdir()
        (config_dir / "system.yaml").write_text("{{invalid yaml!!")

        monkeypatch.setattr("sovyx.cli.commands.dashboard.TOKEN_FILE", tmp_path / "token")
        monkeypatch.setattr(
            "sovyx.cli.commands.dashboard.Path.home", staticmethod(lambda: tmp_path)
        )

        result = runner.invoke(dashboard_app)
        assert result.exit_code == 0
        assert "127.0.0.1" in result.output
