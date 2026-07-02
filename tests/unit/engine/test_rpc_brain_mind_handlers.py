"""Tests for the ``brain.search`` / ``brain.stats`` / ``mind.status`` RPC handlers.

AP #53 closure — these three methods were called by ``cli/main.py``
since the commands shipped but never registered by any daemon, so the
commands always failed with 'Method not found' (the RPC parity test
carried them in its self-sunsetting allowlist). The handlers are the
producer half of the contract; the consumers are
``cli/main.py::brain_search`` (renders a list), ``::brain_stats`` and
``::mind_status`` (render dict entries as ``key: value`` lines).

Mirrors ``test_rpc_doctor_handler.py``: direct handler-level behavior
plus full producer→wire→consumer round-trips over a real socket.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from sovyx.brain.models import Concept
from sovyx.cli.rpc_client import DaemonClient
from sovyx.engine._rpc_handlers import register_cli_handlers
from sovyx.engine.bootstrap import MindManager
from sovyx.engine.rpc_server import DaemonRPCServer
from sovyx.engine.types import ConceptCategory, MindId

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _FakeServiceRegistry:
    """Minimal ServiceRegistry twin — nothing registered by default."""

    def __init__(self) -> None:
        self._instances: dict[type, object] = {}

    def register_instance(self, cls: type, instance: object) -> None:
        self._instances[cls] = instance

    def is_registered(self, cls: type) -> bool:
        return cls in self._instances

    async def resolve(self, cls: type) -> object:
        return self._instances[cls]


class _StubBrainService:
    """BrainService twin returning canned ``(Concept, score)`` tuples."""

    def __init__(self, results: list[tuple[Concept, float]]) -> None:
        self._results = results
        self.calls: list[tuple[str, str, int]] = []

    async def search(
        self,
        query: str,
        mind_id: MindId,
        limit: int = 10,
    ) -> list[tuple[Concept, float]]:
        self.calls.append((query, str(mind_id), limit))
        return self._results[:limit]


class _StubCountRepo:
    """Concept/Episode repository twin exposing only ``count``."""

    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self, mind_id: MindId) -> int:
        return self._count


class _StubCursor:
    def __init__(self, count: int) -> None:
        self._count = count

    async def fetchone(self) -> tuple[int]:
        return (self._count,)


class _StubConnection:
    def __init__(self, count: int) -> None:
        self._count = count

    async def execute(self, sql: str, params: tuple[str, ...]) -> _StubCursor:
        return _StubCursor(self._count)


class _StubBrainPool:
    def __init__(self, relation_count: int) -> None:
        self._relation_count = relation_count

    @asynccontextmanager
    async def read(self) -> AsyncIterator[_StubConnection]:
        yield _StubConnection(self._relation_count)


class _StubDatabaseManager:
    """DatabaseManager twin serving a single stub brain pool."""

    def __init__(self, relation_count: int) -> None:
        self._pool = _StubBrainPool(relation_count)

    def get_brain_pool(self, mind_id: MindId) -> _StubBrainPool:
        return self._pool


def _build_rpc(registry: object) -> DaemonRPCServer:
    rpc = DaemonRPCServer()
    register_cli_handlers(rpc, registry)  # type: ignore[arg-type]
    return rpc


def _concept(name: str, *, importance: float = 0.9, confidence: float = 0.8) -> Concept:
    return Concept(
        mind_id=MindId("default"),
        name=name,
        content=f"content of {name}",
        category=ConceptCategory.FACT,
        importance=importance,
        confidence=confidence,
    )


def _register_brain(
    registry: _FakeServiceRegistry,
    *,
    concepts: int = 0,
    episodes: int = 0,
) -> None:
    """Register concept + episode repository twins under the real keys."""
    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.episode_repo import EpisodeRepository

    registry.register_instance(ConceptRepository, _StubCountRepo(concepts))
    registry.register_instance(EpisodeRepository, _StubCountRepo(episodes))


def _register_engine_config(registry: _FakeServiceRegistry, data_dir: Path) -> None:
    """Register a ``data_dir``-only EngineConfig twin under the real key."""
    from sovyx.engine.config import EngineConfig

    registry.register_instance(EngineConfig, SimpleNamespace(data_dir=data_dir))


def _write_mind_yaml(data_dir: Path, mind_id: str, *, language: str = "pt") -> None:
    mind_dir = data_dir / mind_id
    mind_dir.mkdir(parents=True, exist_ok=True)
    (mind_dir / "mind.yaml").write_text(
        f"name: Sovyx\nlanguage: {language}\n",
        encoding="utf-8",
    )


class TestRegistration:
    """AP #53 closure anchor: the daemon serves all three methods."""

    def test_all_three_methods_registered(self) -> None:
        rpc = _build_rpc(_FakeServiceRegistry())
        assert {"brain.search", "brain.stats", "mind.status"} <= set(rpc._methods)  # noqa: SLF001


class TestBrainSearchHandler:
    async def test_projects_brain_service_results(self) -> None:
        registry = _FakeServiceRegistry()
        paris = _concept("Paris")
        brain = _StubBrainService([(paris, 0.87654)])

        from sovyx.brain.service import BrainService

        registry.register_instance(BrainService, brain)
        rpc = _build_rpc(registry)

        result = await rpc._methods["brain.search"](query="capital", mind_id="default")  # noqa: SLF001

        # The CLI consumer branches on ``isinstance(result, list)``.
        assert isinstance(result, list)
        assert result == [
            {
                "id": str(paris.id),
                "name": "Paris",
                "category": "fact",
                "importance": 0.9,
                "confidence": 0.8,
                "score": 0.8765,
            },
        ]
        assert brain.calls == [("capital", "default", 5)]

    async def test_forwards_mind_id_and_clamps_limit(self) -> None:
        registry = _FakeServiceRegistry()
        brain = _StubBrainService([])

        from sovyx.brain.service import BrainService

        registry.register_instance(BrainService, brain)
        rpc = _build_rpc(registry)

        await rpc._methods["brain.search"](query="q", mind_id="luna", limit=500)  # noqa: SLF001
        await rpc._methods["brain.search"](query="q", mind_id="luna", limit=0)  # noqa: SLF001

        assert brain.calls == [("q", "luna", 100), ("q", "luna", 1)]

    async def test_blank_query_returns_empty_list_without_brain(self) -> None:
        """Mirrors the dashboard's graceful contract — a blank query is a
        no-op even when the brain subsystem is absent."""
        rpc = _build_rpc(_FakeServiceRegistry())
        assert await rpc._methods["brain.search"](query="   ") == []  # noqa: SLF001

    async def test_missing_brain_service_raises_brain_error(self) -> None:
        rpc = _build_rpc(_FakeServiceRegistry())
        with pytest.raises(Exception) as exc_info:  # noqa: PT011 — xdist-safe name check below
            await rpc._methods["brain.search"](query="capital")  # noqa: SLF001
        assert type(exc_info.value).__name__ == "BrainError"
        assert "not available" in str(exc_info.value)


class TestBrainStatsHandler:
    async def test_counts_all_three_axes(self) -> None:
        registry = _FakeServiceRegistry()
        _register_brain(registry, concepts=42, episodes=7)

        from sovyx.persistence.manager import DatabaseManager

        registry.register_instance(DatabaseManager, _StubDatabaseManager(relation_count=13))
        rpc = _build_rpc(registry)

        result = await rpc._methods["brain.stats"](mind_id="default")  # noqa: SLF001

        # The CLI consumer branches on ``isinstance(result, dict)``.
        assert result == {
            "mind_id": "default",
            "concepts": 42,
            "episodes": 7,
            "relations": 13,
        }

    async def test_missing_repositories_raise_brain_error(self) -> None:
        rpc = _build_rpc(_FakeServiceRegistry())
        with pytest.raises(Exception) as exc_info:  # noqa: PT011 — xdist-safe name check below
            await rpc._methods["brain.stats"](mind_id="default")  # noqa: SLF001
        assert type(exc_info.value).__name__ == "BrainError"
        assert "not available" in str(exc_info.value)

    async def test_relation_count_degrades_to_zero_without_database_manager(self) -> None:
        """Relations have no repository counter — that axis alone is
        best-effort; concept/episode counts must still be real."""
        registry = _FakeServiceRegistry()
        _register_brain(registry, concepts=3, episodes=2)
        rpc = _build_rpc(registry)

        result = await rpc._methods["brain.stats"](mind_id="default")  # noqa: SLF001

        assert result == {
            "mind_id": "default",
            "concepts": 3,
            "episodes": 2,
            "relations": 0,
        }

    async def test_empty_mind_id_raises_value_error(self) -> None:
        registry = _FakeServiceRegistry()
        _register_brain(registry)
        rpc = _build_rpc(registry)
        with pytest.raises(Exception) as exc_info:  # noqa: PT011 — xdist-safe name check below
            await rpc._methods["brain.stats"](mind_id="   ")  # noqa: SLF001
        assert type(exc_info.value).__name__ == "ValueError"


class TestMindStatusHandler:
    async def test_active_onboarded_mind_full_snapshot(self, tmp_path: Path) -> None:
        registry = _FakeServiceRegistry()
        mgr = MindManager()
        await mgr.start_mind("default")
        registry.register_instance(MindManager, mgr)
        _register_engine_config(registry, tmp_path)
        _write_mind_yaml(tmp_path, "default", language="pt")
        _register_brain(registry, concepts=42, episodes=7)
        rpc = _build_rpc(registry)

        result = await rpc._methods["mind.status"](mind_id="default")  # noqa: SLF001

        # The CLI consumer branches on ``isinstance(result, dict)``.
        assert result == {
            "mind_id": "default",
            "active": True,
            "name": "Sovyx",
            "language": "pt",
            "concepts": 42,
            "episodes": 7,
        }

    async def test_onboarded_inactive_mind_reports_active_false(self, tmp_path: Path) -> None:
        registry = _FakeServiceRegistry()
        registry.register_instance(MindManager, MindManager())
        _register_engine_config(registry, tmp_path)
        _write_mind_yaml(tmp_path, "luna")
        rpc = _build_rpc(registry)

        result = await rpc._methods["mind.status"](mind_id="luna")  # noqa: SLF001

        assert result["active"] is False
        assert result["name"] == "Sovyx"
        assert result["concepts"] == 0
        assert result["episodes"] == 0

    async def test_unknown_mind_raises_mind_not_found(self, tmp_path: Path) -> None:
        registry = _FakeServiceRegistry()
        registry.register_instance(MindManager, MindManager())
        _register_engine_config(registry, tmp_path)
        rpc = _build_rpc(registry)

        with pytest.raises(Exception) as exc_info:  # noqa: PT011 — xdist-safe name check below
            await rpc._methods["mind.status"](mind_id="typo-mind")  # noqa: SLF001
        assert type(exc_info.value).__name__ == "MindNotFoundError"

    async def test_active_mind_without_yaml_reports_none_identity(self) -> None:
        """Active but no EngineConfig / mind.yaml: honest ``None`` identity
        (AP #48 — never fabricate), no crash, no MindNotFound."""
        registry = _FakeServiceRegistry()
        mgr = MindManager()
        await mgr.start_mind("default")
        registry.register_instance(MindManager, mgr)
        rpc = _build_rpc(registry)

        result = await rpc._methods["mind.status"](mind_id="default")  # noqa: SLF001

        assert result["active"] is True
        assert result["name"] is None
        assert result["language"] is None

    async def test_empty_mind_id_raises_value_error(self) -> None:
        rpc = _build_rpc(_FakeServiceRegistry())
        with pytest.raises(Exception) as exc_info:  # noqa: PT011 — xdist-safe name check below
            await rpc._methods["mind.status"](mind_id="")  # noqa: SLF001
        assert type(exc_info.value).__name__ == "ValueError"


class TestRoundTrip:
    """Full producer→wire→consumer round-trips (AP #40/#53 siblings).

    Exercises the exact ``DaemonClient.call`` path ``cli/main.py`` uses,
    proving each payload is JSON-serialisable and lands in the shape
    the CLI consumer branches on.
    """

    async def test_brain_search_round_trip_returns_list(
        self,
        short_socket_path: Path,
    ) -> None:
        registry = _FakeServiceRegistry()
        paris = _concept("Paris")

        from sovyx.brain.service import BrainService

        registry.register_instance(BrainService, _StubBrainService([(paris, 0.9)]))

        server = DaemonRPCServer(short_socket_path)
        register_cli_handlers(server, registry)  # type: ignore[arg-type]

        await server.start()
        try:
            client = DaemonClient(short_socket_path)
            result = await client.call(
                "brain.search",
                {"query": "capital", "mind_id": "default", "limit": 5},
            )
        finally:
            await server.stop()

        assert isinstance(result, list)
        assert result[0]["name"] == "Paris"
        assert result[0]["id"] == str(paris.id)

    async def test_brain_stats_round_trip_returns_dict(
        self,
        short_socket_path: Path,
    ) -> None:
        registry = _FakeServiceRegistry()
        _register_brain(registry, concepts=1, episodes=2)

        server = DaemonRPCServer(short_socket_path)
        register_cli_handlers(server, registry)  # type: ignore[arg-type]

        await server.start()
        try:
            client = DaemonClient(short_socket_path)
            result = await client.call("brain.stats", {"mind_id": "default"})
        finally:
            await server.stop()

        assert result == {"mind_id": "default", "concepts": 1, "episodes": 2, "relations": 0}

    async def test_mind_status_round_trip_returns_dict(
        self,
        short_socket_path: Path,
        tmp_path: Path,
    ) -> None:
        registry = _FakeServiceRegistry()
        mgr = MindManager()
        await mgr.start_mind("default")
        registry.register_instance(MindManager, mgr)
        _register_engine_config(registry, tmp_path)
        _write_mind_yaml(tmp_path, "default")

        server = DaemonRPCServer(short_socket_path)
        register_cli_handlers(server, registry)  # type: ignore[arg-type]

        await server.start()
        try:
            client = DaemonClient(short_socket_path)
            result = await client.call("mind.status", {"mind_id": "default"})
        finally:
            await server.stop()

        assert isinstance(result, dict)
        assert result["active"] is True
        assert result["name"] == "Sovyx"

    async def test_handler_error_surfaces_as_structured_rpc_error(
        self,
        short_socket_path: Path,
    ) -> None:
        """An absent brain subsystem yields a JSON-RPC error response the
        CLI renders as a message — never a hung connection or crash."""
        server = DaemonRPCServer(short_socket_path)
        register_cli_handlers(server, _FakeServiceRegistry())  # type: ignore[arg-type]

        await server.start()
        try:
            client = DaemonClient(short_socket_path)
            with pytest.raises(Exception) as exc_info:  # noqa: PT011 — xdist-safe name check below
                await client.call("brain.stats", {"mind_id": "default"})
        finally:
            await server.stop()

        assert type(exc_info.value).__name__ == "ChannelConnectionError"
        assert "not available" in str(exc_info.value)

    async def test_unknown_mind_round_trip_error_names_the_mind(
        self,
        short_socket_path: Path,
        tmp_path: Path,
    ) -> None:
        registry = _FakeServiceRegistry()
        registry.register_instance(MindManager, MindManager())
        _register_engine_config(registry, tmp_path)

        server = DaemonRPCServer(short_socket_path)
        register_cli_handlers(server, registry)  # type: ignore[arg-type]

        await server.start()
        try:
            client = DaemonClient(short_socket_path)
            with pytest.raises(Exception) as exc_info:  # noqa: PT011 — xdist-safe name check below
                await client.call("mind.status", {"mind_id": "ghost"})
        finally:
            await server.stop()

        assert "ghost" in str(exc_info.value)
