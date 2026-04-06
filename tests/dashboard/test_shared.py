"""Tests for sovyx.dashboard._shared — shared utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.dashboard._shared import get_active_mind_id


class TestGetActiveMindId:
    @pytest.mark.asyncio()
    async def test_returns_default_when_no_manager(self) -> None:
        """No MindManager registered → returns 'default'."""
        registry = MagicMock()
        registry.is_registered.return_value = False
        result = await get_active_mind_id(registry)
        assert result == "default"

    @pytest.mark.asyncio()
    async def test_returns_first_active_mind(self) -> None:
        """MindManager with active minds → returns first."""
        mock_manager = MagicMock()
        mock_manager.get_active_minds.return_value = ["nyx", "aria"]

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.bootstrap import MindManager

            return cls is MindManager

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=mock_manager)

        result = await get_active_mind_id(registry)
        assert result == "nyx"

    @pytest.mark.asyncio()
    async def test_returns_default_when_no_active_minds(self) -> None:
        """MindManager registered but no active minds → returns 'default'."""
        mock_manager = MagicMock()
        mock_manager.get_active_minds.return_value = []

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.bootstrap import MindManager

            return cls is MindManager

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=mock_manager)

        result = await get_active_mind_id(registry)
        assert result == "default"

    @pytest.mark.asyncio()
    async def test_returns_default_on_exception(self) -> None:
        """If MindManager resolution throws → returns 'default'."""
        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.bootstrap import MindManager

            return cls is MindManager

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(side_effect=RuntimeError("boom"))

        result = await get_active_mind_id(registry)
        assert result == "default"
