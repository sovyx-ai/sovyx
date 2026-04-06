"""VAL-04: Targeted tests for remaining coverage gaps in dashboard modules.

Covers:
- brain.py: _get_relations_via_repo exception path, relation dedup in repo fallback
- settings.py: _persist_to_yaml failure (IOError)
- status.py: concept/episode count resolution, conversation count failure
- _shared.py: get_active_mind_id exception fallback
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path


# ── brain.py: _get_relations_via_repo exception ──


class TestBrainRelationsViaRepoError:
    """Cover the except block in _get_relations (DB path failure → fallback → also fails)."""

    @pytest.mark.asyncio()
    async def test_relations_db_path_error_returns_empty(self) -> None:
        """When DB path fails in _get_relations, it returns []."""
        from sovyx.dashboard.brain import _get_relations

        registry = MagicMock()
        # DatabaseManager is registered but get_brain_pool raises
        from sovyx.persistence.manager import DatabaseManager

        registry.is_registered.return_value = True
        mock_db = MagicMock(spec=DatabaseManager)
        mock_db.get_brain_pool.side_effect = RuntimeError("pool crashed")
        registry.resolve = AsyncMock(return_value=mock_db)

        result = await _get_relations(registry, {"n1", "n2"}, max_links=10)
        assert result == []

    @pytest.mark.asyncio()
    async def test_relations_repo_fallback_exception(self) -> None:
        """When repo fallback path raises (called via _get_relations), returns []."""
        from sovyx.dashboard.brain import _get_relations

        registry = MagicMock()

        # DatabaseManager NOT registered → triggers repo fallback path
        # RelationRepository IS registered but resolve raises
        def is_registered(iface: type) -> bool:
            from sovyx.persistence.manager import DatabaseManager

            return iface is not DatabaseManager

        registry.is_registered = is_registered
        registry.resolve = AsyncMock(side_effect=RuntimeError("repo dead"))

        # _get_relations should catch the exception and return []
        result = await _get_relations(registry, {"n1", "n2"}, max_links=10)
        assert result == []


# ── settings.py: _persist_to_yaml failure ──


class TestSettingsPersistFailure:
    def test_persist_failure_doesnt_crash(self, tmp_path: Path) -> None:
        """When yaml persistence fails, it logs warning but doesn't raise."""
        from sovyx.dashboard.settings import apply_settings
        from sovyx.engine.config import EngineConfig

        config = EngineConfig()

        # Use a path that will fail on write (directory as file)
        bad_path = tmp_path / "readonly"
        bad_path.mkdir()
        bad_file = bad_path / "nonexistent_dir" / "system.yaml"

        # The parent dir doesn't exist, so open() will fail
        with patch("sovyx.dashboard.settings.logger") as mock_logger:
            changes = apply_settings(
                config,
                {"log_level": "DEBUG"},
                config_path=bad_file,
            )
        assert changes == {"log_level": "INFO → DEBUG"}
        mock_logger.warning.assert_called_once()

    def test_persist_to_yaml_io_error(self, tmp_path: Path) -> None:
        """_persist_to_yaml handles IOError gracefully."""
        from sovyx.dashboard.settings import _persist_to_yaml
        from sovyx.engine.config import EngineConfig

        config = EngineConfig()
        # Path that can't be written (use /dev/null parent trick)
        bad_path = tmp_path / "no_exist_dir" / "deep" / "system.yaml"

        # Should not raise
        _persist_to_yaml(config, bad_path)


# ── status.py: concept/episode count + conversation count error ──


class TestStatusCollectorGaps:
    @pytest.mark.asyncio()
    async def test_collect_with_concept_and_episode_repos(self) -> None:
        """StatusCollector resolves concept and episode counts from repos."""
        from sovyx.dashboard.status import StatusCollector

        registry = MagicMock()

        # Set up MindManager
        from sovyx.engine.bootstrap import MindManager

        mock_mind_mgr = MagicMock(spec=MindManager)
        mock_mind_mgr.get_active_minds.return_value = ["aria"]

        # Set up ConceptRepository
        from sovyx.brain.concept_repo import ConceptRepository

        mock_concept_repo = MagicMock(spec=ConceptRepository)
        mock_concept_repo.count = AsyncMock(return_value=42)

        # Set up EpisodeRepository
        from sovyx.brain.episode_repo import EpisodeRepository

        mock_episode_repo = MagicMock(spec=EpisodeRepository)
        mock_episode_repo.count = AsyncMock(return_value=17)

        def is_registered(iface: type) -> bool:
            return iface in {MindManager, ConceptRepository, EpisodeRepository}

        async def resolve(iface: type) -> object:
            lookup = {
                MindManager: mock_mind_mgr,
                ConceptRepository: mock_concept_repo,
                EpisodeRepository: mock_episode_repo,
            }
            return lookup[iface]

        registry.is_registered = is_registered
        registry.resolve = resolve

        collector = StatusCollector(registry, start_time=1000.0)

        with patch("time.time", return_value=1042.5):
            snapshot = await collector.collect()

        assert snapshot.memory_concepts == 42  # noqa: PLR2004
        assert snapshot.memory_episodes == 17  # noqa: PLR2004
        assert snapshot.mind_name == "aria"

    @pytest.mark.asyncio()
    async def test_collect_conversation_count_error(self) -> None:
        """StatusCollector handles conversation count failure gracefully."""
        from sovyx.dashboard.status import StatusCollector

        registry = MagicMock()
        registry.is_registered.return_value = False
        registry.resolve = AsyncMock(side_effect=RuntimeError("nope"))

        collector = StatusCollector(registry, start_time=1000.0)

        with (
            patch(
                "sovyx.dashboard.conversations.count_active_conversations",
                new_callable=AsyncMock,
                side_effect=RuntimeError("conversations broken"),
            ),
            patch("time.time", return_value=1010.0),
        ):
            snapshot = await collector.collect()

        assert snapshot.active_conversations == 0

    @pytest.mark.asyncio()
    async def test_collect_concept_repo_error(self) -> None:
        """StatusCollector handles concept repo error gracefully."""
        from sovyx.dashboard.status import StatusCollector

        registry = MagicMock()

        from sovyx.brain.concept_repo import ConceptRepository

        mock_concept_repo = MagicMock(spec=ConceptRepository)
        mock_concept_repo.count = AsyncMock(side_effect=RuntimeError("db error"))

        def is_registered(iface: type) -> bool:
            return iface is ConceptRepository

        async def resolve(iface: type) -> object:
            return mock_concept_repo

        registry.is_registered = is_registered
        registry.resolve = resolve

        collector = StatusCollector(registry, start_time=1000.0)

        with (
            patch(
                "sovyx.dashboard.conversations.count_active_conversations",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch("time.time", return_value=1010.0),
        ):
            snapshot = await collector.collect()

        assert snapshot.memory_concepts == 0
        assert snapshot.memory_episodes == 0


# ── _shared.py: exception fallback ──


class TestSharedGetActiveMindError:
    @pytest.mark.asyncio()
    async def test_exception_returns_default(self) -> None:
        """When MindManager resolution fails, returns 'default'."""
        from sovyx.dashboard._shared import get_active_mind_id

        registry = MagicMock()
        # is_registered returns True but resolve raises
        registry.is_registered.return_value = True
        registry.resolve = AsyncMock(side_effect=RuntimeError("registry broken"))

        result = await get_active_mind_id(registry)
        assert result == "default"

    @pytest.mark.asyncio()
    async def test_no_active_minds_returns_default(self) -> None:
        """When MindManager has no active minds, returns 'default'."""
        from sovyx.dashboard._shared import get_active_mind_id

        registry = MagicMock()
        from sovyx.engine.bootstrap import MindManager

        mock_mgr = MagicMock(spec=MindManager)
        mock_mgr.get_active_minds.return_value = []

        registry.is_registered.return_value = True
        registry.resolve = AsyncMock(return_value=mock_mgr)

        result = await get_active_mind_id(registry)
        assert result == "default"
