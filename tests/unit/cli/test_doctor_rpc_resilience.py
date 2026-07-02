"""``sovyx doctor`` RPC-section resilience (DOCTOR-1 consumer half).

Covers the daemon-running branch of ``_run_general_doctor``:

* payload from the ``doctor`` RPC renders as check rows (happy path);
* method-not-found (-32601, daemon older than this CLI) renders a
  YELLOW informational row, never a RED failure;
* genuine transport errors stay RED;
* the call carries the extended online-checks budget (the client
  default of 5s sits below the daemon's 8s sweep bound).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from typer.testing import CliRunner

from sovyx.cli.commands.doctor import (
    _ONLINE_CHECKS_RPC_TIMEOUT_S,
    _is_rpc_method_not_found,
)
from sovyx.cli.main import app
from sovyx.engine.errors import ChannelConnectionError

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _async_payload(payload: object) -> Any:  # noqa: ANN401 — mock side_effect
    """Return a side_effect that yields a fresh coroutine per call."""

    async def _call(*_args: object, **_kwargs: object) -> object:
        return payload

    return _call


def _invoke_doctor_json(mock_client: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    parsed: list[dict[str, Any]] = json.loads(result.stdout)
    assert mock_client.return_value.is_daemon_running.called
    return parsed


class TestDoctorRpcSection:
    def test_online_payload_renders_check_rows(self, tmp_path: Path) -> None:
        payload = {
            "overall": "yellow",
            "check_count": 2,
            "checks": {
                "Database": {
                    "status": "green",
                    "message": "Database writable",
                    "metadata": {"latency_ms": 1.0},
                },
                "LLM Providers": {
                    "status": "yellow",
                    "message": "LLM check not configured",
                },
            },
        }
        with (
            patch("sovyx.cli.commands.doctor.DaemonClient") as mock_client,
            patch("sovyx.cli.commands.doctor.Path.home", return_value=tmp_path),
        ):
            mock_client.return_value.is_daemon_running.return_value = True
            mock_client.return_value.call.side_effect = _async_payload(payload)
            rows = _invoke_doctor_json(mock_client)

        by_name = {r["name"]: r for r in rows}
        assert by_name["Database"]["status"] == "green"
        assert by_name["Database"]["metadata"] == {"latency_ms": 1.0}
        assert by_name["LLM Providers"]["status"] == "yellow"
        assert "Daemon RPC" not in by_name

    def test_call_uses_extended_online_checks_budget(self, tmp_path: Path) -> None:
        with (
            patch("sovyx.cli.commands.doctor.DaemonClient") as mock_client,
            patch("sovyx.cli.commands.doctor.Path.home", return_value=tmp_path),
        ):
            mock_client.return_value.is_daemon_running.return_value = True
            mock_client.return_value.call.side_effect = _async_payload({"checks": {}})
            _invoke_doctor_json(mock_client)

        mock_client.return_value.call.assert_called_once_with(
            "doctor",
            timeout=_ONLINE_CHECKS_RPC_TIMEOUT_S,
        )

    def test_method_not_found_renders_yellow_informational(self, tmp_path: Path) -> None:
        """Daemon predating the ``doctor`` RPC: informational, not a failure."""
        with (
            patch("sovyx.cli.commands.doctor.DaemonClient") as mock_client,
            patch("sovyx.cli.commands.doctor.Path.home", return_value=tmp_path),
        ):
            mock_client.return_value.is_daemon_running.return_value = True
            mock_client.return_value.call.side_effect = ChannelConnectionError(
                "RPC error (-32601): Method not found: doctor",
            )
            rows = _invoke_doctor_json(mock_client)

        by_name = {r["name"]: r for r in rows}
        assert by_name["Daemon RPC"]["status"] == "yellow"
        assert "does not support online checks" in by_name["Daemon RPC"]["message"]
        assert "restart the daemon" in by_name["Daemon RPC"]["message"]

    def test_transport_error_stays_red(self, tmp_path: Path) -> None:
        with (
            patch("sovyx.cli.commands.doctor.DaemonClient") as mock_client,
            patch("sovyx.cli.commands.doctor.Path.home", return_value=tmp_path),
        ):
            mock_client.return_value.is_daemon_running.return_value = True
            mock_client.return_value.call.side_effect = ChannelConnectionError(
                "Cannot connect to daemon: connection reset",
            )
            rows = _invoke_doctor_json(mock_client)

        by_name = {r["name"]: r for r in rows}
        assert by_name["Daemon RPC"]["status"] == "red"
        assert "RPC call failed" in by_name["Daemon RPC"]["message"]

    def test_server_side_handler_error_stays_red(self, tmp_path: Path) -> None:
        """A -32000 (handler raised) is a genuine failure, not version skew."""
        with (
            patch("sovyx.cli.commands.doctor.DaemonClient") as mock_client,
            patch("sovyx.cli.commands.doctor.Path.home", return_value=tmp_path),
        ):
            mock_client.return_value.is_daemon_running.return_value = True
            mock_client.return_value.call.side_effect = ChannelConnectionError(
                "RPC error (-32000): boom",
            )
            rows = _invoke_doctor_json(mock_client)

        by_name = {r["name"]: r for r in rows}
        assert by_name["Daemon RPC"]["status"] == "red"

    def test_daemon_not_running_adds_no_rpc_rows(self, tmp_path: Path) -> None:
        """Existing offline-only behavior is preserved."""
        with (
            patch("sovyx.cli.commands.doctor.DaemonClient") as mock_client,
            patch("sovyx.cli.commands.doctor.Path.home", return_value=tmp_path),
        ):
            mock_client.return_value.is_daemon_running.return_value = False
            rows = _invoke_doctor_json(mock_client)

        names = {r["name"] for r in rows}
        assert "Daemon RPC" not in names
        mock_client.return_value.call.assert_not_called()


class TestIsRpcMethodNotFound:
    def test_matches_client_shaped_message(self) -> None:
        exc = ChannelConnectionError("RPC error (-32601): Method not found: doctor")
        assert _is_rpc_method_not_found(exc) is True

    def test_rejects_other_rpc_codes_and_transport_errors(self) -> None:
        assert (
            _is_rpc_method_not_found(ChannelConnectionError("RPC error (-32000): boom")) is False
        )
        assert (
            _is_rpc_method_not_found(
                ChannelConnectionError("Cannot connect to daemon: refused"),
            )
            is False
        )
        assert (
            _is_rpc_method_not_found(ChannelConnectionError("Daemon response timeout (10.0s)"))
            is False
        )
