"""Tests for `sovyx token` CLI command (DASH-06)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


runner = CliRunner()


class TestTokenCommand:
    """Tests for the top-level `sovyx token` command."""

    def test_shows_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Displays the token when file exists."""
        from sovyx.cli.main import app

        token_file = tmp_path / "token"
        token_file.write_text("my-secret-token-abc123")
        monkeypatch.setattr("sovyx.cli.main.TOKEN_FILE", token_file)

        result = runner.invoke(app, ["token"])
        assert result.exit_code == 0
        assert "my-secret-token-abc123" in result.output
        assert "Dashboard Token" in result.output

    def test_missing_token_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Shows error when token file doesn't exist."""
        from sovyx.cli.main import app

        monkeypatch.setattr("sovyx.cli.main.TOKEN_FILE", tmp_path / "notoken")

        result = runner.invoke(app, ["token"])
        assert result.exit_code == 1
        assert "not generated" in result.output.lower()

    def test_empty_token_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Shows error when token file is empty."""
        from sovyx.cli.main import app

        token_file = tmp_path / "token"
        token_file.write_text("")
        monkeypatch.setattr("sovyx.cli.main.TOKEN_FILE", token_file)

        result = runner.invoke(app, ["token"])
        assert result.exit_code == 1
        assert "empty" in result.output.lower()

    def test_whitespace_token_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Token with whitespace is stripped."""
        from sovyx.cli.main import app

        token_file = tmp_path / "token"
        token_file.write_text("  my-token  \n")
        monkeypatch.setattr("sovyx.cli.main.TOKEN_FILE", token_file)

        result = runner.invoke(app, ["token"])
        assert result.exit_code == 0
        assert "my-token" in result.output

    def test_copy_flag_without_clipboard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--copy flag gracefully handles missing clipboard tools."""
        from sovyx.cli.main import app

        token_file = tmp_path / "token"
        token_file.write_text("test-token")
        monkeypatch.setattr("sovyx.cli.main.TOKEN_FILE", token_file)

        # xclip and pbcopy won't exist in test env
        result = runner.invoke(app, ["token", "--copy"])
        assert result.exit_code == 0
        assert "test-token" in result.output

    def test_help_text(self) -> None:
        """Token command has help text."""
        from sovyx.cli.main import app

        result = runner.invoke(app, ["token", "--help"])
        assert result.exit_code == 0
        assert "dashboard" in result.output.lower()
