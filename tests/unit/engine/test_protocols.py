"""Tests for sovyx.engine.protocols — runtime-checkable interfaces."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

from sovyx.engine.protocols import (
    BrainReader,
    BrainWriter,
    ChannelAdapter,
    Lifecycle,
    LLMProvider,
)
from sovyx.engine.types import (
    ChannelType,
    ConceptId,
    ConversationId,
    EpisodeId,
    MindId,
)

# ── Mock implementations for protocol compliance ────────────────────────────


class MockBrainReader:
    """Mock that implements BrainReader protocol."""

    async def search(
        self, query: str, mind_id: MindId, limit: int = 10
    ) -> Sequence[tuple[object, float]]:
        return []

    async def get_concept(self, concept_id: ConceptId) -> object | None:
        return None

    async def recall(
        self, query: str, mind_id: MindId
    ) -> tuple[Sequence[tuple[object, float]], Sequence[object]]:
        return ([], [])

    async def get_related(self, concept_id: ConceptId, limit: int = 10) -> Sequence[object]:
        return []


class MockBrainWriter:
    """Mock that implements BrainWriter protocol."""

    async def learn_concept(
        self, mind_id: MindId, name: str, content: str, **kwargs: object
    ) -> ConceptId:
        return ConceptId("test-concept")

    async def encode_episode(
        self,
        mind_id: MindId,
        conversation_id: ConversationId,
        user_input: str,
        assistant_response: str,
        **kwargs: object,
    ) -> EpisodeId:
        return EpisodeId("test-episode")

    async def strengthen_connection(self, concept_ids: Sequence[ConceptId]) -> None:
        pass


class MockLLMProvider:
    """Mock that implements LLMProvider protocol."""

    @property
    def name(self) -> str:
        return "mock"

    @property
    def is_available(self) -> bool:
        return True

    def supports_model(self, model: str) -> bool:
        return model.startswith("mock-")

    def get_context_window(self, model: str | None = None) -> int:
        return 128_000

    async def close(self) -> None:
        pass

    async def generate(
        self,
        messages: Sequence[dict[str, str]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> object:
        return {"response": "mock"}

    async def stream(
        self,
        messages: Sequence[dict[str, str]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[object]:
        yield {"delta": "mock"}  # pragma: no cover


class MockChannelAdapter:
    """Mock that implements ChannelAdapter protocol."""

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.CLI

    @property
    def capabilities(self) -> set[str]:
        return {"send"}

    @property
    def format_capabilities(self) -> dict[str, object]:
        return {"markdown": False, "max_length": 4096}

    async def initialize(self, config: dict[str, object]) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, target: str, message: str, reply_to: str | None = None) -> str:
        return "msg-001"

    async def edit(self, message_id: str, new_text: str) -> None:
        pass

    async def delete(self, message_id: str) -> None:
        pass

    async def react(self, message_id: str, emoji: str) -> None:
        pass

    async def send_typing(self, target: str) -> None:
        pass


class MockLifecycle:
    """Mock that implements Lifecycle protocol."""

    def __init__(self) -> None:
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running


# ── Protocol compliance tests ───────────────────────────────────────────────


class TestBrainReaderProtocol:
    """BrainReader protocol compliance."""

    def test_is_runtime_checkable(self) -> None:
        assert isinstance(MockBrainReader(), BrainReader)

    def test_non_compliant_fails(self) -> None:
        class NotABrain:
            pass

        assert not isinstance(NotABrain(), BrainReader)

    def test_method_names_aligned(self) -> None:
        """Methods match BrainService (TASK-022) names."""
        methods = {"search", "get_concept", "recall", "get_related"}
        reader = MockBrainReader()
        for method in methods:
            assert hasattr(reader, method)


class TestBrainWriterProtocol:
    """BrainWriter protocol compliance."""

    def test_is_runtime_checkable(self) -> None:
        assert isinstance(MockBrainWriter(), BrainWriter)

    def test_non_compliant_fails(self) -> None:
        class NotAWriter:
            pass

        assert not isinstance(NotAWriter(), BrainWriter)

    def test_method_names_aligned(self) -> None:
        """Methods match BrainService (TASK-022) names."""
        methods = {"learn_concept", "encode_episode", "strengthen_connection"}
        writer = MockBrainWriter()
        for method in methods:
            assert hasattr(writer, method)


class TestLLMProviderProtocol:
    """LLMProvider protocol compliance."""

    def test_is_runtime_checkable(self) -> None:
        assert isinstance(MockLLMProvider(), LLMProvider)

    def test_non_compliant_fails(self) -> None:
        class NotAProvider:
            pass

        assert not isinstance(NotAProvider(), LLMProvider)

    def test_supports_model(self) -> None:
        provider = MockLLMProvider()
        assert provider.supports_model("mock-gpt") is True
        assert provider.supports_model("claude-sonnet") is False

    def test_get_context_window(self) -> None:
        provider = MockLLMProvider()
        assert provider.get_context_window() == 128_000
        assert provider.get_context_window("mock-large") == 128_000

    def test_required_methods(self) -> None:
        methods = {
            "name",
            "is_available",
            "supports_model",
            "get_context_window",
            "close",
            "generate",
        }
        provider = MockLLMProvider()
        for method in methods:
            assert hasattr(provider, method)


class TestChannelAdapterProtocol:
    """ChannelAdapter protocol compliance."""

    def test_is_runtime_checkable(self) -> None:
        assert isinstance(MockChannelAdapter(), ChannelAdapter)

    def test_non_compliant_fails(self) -> None:
        class NotAChannel:
            pass

        assert not isinstance(NotAChannel(), ChannelAdapter)

    def test_required_methods(self) -> None:
        methods = {
            "channel_type",
            "capabilities",
            "format_capabilities",
            "initialize",
            "start",
            "stop",
            "send",
            "edit",
            "delete",
            "react",
            "send_typing",
        }
        adapter = MockChannelAdapter()
        for method in methods:
            assert hasattr(adapter, method)


class TestLifecycleProtocol:
    """Lifecycle protocol compliance."""

    def test_is_runtime_checkable(self) -> None:
        assert isinstance(MockLifecycle(), Lifecycle)

    def test_non_compliant_fails(self) -> None:
        class NotALifecycle:
            pass

        assert not isinstance(NotALifecycle(), Lifecycle)

    async def test_lifecycle_flow(self) -> None:
        svc = MockLifecycle()
        assert svc.is_running is False
        await svc.start()
        assert svc.is_running is True
        await svc.stop()
        assert svc.is_running is False
