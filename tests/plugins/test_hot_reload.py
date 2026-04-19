"""Tests for sovyx.plugins.hot_reload — PluginFileWatcher."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.plugins.hot_reload import PluginFileWatcher, _clear_module_cache

# ── Fixtures ──


class FakePluginInfo:
    """Minimal loaded plugin info."""

    def __init__(self, name: str, module: str) -> None:
        self.plugin = type(f"Fake{name}", (), {"__module__": module})()


class FakePluginManager:
    """Minimal PluginManager stand-in."""

    def __init__(self) -> None:
        self._plugins: dict[str, FakePluginInfo] = {}
        self.unload = AsyncMock()
        self.load_single = AsyncMock()

    @property
    def loaded_plugins(self) -> list[str]:
        return list(self._plugins.keys())

    def get_plugin(self, name: str) -> FakePluginInfo | None:
        return self._plugins.get(name)

    def add(self, name: str, module: str = "sovyx.plugins.official.calculator") -> None:
        self._plugins[name] = FakePluginInfo(name, module)


@pytest.fixture()
def manager() -> FakePluginManager:
    return FakePluginManager()


@pytest.fixture()
def watcher(manager: FakePluginManager, tmp_path: Path) -> PluginFileWatcher:
    return PluginFileWatcher(manager, [tmp_path], debounce_s=0.0)


# ── Properties ──


def test_initial_state(watcher: PluginFileWatcher) -> None:
    assert watcher.is_running is False
    assert watcher.reload_count == 0


# ── _handle_fs_event ──


def test_handle_fs_event_ignores_directories(watcher: PluginFileWatcher) -> None:
    event = MagicMock(src_path="/some/dir", is_directory=True)
    watcher._handle_fs_event(event)
    # No crash, no _on_file_changed call


def test_handle_fs_event_ignores_non_python(watcher: PluginFileWatcher) -> None:
    event = MagicMock(src_path="/some/file.txt", is_directory=False)
    watcher._handle_fs_event(event)


def test_handle_fs_event_processes_python_files(
    watcher: PluginFileWatcher, manager: FakePluginManager
) -> None:
    manager.add("calculator")
    event = MagicMock(src_path="/sovyx/plugins/official/calculator.py", is_directory=False)
    with patch.object(watcher, "_on_file_changed") as mock_on_changed:
        watcher._handle_fs_event(event)
        mock_on_changed.assert_called_once_with("/sovyx/plugins/official/calculator.py")


# ── Debouncing ──


def test_debounce_blocks_rapid_changes(manager: FakePluginManager, tmp_path: Path) -> None:
    watcher = PluginFileWatcher(manager, [tmp_path], debounce_s=10.0)
    manager.add("calculator")

    with (
        patch.object(watcher, "_resolve_plugin_name", return_value="calculator"),
        patch("asyncio.get_running_loop", side_effect=RuntimeError),
    ):
        watcher._on_file_changed("/some/calculator.py")
        # Second call within debounce window — should be skipped
        watcher._on_file_changed("/some/calculator.py")
        # _resolve_plugin_name only called once due to debounce
        assert watcher._resolve_plugin_name.call_count == 1  # type: ignore[union-attr]


def test_debounce_allows_after_window(manager: FakePluginManager, tmp_path: Path) -> None:
    watcher = PluginFileWatcher(manager, [tmp_path], debounce_s=0.0)
    manager.add("calculator")

    with patch.object(watcher, "_resolve_plugin_name", return_value=None):
        watcher._on_file_changed("/some/calculator.py")
        watcher._on_file_changed("/some/calculator.py")
        assert watcher._resolve_plugin_name.call_count == 2  # type: ignore[union-attr]


# ── _resolve_plugin_name ──


def test_resolve_plugin_name_found(watcher: PluginFileWatcher, manager: FakePluginManager) -> None:
    manager.add("calculator", module="sovyx.plugins.official.calculator")
    result = watcher._resolve_plugin_name("/path/to/sovyx/plugins/official/calculator.py")
    assert result == "calculator"


def test_resolve_plugin_name_not_found(
    watcher: PluginFileWatcher, manager: FakePluginManager
) -> None:
    manager.add("calculator", module="sovyx.plugins.official.calculator")
    result = watcher._resolve_plugin_name("/completely/unrelated/path.py")
    assert result is None


def test_resolve_returns_none_for_missing_plugin(
    watcher: PluginFileWatcher, manager: FakePluginManager
) -> None:
    # No plugins loaded
    result = watcher._resolve_plugin_name("/any/path.py")
    assert result is None


def test_resolve_skips_none_plugin(watcher: PluginFileWatcher, manager: FakePluginManager) -> None:
    manager._plugins["ghost"] = None  # type: ignore[assignment]
    result = watcher._resolve_plugin_name("/any/path.py")
    assert result is None


# ── _on_file_changed ──


def test_on_file_changed_no_matching_plugin(
    watcher: PluginFileWatcher,
) -> None:
    # No plugins loaded — _resolve_plugin_name returns None, no task created
    watcher._on_file_changed("/random/file.py")


def test_on_file_changed_no_event_loop(
    watcher: PluginFileWatcher, manager: FakePluginManager
) -> None:
    manager.add("calculator")
    with patch.object(watcher, "_resolve_plugin_name", return_value="calculator"):
        # No running loop — should log warning, not crash
        watcher._on_file_changed("/calculator.py")


@pytest.mark.anyio()
async def test_on_file_changed_with_event_loop(
    watcher: PluginFileWatcher, manager: FakePluginManager
) -> None:
    """_on_file_changed schedules reload task when event loop is running."""
    manager.add("calculator")
    with (
        patch.object(watcher, "_resolve_plugin_name", return_value="calculator"),
        patch.object(watcher, "_reload_plugin", new_callable=AsyncMock) as mock_reload,
    ):
        watcher._on_file_changed("/calculator.py")
        # Let the event loop process the created task
        await asyncio.sleep(0.05)
        mock_reload.assert_awaited_once_with("calculator")


# ── _reload_plugin ──


@pytest.mark.anyio()
async def test_reload_plugin_success(
    watcher: PluginFileWatcher, manager: FakePluginManager
) -> None:
    manager.add("calculator", module="sovyx.plugins.official.calculator")

    with patch("importlib.import_module") as mock_import:
        fake_module = MagicMock()
        fake_class = MagicMock(return_value=MagicMock())
        fake_module.Fakecalculator = fake_class
        # Match the plugin class name
        plugin_info = manager.get_plugin("calculator")
        class_name = type(plugin_info.plugin).__name__  # type: ignore[union-attr]
        setattr(fake_module, class_name, fake_class)
        mock_import.return_value = fake_module

        await watcher._reload_plugin("calculator")

        manager.unload.assert_awaited_once_with("calculator")
        manager.load_single.assert_awaited_once()
        assert watcher.reload_count == 1


@pytest.mark.anyio()
async def test_reload_plugin_not_found(
    watcher: PluginFileWatcher, manager: FakePluginManager
) -> None:
    # Plugin doesn't exist
    await watcher._reload_plugin("nonexistent")
    assert watcher.reload_count == 0


@pytest.mark.anyio()
async def test_reload_plugin_retries_on_failure(
    watcher: PluginFileWatcher, manager: FakePluginManager
) -> None:
    manager.add("calculator", module="sovyx.plugins.official.calculator")
    manager.unload = AsyncMock(side_effect=RuntimeError("fail"))

    await watcher._reload_plugin("calculator")

    # Should have tried 3 times (MAX_RELOAD_ATTEMPTS)
    assert manager.unload.await_count == 3
    assert watcher.reload_count == 0


# ── start/stop ──


def test_start_without_watchdog(
    watcher: PluginFileWatcher,
) -> None:
    """start() gracefully handles missing watchdog."""
    original_import = (
        __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
    )  # type: ignore[union-attr]

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if "watchdog" in name:
            raise ImportError("no watchdog")
        return original_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        watcher.start()
    assert watcher.is_running is False


def test_stop_without_start(watcher: PluginFileWatcher) -> None:
    watcher.stop()
    assert watcher.is_running is False


def test_stop_with_observer(watcher: PluginFileWatcher) -> None:
    mock_observer = MagicMock()
    watcher._observer = mock_observer
    watcher._running = True

    watcher.stop()

    mock_observer.stop.assert_called_once()
    mock_observer.join.assert_called_once_with(timeout=5)
    assert watcher.is_running is False
    assert watcher._observer is None


def test_start_with_watchdog(tmp_path: Path, manager: FakePluginManager) -> None:
    """start() with watchdog available sets up observer and handler."""
    watch_dir = tmp_path / "plugins"
    watch_dir.mkdir()
    watcher = PluginFileWatcher(manager, [watch_dir], debounce_s=0.0)

    mock_observer = MagicMock()
    mock_observer_cls = MagicMock(return_value=mock_observer)

    # Create mock watchdog modules
    mock_events = MagicMock()
    mock_observers = MagicMock()
    mock_observers.Observer = mock_observer_cls

    with patch.dict(
        sys.modules,
        {
            "watchdog": MagicMock(),
            "watchdog.events": mock_events,
            "watchdog.observers": mock_observers,
        },
    ):
        watcher.start()

    assert watcher.is_running is True
    mock_observer.start.assert_called_once()
    mock_observer.schedule.assert_called_once()
    assert watcher._observer is mock_observer

    watcher.stop()
    assert watcher.is_running is False


def test_start_skips_nonexistent_dirs(tmp_path: Path, manager: FakePluginManager) -> None:
    """start() skips watch dirs that don't exist."""
    nonexistent = tmp_path / "does_not_exist"
    watcher = PluginFileWatcher(manager, [nonexistent], debounce_s=0.0)

    mock_observer = MagicMock()
    mock_observer_cls = MagicMock(return_value=mock_observer)

    mock_events = MagicMock()
    mock_observers = MagicMock()
    mock_observers.Observer = mock_observer_cls

    with patch.dict(
        sys.modules,
        {
            "watchdog": MagicMock(),
            "watchdog.events": mock_events,
            "watchdog.observers": mock_observers,
        },
    ):
        watcher.start()

    assert watcher.is_running is True
    mock_observer.schedule.assert_not_called()  # No dirs to watch
    watcher.stop()


# ── _clear_module_cache ──


def test_clear_module_cache_removes_matching() -> None:
    sentinel = "sovyx_test_hot_reload_sentinel"
    sys.modules[sentinel] = MagicMock()  # type: ignore[assignment]
    sys.modules[f"{sentinel}.sub"] = MagicMock()  # type: ignore[assignment]

    count = _clear_module_cache(sentinel)

    assert count == 2
    assert sentinel not in sys.modules
    assert f"{sentinel}.sub" not in sys.modules


def test_clear_module_cache_no_matches() -> None:
    count = _clear_module_cache("nonexistent_module_xyz_12345")
    assert count == 0


def test_clear_module_cache_only_prefix() -> None:
    """Doesn't remove modules that merely contain the prefix as substring."""
    sentinel = "zz_hot_test"
    other = "azz_hot_test_nope"
    sys.modules[sentinel] = MagicMock()  # type: ignore[assignment]
    sys.modules[other] = MagicMock()  # type: ignore[assignment]

    count = _clear_module_cache(sentinel)

    assert count == 1
    assert sentinel not in sys.modules
    assert other in sys.modules
    # Cleanup
    del sys.modules[other]


def test_clear_module_cache_does_not_evict_sibling_packages() -> None:
    """Regression: clearing ``pkg.a.b`` must not touch sibling ``pkg.c``.

    A prior implementation used ``module_name.split(".")[0]`` as the prefix,
    which nuked every sibling under the top-level package — reloading a
    plugin would silently evict ``sovyx.voice.health.*`` and friends from
    ``sys.modules``, poisoning the rest of the test suite with fresh module
    objects that diverged from callers' captured references.
    """
    root = "sovyx_regression_sibling"
    target = f"{root}.plugins.calculator"
    target_child = f"{target}.inner"
    sibling_top = f"{root}.voice"
    sibling_nested = f"{root}.voice.health"
    unrelated = "totally_other_pkg.thing"

    for mod in (target, target_child, sibling_top, sibling_nested, unrelated):
        sys.modules[mod] = MagicMock()  # type: ignore[assignment]

    try:
        count = _clear_module_cache(target)

        assert count == 2
        assert target not in sys.modules
        assert target_child not in sys.modules
        assert sibling_top in sys.modules
        assert sibling_nested in sys.modules
        assert unrelated in sys.modules
    finally:
        for mod in (sibling_top, sibling_nested, unrelated):
            sys.modules.pop(mod, None)
