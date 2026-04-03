"""Tests for sovyx.engine.bootstrap — Bootstrap + MindManager."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from sovyx.engine.bootstrap import MindManager, bootstrap
from sovyx.engine.config import DatabaseConfig, EngineConfig
from sovyx.engine.registry import ServiceRegistry
from sovyx.mind.config import MindConfig
from sovyx.persistence.manager import DatabaseManager


class TestMindManager:
    """MindManager lifecycle."""

    async def test_load_and_start(self) -> None:
        mgr = MindManager()
        await mgr.load_mind("aria", {"brain": "mock"})
        await mgr.start_mind("aria")
        assert mgr.get_active_minds() == ["aria"]

    async def test_stop_mind(self) -> None:
        mgr = MindManager()
        await mgr.start_mind("aria")
        await mgr.stop_mind("aria")
        assert mgr.get_active_minds() == []

    async def test_double_start_idempotent(self) -> None:
        mgr = MindManager()
        await mgr.start_mind("aria")
        await mgr.start_mind("aria")
        assert len(mgr.get_active_minds()) == 1

    async def test_stop_nonexistent_no_crash(self) -> None:
        mgr = MindManager()
        await mgr.stop_mind("ghost")
        assert mgr.get_active_minds() == []


class TestBootstrap:
    """Bootstrap sequence integration."""

    async def test_returns_registry(self, tmp_path: Path) -> None:
        """Bootstrap returns a ServiceRegistry with services registered."""
        config = EngineConfig(database=DatabaseConfig(data_dir=tmp_path))
        mind = MindConfig(name="TestMind")
        registry = await bootstrap(config, [mind])

        assert isinstance(registry, ServiceRegistry)

        # Core services should be registered
        from sovyx.brain.service import BrainService
        from sovyx.bridge.identity import PersonResolver
        from sovyx.bridge.manager import BridgeManager
        from sovyx.bridge.sessions import ConversationTracker
        from sovyx.cognitive.gate import CogLoopGate
        from sovyx.cognitive.loop import CognitiveLoop
        from sovyx.context.assembler import ContextAssembler
        from sovyx.engine.events import EventBus
        from sovyx.llm.router import LLMRouter
        from sovyx.mind.personality import PersonalityEngine
        from sovyx.persistence.manager import DatabaseManager

        assert registry.is_registered(EventBus)
        assert registry.is_registered(DatabaseManager)
        assert registry.is_registered(BrainService)
        assert registry.is_registered(PersonalityEngine)
        assert registry.is_registered(ContextAssembler)
        assert registry.is_registered(LLMRouter)
        assert registry.is_registered(CognitiveLoop)
        assert registry.is_registered(CogLoopGate)
        assert registry.is_registered(PersonResolver)
        assert registry.is_registered(ConversationTracker)
        assert registry.is_registered(BridgeManager)
        assert registry.is_registered(MindManager)

        # Cleanup
        db = await registry.resolve(DatabaseManager)
        await db.stop()

    async def test_mind_started(self, tmp_path: Path) -> None:
        """Mind is started after bootstrap."""
        config = EngineConfig(database=DatabaseConfig(data_dir=tmp_path))
        mind = MindConfig(name="Aria")
        registry = await bootstrap(config, [mind])

        mgr = await registry.resolve(MindManager)
        assert "aria" in mgr.get_active_minds()

        db = await registry.resolve(DatabaseManager)
        await db.stop()

    async def test_no_minds_raises(self, tmp_path: Path) -> None:
        """Empty mind_configs raises ValueError."""
        config = EngineConfig(database=DatabaseConfig(data_dir=tmp_path))
        with pytest.raises(ValueError, match="No minds"):
            await bootstrap(config, [])

    async def test_ollama_always_present(self, tmp_path: Path) -> None:
        """Ollama provider is always in the router (no API key needed)."""
        config = EngineConfig(database=DatabaseConfig(data_dir=tmp_path))
        mind = MindConfig(name="Test")
        registry = await bootstrap(config, [mind])

        from sovyx.llm.router import LLMRouter

        router = await registry.resolve(LLMRouter)
        provider_names = [p.name for p in router._providers]
        assert "ollama" in provider_names

        db = await registry.resolve(DatabaseManager)
        await db.stop()

    async def test_databases_created(self, tmp_path: Path) -> None:
        """Bootstrap creates database files."""
        config = EngineConfig(database=DatabaseConfig(data_dir=tmp_path))
        mind = MindConfig(name="Test")
        registry = await bootstrap(config, [mind])

        assert (tmp_path / "system.db").exists()
        assert (tmp_path / "test" / "brain.db").exists()
        assert (tmp_path / "test" / "conversations.db").exists()

        db = await registry.resolve(DatabaseManager)
        await db.stop()
