"""Sovyx Plugin Testing Harness — mock contexts for plugin developers.

Provides MockPluginContext and friends so plugin developers can test
their plugins without running the full Sovyx engine.

Usage::

    from sovyx.plugins.testing import MockPluginContext

    ctx = MockPluginContext(plugin_name="my-plugin")
    brain = ctx.brain  # MockBrainAccess
    events = ctx.events  # MockEventBus

    # Seed brain data
    brain.seed([{"name": "user-pref", "content": "dark mode"}])

    # Test your plugin
    plugin = MyPlugin(brain=brain)
    result = await plugin.my_tool("query")

Ref: SPE-008-SDK-TESTING
"""

from __future__ import annotations

import dataclasses
import typing
from typing import Any

if typing.TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable


# ── MockBrainAccess ─────────────────────────────────────────────────


class MockBrainAccess:
    """Mock brain access for testing plugins that use brain:read/write.

    Supports seeding data, tracking calls, and asserting on operations.
    """

    def __init__(self) -> None:
        self._concepts: list[dict[str, object]] = []
        self._learned: list[dict[str, str]] = []
        self._search_calls: list[dict[str, object]] = []
        self._next_id = 1

    def seed(self, concepts: list[dict[str, object]]) -> None:
        """Pre-populate brain with test data.

        Args:
            concepts: List of concept dicts with name, content, category keys.
        """
        self._concepts.extend(concepts)

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        """Search seeded concepts by simple substring matching."""
        self._search_calls.append({"query": query, "limit": limit})
        q = query.lower()
        results: list[dict[str, object]] = []
        for concept in self._concepts:
            name = str(concept.get("name", "")).lower()
            content = str(concept.get("content", "")).lower()
            if q in name or q in content:
                results.append(concept)
            if len(results) >= limit:
                break
        return results

    async def learn(
        self,
        name: str,
        content: str,
        *,
        category: str = "fact",
        metadata: dict[str, object] | None = None,
    ) -> str:
        """Store a concept and return a mock ID."""
        concept_id = f"mock-{self._next_id}"
        self._next_id += 1
        self._learned.append(
            {
                "id": concept_id,
                "name": name,
                "content": content,
                "category": category,
            }
        )
        self._concepts.append(
            {
                "name": name,
                "content": content,
                "category": category,
            }
        )
        return concept_id

    async def find_similar(
        self,
        content: str,
        *,
        threshold: float = 0.88,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        """Mock find_similar — returns empty (no semantic search in mock)."""
        return []

    async def classify_content(
        self,
        old_content: str,
        new_content: str,
    ) -> str:
        """Mock classify — always returns UNRELATED."""
        return "UNRELATED"

    async def reinforce(
        self,
        concept_id: str,
        *,
        importance_delta: float = 0.05,
        confidence_delta: float = 0.10,
    ) -> dict[str, object] | None:
        """Mock reinforce — returns None."""
        return None

    async def forget(self, concept_id: str) -> bool:
        """Mock forget — removes from internal list."""
        before = len(self._concepts)
        self._concepts = [c for c in self._concepts if str(c.get("id", "")) != concept_id]
        return len(self._concepts) < before

    async def forget_all(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        """Mock forget_all — returns empty."""
        return []

    async def create_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str = "related_to",
    ) -> str:
        """Mock create_relation — returns mock ID."""
        return f"rel-mock-{self._next_id}"

    async def boost_importance(self, concept_id: str, delta: float) -> None:
        """Mock boost_importance — no-op."""

    async def get_related(
        self,
        concept_id: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        """Mock get_related — returns empty."""
        return []

    async def search_episodes(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        """Mock search_episodes — returns empty."""
        return []

    async def get_stats(self) -> dict[str, object]:
        """Mock get_stats — returns counts from internal list."""
        return {
            "total_concepts": len(self._concepts),
            "categories": {},
            "total_relations": 0,
            "total_episodes": 0,
        }

    async def get_top_concepts(
        self,
        limit: int = 10,
        *,
        category: str | None = None,
    ) -> list[dict[str, object]]:
        """Mock get_top_concepts — returns from internal list."""
        return self._concepts[:limit]

    async def update(
        self,
        concept_id: str,
        *,
        content: str | None = None,
        confidence: float | None = None,
    ) -> bool:
        """Mock update — no-op."""
        return True

    @property
    def learned_concepts(self) -> list[dict[str, str]]:
        """All concepts created via learn()."""
        return list(self._learned)

    @property
    def search_history(self) -> list[dict[str, object]]:
        """All search() calls made."""
        return list(self._search_calls)

    def assert_learned(self, name: str) -> None:
        """Assert that a concept with given name was learned."""
        names = [c["name"] for c in self._learned]
        if name not in names:
            msg = f"Expected concept '{name}' to be learned. Got: {names}"
            raise AssertionError(msg)

    def assert_searched(self, query: str) -> None:
        """Assert that a search was made with given query."""
        queries = [str(c["query"]) for c in self._search_calls]
        if query not in queries:
            msg = f"Expected search for '{query}'. Got: {queries}"
            raise AssertionError(msg)


# ── MockEventBus ────────────────────────────────────────────────────


class MockEventBus:
    """Mock event bus for testing plugins that emit events."""

    def __init__(self) -> None:
        self._emitted: list[object] = []
        self._handlers: dict[type, list[Callable[..., Any]]] = {}

    async def emit(self, event: object) -> None:
        """Record emitted event."""
        self._emitted.append(event)

    @property
    def emitted_events(self) -> list[object]:
        """All emitted events."""
        return list(self._emitted)

    def assert_emitted(self, event_type: type) -> None:
        """Assert an event of given type was emitted."""
        types = [type(e) for e in self._emitted]
        if event_type not in types:
            msg = f"Expected {event_type.__name__} to be emitted. Got: {types}"
            raise AssertionError(msg)

    def assert_not_emitted(self, event_type: type) -> None:
        """Assert an event of given type was NOT emitted."""
        types = [type(e) for e in self._emitted]
        if event_type in types:
            msg = f"Expected {event_type.__name__} NOT to be emitted."
            raise AssertionError(msg)

    def clear(self) -> None:
        """Clear recorded events."""
        self._emitted.clear()


# ── MockHttpClient ──────────────────────────────────────────────────


@dataclasses.dataclass
class MockHttpResponse:
    """Mock HTTP response."""

    status_code: int = 200
    body: str = ""
    json_data: dict[str, Any] | None = None

    def json(self) -> dict[str, Any]:
        """Return JSON data."""
        return self.json_data or {}


class MockHttpClient:
    """Mock HTTP client for testing plugins with network access.

    Pre-configure responses for URLs.
    """

    def __init__(self) -> None:
        self._responses: dict[str, MockHttpResponse] = {}
        self._requests: list[dict[str, object]] = []
        self._default_response = MockHttpResponse()

    def add_response(
        self,
        url: str,
        *,
        status: int = 200,
        body: str = "",
        json_data: dict[str, Any] | None = None,
    ) -> None:
        """Pre-configure a response for a URL pattern."""
        self._responses[url] = MockHttpResponse(
            status_code=status,
            body=body,
            json_data=json_data,
        )

    async def get(
        self,
        url: str,
        **kwargs: object,
    ) -> MockHttpResponse:
        """Simulate GET request."""
        self._requests.append({"method": "GET", "url": url, **kwargs})
        return self._find_response(url)

    async def post(
        self,
        url: str,
        **kwargs: object,
    ) -> MockHttpResponse:
        """Simulate POST request."""
        self._requests.append({"method": "POST", "url": url, **kwargs})
        return self._find_response(url)

    def _find_response(self, url: str) -> MockHttpResponse:
        """Find matching response for URL."""
        for pattern, response in self._responses.items():
            if pattern in url:
                return response
        return self._default_response

    @property
    def request_history(self) -> list[dict[str, object]]:
        """All requests made."""
        return list(self._requests)

    def assert_called(self, url: str) -> None:
        """Assert a request was made to URL."""
        urls = [str(r["url"]) for r in self._requests]
        if not any(url in u for u in urls):
            msg = f"Expected request to '{url}'. Got: {urls}"
            raise AssertionError(msg)


# ── MockFsAccess ────────────────────────────────────────────────────


class MockFsAccess:
    """Mock filesystem access for testing plugins with fs permissions.

    Uses an in-memory dict as filesystem.
    """

    def __init__(self) -> None:
        self._files: dict[str, str] = {}

    def write(self, path: str, content: str) -> None:
        """Write content to a path."""
        self._files[path] = content

    def read(self, path: str) -> str | None:
        """Read content from a path."""
        return self._files.get(path)

    def exists(self, path: str) -> bool:
        """Check if path exists."""
        return path in self._files

    def list_files(self) -> list[str]:
        """List all written files."""
        return list(self._files.keys())

    def assert_written(self, path: str) -> None:
        """Assert a file was written."""
        if path not in self._files:
            msg = f"Expected '{path}' to be written. Files: {list(self._files.keys())}"
            raise AssertionError(msg)


# ── MockPluginContext ───────────────────────────────────────────────


class MockPluginContext:
    """Complete mock plugin context for testing.

    Provides all mock access objects in one place.

    Usage::

        ctx = MockPluginContext("my-plugin")
        ctx.brain.seed([{"name": "x", "content": "y"}])
        # Use ctx.brain, ctx.events, ctx.http, ctx.fs in your plugin
    """

    def __init__(self, plugin_name: str = "test-plugin") -> None:
        self.plugin_name = plugin_name
        self.brain = MockBrainAccess()
        self.events = MockEventBus()
        self.http = MockHttpClient()
        self.fs = MockFsAccess()
