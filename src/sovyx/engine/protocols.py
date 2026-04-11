"""Sovyx protocols (interfaces) for dependency injection.

Runtime-checkable Protocol classes that define contracts between modules.
Implementations are registered in the ServiceRegistry (TASK-038).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.engine.types import (
        ChannelType,
        ConceptCategory,
        ConceptId,
        ConversationId,
        EpisodeId,
        MindId,
    )


@runtime_checkable
class BrainReader(Protocol):
    """Read-only brain interface for plugins and modules.

    Method names aligned with BrainService (TASK-022).
    """

    async def search(
        self, query: str, mind_id: MindId, limit: int = 10
    ) -> Sequence[tuple[object, float]]:
        """Search concepts by query. Returns (concept, score) pairs."""
        ...

    async def get_concept(self, concept_id: ConceptId) -> object | None:
        """Get a concept by ID, or None if not found."""
        ...

    async def recall(
        self, query: str, mind_id: MindId
    ) -> tuple[Sequence[tuple[object, float]], Sequence[object]]:
        """Recall concepts and episodes. Returns (concepts_with_scores, episodes)."""
        ...

    async def get_related(self, concept_id: ConceptId, limit: int = 10) -> Sequence[object]:
        """Get concepts related to the given concept."""
        ...


@runtime_checkable
class BrainWriter(Protocol):
    """Write interface for brain memory.

    Method names aligned with BrainService (TASK-022).
    """

    async def learn_concept(
        self,
        mind_id: MindId,
        name: str,
        content: str,
        category: ConceptCategory = ...,
        source: str = ...,
        *,
        importance: float | None = None,
        confidence: float | None = None,
        emotional_valence: float = 0.0,
        **kwargs: object,
    ) -> ConceptId:
        """Learn a new concept. Returns the concept ID.

        Args:
            importance: Initial importance [0.0, 1.0] or None for default.
            confidence: Initial confidence [0.0, 1.0] or None for default.
            emotional_valence: Sentiment score [-1.0, 1.0].
        """
        ...

    async def encode_episode(
        self,
        mind_id: MindId,
        conversation_id: ConversationId,
        user_input: str,
        assistant_response: str,
        **kwargs: object,
    ) -> EpisodeId:
        """Encode a conversation episode. Returns the episode ID."""
        ...

    async def strengthen_connection(self, concept_ids: Sequence[ConceptId]) -> None:
        """Strengthen Hebbian connections between concepts."""
        ...


@runtime_checkable
class LLMProvider(Protocol):
    """Interface for LLM provider adapters."""

    @property
    def name(self) -> str:
        """Provider name (e.g., 'anthropic', 'openai', 'ollama')."""
        ...

    @property
    def is_available(self) -> bool:
        """True if the provider is configured and reachable."""
        ...

    def supports_model(self, model: str) -> bool:
        """True if this provider can serve the given model."""
        ...

    def get_context_window(self, model: str | None = None) -> int:
        """Context window size for the given model (or default model)."""
        ...

    async def close(self) -> None:
        """Close httpx client and release connections."""
        ...

    async def generate(
        self,
        messages: Sequence[dict[str, str]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> object:
        """Generate a response from the LLM."""
        ...


@runtime_checkable
class ChannelAdapter(Protocol):
    """Interface for communication channel adapters (Telegram, CLI, etc.)."""

    @property
    def channel_type(self) -> ChannelType:
        """The type of channel."""
        ...

    @property
    def capabilities(self) -> set[str]:
        """Supported capabilities: send, edit, delete, react, typing."""
        ...

    @property
    def format_capabilities(self) -> dict[str, object]:
        """Format support: markdown, max_length, etc."""
        ...

    async def initialize(self, config: dict[str, object]) -> None:
        """Initialize the channel with configuration."""
        ...

    async def start(self) -> None:
        """Start the channel (connect, begin polling/webhook)."""
        ...

    async def stop(self) -> None:
        """Stop the channel gracefully."""
        ...

    async def send(
        self,
        target: str,
        message: str,
        reply_to: str | None = None,
        buttons: list[list[object]] | None = None,
    ) -> str:
        """Send a message. Returns platform message ID.

        Args:
            target: Chat ID (where to send).
            message: Message text.
            reply_to: Platform message ID to reply to.
            buttons: Optional inline buttons (list of rows of InlineButton).
                Channels that don't support inline buttons should convert
                them to numbered text options or ignore them.
        """
        ...

    async def edit(
        self,
        message_id: str,
        new_text: str,
        buttons: list[list[object]] | None = None,
        target: str | None = None,
    ) -> None:
        """Edit a previously sent message.

        Args:
            message_id: Platform message ID to edit.
            new_text: New message text.
            buttons: New inline buttons (None = remove buttons).
            target: Chat ID (required by some platforms like Telegram).
        """
        ...

    async def delete(self, message_id: str) -> None:
        """Delete a message."""
        ...

    async def react(self, message_id: str, emoji: str) -> None:
        """React to a message with an emoji."""
        ...

    async def send_typing(self, target: str) -> None:
        """Send typing indicator to a channel."""
        ...


@runtime_checkable
class Lifecycle(Protocol):
    """Lifecycle interface for services managed by the engine."""

    async def start(self) -> None:
        """Start the service."""
        ...

    async def stop(self) -> None:
        """Stop the service gracefully."""
        ...

    @property
    def is_running(self) -> bool:
        """True if the service is currently running."""
        ...
