"""Tests for dashboard lifecycle integration with Engine."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.dashboard.server import DashboardServer

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clean_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("test-token")
    monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", token_file)


class TestDashboardServer:
    """Test DashboardServer start/stop lifecycle."""

    @pytest.mark.asyncio()
    async def test_start_creates_app(self) -> None:
        server = DashboardServer()
        mock_uvi_server = MagicMock()
        mock_uvi_server.serve = AsyncMock()

        with (
            patch("uvicorn.Config") as mock_config,
            patch("uvicorn.Server", return_value=mock_uvi_server),
        ):
            mock_config.return_value = MagicMock()
            await server.start()

            assert server.app is not None
            assert server.ws_manager is not None

    @pytest.mark.asyncio()
    async def test_start_with_custom_config(self) -> None:
        from sovyx.engine.config import APIConfig

        config = APIConfig(host="0.0.0.0", port=9999)
        server = DashboardServer(config=config)
        mock_uvi_server = MagicMock()
        mock_uvi_server.serve = AsyncMock()

        with (
            patch("uvicorn.Config") as mock_config,
            patch("uvicorn.Server", return_value=mock_uvi_server),
        ):
            mock_config.return_value = MagicMock()
            await server.start()

            call_kwargs = mock_config.call_args
            assert call_kwargs.kwargs["host"] == "0.0.0.0"
            assert call_kwargs.kwargs["port"] == 9999

    @pytest.mark.asyncio()
    async def test_stop_sets_should_exit(self) -> None:
        server = DashboardServer()
        mock_uvi_server = MagicMock()
        server._server = mock_uvi_server

        await server.stop()

        assert mock_uvi_server.should_exit is True

    @pytest.mark.asyncio()
    async def test_stop_noop_when_not_started(self) -> None:
        server = DashboardServer()
        # Should not raise
        await server.stop()

    @pytest.mark.asyncio()
    async def test_ws_manager_accessible_after_start(self) -> None:

        server = DashboardServer()
        mock_uvi_server = MagicMock()
        mock_uvi_server.serve = AsyncMock()

        with (
            patch("uvicorn.Config", return_value=MagicMock()),
            patch("uvicorn.Server", return_value=mock_uvi_server),
        ):
            await server.start()
            assert type(server.ws_manager).__name__ == "ConnectionManager"


class TestDashboardCLI:
    """Test 'sovyx dashboard' CLI command."""

    def test_dashboard_info(self) -> None:
        from typer.testing import CliRunner

        from sovyx.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["dashboard"])
        assert result.exit_code == 0
        assert "Sovyx Dashboard" in result.output
        assert "7777" in result.output

    def test_dashboard_with_token_flag(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from sovyx.cli.main import app

        # Write a known token
        token_file = tmp_path / "token"
        token_file.write_text("my-test-token-123")

        runner = CliRunner()
        with patch("sovyx.cli.commands.dashboard.TOKEN_FILE", token_file):
            result = runner.invoke(app, ["dashboard", "--token"])

        assert result.exit_code == 0
        assert "my-test-token-123" in result.output
