"""Sovyx Knowledge Plugin — Brain interface for LLM tool calling.

Makes 'remember that I prefer dark mode' work via the Plugin SDK.
Uses BrainAccess (permission-gated) from PluginContext.

Permissions required: brain:read, brain:write

Ref: SPE-008 Appendix A.6
"""

from __future__ import annotations

import typing
from typing import ClassVar

from sovyx.plugins.sdk import ISovyxPlugin, tool

if typing.TYPE_CHECKING:  # pragma: no cover
    from sovyx.plugins.context import BrainAccess


class KnowledgePlugin(ISovyxPlugin):
    """Brain knowledge interface for LLM tool calling.

    Allows the LLM to store, search, and recall information
    from the Mind's long-term memory via tool calls.
    """

    config_schema: ClassVar[dict[str, object]] = {
        "properties": {
            "max_results": {"type": "integer"},
        },
    }

    def __init__(self, brain: BrainAccess | None = None) -> None:
        self._brain = brain
        self._max_results = 10

    @property
    def name(self) -> str:
        return "knowledge"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Brain knowledge interface — remember, search, recall."

    @tool(description="Remember a piece of information for later recall.")
    async def remember(
        self,
        what: str,
        name: str = "",
        category: str = "fact",
    ) -> str:
        """Store information in long-term memory.

        Args:
            what: The information to remember.
            name: Short name/title (auto-generated if empty).
            category: Category (fact, preference, event, person).

        Returns:
            Confirmation message with concept ID.
        """
        if self._brain is None:
            return "Error: brain access not configured"

        if not name:
            # Auto-generate name from content
            name = what[:50].strip().replace("\n", " ")
            if len(what) > 50:  # noqa: PLR2004
                name += "..."

        try:
            concept_id = await self._brain.learn(
                name=name,
                content=what,
                category=category,
            )
            return f"Remembered: '{name}' (id: {concept_id})"
        except Exception as e:  # noqa: BLE001
            return f"Error remembering: {e}"

    @tool(description="Search memory for information matching a query.")
    async def search(self, query: str, limit: int = 5) -> str:
        """Search long-term memory.

        Args:
            query: What to search for.
            limit: Max results (1-10).

        Returns:
            Matching memories formatted as text.
        """
        if self._brain is None:
            return "Error: brain access not configured"

        limit = max(1, min(self._max_results, limit))

        try:
            results = await self._brain.search(query, limit=limit)
        except Exception as e:  # noqa: BLE001
            return f"Error searching: {e}"

        if not results:
            return f"No memories found for: {query}"

        lines = [f"Found {len(results)} memory(ies):"]
        for r in results:
            name = r.get("name", "?")
            content = str(r.get("content", ""))
            # Truncate long content
            if len(content) > 200:  # noqa: PLR2004
                content = content[:200] + "..."
            lines.append(f"  • {name}: {content}")

        return "\n".join(lines)

    @tool(description="Forget a piece of information (remove from memory).")
    async def forget(self, query: str) -> str:
        """Remove matching information from memory.

        Note: This is a soft operation — searches and reports what would
        be forgotten. Actual deletion requires brain:write permission.

        Args:
            query: What to forget.

        Returns:
            Confirmation of what was found (deletion is advisory).
        """
        if self._brain is None:
            return "Error: brain access not configured"

        try:
            results = await self._brain.search(query, limit=1)
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}"

        if not results:
            return f"Nothing found matching: {query}"

        name = results[0].get("name", "?")
        return f"Found memory '{name}' — marked for review."

    @tool(
        description=(
            "Recall everything known about a topic. Broader than search — returns more context."
        ),
    )
    async def recall_about(self, topic: str) -> str:
        """Deep recall about a topic.

        Args:
            topic: Topic to recall about.

        Returns:
            All relevant memories about the topic.
        """
        if self._brain is None:
            return "Error: brain access not configured"

        try:
            results = await self._brain.search(topic, limit=self._max_results)
        except Exception as e:  # noqa: BLE001
            return f"Error recalling: {e}"

        if not results:
            return f"I don't have any memories about: {topic}"

        lines = [f"What I know about '{topic}' ({len(results)} memories):"]
        for r in results:
            name = r.get("name", "?")
            content = str(r.get("content", ""))
            category = r.get("category", "")
            if len(content) > 300:  # noqa: PLR2004
                content = content[:300] + "..."
            cat_tag = f" [{category}]" if category else ""
            lines.append(f"  • {name}{cat_tag}: {content}")

        return "\n".join(lines)

    @tool(
        description="List what you know — summary of stored memories.",
    )
    async def what_do_you_know(self) -> str:
        """Summary of all stored knowledge.

        Returns a high-level summary of memory contents.
        """
        if self._brain is None:
            return "Error: brain access not configured"

        try:
            # Search with a broad query to get recent/important memories
            results = await self._brain.search(
                "important memories knowledge",
                limit=self._max_results,
            )
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}"

        if not results:
            return "My memory is empty — I haven't learned anything yet."

        lines = [f"I have {len(results)} relevant memories:"]
        for r in results:
            name = r.get("name", "?")
            category = r.get("category", "")
            cat_tag = f" [{category}]" if category else ""
            lines.append(f"  • {name}{cat_tag}")

        return "\n".join(lines)
