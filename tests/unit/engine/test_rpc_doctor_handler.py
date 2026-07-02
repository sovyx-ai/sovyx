"""Tests for the daemon-side ``doctor`` RPC handler (DOCTOR-1 closure).

The handler is the producer half of the ``sovyx doctor`` online-check
contract; the consumer half is
``sovyx.cli.commands.doctor._online_checks_from_rpc``. The round-trip
test parses the handler's payload with the REAL consumer helper so
producer/consumer drift fails here first (AP #40/#53 — one shared
symbol, never two independent reimplementations).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import sovyx.engine._rpc_handlers as rpc_handlers_module
from sovyx.cli.commands.doctor import _online_checks_from_rpc
from sovyx.cli.rpc_client import DaemonClient
from sovyx.engine._rpc_handlers import register_cli_handlers
from sovyx.engine.rpc_server import DaemonRPCServer
from sovyx.observability.health import (
    CheckResult,
    CheckStatus,
    HealthCheck,
    HealthRegistry,
)

if TYPE_CHECKING:
    from pathlib import Path

# The six online checks ``create_engine_health_registry`` wires — the
# set the doctor callback docstring and docs/getting-started.md promise.
_ONLINE_CHECK_NAMES = {
    "Database",
    "Brain Index",
    "LLM Providers",
    "Channels",
    "Consolidation",
    "Cost Budget",
}


class _FakeServiceRegistry:
    """Minimal ServiceRegistry twin — nothing registered by default.

    ``create_engine_health_registry`` probes ``is_registered`` per
    subsystem; with everything absent each online check degrades to
    its own "not configured" row, which is exactly the graceful-
    degradation contract the handler must preserve.
    """

    def __init__(self) -> None:
        self._instances: dict[type, object] = {}

    def register_instance(self, cls: type, instance: object) -> None:
        self._instances[cls] = instance

    def is_registered(self, cls: type) -> bool:
        return cls in self._instances

    async def resolve(self, cls: type) -> object:
        return self._instances[cls]


class _StubCheck(HealthCheck):
    """Deterministic check with an optional artificial delay."""

    def __init__(
        self,
        name: str = "Stub Check",
        status: CheckStatus = CheckStatus.GREEN,
        message: str = "stub ok",
        metadata: dict[str, Any] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._name = name
        self._status = status
        self._message = message
        self._metadata = metadata or {}
        self._delay_s = delay_s

    @property
    def name(self) -> str:
        return self._name

    async def check(self) -> CheckResult:
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return CheckResult(
            name=self._name,
            status=self._status,
            message=self._message,
            metadata=self._metadata,
        )


def _build_rpc(registry: object) -> DaemonRPCServer:
    rpc = DaemonRPCServer()
    register_cli_handlers(rpc, registry)  # type: ignore[arg-type]
    return rpc


class TestDoctorHandler:
    """Direct handler-level behavior (no socket)."""

    async def test_doctor_method_is_registered(self) -> None:
        rpc = _build_rpc(_FakeServiceRegistry())
        assert "doctor" in rpc._methods  # noqa: SLF001

    async def test_fallback_builds_engine_registry_with_all_online_checks(self) -> None:
        """No HealthRegistry singleton → handler wires one from the registry.

        With every subsystem unregistered each check reports its own
        "not configured" degradation instead of crashing the handler.
        """
        rpc = _build_rpc(_FakeServiceRegistry())
        result = await rpc._methods["doctor"]()  # noqa: SLF001

        assert isinstance(result, dict)
        assert set(result["checks"]) == _ONLINE_CHECK_NAMES
        assert result["check_count"] == len(_ONLINE_CHECK_NAMES)
        assert result["overall"] in {s.value for s in CheckStatus}
        for check_data in result["checks"].values():
            assert check_data["status"] in {s.value for s in CheckStatus}
            assert isinstance(check_data["message"], str)
            assert isinstance(check_data["metadata"], dict)

    async def test_reuses_bootstrap_health_registry_singleton(self) -> None:
        """Bootstrap-registered singleton is resolved, never rebuilt."""
        registry = _FakeServiceRegistry()
        health = HealthRegistry()
        health.register(_StubCheck(name="Only Check", message="singleton wins"))
        registry.register_instance(HealthRegistry, health)

        rpc = _build_rpc(registry)
        result = await rpc._methods["doctor"]()  # noqa: SLF001

        assert set(result["checks"]) == {"Only Check"}
        assert result["checks"]["Only Check"]["message"] == "singleton wins"
        assert result["overall"] == "green"

    async def test_slow_check_yields_partial_results(self) -> None:
        """A check exceeding the per-check budget becomes its own RED row
        while its siblings still report — partial results, never a hang."""
        registry = _FakeServiceRegistry()
        health = HealthRegistry()
        health.register(_StubCheck(name="Fast Check"))
        health.register(_StubCheck(name="Slow Check", delay_s=30.0))
        registry.register_instance(HealthRegistry, health)

        rpc = _build_rpc(registry)
        with patch.object(rpc_handlers_module, "_DOCTOR_CHECK_TIMEOUT_S", 0.05):
            result = await rpc._methods["doctor"]()  # noqa: SLF001

        assert result["checks"]["Fast Check"]["status"] == "green"
        assert result["checks"]["Slow Check"]["status"] == "red"
        assert "timed out" in result["checks"]["Slow Check"]["message"]

    async def test_total_timeout_returns_note_instead_of_hanging(self) -> None:
        """A pathological sweep stall hits the outer bound and returns a
        synthetic RED row + ``note="timed_out"``."""

        class _HangingRegistry(HealthRegistry):
            async def run_all(self, timeout: float = 10.0) -> list[CheckResult]:
                await asyncio.sleep(30.0)
                return []

        registry = _FakeServiceRegistry()
        registry.register_instance(HealthRegistry, _HangingRegistry())

        rpc = _build_rpc(registry)
        with patch.object(rpc_handlers_module, "_DOCTOR_TOTAL_TIMEOUT_S", 0.05):
            result = await rpc._methods["doctor"]()  # noqa: SLF001

        assert result["note"] == "timed_out"
        assert result["overall"] == "red"
        only_row = result["checks"]["Online Checks"]
        assert only_row["status"] == "red"
        assert "timed out" in only_row["message"]

    async def test_client_budget_exceeds_daemon_sweep_bound(self) -> None:
        """The CLI call budget must stay above the daemon's outer bound so
        a slow sweep surfaces as check rows, not a transport error."""
        from sovyx.cli.commands.doctor import _ONLINE_CHECKS_RPC_TIMEOUT_S

        assert _ONLINE_CHECKS_RPC_TIMEOUT_S > rpc_handlers_module._DOCTOR_TOTAL_TIMEOUT_S  # noqa: SLF001


class TestDoctorRoundTrip:
    """Full producer→wire→consumer round-trip (AP #40 sibling)."""

    async def test_round_trip_parses_through_real_consumer(
        self,
        short_socket_path: Path,
    ) -> None:
        registry = _FakeServiceRegistry()
        health = HealthRegistry()
        health.register(
            _StubCheck(
                name="Database",
                status=CheckStatus.GREEN,
                message="Database writable",
                metadata={"latency_ms": 1.2},
            ),
        )
        health.register(
            _StubCheck(
                name="LLM Providers",
                status=CheckStatus.YELLOW,
                message="LLM check not configured",
            ),
        )
        registry.register_instance(HealthRegistry, health)

        server = DaemonRPCServer(short_socket_path)
        register_cli_handlers(server, registry)  # type: ignore[arg-type]

        await server.start()
        try:
            client = DaemonClient(short_socket_path)
            rpc_result = await client.call("doctor")
        finally:
            await server.stop()

        rows = _online_checks_from_rpc(rpc_result)
        by_name = {r.name: r for r in rows}
        assert set(by_name) == {"Database", "LLM Providers"}
        assert by_name["Database"].status is CheckStatus.GREEN
        assert by_name["Database"].message == "Database writable"
        assert by_name["Database"].metadata == {"latency_ms": 1.2}
        assert by_name["LLM Providers"].status is CheckStatus.YELLOW

    async def test_round_trip_default_wiring_contains_promised_checks(
        self,
        short_socket_path: Path,
    ) -> None:
        """Fallback wiring over an empty registry still round-trips every
        online check name the docs promise."""
        server = DaemonRPCServer(short_socket_path)
        register_cli_handlers(server, _FakeServiceRegistry())  # type: ignore[arg-type]

        await server.start()
        try:
            client = DaemonClient(short_socket_path)
            rpc_result = await client.call("doctor")
        finally:
            await server.stop()

        rows = _online_checks_from_rpc(rpc_result)
        assert {r.name for r in rows} == _ONLINE_CHECK_NAMES
        assert all(isinstance(r.status, CheckStatus) for r in rows)
