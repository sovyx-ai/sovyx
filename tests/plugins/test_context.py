"""Tests for Sovyx Plugin Context — PluginContext, BrainAccess, EventBusAccess.

Coverage target: ≥95% on plugins/context.py
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.plugins.context import (
    _MAX_CONCEPT_CONTENT,
    _MAX_SEARCH_RESULTS,
    BrainAccess,
    EventBusAccess,
    PluginContext,
)
from sovyx.plugins.permissions import (
    PermissionDeniedError,
    PermissionEnforcer,
)

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def enforcer_all() -> PermissionEnforcer:
    """Enforcer with all permissions granted."""
    return PermissionEnforcer(
        "test-plugin",
        {
            "brain:read",
            "brain:write",
            "event:subscribe",
            "event:emit",
            "network:internet",
            "fs:read",
            "fs:write",
        },
    )


@pytest.fixture()
def enforcer_readonly() -> PermissionEnforcer:
    """Enforcer with only read permissions."""
    return PermissionEnforcer("test-plugin", {"brain:read", "event:subscribe"})


@pytest.fixture()
def enforcer_none() -> PermissionEnforcer:
    """Enforcer with no permissions."""
    return PermissionEnforcer("test-plugin", set())


@pytest.fixture()
def mock_brain() -> MagicMock:
    """Mock BrainService matching real API."""
    brain = MagicMock()

    # Mock search result: list[tuple[Concept, float]]
    concept_mock = MagicMock()
    concept_mock.name = "test-concept"
    concept_mock.content = "test content"
    concept_mock.category.value = "fact"
    concept_mock.importance = 0.8

    brain.search = AsyncMock(return_value=[(concept_mock, 0.9)])

    # Mock learn_concept result: ConceptId (str newtype)
    brain.learn_concept = AsyncMock(return_value="concept-123")

    return brain


@pytest.fixture()
def mock_event_bus() -> MagicMock:
    """Mock EventBus."""
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.unsubscribe = MagicMock()
    bus.emit = AsyncMock()
    return bus


# ── BrainAccess ─────────────────────────────────────────────────────


class TestBrainAccess:
    """Tests for BrainAccess scoped brain interface."""

    @pytest.mark.anyio()
    async def test_search_success(
        self, mock_brain: MagicMock, enforcer_all: PermissionEnforcer
    ) -> None:
        """Search returns sanitized concept dicts."""
        access = BrainAccess(
            mock_brain,
            enforcer_all,
            write_allowed=True,
            plugin_name="test",
        )
        results = await access.search("hello")
        assert len(results) == 1
        assert results[0]["name"] == "test-concept"
        assert results[0]["content"] == "test content"
        assert results[0]["category"] == "fact"  # enum .value
        assert results[0]["importance"] == 0.8
        mock_brain.search.assert_called_once()

    @pytest.mark.anyio()
    async def test_search_caps_limit(
        self, mock_brain: MagicMock, enforcer_all: PermissionEnforcer
    ) -> None:
        """Search caps limit at MAX_SEARCH_RESULTS."""
        access = BrainAccess(
            mock_brain,
            enforcer_all,
            write_allowed=True,
            plugin_name="test",
        )
        await access.search("query", limit=999)
        mock_brain.search.assert_called_once()
        call_kwargs = mock_brain.search.call_args
        assert call_kwargs.kwargs["limit"] == _MAX_SEARCH_RESULTS

    @pytest.mark.anyio()
    async def test_search_denied_without_permission(
        self, mock_brain: MagicMock, enforcer_none: PermissionEnforcer
    ) -> None:
        """Search raises PermissionDeniedError without brain:read."""
        access = BrainAccess(
            mock_brain,
            enforcer_none,
            write_allowed=False,
            plugin_name="test",
        )
        with pytest.raises(PermissionDeniedError):
            await access.search("hello")

    @pytest.mark.anyio()
    async def test_learn_success(
        self, mock_brain: MagicMock, enforcer_all: PermissionEnforcer
    ) -> None:
        """Learn creates concept with forced source tagging."""
        access = BrainAccess(
            mock_brain,
            enforcer_all,
            write_allowed=True,
            plugin_name="weather",
        )
        concept_id = await access.learn("rain today", "It's raining")
        assert concept_id == "concept-123"
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["source"] == "plugin:weather"

    @pytest.mark.anyio()
    async def test_learn_denied_without_permission(
        self, mock_brain: MagicMock, enforcer_readonly: PermissionEnforcer
    ) -> None:
        """Learn raises without brain:write permission."""
        access = BrainAccess(
            mock_brain,
            enforcer_readonly,
            write_allowed=False,
            plugin_name="test",
        )
        with pytest.raises(PermissionDeniedError):
            await access.learn("test", "content")

    @pytest.mark.anyio()
    async def test_learn_denied_write_not_allowed(
        self, mock_brain: MagicMock, enforcer_all: PermissionEnforcer
    ) -> None:
        """Learn raises if write_allowed=False even with permission."""
        access = BrainAccess(
            mock_brain,
            enforcer_all,
            write_allowed=False,
            plugin_name="test",
        )
        with pytest.raises(PermissionDeniedError):
            await access.learn("test", "content")

    @pytest.mark.anyio()
    async def test_learn_content_too_large(
        self, mock_brain: MagicMock, enforcer_all: PermissionEnforcer
    ) -> None:
        """Learn rejects content exceeding 10KB."""
        access = BrainAccess(
            mock_brain,
            enforcer_all,
            write_allowed=True,
            plugin_name="test",
        )
        large_content = "x" * (_MAX_CONCEPT_CONTENT + 1)
        with pytest.raises(ValueError, match="10240"):
            await access.learn("big", large_content)

    @pytest.mark.anyio()
    async def test_learn_invalid_category_defaults_to_fact(
        self, mock_brain: MagicMock, enforcer_all: PermissionEnforcer
    ) -> None:
        """Invalid category string defaults to FACT."""
        access = BrainAccess(
            mock_brain,
            enforcer_all,
            write_allowed=True,
            plugin_name="test",
        )
        await access.learn("item", "content", category="nonexistent_category")
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"].value == "fact"

    @pytest.mark.anyio()
    async def test_learn_custom_category(
        self, mock_brain: MagicMock, enforcer_all: PermissionEnforcer
    ) -> None:
        """Learn passes custom category."""
        access = BrainAccess(
            mock_brain,
            enforcer_all,
            write_allowed=True,
            plugin_name="test",
        )
        await access.learn("item", "content", category="preference")
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"].value == "preference"


# ── EventBusAccess ──────────────────────────────────────────────────


class TestEventBusAccess:
    """Tests for EventBusAccess scoped event interface."""

    def test_subscribe_success(
        self, mock_event_bus: MagicMock, enforcer_all: PermissionEnforcer
    ) -> None:
        """Subscribe registers handler and tracks it."""
        access = EventBusAccess(mock_event_bus, enforcer_all, plugin_name="test")
        handler = AsyncMock()
        # Use a mock event type
        event_type = type("TestEvent", (), {})
        access.subscribe(event_type, handler)  # type: ignore[arg-type]
        mock_event_bus.subscribe.assert_called_once()
        assert access.subscription_count == 1

    def test_subscribe_denied(
        self, mock_event_bus: MagicMock, enforcer_none: PermissionEnforcer
    ) -> None:
        """Subscribe raises without event:subscribe."""
        access = EventBusAccess(mock_event_bus, enforcer_none, plugin_name="test")
        with pytest.raises(PermissionDeniedError):
            access.subscribe(type("E", (), {}), AsyncMock())  # type: ignore[arg-type]

    @pytest.mark.anyio()
    async def test_emit_success(
        self, mock_event_bus: MagicMock, enforcer_all: PermissionEnforcer
    ) -> None:
        """Emit forwards event to bus."""
        access = EventBusAccess(mock_event_bus, enforcer_all, plugin_name="test")
        event = MagicMock()
        await access.emit(event)
        mock_event_bus.emit.assert_called_once_with(event)

    @pytest.mark.anyio()
    async def test_emit_denied(
        self, mock_event_bus: MagicMock, enforcer_none: PermissionEnforcer
    ) -> None:
        """Emit raises without event:emit."""
        access = EventBusAccess(mock_event_bus, enforcer_none, plugin_name="test")
        with pytest.raises(PermissionDeniedError):
            await access.emit(MagicMock())

    def test_cleanup_unsubscribes_all(
        self, mock_event_bus: MagicMock, enforcer_all: PermissionEnforcer
    ) -> None:
        """Cleanup removes all subscriptions."""
        access = EventBusAccess(mock_event_bus, enforcer_all, plugin_name="test")
        handler1 = AsyncMock()
        handler2 = AsyncMock()
        evt1 = type("E1", (), {})
        evt2 = type("E2", (), {})
        access.subscribe(evt1, handler1)  # type: ignore[arg-type]
        access.subscribe(evt2, handler2)  # type: ignore[arg-type]
        assert access.subscription_count == 2

        access.cleanup()
        assert access.subscription_count == 0
        assert mock_event_bus.unsubscribe.call_count == 2

    def test_cleanup_idempotent(
        self, mock_event_bus: MagicMock, enforcer_all: PermissionEnforcer
    ) -> None:
        """Cleanup can be called multiple times safely."""
        access = EventBusAccess(mock_event_bus, enforcer_all, plugin_name="test")
        access.cleanup()
        access.cleanup()
        assert access.subscription_count == 0


# ── PluginContext ───────────────────────────────────────────────────


class TestPluginContext:
    """Tests for PluginContext dataclass."""

    def test_creation_minimal(self, tmp_path: Path) -> None:
        """Minimal context with no permission-gated services."""
        ctx = PluginContext(
            plugin_name="test",
            plugin_version="1.0.0",
            data_dir=tmp_path,
            config={},
            logger=logging.getLogger("test"),
        )
        assert ctx.plugin_name == "test"
        assert ctx.plugin_version == "1.0.0"
        assert ctx.brain is None
        assert ctx.event_bus is None
        assert ctx.http is None
        assert ctx.filesystem is None

    def test_creation_with_brain(
        self,
        tmp_path: Path,
        mock_brain: MagicMock,
        enforcer_all: PermissionEnforcer,
    ) -> None:
        """Context with brain access provided."""
        brain_access = BrainAccess(
            mock_brain,
            enforcer_all,
            write_allowed=True,
            plugin_name="test",
        )
        ctx = PluginContext(
            plugin_name="test",
            plugin_version="1.0.0",
            data_dir=tmp_path,
            config={"api_key": "xxx"},
            logger=logging.getLogger("test"),
            brain=brain_access,
        )
        assert ctx.brain is not None
        assert ctx.config == {"api_key": "xxx"}

    @pytest.mark.anyio()
    async def test_call_tool_not_implemented(self, tmp_path: Path) -> None:
        """call_tool raises NotImplementedError (v1.1 feature)."""
        ctx = PluginContext(
            plugin_name="test",
            plugin_version="1.0.0",
            data_dir=tmp_path,
            config={},
            logger=logging.getLogger("test"),
        )
        with pytest.raises(NotImplementedError, match="v1.1"):
            await ctx.call_tool("weather.get_weather", {"location": "NYC"})

    def test_is_plugin_available_returns_false(self, tmp_path: Path) -> None:
        """is_plugin_available returns False (v1.1 feature)."""
        ctx = PluginContext(
            plugin_name="test",
            plugin_version="1.0.0",
            data_dir=tmp_path,
            config={},
            logger=logging.getLogger("test"),
        )
        assert ctx.is_plugin_available("weather") is False

    def test_data_dir_is_path(self, tmp_path: Path) -> None:
        """data_dir is a Path object."""
        ctx = PluginContext(
            plugin_name="test",
            plugin_version="1.0.0",
            data_dir=tmp_path,
            config={},
            logger=logging.getLogger("test"),
        )
        assert isinstance(ctx.data_dir, Path)
