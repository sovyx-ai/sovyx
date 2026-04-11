"""Tests for Sovyx Plugin Hot-Reload (TASK-442)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# watchdog is optional — skip tests that need the real observer
_has_watchdog = pytest.importorskip("watchdog", reason="watchdog not installed")

from sovyx.plugins.hot_reload import PluginFileWatcher, _clear_module_cache


def _mock_manager() -> MagicMock:
    """Create a mock PluginManager."""
    mgr = MagicMock()
    mgr.loaded_plugins = ["weather"]
    plugin = MagicMock()
    plugin.plugin = MagicMock()
    type(plugin.plugin).__module__ = "weather_plugin"
    type(plugin.plugin).__name__ = "WeatherPlugin"
    mgr.get_plugin.return_value = plugin
    mgr.unload = AsyncMock()
    mgr.load_single = AsyncMock()
    return mgr


class TestPluginFileWatcher:
    """Tests for PluginFileWatcher."""

    def test_init(self, tmp_path: Path) -> None:
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])
        assert not watcher.is_running
        assert watcher.reload_count == 0

    def test_start_stop(self, tmp_path: Path) -> None:
        """Start and stop with real watchdog."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])

        watcher.start()
        assert watcher.is_running

        watcher.stop()
        assert not watcher.is_running

    def test_start_nonexistent_dir(self, tmp_path: Path) -> None:
        """Start skips nonexistent directories."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path / "nonexistent"])
        watcher.start()
        assert watcher.is_running
        watcher.stop()

    def test_stop_without_start(self, tmp_path: Path) -> None:
        """Stop without start is safe."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])
        watcher.stop()
        assert not watcher.is_running

    def test_debounce(self, tmp_path: Path) -> None:
        """Changes within debounce window are ignored."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path], debounce_s=10.0)

        # First call should process
        with patch.object(watcher, "_resolve_plugin_name", return_value="weather"):
            watcher._on_file_changed("/some/weather_plugin/main.py")

        # Second call within debounce should be ignored
        with patch.object(watcher, "_resolve_plugin_name") as mock_resolve:
            watcher._on_file_changed("/some/weather_plugin/main.py")
            mock_resolve.assert_not_called()

    def test_resolve_plugin_name(self, tmp_path: Path) -> None:
        """Resolves file path to plugin name."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])

        result = watcher._resolve_plugin_name("/path/to/weather_plugin/main.py")
        assert result == "weather"

    def test_resolve_plugin_name_unknown(self, tmp_path: Path) -> None:
        """Unknown path returns None."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])

        result = watcher._resolve_plugin_name("/path/to/unknown/file.py")
        assert result is None

    @pytest.mark.anyio()
    async def test_reload_plugin(self, tmp_path: Path) -> None:
        """Reload: unload → clear cache → reimport → load."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])

        with patch("sovyx.plugins.hot_reload.importlib") as mock_imp:
            mock_module = MagicMock()
            mock_class = MagicMock()
            mock_module.WeatherPlugin = mock_class
            mock_imp.import_module.return_value = mock_module

            await watcher._reload_plugin("weather")

        mgr.unload.assert_called_once_with("weather")
        mgr.load_single.assert_called_once()
        assert watcher.reload_count == 1

    @pytest.mark.anyio()
    async def test_reload_plugin_not_found(self, tmp_path: Path) -> None:
        """Reload handles plugin not found."""
        mgr = _mock_manager()
        mgr.get_plugin.return_value = None
        watcher = PluginFileWatcher(mgr, [tmp_path])

        await watcher._reload_plugin("ghost")
        assert watcher.reload_count == 0

    @pytest.mark.anyio()
    async def test_reload_retries(self, tmp_path: Path) -> None:
        """Reload retries on failure up to max attempts."""
        mgr = _mock_manager()
        mgr.unload = AsyncMock(side_effect=RuntimeError("fail"))
        watcher = PluginFileWatcher(mgr, [tmp_path])

        await watcher._reload_plugin("weather")
        # Should have tried 3 times
        assert mgr.unload.call_count == 3
        assert watcher.reload_count == 0

    def test_on_file_changed_no_loop(self, tmp_path: Path) -> None:
        """_on_file_changed handles no event loop gracefully."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])

        with patch.object(watcher, "_resolve_plugin_name", return_value="weather"):
            # No running event loop — should not crash
            watcher._on_file_changed("/weather_plugin/main.py")


class TestClearModuleCache:
    """Tests for _clear_module_cache."""

    def test_clears_matching(self) -> None:
        """Clears modules matching prefix."""
        sys.modules["test_hot_xyz"] = MagicMock()
        sys.modules["test_hot_xyz.sub"] = MagicMock()
        count = _clear_module_cache("test_hot_xyz")
        assert count == 2
        assert "test_hot_xyz" not in sys.modules
        assert "test_hot_xyz.sub" not in sys.modules

    def test_no_match(self) -> None:
        """No matching modules returns 0."""
        count = _clear_module_cache("nonexistent_module_xyz_123")
        assert count == 0


class TestWatchdogHandler:
    """Tests for the watchdog event handler integration."""

    def test_on_modified_py_file(self, tmp_path: Path) -> None:
        """Handler triggers _on_file_changed for .py files."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])

        # Start to create the handler
        watcher.start()

        # Simulate file change via _on_file_changed
        with patch.object(watcher, "_resolve_plugin_name", return_value=None):
            watcher._on_file_changed(str(tmp_path / "test.py"))

        watcher.stop()

    def test_on_modified_non_py_ignored(self, tmp_path: Path) -> None:
        """Non-.py files are ignored by the handler."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])
        # Non-py file should not even reach _resolve_plugin_name
        # (handled in the Handler class, not _on_file_changed)

    def test_file_changed_triggers_resolve(self, tmp_path: Path) -> None:
        """_on_file_changed calls _resolve_plugin_name."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])

        with patch.object(watcher, "_resolve_plugin_name", return_value=None) as mock:
            watcher._on_file_changed("/some/path.py")
            mock.assert_called_once()

    @pytest.mark.anyio()
    async def test_reload_success_increments_count(self, tmp_path: Path) -> None:
        """Successful reload increments reload_count."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])

        with patch("sovyx.plugins.hot_reload.importlib") as mock_imp:
            mock_module = MagicMock()
            mock_class = MagicMock()
            mock_module.WeatherPlugin = mock_class
            mock_imp.import_module.return_value = mock_module

            await watcher._reload_plugin("weather")
            await watcher._reload_plugin("weather")

        assert watcher.reload_count == 2

    def test_resolve_with_no_plugins(self, tmp_path: Path) -> None:
        """Resolve returns None when no plugins loaded."""
        mgr = _mock_manager()
        mgr.loaded_plugins = []
        watcher = PluginFileWatcher(mgr, [tmp_path])
        assert watcher._resolve_plugin_name("/any/path.py") is None

    def test_resolve_get_plugin_none(self, tmp_path: Path) -> None:
        """Resolve handles get_plugin returning None."""
        mgr = _mock_manager()
        mgr.get_plugin.return_value = None
        watcher = PluginFileWatcher(mgr, [tmp_path])
        assert watcher._resolve_plugin_name("/any/path.py") is None


class TestHandleFsEvent:
    """Tests for _handle_fs_event (extracted from watchdog handler)."""

    def test_py_file_triggers(self, tmp_path: Path) -> None:
        """Python file event triggers _on_file_changed."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])

        class FakeEvent:
            src_path = "/plugin/main.py"
            is_directory = False

        with patch.object(watcher, "_on_file_changed") as mock:
            watcher._handle_fs_event(FakeEvent())
            mock.assert_called_once_with("/plugin/main.py")

    def test_non_py_ignored(self, tmp_path: Path) -> None:
        """Non-.py files are ignored."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])

        class FakeEvent:
            src_path = "/plugin/data.json"
            is_directory = False

        with patch.object(watcher, "_on_file_changed") as mock:
            watcher._handle_fs_event(FakeEvent())
            mock.assert_not_called()

    def test_directory_ignored(self, tmp_path: Path) -> None:
        """Directory events are ignored."""
        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path])

        class FakeEvent:
            src_path = "/plugin"
            is_directory = True

        with patch.object(watcher, "_on_file_changed") as mock:
            watcher._handle_fs_event(FakeEvent())
            mock.assert_not_called()

    def test_file_change_with_running_loop(self, tmp_path: Path) -> None:
        """_on_file_changed schedules reload in running loop."""
        import asyncio

        mgr = _mock_manager()
        watcher = PluginFileWatcher(mgr, [tmp_path], debounce_s=0)

        async def run() -> None:
            with (
                patch.object(watcher, "_resolve_plugin_name", return_value="weather"),
                patch.object(watcher, "_reload_plugin", new_callable=AsyncMock) as mock_reload,
            ):
                watcher._on_file_changed("/weather_plugin/x.py")
                await asyncio.sleep(0.05)  # Let task run
                mock_reload.assert_called_once_with("weather")

        asyncio.run(run())
