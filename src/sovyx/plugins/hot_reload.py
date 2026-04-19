"""Sovyx Plugin Hot-Reload — development mode file watcher.

Watches plugin directories for file changes and reloads plugins
automatically. Only for development — production requires restart.

Spec: SPE-008 Appendix C.5
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
import typing

from sovyx.observability.logging import get_logger

if typing.TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from sovyx.plugins.manager import PluginManager

logger = get_logger(__name__)

# Debounce: ignore changes within this window (seconds)
_DEBOUNCE_S = 1.0
_MAX_RELOAD_ATTEMPTS = 3


class PluginFileWatcher:
    """Watch plugin directories and trigger reloads on changes.

    Uses watchdog for filesystem monitoring. When a .py file changes,
    debounces, then triggers teardown → module clear → reimport → setup.
    """

    def __init__(
        self,
        plugin_manager: PluginManager,
        watch_dirs: list[Path],
        debounce_s: float = _DEBOUNCE_S,
    ) -> None:
        self._manager = plugin_manager
        self._watch_dirs = watch_dirs
        self._debounce_s = debounce_s
        self._observer: object | None = None
        self._running = False
        self._last_change: dict[str, float] = {}
        self._reload_count = 0

    @property
    def is_running(self) -> bool:
        """True if watcher is active."""
        return self._running

    @property
    def reload_count(self) -> int:
        """Number of successful reloads performed."""
        return self._reload_count

    def _handle_fs_event(self, event: object) -> None:
        """Handle a filesystem event from watchdog."""
        src_path = getattr(event, "src_path", "")
        is_dir = getattr(event, "is_directory", False)
        if is_dir or not str(src_path).endswith(".py"):
            return
        self._on_file_changed(str(src_path))

    def start(self) -> None:
        """Start watching plugin directories."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning("watchdog_not_installed", msg="pip install watchdog for hot-reload")
            return

        watcher_ref = self

        class _Handler(FileSystemEventHandler):  # type: ignore[misc]
            def on_modified(self, event: object) -> None:
                watcher_ref._handle_fs_event(event)

            def on_created(self, event: object) -> None:
                watcher_ref._handle_fs_event(event)

        observer = Observer()
        handler = _Handler()

        for watch_dir in self._watch_dirs:
            if watch_dir.exists():
                observer.schedule(handler, str(watch_dir), recursive=True)
                logger.info("watching_plugin_dir", path=str(watch_dir))

        observer.start()
        self._observer = observer
        self._running = True
        logger.info("plugin_hot_reload_started", dirs=len(self._watch_dirs))

    def stop(self) -> None:
        """Stop watching."""
        if self._observer is not None:
            obs = self._observer
            if hasattr(obs, "stop"):
                obs.stop()
            if hasattr(obs, "join"):
                obs.join(timeout=5)
            self._observer = None
        self._running = False
        logger.info("plugin_hot_reload_stopped")

    def _on_file_changed(self, path: str) -> None:
        """Handle file change event (with debouncing)."""
        now = time.monotonic()
        last = self._last_change.get(path, 0.0)
        if now - last < self._debounce_s:
            return
        self._last_change[path] = now

        # Find which plugin this file belongs to
        plugin_name = self._resolve_plugin_name(path)
        if not plugin_name:
            return

        logger.info(
            "plugin_file_changed",
            path=path,
            plugin=plugin_name,
        )

        # Schedule async reload
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._reload_plugin(plugin_name))
        except RuntimeError:
            # No event loop — log and skip
            logger.warning("hot_reload_no_loop", plugin=plugin_name)

    def _resolve_plugin_name(self, path: str) -> str | None:
        """Resolve file path to plugin name."""
        for plugin_name in self._manager.loaded_plugins:
            plugin = self._manager.get_plugin(plugin_name)
            if plugin is None:
                continue
            # Check if the file path contains the plugin's module
            module = type(plugin.plugin).__module__
            module_parts = module.split(".")
            if any(part in path for part in module_parts):
                return plugin_name
        return None

    async def _reload_plugin(self, plugin_name: str) -> None:
        """Reload a plugin: teardown → clear modules → reimport → setup."""
        for attempt in range(_MAX_RELOAD_ATTEMPTS):
            try:
                # 1. Get current plugin info
                loaded = self._manager.get_plugin(plugin_name)
                if loaded is None:
                    logger.warning("reload_plugin_not_found", plugin=plugin_name)
                    return

                plugin_class = type(loaded.plugin)
                module_name = plugin_class.__module__

                # 2. Unload
                await self._manager.unload(plugin_name)

                # 3. Clear module cache
                _clear_module_cache(module_name)

                # 4. Reimport and reload
                module = importlib.import_module(module_name)
                new_class = getattr(module, plugin_class.__name__)
                new_plugin = new_class()

                # 5. Load
                await self._manager.load_single(new_plugin)

                self._reload_count += 1
                logger.info(
                    "plugin_reloaded",
                    plugin=plugin_name,
                    attempt=attempt + 1,
                )
                return

            except Exception as e:  # noqa: BLE001
                logger.error(
                    "plugin_reload_failed",
                    plugin=plugin_name,
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt >= _MAX_RELOAD_ATTEMPTS - 1:
                    logger.error(
                        "plugin_reload_gave_up",
                        plugin=plugin_name,
                        attempts=_MAX_RELOAD_ATTEMPTS,
                    )


def _clear_module_cache(module_name: str) -> int:
    """Clear ``module_name`` and its strict submodules from :data:`sys.modules`.

    Only evicts entries that match ``module_name`` exactly or are
    descendants of it (``"{module_name}.*"``). Splitting on the first
    dotted segment would nuke every sibling package — e.g. reloading
    ``sovyx.plugins.official.calculator`` would also drop
    ``sovyx.voice.health.*``, and subsequent re-imports would hand out
    fresh module objects that diverge from callers who already captured
    the originals (bleeding cross-test pollution into the rest of the
    suite).

    Returns number of modules cleared.
    """
    to_remove = [
        key for key in sys.modules if key == module_name or key.startswith(f"{module_name}.")
    ]
    for key in to_remove:
        del sys.modules[key]
    return len(to_remove)
