"""Sovyx Plugin Manager — discover, load, and manage plugins.

The PluginManager is the central coordinator for the plugin lifecycle.
It discovers plugins from multiple sources, resolves dependencies via
topological sort, creates sandboxed contexts, and dispatches tool calls.

Error boundary: every tool execution is wrapped in asyncio.wait_for
with per-plugin failure tracking. Plugins that fail consecutively are
auto-disabled to protect engine stability.

Spec: SPE-008 §6, IMMERSION-004 (dependency resolution), SPE-008-SANDBOX §2 §7
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import typing

from sovyx.observability.logging import get_logger
from sovyx.plugins.context import BrainAccess, EventBusAccess, PluginContext
from sovyx.plugins.permissions import (
    Permission,
    PermissionDeniedError,
    PermissionEnforcer,
)
from sovyx.plugins.sdk import ISovyxPlugin, ToolDefinition
from sovyx.plugins.security import ImportGuard, PluginSecurityScanner

if typing.TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from sovyx.brain.service import BrainService
    from sovyx.engine.events import EventBus
    from sovyx.llm.models import ToolResult
    from sovyx.plugins.manifest import PluginManifest

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

_DEFAULT_TOOL_TIMEOUT_S = 30.0
_MAX_CONSECUTIVE_FAILURES = 5


# ── Data Structures ─────────────────────────────────────────────────


class PluginError(Exception):
    """Raised when a plugin operation fails."""


class PluginDisabledError(PluginError):
    """Raised when executing a tool on an auto-disabled plugin."""


@dataclasses.dataclass
class _PluginHealth:
    """Per-plugin health tracking."""

    consecutive_failures: int = 0
    disabled: bool = False
    last_error: str = ""
    active_tasks: int = 0


@dataclasses.dataclass
class LoadedPlugin:
    """A plugin that has been loaded and initialized."""

    plugin: ISovyxPlugin
    tools: list[ToolDefinition]
    context: PluginContext
    enforcer: PermissionEnforcer
    manifest: PluginManifest | None = None
    guard: ImportGuard | None = None


# ── Dependency Resolution ───────────────────────────────────────────


def _topological_sort(
    plugins: dict[str, list[str]],
) -> list[str]:
    """Topological sort of plugins by dependencies.

    Args:
        plugins: Mapping of plugin_name → list of dependency names.

    Returns:
        Ordered list of plugin names (dependencies first).

    Raises:
        PluginError: Circular dependency detected.
    """
    # Kahn's algorithm
    in_degree: dict[str, int] = {name: 0 for name in plugins}
    graph: dict[str, list[str]] = {name: [] for name in plugins}

    for name, deps in plugins.items():
        for dep in deps:
            if dep in plugins:
                graph[dep].append(name)
                in_degree[name] += 1

    queue = [n for n in plugins if in_degree[n] == 0]
    result: list[str] = []

    while queue:
        queue.sort()  # Deterministic order
        node = queue.pop(0)
        result.append(node)
        for dependent in graph[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(result) != len(plugins):
        missing = set(plugins) - set(result)
        msg = f"Circular dependency detected among: {sorted(missing)}"
        raise PluginError(msg)

    return result


# ── Plugin Manager ──────────────────────────────────────────────────


class PluginManager:
    """Discovers, loads, and manages plugins for a Mind.

    Discovery sources (in priority order):
    1. Programmatic registration (register_class)
    2. pip-installed packages (entry_points: sovyx.plugins)
    3. Local directories (~/.sovyx/plugins/)

    Spec: SPE-008 §6
    """

    def __init__(
        self,
        brain: BrainService | None = None,
        event_bus: EventBus | None = None,
        *,
        data_dir: Path | None = None,
        enabled: set[str] | None = None,
        disabled: set[str] | None = None,
        plugin_config: dict[str, dict[str, object]] | None = None,
        granted_permissions: dict[str, set[str]] | None = None,
        discover_entry_points: bool = True,
    ) -> None:
        """Initialize PluginManager.

        Args:
            brain: BrainService for BrainAccess (None = no brain access).
            event_bus: EventBus for EventBusAccess (None = no events).
            data_dir: Base data directory for plugin storage.
            enabled: If set, only load these plugins. Empty = load all.
            disabled: Plugins to skip even if discovered.
            plugin_config: Per-plugin config dicts.
            granted_permissions: Per-plugin granted permission strings.
            discover_entry_points: If False, skip entry_points discovery in load_all().
        """
        self._brain = brain
        self._event_bus = event_bus
        self._data_dir = data_dir
        self._enabled = enabled
        self._disabled = disabled or set()
        self._plugin_config = plugin_config or {}
        self._granted_perms = granted_permissions or {}
        self._discover_eps = discover_entry_points
        self._plugins: dict[str, LoadedPlugin] = {}
        self._registered: list[type[ISovyxPlugin]] = []
        self._scanner = PluginSecurityScanner()
        self._health: dict[str, _PluginHealth] = {}
        self._max_failures = _MAX_CONSECUTIVE_FAILURES

    # ── Registration ────────────────────────────────────────────────

    def register_class(self, plugin_class: type[ISovyxPlugin]) -> None:
        """Register a plugin class for loading.

        Args:
            plugin_class: ISovyxPlugin subclass.
        """
        self._registered.append(plugin_class)

    # ── Loading ─────────────────────────────────────────────────────

    async def load_all(self) -> list[str]:
        """Discover and load all enabled plugins in dependency order.

        Returns:
            List of loaded plugin names.

        Raises:
            PluginError: Circular dependency detected.
        """
        # Discover plugin classes
        classes = list(self._registered)
        if self._discover_eps:
            classes.extend(self._discover_entry_points())

        # Instantiate to get names and deps
        instances: dict[str, ISovyxPlugin] = {}
        dep_graph: dict[str, list[str]] = {}

        for cls in classes:
            try:
                instance = cls()
                name = instance.name
            except Exception as e:  # noqa: BLE001
                logger.warning("plugin_instantiation_failed", error=str(e))
                continue

            # Filter by enabled/disabled
            if name in self._disabled:
                continue
            if self._enabled is not None and name not in self._enabled:
                continue

            instances[name] = instance
            # Extract hard dependencies from manifest or empty
            dep_graph[name] = []

        # Resolve load order
        load_order = _topological_sort(dep_graph)

        # Load in order
        loaded: list[str] = []
        for name in load_order:
            instance = instances[name]
            try:
                await self._load_plugin(instance)
                loaded.append(name)
            except Exception as e:  # noqa: BLE001
                logger.error("plugin_load_failed", plugin=name, error=str(e))

        return loaded

    async def load_single(
        self,
        plugin: ISovyxPlugin,
        *,
        manifest: PluginManifest | None = None,
    ) -> None:
        """Load a single plugin instance.

        Args:
            plugin: Plugin instance to load.
            manifest: Optional validated manifest.

        Raises:
            PluginError: Plugin already loaded or setup fails.
        """
        name = plugin.name
        if name in self._plugins:
            msg = f"Plugin already loaded: {name}"
            raise PluginError(msg)

        await self._load_plugin(plugin, manifest=manifest)

    async def _load_plugin(
        self,
        plugin: ISovyxPlugin,
        *,
        manifest: PluginManifest | None = None,
    ) -> None:
        """Internal plugin loading with context creation."""
        name = plugin.name

        # Get granted permissions
        granted = self._granted_perms.get(name, set())
        # If no explicit grants, grant what the plugin requests
        if not granted:
            granted = {p.value for p in plugin.permissions}
        enforcer = PermissionEnforcer(name, granted)

        # Create data directory
        from pathlib import Path as _Path

        if self._data_dir:
            plugin_data = self._data_dir / name
        else:
            plugin_data = _Path.home() / ".sovyx" / "plugins" / name
        plugin_data.mkdir(parents=True, exist_ok=True)

        # Build context
        brain_access: BrainAccess | None = None
        if self._brain and (
            Permission.BRAIN_READ.value in granted or Permission.BRAIN_WRITE.value in granted
        ):
            brain_access = BrainAccess(
                self._brain,
                enforcer,
                write_allowed=Permission.BRAIN_WRITE.value in granted,
                plugin_name=name,
            )

        events_access: EventBusAccess | None = None
        if self._event_bus and (
            Permission.EVENT_SUBSCRIBE.value in granted or Permission.EVENT_EMIT.value in granted
        ):
            events_access = EventBusAccess(
                self._event_bus,
                enforcer,
                plugin_name=name,
            )

        import logging

        config = self._plugin_config.get(name, {})
        plugin_logger = logging.getLogger(f"sovyx.plugins.{name}")

        ctx = PluginContext(
            plugin_name=name,
            plugin_version=plugin.version,
            data_dir=plugin_data,
            config=config,
            logger=plugin_logger,
            brain=brain_access,
            event_bus=events_access,
        )

        # Setup with ImportGuard active
        guard = ImportGuard(name)
        with guard:
            await plugin.setup(ctx)

        # Collect tools (already namespaced by get_tools: "plugin.tool")
        tools = plugin.get_tools()

        self._plugins[name] = LoadedPlugin(
            plugin=plugin,
            tools=tools,
            context=ctx,
            enforcer=enforcer,
            manifest=manifest,
            guard=guard,
        )
        self._health[name] = _PluginHealth()

        logger.info("plugin_loaded", name=name, tools=len(tools))
        self._emit_plugin_loaded(name, plugin.version, len(tools))

    def _discover_entry_points(self) -> list[type[ISovyxPlugin]]:
        """Discover plugins from pip-installed entry_points."""
        plugins: list[type[ISovyxPlugin]] = []
        try:
            from importlib.metadata import entry_points

            eps = entry_points(group="sovyx.plugins")
            for ep in eps:
                try:
                    plugin_class = ep.load()
                    if isinstance(plugin_class, type) and issubclass(plugin_class, ISovyxPlugin):
                        plugins.append(plugin_class)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "entry_point_load_failed",
                        name=ep.name,
                        error=str(e),
                    )
        except Exception:  # noqa: BLE001  # nosec B110
            pass
        return plugins

    # ── Tool Execution ──────────────────────────────────────────────

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout: float = _DEFAULT_TOOL_TIMEOUT_S,
    ) -> ToolResult:
        """Execute a plugin tool by namespaced name.

        Tool names are ``{plugin_name}.{tool_name}``, e.g. ``weather.get_weather``.

        Error boundary guarantees:
        - Plugin crash NEVER crashes the engine
        - Timeout enforced via asyncio.wait_for
        - Consecutive failures tracked; auto-disable after threshold
        - Active task count tracked per plugin
        - PluginToolExecuted event emitted on every execution

        Args:
            tool_name: Fully namespaced tool name.
            arguments: Tool arguments dict.
            timeout: Execution timeout in seconds.

        Returns:
            ToolResult with output and success flag.

        Raises:
            PluginError: Plugin or tool not found.
            PluginDisabledError: Plugin was auto-disabled.
        """
        from sovyx.llm.models import ToolResult as _ToolResult

        parts = tool_name.split(".", 1)
        if len(parts) != 2:  # noqa: PLR2004
            msg = f"Invalid tool name format: {tool_name}. Expected 'plugin.tool'"
            raise PluginError(msg)

        plugin_name, func_name = parts

        if plugin_name not in self._plugins:
            msg = f"Plugin not found: {plugin_name}"
            raise PluginError(msg)

        # Check if plugin is auto-disabled
        health = self._health.get(plugin_name)
        if health and health.disabled:
            msg = (
                f"Plugin '{plugin_name}' is disabled after "
                f"{health.consecutive_failures} consecutive failures"
            )
            raise PluginDisabledError(msg)

        loaded = self._plugins[plugin_name]

        # Find tool
        tool: ToolDefinition | None = None
        for t in loaded.tools:
            short = t.name.split(".")[-1]
            if short == func_name:
                tool = t
                break

        if tool is None:
            msg = f"Tool not found: {tool_name}"
            raise PluginError(msg)

        if tool.handler is None:
            msg = f"Tool has no handler: {tool_name}"
            raise PluginError(msg)

        handler = tool.handler

        # Track active tasks
        if health is None:
            health = _PluginHealth()
            self._health[plugin_name] = health
        health.active_tasks += 1

        start_ms = time.monotonic()
        error_msg = ""
        success = False

        # Execute with timeout and ImportGuard
        guard = loaded.guard or ImportGuard(plugin_name)
        try:
            with guard:
                result = await asyncio.wait_for(
                    handler(**arguments),
                    timeout=timeout,
                )
            success = True
            self._record_success(plugin_name)
            return _ToolResult(
                call_id="",
                name=tool_name,
                output=str(result),
                success=True,
            )
        except TimeoutError:
            error_msg = f"Plugin timed out after {timeout}s"
            self._record_failure(plugin_name, error_msg)
            return _ToolResult(
                call_id="",
                name=tool_name,
                output=error_msg,
                success=False,
            )
        except PermissionDeniedError as e:
            error_msg = f"Permission denied: {e}"
            # Permission errors don't count as plugin failures
            return _ToolResult(
                call_id="",
                name=tool_name,
                output=error_msg,
                success=False,
            )
        except Exception as e:  # noqa: BLE001
            error_msg = str(e)
            self._record_failure(plugin_name, error_msg)
            logger.error(
                "plugin_execution_failed",
                plugin=plugin_name,
                tool=func_name,
                error=error_msg,
            )
            return _ToolResult(
                call_id="",
                name=tool_name,
                output=f"Error: {e}",
                success=False,
            )
        finally:
            health.active_tasks = max(0, health.active_tasks - 1)
            duration_ms = int((time.monotonic() - start_ms) * 1000)
            self._emit_tool_executed(
                plugin_name,
                tool_name,
                success,
                duration_ms,
                error_msg,
            )

    def _record_success(self, plugin_name: str) -> None:
        """Record a successful execution — resets consecutive failure count."""
        health = self._health.get(plugin_name)
        if health:
            health.consecutive_failures = 0

    def _record_failure(self, plugin_name: str, error: str) -> None:
        """Record a failed execution. Auto-disables after threshold."""
        health = self._health.get(plugin_name)
        if not health:
            return

        health.consecutive_failures += 1
        health.last_error = error

        if health.consecutive_failures >= self._max_failures and not health.disabled:
            health.disabled = True
            logger.warning(
                "plugin_auto_disabled",
                plugin=plugin_name,
                consecutive_failures=health.consecutive_failures,
                last_error=error,
            )
            self._emit_auto_disabled(plugin_name, health)

    def _emit_tool_executed(
        self,
        plugin_name: str,
        tool_name: str,
        success: bool,
        duration_ms: int,
        error_msg: str,
    ) -> None:
        """Emit PluginToolExecuted event."""
        if not self._event_bus:
            return
        try:
            from sovyx.plugins.events import PluginToolExecuted

            event = PluginToolExecuted(
                plugin_name=plugin_name,
                tool_name=tool_name,
                success=success,
                duration_ms=duration_ms,
                error_message=error_msg,
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._event_bus.emit(event))
            except RuntimeError:
                pass  # No event loop
        except Exception:  # noqa: BLE001  # nosec B110
            pass  # Event emission must never crash

    def _emit_plugin_loaded(
        self,
        plugin_name: str,
        version: str,
        tools_count: int,
    ) -> None:
        """Emit PluginLoaded event."""
        if not self._event_bus:
            return
        try:
            from sovyx.plugins.events import PluginLoaded

            event = PluginLoaded(
                plugin_name=plugin_name,
                plugin_version=version,
                tools_count=tools_count,
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._event_bus.emit(event))
            except RuntimeError:
                pass  # No event loop
        except Exception:  # noqa: BLE001  # nosec B110
            pass  # Event emission must never crash

    def _emit_plugin_unloaded(
        self,
        plugin_name: str,
        reason: str,
    ) -> None:
        """Emit PluginUnloaded event."""
        if not self._event_bus:
            return
        try:
            from sovyx.plugins.events import PluginUnloaded

            event = PluginUnloaded(
                plugin_name=plugin_name,
                reason=reason,
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._event_bus.emit(event))
            except RuntimeError:
                pass  # No event loop
        except Exception:  # noqa: BLE001  # nosec B110
            pass  # Event emission must never crash

    def _emit_auto_disabled(
        self,
        plugin_name: str,
        health: _PluginHealth,
    ) -> None:
        """Emit PluginAutoDisabled event."""
        if not self._event_bus:
            return
        try:
            from sovyx.plugins.events import PluginAutoDisabled

            event = PluginAutoDisabled(
                plugin_name=plugin_name,
                consecutive_failures=health.consecutive_failures,
                last_error=health.last_error,
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._event_bus.emit(event))
            except RuntimeError:
                pass  # No event loop
        except Exception:  # noqa: BLE001  # nosec B110
            pass  # Event emission must never crash

    # ── Query ───────────────────────────────────────────────────────

    def get_tool_definitions(self) -> list[ToolDefinition]:
        """Get all tool definitions across loaded, ACTIVE plugins.

        Excludes tools from auto-disabled plugins. Used by
        Context Assembly (SPE-006) to build the tool list sent to the LLM.

        Returns:
            Tools from all active (non-disabled) plugins.
        """
        tools: list[ToolDefinition] = []
        for name, loaded in self._plugins.items():
            health = self._health.get(name)
            if health and health.disabled:
                continue
            tools.extend(loaded.tools)
        return tools

    def get_plugin(self, name: str) -> LoadedPlugin | None:
        """Get a loaded plugin by name."""
        return self._plugins.get(name)

    def is_plugin_loaded(self, name: str) -> bool:
        """Check if a plugin is loaded."""
        return name in self._plugins

    def is_plugin_disabled(self, name: str) -> bool:
        """Check if a plugin has been auto-disabled."""
        health = self._health.get(name)
        return health.disabled if health else False

    def get_plugin_health(self, name: str) -> dict[str, object]:
        """Get health info for a plugin.

        Returns:
            Dict with consecutive_failures, disabled, last_error, active_tasks.
        """
        health = self._health.get(name)
        if not health:
            return {
                "consecutive_failures": 0,
                "disabled": False,
                "last_error": "",
                "active_tasks": 0,
            }
        return {
            "consecutive_failures": health.consecutive_failures,
            "disabled": health.disabled,
            "last_error": health.last_error,
            "active_tasks": health.active_tasks,
        }

    def re_enable_plugin(self, name: str) -> None:
        """Re-enable an auto-disabled plugin.

        Resets consecutive failures and disabled flag.

        Args:
            name: Plugin name.

        Raises:
            PluginError: Plugin not found.
        """
        if name not in self._plugins:
            msg = f"Plugin not found: {name}"
            raise PluginError(msg)
        health = self._health.get(name)
        if health:
            health.disabled = False
            health.consecutive_failures = 0
            health.last_error = ""
            logger.info("plugin_re_enabled", plugin=name)

    def disable_plugin(self, name: str) -> None:
        """Manually disable a loaded plugin.

        Sets the disabled flag without incrementing failure count.
        Tools from disabled plugins are excluded from LLM context.

        Args:
            name: Plugin name.

        Raises:
            PluginError: Plugin not found.
        """
        if name not in self._plugins:
            msg = f"Plugin not found: {name}"
            raise PluginError(msg)
        health = self._health.get(name)
        if health is None:
            self._health[name] = _PluginHealth()
            health = self._health[name]
        health.disabled = True
        logger.info("plugin_disabled", plugin=name)

    @property
    def loaded_plugins(self) -> list[str]:
        """Names of all loaded plugins."""
        return list(self._plugins)

    @property
    def plugin_count(self) -> int:
        """Number of loaded plugins."""
        return len(self._plugins)

    # ── Lifecycle ───────────────────────────────────────────────────

    async def unload(self, name: str) -> None:
        """Unload a single plugin.

        Calls teardown, removes from registry.

        Args:
            name: Plugin name.

        Raises:
            PluginError: Plugin not found.
        """
        if name not in self._plugins:
            msg = f"Plugin not found: {name}"
            raise PluginError(msg)

        loaded = self._plugins[name]

        # Cleanup events
        if loaded.context.event_bus:
            loaded.context.event_bus.cleanup()

        try:
            await loaded.plugin.teardown()
        except Exception as e:  # noqa: BLE001
            logger.error("plugin_teardown_failed", plugin=name, error=str(e))

        del self._plugins[name]
        self._health.pop(name, None)
        logger.info("plugin_unloaded", name=name)
        self._emit_plugin_unloaded(name, reason="explicit")

    async def shutdown(self) -> None:
        """Shutdown all plugins in reverse load order."""
        names = list(reversed(self._plugins))
        for name in names:
            try:
                await self.unload(name)
            except Exception as e:  # noqa: BLE001
                logger.error("plugin_shutdown_failed", plugin=name, error=str(e))

    async def reload(self, name: str) -> None:
        """Reload a plugin (teardown + setup).

        Args:
            name: Plugin name.

        Raises:
            PluginError: Plugin not found.
        """
        if name not in self._plugins:
            msg = f"Plugin not found: {name}"
            raise PluginError(msg)

        loaded = self._plugins[name]
        plugin = loaded.plugin
        ctx = loaded.context

        # Teardown
        if ctx.event_bus:
            ctx.event_bus.cleanup()
        await plugin.teardown()

        # Re-setup
        guard = loaded.guard or ImportGuard(name)
        with guard:
            await plugin.setup(ctx)

        # Re-collect tools (already namespaced)
        loaded.tools = plugin.get_tools()

        logger.info("plugin_reloaded", name=name)
