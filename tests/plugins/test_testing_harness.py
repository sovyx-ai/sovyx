"""Tests for Sovyx Plugin Testing Harness (TASK-447)."""

from __future__ import annotations

import pytest

from sovyx.plugins.testing import (
    MockBrainAccess,
    MockEventBus,
    MockFsAccess,
    MockHttpClient,
    MockHttpResponse,
    MockPluginContext,
)


class TestMockBrainAccess:
    """Tests for MockBrainAccess."""

    @pytest.mark.anyio()
    async def test_search_empty(self) -> None:
        brain = MockBrainAccess()
        results = await brain.search("anything")
        assert results == []

    @pytest.mark.anyio()
    async def test_search_seeded(self) -> None:
        brain = MockBrainAccess()
        brain.seed(
            [
                {"name": "pref", "content": "dark mode"},
                {"name": "fact", "content": "born in 1990"},
            ]
        )
        results = await brain.search("dark")
        assert len(results) == 1
        assert results[0]["name"] == "pref"

    @pytest.mark.anyio()
    async def test_search_limit(self) -> None:
        brain = MockBrainAccess()
        brain.seed([{"name": f"c{i}", "content": "match"} for i in range(10)])
        results = await brain.search("match", limit=3)
        assert len(results) == 3

    @pytest.mark.anyio()
    async def test_learn(self) -> None:
        brain = MockBrainAccess()
        cid = await brain.learn("test", "content", category="fact")
        assert cid == "mock-1"
        assert len(brain.learned_concepts) == 1
        assert brain.learned_concepts[0]["name"] == "test"

    @pytest.mark.anyio()
    async def test_learn_adds_to_searchable(self) -> None:
        brain = MockBrainAccess()
        await brain.learn("dark-mode", "user prefers dark mode")
        results = await brain.search("dark")
        assert len(results) == 1

    def test_assert_learned(self) -> None:
        brain = MockBrainAccess()
        with pytest.raises(AssertionError):
            brain.assert_learned("missing")

    @pytest.mark.anyio()
    async def test_assert_learned_success(self) -> None:
        brain = MockBrainAccess()
        await brain.learn("found", "content")
        brain.assert_learned("found")  # Should not raise

    def test_assert_searched(self) -> None:
        brain = MockBrainAccess()
        with pytest.raises(AssertionError):
            brain.assert_searched("missing")

    @pytest.mark.anyio()
    async def test_assert_searched_success(self) -> None:
        brain = MockBrainAccess()
        await brain.search("hello")
        brain.assert_searched("hello")

    @pytest.mark.anyio()
    async def test_search_history(self) -> None:
        brain = MockBrainAccess()
        await brain.search("q1")
        await brain.search("q2", limit=3)
        assert len(brain.search_history) == 2

    @pytest.mark.anyio()
    async def test_incremental_ids(self) -> None:
        brain = MockBrainAccess()
        id1 = await brain.learn("a", "x")
        id2 = await brain.learn("b", "y")
        assert id1 != id2


class TestMockEventBus:
    """Tests for MockEventBus."""

    @pytest.mark.anyio()
    async def test_emit(self) -> None:
        bus = MockEventBus()
        await bus.emit("event1")
        assert len(bus.emitted_events) == 1

    @pytest.mark.anyio()
    async def test_assert_emitted(self) -> None:
        bus = MockEventBus()
        await bus.emit("hello")
        bus.assert_emitted(str)

    def test_assert_emitted_fails(self) -> None:
        bus = MockEventBus()
        with pytest.raises(AssertionError):
            bus.assert_emitted(str)

    @pytest.mark.anyio()
    async def test_assert_not_emitted(self) -> None:
        bus = MockEventBus()
        bus.assert_not_emitted(str)

    @pytest.mark.anyio()
    async def test_assert_not_emitted_fails(self) -> None:
        bus = MockEventBus()
        await bus.emit("x")
        with pytest.raises(AssertionError):
            bus.assert_not_emitted(str)

    @pytest.mark.anyio()
    async def test_clear(self) -> None:
        bus = MockEventBus()
        await bus.emit("x")
        bus.clear()
        assert len(bus.emitted_events) == 0


class TestMockHttpClient:
    """Tests for MockHttpClient."""

    @pytest.mark.anyio()
    async def test_default_response(self) -> None:
        client = MockHttpClient()
        resp = await client.get("https://example.com")
        assert resp.status_code == 200

    @pytest.mark.anyio()
    async def test_configured_response(self) -> None:
        client = MockHttpClient()
        client.add_response("example.com", status=404, body="not found")
        resp = await client.get("https://example.com/page")
        assert resp.status_code == 404
        assert resp.body == "not found"

    @pytest.mark.anyio()
    async def test_json_response(self) -> None:
        client = MockHttpClient()
        client.add_response("api.test", json_data={"key": "value"})
        resp = await client.get("https://api.test/data")
        assert resp.json() == {"key": "value"}

    @pytest.mark.anyio()
    async def test_post(self) -> None:
        client = MockHttpClient()
        resp = await client.post("https://api.test", data="body")
        assert resp.status_code == 200
        assert len(client.request_history) == 1
        assert client.request_history[0]["method"] == "POST"

    @pytest.mark.anyio()
    async def test_assert_called(self) -> None:
        client = MockHttpClient()
        await client.get("https://example.com")
        client.assert_called("example.com")

    def test_assert_called_fails(self) -> None:
        client = MockHttpClient()
        with pytest.raises(AssertionError):
            client.assert_called("nowhere.com")

    def test_json_default(self) -> None:
        resp = MockHttpResponse()
        assert resp.json() == {}


class TestMockFsAccess:
    """Tests for MockFsAccess."""

    def test_write_read(self) -> None:
        fs = MockFsAccess()
        fs.write("/data/file.txt", "hello")
        assert fs.read("/data/file.txt") == "hello"

    def test_read_missing(self) -> None:
        fs = MockFsAccess()
        assert fs.read("/missing") is None

    def test_exists(self) -> None:
        fs = MockFsAccess()
        assert not fs.exists("/x")
        fs.write("/x", "data")
        assert fs.exists("/x")

    def test_list_files(self) -> None:
        fs = MockFsAccess()
        fs.write("/a", "1")
        fs.write("/b", "2")
        assert set(fs.list_files()) == {"/a", "/b"}

    def test_assert_written(self) -> None:
        fs = MockFsAccess()
        with pytest.raises(AssertionError):
            fs.assert_written("/missing")
        fs.write("/found", "data")
        fs.assert_written("/found")


class TestMockPluginContext:
    """Tests for MockPluginContext."""

    def test_creates_all_mocks(self) -> None:
        ctx = MockPluginContext("my-plugin")
        assert ctx.plugin_name == "my-plugin"
        assert isinstance(ctx.brain, MockBrainAccess)
        assert isinstance(ctx.events, MockEventBus)
        assert isinstance(ctx.http, MockHttpClient)
        assert isinstance(ctx.fs, MockFsAccess)

    def test_default_name(self) -> None:
        ctx = MockPluginContext()
        assert ctx.plugin_name == "test-plugin"

    @pytest.mark.anyio()
    async def test_end_to_end(self) -> None:
        """Simulate a plugin using the mock context."""
        ctx = MockPluginContext("knowledge")
        ctx.brain.seed(
            [
                {"name": "user-pref", "content": "dark mode preferred"},
            ]
        )

        # Simulate plugin searching brain
        results = await ctx.brain.search("dark mode")
        assert len(results) == 1

        # Simulate plugin learning
        await ctx.brain.learn("new-fact", "test fact")
        ctx.brain.assert_learned("new-fact")

        # Simulate plugin emitting event
        await ctx.events.emit({"type": "tool_executed"})
        assert len(ctx.events.emitted_events) == 1
