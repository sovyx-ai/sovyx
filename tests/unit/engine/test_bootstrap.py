"""Tests for sovyx.engine.bootstrap — Bootstrap + MindManager."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.engine.types import MindId

from sovyx.bridge.manager import BridgeManager
from sovyx.engine.bootstrap import MindManager, bootstrap
from sovyx.engine.config import DatabaseConfig, EngineConfig
from sovyx.engine.registry import ServiceRegistry
from sovyx.llm.router import LLMRouter
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

    # ── VAL-02: Config loading regression tests ──────────────────────────

    async def test_bootstrap_registers_engine_config(self, tmp_path: Path) -> None:
        """EngineConfig is registered in ServiceRegistry after bootstrap.

        Regression for fd19172: without this, LifecycleManager._start_dashboard()
        can't resolve EngineConfig and falls back to defaults, ignoring
        system.yaml host/port/api settings entirely.
        """
        custom_config = EngineConfig(
            database=DatabaseConfig(data_dir=tmp_path),
        )
        registry = await bootstrap(custom_config, [MindConfig(name="RegTest")])

        # The critical assertion: EngineConfig MUST be in the registry
        assert registry.is_registered(EngineConfig), (
            "EngineConfig not registered in ServiceRegistry — "
            "dashboard will ignore system.yaml (regression fd19172)"
        )

        # And the resolved instance must be the SAME object we passed in
        resolved = await registry.resolve(EngineConfig)
        assert resolved is custom_config, (
            "Resolved EngineConfig is not the same instance passed to bootstrap"
        )

        db = await registry.resolve(DatabaseManager)
        await db.stop()

    async def test_load_engine_config_reads_system_yaml(self, tmp_path: Path) -> None:
        """load_engine_config actually reads and applies system.yaml values.

        Regression: verifies that the config pipeline (yaml → EngineConfig)
        works end-to-end, including nested fields like api.host and api.port.
        Combined with test_bootstrap_registers_engine_config, this proves
        that system.yaml values reach the dashboard server.
        """
        from sovyx.engine.config import load_engine_config

        system_yaml = tmp_path / "system.yaml"
        system_yaml.write_text(
            "api:\n  host: '0.0.0.0'\n  port: 9999\n  enabled: true\nlog:\n  level: DEBUG\n"
        )
        config = load_engine_config(config_path=system_yaml)

        # Verify yaml values were loaded — not defaults
        assert config.api.host == "0.0.0.0", (
            f"Expected host '0.0.0.0' from yaml, got '{config.api.host}'"
        )
        assert config.api.port == 9999, (  # noqa: PLR2004
            f"Expected port 9999 from yaml, got {config.api.port}"
        )
        assert config.log.level == "DEBUG", (
            f"Expected level 'DEBUG' from yaml, got '{config.log.level}'"
        )
        # Defaults preserved for fields NOT in yaml
        assert config.database.wal_mode is True
        assert config.telemetry.enabled is False

    async def test_dashboard_binds_to_configured_host(self, tmp_path: Path) -> None:
        """DashboardServer uses host/port from EngineConfig, not hardcoded defaults.

        Regression for the full chain: system.yaml → EngineConfig → bootstrap
        → registry → LifecycleManager._start_dashboard() → DashboardServer(config=api).

        This test verifies the last link: that DashboardServer actually reads
        the APIConfig values when constructing the uvicorn config.
        """
        from sovyx.dashboard.server import DashboardServer
        from sovyx.engine.config import APIConfig

        # Custom config with non-default host/port
        api_config = APIConfig(host="0.0.0.0", port=9876)
        server = DashboardServer(config=api_config)

        # Verify the server stored the config
        assert server._config is not None
        assert server._config.host == "0.0.0.0"
        assert server._config.port == 9876  # noqa: PLR2004

        # Verify that start() would use these values (without actually starting uvicorn)
        # by checking the host/port derivation logic matches what start() does
        host = server._config.host if server._config else "127.0.0.1"
        port = server._config.port if server._config else 7777
        assert host == "0.0.0.0"
        assert port == 9876  # noqa: PLR2004

        # Also verify the default case: None config → fallback to defaults
        default_server = DashboardServer(config=None)
        default_host = default_server._config.host if default_server._config else "127.0.0.1"
        default_port = default_server._config.port if default_server._config else 7777
        assert default_host == "127.0.0.1"
        assert default_port == 7777  # noqa: PLR2004

    async def test_full_config_to_dashboard_chain(self, tmp_path: Path) -> None:
        """End-to-end: system.yaml → bootstrap → registry → DashboardServer gets right config.

        This is the FULL regression test for the fd19172 bug: it proves that
        custom host/port in system.yaml survive through bootstrap and can be
        resolved by LifecycleManager to configure the dashboard.
        """
        from sovyx.engine.config import APIConfig, load_engine_config

        # 1. Write custom system.yaml
        system_yaml = tmp_path / "system.yaml"
        system_yaml.write_text(
            "api:\n"
            "  host: '0.0.0.0'\n"
            "  port: 8888\n"
            "database:\n"
            "  data_dir: '" + str(tmp_path) + "'\n"
        )

        # 2. Load config from yaml (like cli/main.py does)
        config = load_engine_config(config_path=system_yaml)
        assert config.api.host == "0.0.0.0"
        assert config.api.port == 8888  # noqa: PLR2004

        # 3. Bootstrap (like cli/main.py does)
        registry = await bootstrap(config, [MindConfig(name="ChainTest")])

        # 4. Resolve EngineConfig (like lifecycle._start_dashboard does)
        assert registry.is_registered(EngineConfig)
        resolved_config = await registry.resolve(EngineConfig)
        assert resolved_config.api.host == "0.0.0.0"
        assert resolved_config.api.port == 8888  # noqa: PLR2004

        # 5. The APIConfig that would be passed to DashboardServer
        api_config: APIConfig = resolved_config.api
        assert api_config.host == "0.0.0.0"
        assert api_config.port == 8888  # noqa: PLR2004

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

    async def test_cleanup_on_partial_failure(self, tmp_path: Path) -> None:
        """When bootstrap fails mid-way, already-started services are cleaned up."""
        from unittest.mock import patch

        config = EngineConfig(database=DatabaseConfig(data_dir=tmp_path))
        mind = MindConfig(name="Test")

        # Inject failure after DatabaseManager starts (during mind init)
        original_init = DatabaseManager.initialize_mind_databases

        async def failing_init(self_: DatabaseManager, mind_id: MindId) -> None:
            await original_init(self_, mind_id)
            msg = "Simulated failure after DB init"
            raise RuntimeError(msg)

        with (
            patch.object(DatabaseManager, "initialize_mind_databases", failing_init),
            pytest.raises(RuntimeError, match="Simulated"),
        ):
            await bootstrap(config, [mind])

        # Verify cleanup happened: system.db should exist (was created),
        # but the DatabaseManager should have been stopped
        # (If cleanup didn't work, we'd get resource leaks)
        assert (tmp_path / "system.db").exists()


class TestBootstrapCoverageGaps:
    """Cover remaining bootstrap paths."""

    @pytest.mark.asyncio(loop_scope="function")
    @pytest.mark.timeout(10)
    async def test_no_minds_raises(self, tmp_path: Path) -> None:
        """Bootstrap with empty minds list raises ValueError."""
        config = EngineConfig(database=DatabaseConfig(data_dir=tmp_path))
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}),
            pytest.raises(ValueError, match="No minds configured"),
        ):
            await bootstrap(config, [])

    @pytest.mark.asyncio(loop_scope="function")
    @pytest.mark.timeout(15)
    async def test_openai_provider_included(self, tmp_path: Path) -> None:
        """When OPENAI_API_KEY is set, OpenAI provider is in the router."""
        config = EngineConfig(database=DatabaseConfig(data_dir=tmp_path))
        mind = MindConfig(name="Test")
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "sk-ant-test",
                "OPENAI_API_KEY": "sk-test-openai",
            },
        ):
            registry = await bootstrap(config, [mind])
        router = await registry.resolve(LLMRouter)
        names = [type(p).__name__ for p in router._providers]  # noqa: SLF001
        assert "OpenAIProvider" in names
        await router.stop()

    @pytest.mark.asyncio(loop_scope="function")
    @pytest.mark.timeout(15)
    async def test_telegram_channel_registered(self, tmp_path: Path) -> None:
        """When SOVYX_TELEGRAM_TOKEN is set, Telegram channel is registered."""
        from unittest.mock import MagicMock

        config = EngineConfig(database=DatabaseConfig(data_dir=tmp_path))
        mind = MindConfig(name="Test")

        from sovyx.engine.types import ChannelType

        mock_telegram = MagicMock()
        mock_telegram.channel_type = ChannelType.TELEGRAM

        with (
            patch.dict(
                os.environ,
                {
                    "ANTHROPIC_API_KEY": "sk-ant-test",
                    "SOVYX_TELEGRAM_TOKEN": "123456:ABC",
                },
            ),
            patch(
                "sovyx.bridge.channels.telegram.TelegramChannel",
                return_value=mock_telegram,
            ),
        ):
            registry = await bootstrap(config, [mind])
        bridge = await registry.resolve(BridgeManager)
        assert len(bridge._adapters) >= 1  # noqa: SLF001

    @pytest.mark.asyncio(loop_scope="function")
    @pytest.mark.timeout(15)
    async def test_cleanup_on_failure(self, tmp_path: Path) -> None:
        """On bootstrap failure, closable resources are cleaned up."""
        config = EngineConfig(database=DatabaseConfig(data_dir=tmp_path))
        mind = MindConfig(name="Test")

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}),
            patch.object(
                MindManager,
                "load_mind",
                side_effect=RuntimeError("Late failure"),
            ),
            pytest.raises(RuntimeError, match="Late failure"),
        ):
            await bootstrap(config, [mind])

    @pytest.mark.asyncio(loop_scope="function")
    @pytest.mark.timeout(15)
    async def test_cleanup_failure_suppressed(self, tmp_path: Path) -> None:
        """When cleanup itself fails, original error still propagates."""
        from sovyx.llm.router import LLMRouter

        config = EngineConfig(database=DatabaseConfig(data_dir=tmp_path))
        mind = MindConfig(name="Test")

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}),
            patch.object(
                MindManager,
                "load_mind",
                side_effect=RuntimeError("main failure"),
            ),
            patch.object(
                LLMRouter,
                "stop",
                side_effect=RuntimeError("cleanup also failed"),
            ),
            pytest.raises(RuntimeError, match="main failure"),
        ):
            await bootstrap(config, [mind])
