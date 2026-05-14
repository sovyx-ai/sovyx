"""Sovyx Plugin Manager — discover, load, and manage plugins.

The PluginManager is the central coordinator for the plugin lifecycle.
It discovers plugins from multiple sources, resolves dependencies via
topological sort, creates sandboxed contexts, and dispatches tool calls.

Error boundary: every tool execution is wrapped in asyncio.wait_for
with per-plugin failure tracking. Plugins that fail consecutively are
auto-disabled to protect engine stability.

Lifecycle safety (v0.32.0 Phase C C2): ``unload`` and ``reload`` wait
for any in-flight tool tasks to complete (or force-cancel after a
configurable timeout) BEFORE running ``teardown()`` and dropping the
plugin from the registry. This prevents the race where a tool was
mid-execution holding the plugin's HTTP client / sandbox enforcer /
DB handle while teardown ripped those resources out from under it.
The book-keeping is per-plugin via ``_in_flight_tasks`` (bounded by
plugin count, typically <20) and registered/cleared on every
``execute()`` invocation.

Supply-chain safety (v0.32.0 Phase C M1): entry-point auto-discovery
default-denies third-party pip packages. First-party plugins
(``ep.dist.name == "sovyx"``) always load; third-party plugins must
be explicitly opted in via ``EngineConfig.plugins.allow_third_party_plugins``
+ ``trusted_plugin_packages``. The skip is structured-logged for the
audit trail. Without this gate, any pip install registering a
``sovyx.plugins`` entry point would auto-execute its module body on
the next daemon boot — BEFORE the AST scanner could even run.

Spec: SPE-008 §6, IMMERSION-004 (dependency resolution), SPE-008-SANDBOX §2 §7
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import typing

from sovyx.observability.audit import get_audit_logger
from sovyx.observability.logging import get_logger
from sovyx.plugins._dependency import _topological_sort
from sovyx.plugins._event_emitter import PluginEventEmitter
from sovyx.plugins._manager_types import (
    LoadedPlugin,
    PluginDisabledError,
    PluginError,
    _PluginHealth,
)
from sovyx.plugins.context import BrainAccess, EventBusAccess, PluginContext
from sovyx.plugins.lifecycle import (
    emit_plugin_loaded as _emit_lifecycle_loaded,
)
from sovyx.plugins.lifecycle import (
    emit_plugin_unloaded as _emit_lifecycle_unloaded,
)
from sovyx.plugins.lifecycle import (
    probe_now as _probe_now,
)
from sovyx.plugins.permissions import (
    Permission,
    PermissionEnforcer,
)
from sovyx.plugins.sdk import ISovyxPlugin, ToolDefinition
from sovyx.plugins.security import ImportGuard, PluginSecurityScanner

if typing.TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from sovyx.brain.service import BrainService
    from sovyx.engine.events import EventBus
    from sovyx.engine.types import MindId
    from sovyx.llm.models import ToolResult
    from sovyx.plugins.manifest import PluginManifest

logger = get_logger(__name__)
audit_logger = get_audit_logger()


def _hash_manifest(manifest: PluginManifest | None) -> str | None:
    """SHA-256 of the manifest's canonical JSON form, or None if absent.

    Used by the plugin permission audit to fingerprint exactly which
    manifest version granted a permission set, so a later
    ``audit.plugin.reloaded`` with a different hash flags that the
    permission contract changed across the reload.
    """
    if manifest is None:
        return None
    import hashlib  # noqa: PLC0415 — single-call import isolation.

    payload = manifest.model_dump_json().encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ── Constants ───────────────────────────────────────────────────────

_DEFAULT_TOOL_TIMEOUT_S = 30.0
_MAX_CONSECUTIVE_FAILURES = 5

# v0.32.0 Phase C C2 — default unload-wait timeout. After this many
# seconds the manager force-cancels in-flight tool tasks instead of
# waiting indefinitely. 30 s mirrors the default tool execution
# timeout so a unload/reload during a long-running tool gives the
# tool ONE full chance to finish before getting cancelled.
_DEFAULT_UNLOAD_TIMEOUT_S = 30.0

# v0.32.0 Phase C C2 — grace window for force-cancelled tasks. After
# `task.cancel()` we wait this many seconds for the cancellation to
# propagate (CancelledError bubbling up + finally blocks running)
# before declaring the task lost. Short on purpose: any tool that
# blocks past this window is misbehaving + the manager must not stall
# the daemon shutdown path.
_FORCE_CANCEL_GRACE_S = 1.0

# v0.32.0 Phase C M1 — first-party distribution name. Entry points
# whose ``ep.dist.name`` matches this value are always loaded; all
# others go through the operator allowlist gate. Sourced from
# ``pyproject.toml`` ``[project]name`` — must stay in sync with that
# value (single string literal; CI's package-build step would surface
# a drift via pytest collection failures on this constant).
_FIRST_PARTY_DIST_NAME = "sovyx"

# Hard byte cap for plugin.args_preview / plugin.result_preview fields
# emitted on every invoke. Content sanitization (PII masking) happens
# downstream in the structlog PIIRedactor processor — this helper only
# enforces size so a 5 MB result never lands in a single log record.
_INVOKE_PREVIEW_MAX_BYTES = 256


def _clamp_preview(value: object, *, max_bytes: int = _INVOKE_PREVIEW_MAX_BYTES) -> str:
    """Render *value* as a short str preview clamped to *max_bytes* UTF-8 bytes.

    Dicts and lists are JSON-serialized with a ``default=str`` fallback so
    non-JSON-native types (paths, datetimes, exceptions) still produce a
    bounded preview instead of raising. Truncation always happens on a
    UTF-8 boundary; a Unicode replacement character is never emitted.
    """
    if isinstance(value, (dict, list, tuple)):
        import json as _json

        rendered = _json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
    else:
        rendered = str(value)
    data = rendered.encode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return rendered
    return data[:max_bytes].decode("utf-8", errors="ignore") + "…"


# Public re-exports — keep backward-compatible imports working.
__all__ = [
    "LoadedPlugin",
    "PluginDisabledError",
    "PluginError",
    "PluginManager",
    "_PluginHealth",
    "_topological_sort",
]


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
        allow_third_party_plugins: bool = False,
        trusted_plugin_packages: list[str] | None = None,
        mind_id: MindId | None = None,
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
            allow_third_party_plugins: M1 supply-chain gate. When False
                (default), third-party entry-point plugins are skipped
                without ever calling ``ep.load()``. First-party (Sovyx
                distribution) plugins always load.
            trusted_plugin_packages: Allowlist of pip package names
                (PEP 503 normalized). Only consulted when
                ``allow_third_party_plugins=True``. Empty list with the
                gate enabled still skips all third-party packages.
            mind_id: The mind whose brain plugins query/mutate. REQUIRED
                whenever ``brain`` is provided (BrainAccess is mind-
                scoped at load time per `MISSION-plugin-mind-scope-2026-05-13`
                D-T0-3 Option F). May be ``None`` only when ``brain`` is
                also ``None`` (test fixtures with no brain wiring; no
                BrainAccess will be granted to any plugin).
        """
        self._brain = brain
        self._event_bus = event_bus
        self._emitter = PluginEventEmitter(event_bus)
        self._data_dir = data_dir
        self._mind_id = mind_id
        if brain is not None and mind_id is None:
            msg = (
                "PluginManager requires mind_id when brain is provided. "
                "Pass mind_id=<MindId> from the active mind context (see "
                "engine/bootstrap.py per-mind loop). Plugins query brain "
                "data scoped to this mind; the pre-Phase-1 'default' "
                "sentinel fallback is removed."
            )
            raise ValueError(msg)
        self._enabled = enabled
        self._disabled = disabled or set()
        self._plugin_config = plugin_config or {}
        self._granted_perms = granted_permissions or {}
        self._discover_eps = discover_entry_points
        self._allow_third_party = allow_third_party_plugins
        self._trusted_packages: set[str] = set(trusted_plugin_packages or [])
        self._plugins: dict[str, LoadedPlugin] = {}
        self._registered: list[type[ISovyxPlugin]] = []
        self._scanner = PluginSecurityScanner()
        self._health: dict[str, _PluginHealth] = {}
        self._max_failures = _MAX_CONSECUTIVE_FAILURES
        # v0.32.0 Phase C C2 — per-plugin in-flight task tracking.
        # Bounded by plugin count (typically <20 plugins per Mind), so
        # a plain dict is fine — anti-pattern #15 (LRULockDict) targets
        # unbounded one-key-per-event growth, which doesn't apply here
        # because keys are plugin names and entries are torn down on
        # ``unload``. Each value is a set of currently-running asyncio
        # Task objects, used by ``unload``/``reload`` to wait for clean
        # completion before teardown.
        self._in_flight_tasks: dict[str, set[asyncio.Task[object]]] = {}

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
        # Captured before any I/O so the probe brackets the entire
        # load operation, including setup() and tool collection.
        probe = _probe_now()

        # Get granted permissions
        granted = self._granted_perms.get(name, set())
        # If no explicit grants, grant what the plugin requests
        if not granted:
            granted = {p.value for p in plugin.permissions}
        enforcer = PermissionEnforcer(name, granted)

        audit_logger.info(
            "audit.plugin.permissions.granted",
            **{
                "plugin.id": name,
                "plugin.version": plugin.version,
                "plugin.manifest_hash": _hash_manifest(manifest),
                "plugin.permissions": sorted(granted),
                "plugin.permission_count": len(granted),
            },
        )

        # Create data directory
        from pathlib import Path as _Path

        if self._data_dir:
            plugin_data = self._data_dir / name
        else:
            plugin_data = _Path.home() / ".sovyx" / "plugins" / name
        plugin_data.mkdir(parents=True, exist_ok=True)

        # Build context. Mission `MISSION-plugin-mind-scope-2026-05-13`
        # D-T0-3 — BrainAccess is mind-scoped at LOAD time (Option F).
        # Plugin author code stashes ``self._brain = context.brain``
        # during ``setup()`` (verified at official/knowledge.py:163);
        # per-invocation rebinding would force every plugin author to
        # update their setup contract, so we resolve the active mind
        # ONCE here against the daemon's single-mind invariant. Multi-
        # mind future work (Phase 8 of skype-grade voice mission) will
        # require re-architecting the plugin context lifecycle.
        brain_access: BrainAccess | None = None
        if self._brain and (
            Permission.BRAIN_READ.value in granted or Permission.BRAIN_WRITE.value in granted
        ):
            # The constructor invariant enforces mind_id is non-None
            # whenever brain is non-None; the type checker still wants
            # the narrowing.
            assert self._mind_id is not None
            brain_access = BrainAccess(
                self._brain,
                enforcer,
                write_allowed=Permission.BRAIN_WRITE.value in granted,
                plugin_name=name,
                mind_id=self._mind_id,
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
        # v0.32.0 Phase C C2 — initialize in-flight task tracking
        # bucket. ``execute()`` uses ``setdefault`` defensively, so
        # this is just for explicit lifecycle clarity.
        self._in_flight_tasks.setdefault(name, set())

        logger.info("plugin_loaded", name=name, tools=len(tools))
        _emit_lifecycle_loaded(
            name,
            probe,
            plugin_version=plugin.version,
            tool_count=len(tools),
        )
        self._emit_plugin_loaded(name, plugin.version, len(tools))

    def _discover_entry_points(self) -> list[type[ISovyxPlugin]]:
        """Discover plugins from pip-installed entry_points.

        v0.32.0 Phase C M1 supply-chain gate:

        * **First-party** plugins (``ep.dist.name == "sovyx"``) always
          load — these ship in the same wheel as the engine and are
          covered by the same release-signing posture.
        * **Third-party** plugins go through the operator opt-in
          allowlist. With ``allow_third_party_plugins=False`` (default)
          the entry point is skipped WITHOUT ever calling ``ep.load()``
          — that's the supply-chain contract: arbitrary code in the
          package's module body MUST NOT run unless the operator has
          explicitly trusted the package. With the gate enabled, the
          ``ep.dist.name`` (PEP 503 normalized) must additionally
          appear in ``trusted_plugin_packages``.

        Each skip emits a structured ``plugin.entry_point.skipped_third_party``
        event with the package name and reason, giving operators an
        audit trail for what a daemon could-have-loaded but didn't.
        """
        plugins: list[type[ISovyxPlugin]] = []
        try:
            from importlib.metadata import entry_points

            eps = entry_points(group="sovyx.plugins")
            for ep in eps:
                # M1 — resolve the source distribution name BEFORE
                # ``ep.load()`` so we can default-deny third-party
                # packages without executing their module body.
                dist_name = self._resolve_ep_dist_name(ep)
                is_first_party = dist_name == _FIRST_PARTY_DIST_NAME

                if not is_first_party:
                    if not self._allow_third_party:
                        logger.warning(
                            "plugin.entry_point.skipped_third_party",
                            name=ep.name,
                            package=dist_name,
                            reason="default_deny",
                        )
                        continue
                    if dist_name not in self._trusted_packages:
                        logger.warning(
                            "plugin.entry_point.skipped_third_party",
                            name=ep.name,
                            package=dist_name,
                            reason="not_in_allowlist",
                        )
                        continue

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

    @staticmethod
    def _resolve_ep_dist_name(ep: object) -> str:
        """Best-effort resolve the source distribution name for an entry point.

        ``EntryPoint.dist`` is the modern accessor (Python 3.10+) and
        returns a ``Distribution`` whose ``.name`` is the pip package
        name. Defensive against:

        * older importlib.metadata variants where ``dist`` is missing,
        * test fixtures that use ``MagicMock`` and don't expose a
          ``.dist.name`` attribute,
        * pip packages that registered an entry point without a parent
          ``RECORD`` (extremely rare, but observed in editable installs).

        Returns an empty string when resolution fails — that string
        will NEVER match ``_FIRST_PARTY_DIST_NAME`` and will NEVER be
        in ``_trusted_packages``, so the supply-chain gate stays
        fail-closed.
        """
        try:
            dist = getattr(ep, "dist", None)
            if dist is None:
                return ""
            name = getattr(dist, "name", None)
            if not isinstance(name, str):
                return ""
            return name
        except Exception:  # noqa: BLE001
            return ""

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

        # v0.32.0 Phase C C2 — register the current task in the
        # per-plugin in-flight set so ``unload`` / ``reload`` can wait
        # on it before tearing the plugin down. We use
        # ``asyncio.current_task()`` so the registered handle is the
        # task actually running this coroutine; the done_callback
        # cleans the entry on completion (success / exception /
        # cancellation), keeping the set bounded by truly-running
        # tasks even under concurrent ``execute()`` calls.
        in_flight = self._in_flight_tasks.setdefault(plugin_name, set())
        current = asyncio.current_task()
        if current is not None:
            in_flight.add(current)
            current.add_done_callback(in_flight.discard)

        start_ms = time.monotonic()
        error_msg = ""
        success = False
        result_preview = ""

        args_preview = _clamp_preview(arguments)
        logger.info(
            "plugin.invoke.started",
            **{
                "plugin_id": plugin_name,
                "plugin.tool_name": tool_name,
                "plugin.args_preview": args_preview,
                "plugin.timeout_s": timeout,
            },
        )

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
            result_preview = _clamp_preview(result)
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
        except Exception as e:  # noqa: BLE001
            # Anti-pattern #8: isinstance/except-by-class fails under pytest-cov
            # reimport. Dispatch by name.
            if type(e).__name__ == "PermissionDeniedError":
                error_msg = f"Permission denied: {e}"
                logger.warning(
                    "plugin.invoke.permission_denied",
                    **{
                        "plugin_id": plugin_name,
                        "plugin.tool_name": tool_name,
                        "plugin.permission.attempted_resource": getattr(e, "resource", ""),
                        "plugin.permission.required": list(getattr(e, "required", []) or []),
                        "plugin.permission.detail": str(e),
                    },
                )
                # Permission errors don't count as plugin failures
                return _ToolResult(
                    call_id="",
                    name=tool_name,
                    output=error_msg,
                    success=False,
                )
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
            logger.info(
                "plugin.invoke.completed",
                **{
                    "plugin_id": plugin_name,
                    "plugin.tool_name": tool_name,
                    "plugin.duration_ms": duration_ms,
                    "plugin.success": success,
                    "plugin.result_preview": result_preview,
                    "plugin.health.consecutive_failures": health.consecutive_failures,
                    "plugin.health.active_tasks": health.active_tasks,
                    "plugin.error": error_msg,
                },
            )
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

    # ── Lifecycle event emission (delegated to PluginEventEmitter) ──

    def _emit_tool_executed(
        self,
        plugin_name: str,
        tool_name: str,
        success: bool,
        duration_ms: int,
        error_msg: str,
    ) -> None:
        """Emit PluginToolExecuted (delegate) + record metrics (T05)."""
        self._emitter.tool_executed(
            plugin_name=plugin_name,
            tool_name=tool_name,
            success=success,
            duration_ms=duration_ms,
            error_msg=error_msg,
        )
        # T05 of pre-wake-word-hardening mission (2026-05-02): record
        # the count + latency metrics alongside the existing structured
        # event. Plugin observability becomes time-series-aggregable
        # (was log-event-only).
        from sovyx.plugins._metrics import (  # noqa: PLC0415 — lazy import
            record_tool_executed,
            record_tool_latency,
        )

        record_tool_executed(
            plugin=plugin_name,
            tool=tool_name,
            outcome="ok" if success else "error",
        )
        record_tool_latency(
            plugin=plugin_name,
            tool=tool_name,
            duration_ms=float(duration_ms),
        )

    def _emit_plugin_loaded(
        self,
        plugin_name: str,
        version: str,
        tools_count: int,
    ) -> None:
        """Emit PluginLoaded (delegate)."""
        self._emitter.loaded(
            plugin_name=plugin_name,
            version=version,
            tools_count=tools_count,
        )

    def _emit_plugin_unloaded(self, plugin_name: str, reason: str) -> None:
        """Emit PluginUnloaded (delegate)."""
        self._emitter.unloaded(plugin_name=plugin_name, reason=reason)

    def _emit_auto_disabled(self, plugin_name: str, health: _PluginHealth) -> None:
        """Emit PluginAutoDisabled (delegate) + record metric (T05)."""
        self._emitter.auto_disabled(plugin_name=plugin_name, health=health)
        # T05: time-series surface for the auto-disable signal
        # (was log-event-only). Reason "consecutive_failures" matches
        # the manager-side trigger at ``_record_failure``.
        from sovyx.plugins._metrics import record_auto_disabled  # noqa: PLC0415

        record_auto_disabled(plugin=plugin_name, reason="consecutive_failures")

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

    async def unload(self, name: str, *, timeout: float = _DEFAULT_UNLOAD_TIMEOUT_S) -> None:
        """Unload a single plugin.

        v0.32.0 Phase C C2 — waits for in-flight tool executions on
        this plugin to complete before running ``teardown()`` and
        dropping the registry entry. After ``timeout`` seconds, any
        still-pending tasks are force-cancelled with a short grace
        window for ``CancelledError`` to propagate. This prevents the
        race where a tool was mid-execution holding the plugin's HTTP
        client / sandbox enforcer / DB handle while teardown ripped
        those resources out from under it.

        Args:
            name: Plugin name.
            timeout: How long to wait for in-flight tool tasks to
                finish naturally before force-cancelling them. Default
                30 s mirrors the default per-tool execution timeout.

        Raises:
            PluginError: Plugin not found.
        """
        if name not in self._plugins:
            msg = f"Plugin not found: {name}"
            raise PluginError(msg)

        loaded = self._plugins[name]
        probe = _probe_now()

        # v0.32.0 Phase C C2 — drain in-flight tool tasks BEFORE
        # teardown. We snapshot the current set so concurrent registers
        # (a new tool starting between snapshot and wait) are picked up
        # by the second drain pass; the inner ``execute()`` coroutine
        # holds a strong reference to its task via ``asyncio.current_task()``
        # so the snapshot can't lose a task to garbage collection.
        await self._drain_in_flight(name, timeout=timeout)

        # Cleanup events
        if loaded.context.event_bus:
            loaded.context.event_bus.cleanup()

        try:
            await loaded.plugin.teardown()
        except Exception as e:  # noqa: BLE001
            logger.error("plugin_teardown_failed", plugin=name, error=str(e))

        del self._plugins[name]
        self._health.pop(name, None)
        # Drop the in-flight set last — by this point it should be
        # empty (drain forced cancellation if needed) but the explicit
        # pop avoids leaking the bucket between unload/reload cycles.
        self._in_flight_tasks.pop(name, None)
        logger.info("plugin_unloaded", name=name)
        _emit_lifecycle_unloaded(name, probe, reason="explicit")
        self._emit_plugin_unloaded(name, reason="explicit")

    async def _drain_in_flight(self, name: str, *, timeout: float) -> None:
        """Wait for in-flight tool tasks for *name* to complete.

        v0.32.0 Phase C C2 — two-phase drain:

        1. Snapshot the current in-flight set + ``asyncio.wait`` it
           with the operator-provided ``timeout``. Tasks that complete
           naturally are removed via the done_callback wired in
           ``execute()``.
        2. Any tasks still pending after ``timeout`` are force-cancelled
           via ``task.cancel()`` and waited on with a short grace
           window (``_FORCE_CANCEL_GRACE_S``) so the ``CancelledError``
           can propagate through ``asyncio.wait_for`` and the tool's
           ``finally`` blocks.

        This method does NOT raise on cancellation — best-effort drain
        is the contract. The structured events surface the count + the
        timeout so operators can spot a misbehaving plugin from the
        audit trail.
        """
        tasks = self._in_flight_tasks.get(name, set())
        if not tasks:
            return

        # Phase 1: cooperative wait with the operator timeout.
        snapshot = {t for t in tasks if not t.done()}
        if not snapshot:
            return

        logger.info(
            "plugin.unload.waiting_for_tasks",
            plugin=name,
            count=len(snapshot),
            timeout_s=timeout,
        )
        # Exclude the current task from the wait set — if ``unload``
        # is being called from within an ``execute()`` coroutine on
        # the same plugin (extremely rare but possible via a nested
        # tool helper), waiting on ourselves would deadlock.
        current = asyncio.current_task()
        wait_set = {t for t in snapshot if t is not current}
        if wait_set:
            await asyncio.wait(wait_set, timeout=timeout)

        # Phase 2: force-cancel anything still pending.
        pending = {t for t in snapshot if not t.done() and t is not current}
        if pending:
            logger.warning(
                "plugin.unload.forced_cancel",
                plugin=name,
                count=len(pending),
            )
            for task in pending:
                task.cancel()
            # Grace window for cancellation to propagate. We use
            # ``return_exceptions=True`` so a task whose ``finally``
            # block raises doesn't bubble up here — drain semantics
            # are best-effort, not transactional.
            await asyncio.gather(
                *(self._await_with_grace(t) for t in pending),
                return_exceptions=True,
            )

    @staticmethod
    async def _await_with_grace(task: asyncio.Task[object]) -> None:
        """Await *task* with a short grace window after cancel.

        Used by the force-cancel branch of ``_drain_in_flight``. We
        wrap the await in ``asyncio.wait_for`` with a strict deadline
        so a task that swallows ``CancelledError`` (anti-pattern) can't
        stall the unload path forever. ``TimeoutError`` is suppressed
        because the drain is best-effort.
        """
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, timeout=_FORCE_CANCEL_GRACE_S)

    async def shutdown(self) -> None:
        """Shutdown all plugins in reverse load order."""
        names = list(reversed(self._plugins))
        for name in names:
            try:
                await self.unload(name)
            except Exception as e:  # noqa: BLE001
                logger.error("plugin_shutdown_failed", plugin=name, error=str(e))

    async def reload(self, name: str, *, timeout: float = _DEFAULT_UNLOAD_TIMEOUT_S) -> None:
        """Reload a plugin (teardown + setup).

        v0.32.0 Phase C C2 — drains in-flight tool tasks BEFORE
        teardown using the same protocol as ``unload``. A reload while
        a long-running tool was mid-execution would otherwise observe
        the tool's globals get orphaned (when ``hot_reload._clear_module_cache``
        strips ``sys.modules`` entries) or the plugin's HTTP client get
        closed mid-request. The drain gives in-flight calls a clean
        chance to finish; the force-cancel grace window guarantees the
        reload won't stall on a misbehaving tool.

        Args:
            name: Plugin name.
            timeout: How long to wait for in-flight tool tasks to
                finish naturally before force-cancelling them.

        Raises:
            PluginError: Plugin not found.
        """
        if name not in self._plugins:
            msg = f"Plugin not found: {name}"
            raise PluginError(msg)

        loaded = self._plugins[name]
        plugin = loaded.plugin
        ctx = loaded.context

        # v0.32.0 Phase C C2 — drain BEFORE teardown.
        await self._drain_in_flight(name, timeout=timeout)

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

    async def reconfigure(
        self,
        name: str,
        new_config: dict[str, object],
        *,
        timeout: float = _DEFAULT_UNLOAD_TIMEOUT_S,
    ) -> None:
        """Update a plugin's config and re-initialize it.

        Tears down the current instance, rebuilds PluginContext with
        the new config, and re-runs setup. Tools are re-collected.
        The plugin_config cache is updated so subsequent reloads
        preserve the new config.

        v0.32.0 Phase C C2 — same drain-before-teardown protocol as
        ``reload``: in-flight tool tasks get a chance to complete
        before the plugin's resources are torn down.

        Args:
            name: Plugin name.
            new_config: New configuration dict to apply.
            timeout: How long to wait for in-flight tool tasks before
                force-cancellation.

        Raises:
            PluginError: Plugin not found.
        """
        if name not in self._plugins:
            msg = f"Plugin not found: {name}"
            raise PluginError(msg)

        # Update the config cache
        self._plugin_config[name] = new_config

        loaded = self._plugins[name]
        plugin = loaded.plugin
        old_ctx = loaded.context

        # v0.32.0 Phase C C2 — drain BEFORE teardown.
        await self._drain_in_flight(name, timeout=timeout)

        # Teardown
        if old_ctx.event_bus:
            old_ctx.event_bus.cleanup()
        await plugin.teardown()

        # Rebuild context with new config (preserve everything else)
        new_ctx = PluginContext(
            plugin_name=old_ctx.plugin_name,
            plugin_version=old_ctx.plugin_version,
            data_dir=old_ctx.data_dir,
            config=new_config,
            logger=old_ctx.logger,
            brain=old_ctx.brain,
            event_bus=old_ctx.event_bus,
        )

        # Re-setup with new context
        guard = loaded.guard or ImportGuard(name)
        with guard:
            await plugin.setup(new_ctx)

        # Update loaded entry
        loaded.context = new_ctx
        loaded.tools = plugin.get_tools()

        logger.info("plugin_reconfigured", name=name)
