"""Sovyx Plugin Context — Sandboxed access objects for plugins.

PluginContext is the single entry point for plugins to interact with
the engine. Each access object (brain, events, etc.) is gated by
permissions and enforced at runtime by PermissionEnforcer.

Spec: SPE-008 §3 (PluginContext), SPE-008-SANDBOX §4.2
"""

from __future__ import annotations

import dataclasses
import typing

from sovyx.plugins.permissions import (
    PermissionDeniedError,
    PermissionEnforcer,
)

if typing.TYPE_CHECKING:  # pragma: no cover
    import logging
    from pathlib import Path

    from sovyx.brain.service import BrainService
    from sovyx.engine.events import Event, EventBus, EventHandler


# ── Brain Access (permission-gated) ────────────────────────────────


_MAX_SEARCH_RESULTS = 50
_MAX_CONCEPT_CONTENT = 10_240  # 10KB per concept


class BrainAccess:
    """Scoped brain access for plugins.

    Enforces:
    - brain:read for search/recall
    - brain:write for learn (create concepts)
    - Source tagging: all plugin-created concepts tagged "plugin:{name}"
    - Result cap: max 50 results per search
    - Content limit: max 10KB per concept

    Spec: SPE-008-SANDBOX §4.2 (EnforcedBrainAccess)
    """

    def __init__(
        self,
        brain: BrainService,
        enforcer: PermissionEnforcer,
        *,
        write_allowed: bool,
        plugin_name: str,
        mind_id: str = "default",
    ) -> None:
        self._brain = brain
        self._enforcer = enforcer
        self._write = write_allowed
        self._plugin = plugin_name
        self._mind_id = mind_id

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, object]]:
        """Search concepts by semantic similarity.

        Uses BrainService.search() which returns (Concept, score) tuples.

        Args:
            query: Search text.
            limit: Max results (capped at 50).

        Returns:
            List of dicts with name, content, category, importance.

        Raises:
            PermissionDeniedError: brain:read not granted.
        """
        from sovyx.engine.types import MindId

        self._enforcer.check("brain:read")
        capped = min(limit, _MAX_SEARCH_RESULTS)
        results = await self._brain.search(
            query=query,
            mind_id=MindId(self._mind_id),
            limit=capped,
        )
        return [
            {
                "name": concept.name,
                "content": concept.content,
                "category": concept.category.value,
                "importance": concept.importance,
            }
            for concept, _score in results
        ]

    async def learn(
        self,
        name: str,
        content: str,
        *,
        category: str = "fact",
    ) -> str:
        """Create a new concept in the Mind's memory.

        All plugin-created concepts are tagged with source="plugin:{name}".
        Content is limited to 10KB.

        Args:
            name: Concept name/title.
            content: Concept content text (max 10KB).
            category: Category string. Default "fact".

        Returns:
            Created concept ID string.

        Raises:
            PermissionDeniedError: brain:write not granted.
            ValueError: Content exceeds 10KB limit.
        """
        from sovyx.engine.types import ConceptCategory, MindId

        self._enforcer.check("brain:write")
        if not self._write:
            raise PermissionDeniedError(self._plugin, "brain:write")
        if len(content) > _MAX_CONCEPT_CONTENT:
            msg = (
                f"Concept content exceeds {_MAX_CONCEPT_CONTENT} byte limit ({len(content)} bytes)"
            )
            raise ValueError(msg)

        # Map string category to enum, default FACT
        try:
            cat_enum = ConceptCategory(category)
        except ValueError:
            cat_enum = ConceptCategory.FACT

        concept_id = await self._brain.learn_concept(
            mind_id=MindId(self._mind_id),
            name=name,
            content=content,
            category=cat_enum,
            source=f"plugin:{self._plugin}",
        )
        return str(concept_id)


# ── Event Bus Access (permission-gated) ─────────────────────────────


class EventBusAccess:
    """Scoped event bus access for plugins.

    Enforces:
    - event:subscribe for listening
    - event:emit for emitting
    - Auto-cleanup of all subscriptions on teardown

    Spec: SPE-008 §3 (PluginContext events), SPE-008-PLUGIN-IPC §1
    """

    def __init__(
        self,
        event_bus: EventBus,
        enforcer: PermissionEnforcer,
        *,
        plugin_name: str,
    ) -> None:
        self._bus = event_bus
        self._enforcer = enforcer
        self._plugin = plugin_name
        self._subscriptions: list[tuple[type[Event], EventHandler]] = []

    def subscribe(
        self,
        event_type: type[Event],
        handler: EventHandler,
    ) -> None:
        """Subscribe to a typed engine event.

        Subscriptions are tracked and auto-cleaned on teardown().

        Args:
            event_type: Event class to listen for.
            handler: Async handler coroutine.

        Raises:
            PermissionDeniedError: event:subscribe not granted.
        """
        self._enforcer.check("event:subscribe")
        self._bus.subscribe(event_type, handler)
        self._subscriptions.append((event_type, handler))

    async def emit(self, event: Event) -> None:
        """Emit an event. Plugins can emit any event type.

        For cross-plugin communication, use PluginEvent with
        namespace "plugin.{plugin_name}.*".

        Args:
            event: Event instance to emit.

        Raises:
            PermissionDeniedError: event:emit not granted.
        """
        self._enforcer.check("event:emit")
        await self._bus.emit(event)

    def cleanup(self) -> None:
        """Unsubscribe all handlers. Called during plugin teardown."""
        for event_type, handler in self._subscriptions:
            self._bus.unsubscribe(event_type, handler)
        self._subscriptions.clear()

    @property
    def subscription_count(self) -> int:
        """Number of active subscriptions."""
        return len(self._subscriptions)


# ── Plugin Context ──────────────────────────────────────────────────


@dataclasses.dataclass
class PluginContext:
    """Sandboxed context provided to plugins during setup().

    Plugins ONLY get access objects for declared+approved permissions.
    Undeclared services are None.

    Always available:
    - plugin_name, plugin_version, data_dir, config, logger

    Permission-gated (None if not granted):
    - brain: BrainAccess (brain:read / brain:write)
    - event_bus: EventBusAccess (event:subscribe / event:emit)
    - http: SandboxedHttpClient (network:internet / network:local)
    - filesystem: SandboxedFsAccess (fs:read / fs:write)

    Spec: SPE-008 §3
    """

    # Always available
    plugin_name: str
    plugin_version: str
    data_dir: Path
    config: dict[str, object]
    logger: logging.Logger

    # Permission-gated (None = not granted)
    brain: BrainAccess | None = None
    event_bus: EventBusAccess | None = None
    http: object | None = None  # SandboxedHttpClient (TASK-429)
    filesystem: object | None = None  # SandboxedFsAccess (TASK-430)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float = 5.0,
    ) -> object:
        """Invoke a tool from another plugin (cross-plugin IPC).

        Available in v1.1. Current implementation raises NotImplementedError.

        Args:
            tool_name: Fully qualified "plugin_name.tool_name".
            arguments: Tool arguments dict.
            timeout_seconds: Max wait time.

        Returns:
            ToolResult from the target plugin.

        Raises:
            NotImplementedError: Cross-plugin tool invocation not yet available.

        Spec: SPE-008-PLUGIN-IPC §2
        """
        raise NotImplementedError(
            f"Cross-plugin tool invocation available in v1.1. Requested: {tool_name}"
        )

    def is_plugin_available(self, plugin_name: str) -> bool:
        """Check if another plugin is installed and active.

        Available in v1.1. Currently returns False.

        Args:
            plugin_name: Plugin identifier.

        Returns:
            True if plugin is loaded and active.

        Spec: SPE-008-PLUGIN-IPC §2.2
        """
        return False
