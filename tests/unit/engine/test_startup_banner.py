"""Tests for startup banner (DASH-07)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class TestStartupBanner:
    """Verify _print_startup_banner output."""

    def _make_lifecycle(self) -> object:
        """Create a LifecycleManager with mock registry and event bus."""
        from sovyx.engine.lifecycle import LifecycleManager

        mock_registry = MagicMock()
        mock_registry.is_registered.return_value = False
        mock_events = MagicMock()
        return LifecycleManager(mock_registry, mock_events)

    def test_banner_shows_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Banner contains the dashboard URL."""
        token_file = tmp_path / "token"
        token_file.write_text("test-token-123")
        monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", token_file)

        lm = self._make_lifecycle()
        lm._print_startup_banner()  # noqa: SLF001

        captured = capsys.readouterr()
        assert "127.0.0.1:7777" in captured.out

    def test_banner_shows_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Banner shows the actual token."""
        token_file = tmp_path / "token"
        token_file.write_text("my-secret-abc")
        monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", token_file)

        lm = self._make_lifecycle()
        lm._print_startup_banner()  # noqa: SLF001

        captured = capsys.readouterr()
        assert "my-secret-abc" in captured.out

    def test_banner_no_token_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Banner shows placeholder when token file missing."""
        monkeypatch.setattr(
            "sovyx.dashboard.server.TOKEN_FILE", tmp_path / "nofile",
        )

        lm = self._make_lifecycle()
        lm._print_startup_banner()  # noqa: SLF001

        captured = capsys.readouterr()
        assert "not generated" in captured.out

    def test_banner_mentions_sovyx_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Banner mentions `sovyx token` command."""
        token_file = tmp_path / "token"
        token_file.write_text("token")
        monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", token_file)

        lm = self._make_lifecycle()
        lm._print_startup_banner()  # noqa: SLF001

        captured = capsys.readouterr()
        assert "sovyx token" in captured.out

    def test_banner_has_box_drawing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Banner uses box drawing characters."""
        token_file = tmp_path / "token"
        token_file.write_text("t")
        monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", token_file)

        lm = self._make_lifecycle()
        lm._print_startup_banner()  # noqa: SLF001

        captured = capsys.readouterr()
        assert "╔" in captured.out
        assert "╚" in captured.out
