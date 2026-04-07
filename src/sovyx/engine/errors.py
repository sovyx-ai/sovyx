"""Sovyx error hierarchy.

All custom exceptions inherit from SovyxError. Each domain has its own
subtree for precise catching and structured logging.
"""

from __future__ import annotations


class SovyxError(Exception):
    """Base for all Sovyx errors.

    Attributes:
        context: Structured key-value pairs for logging and diagnostics.
    """

    def __init__(self, message: str, *, context: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.context: dict[str, object] = context or {}


# ── Engine ──────────────────────────────────────────────────────────────────


class EngineError(SovyxError):
    """Error in the core engine infrastructure."""


class BootstrapError(EngineError):
    """Failed to bootstrap the engine (DI, service init)."""


class ShutdownError(EngineError):
    """Error during graceful shutdown sequence."""


class ServiceNotRegisteredError(EngineError):
    """Requested service not found in the service registry."""


class LifecycleError(EngineError):
    """Error in daemon lifecycle management (PID lock, signals)."""


class HealthCheckError(EngineError):
    """One or more health checks failed."""


# ── Configuration ───────────────────────────────────────────────────────────


class ConfigError(SovyxError):
    """Error in configuration loading or validation."""


class ConfigNotFoundError(ConfigError):
    """Configuration file not found at expected path."""


class ConfigValidationError(ConfigError):
    """Configuration values failed validation."""


# ── Persistence ─────────────────────────────────────────────────────────────


class PersistenceError(SovyxError):
    """Error in the persistence layer (SQLite, migrations)."""


class DatabaseConnectionError(PersistenceError):
    """Failed to connect to or open the database."""


class MigrationError(PersistenceError):
    """Database migration failed to apply."""


class SchemaError(PersistenceError):
    """Database schema is invalid or corrupted."""


class TransactionError(PersistenceError):
    """Database transaction failed (commit, rollback)."""


# ── Brain ───────────────────────────────────────────────────────────────────


class BrainError(SovyxError):
    """Error in the brain memory system."""


class ConceptNotFoundError(BrainError):
    """Concept with given ID does not exist."""

    concept_id: str = ""

    def __init__(
        self,
        message: str = "",
        *,
        concept_id: str = "",
        context: dict[str, object] | None = None,
    ) -> None:
        self.concept_id = concept_id
        ctx = dict(context or {})
        if concept_id:
            ctx["concept_id"] = concept_id
        msg = message or f"Concept not found: {concept_id}"
        super().__init__(msg, context=ctx)


class EpisodeNotFoundError(BrainError):
    """Episode with given ID does not exist."""

    episode_id: str = ""

    def __init__(
        self,
        message: str = "",
        *,
        episode_id: str = "",
        context: dict[str, object] | None = None,
    ) -> None:
        self.episode_id = episode_id
        ctx = dict(context or {})
        if episode_id:
            ctx["episode_id"] = episode_id
        msg = message or f"Episode not found: {episode_id}"
        super().__init__(msg, context=ctx)


class EmbeddingError(BrainError):
    """Failed to generate or load embeddings (ONNX, model)."""


class SearchError(BrainError):
    """Error during brain search (KNN, FTS5, hybrid)."""


class ConsolidationError(BrainError):
    """Memory consolidation cycle failed."""


# ── Cognitive ───────────────────────────────────────────────────────────────


class CognitiveError(SovyxError):
    """Error in the cognitive loop."""


class PerceptionError(CognitiveError):
    """Error during perception phase (input processing)."""


class AttentionError(CognitiveError):
    """Error during attention phase (filtering, safety)."""


class ThinkError(CognitiveError):
    """Error during think phase (context assembly, LLM call)."""


class ActionError(CognitiveError):
    """Error during act phase (tool execution, response delivery)."""


class ReflectionError(CognitiveError):
    """Error during reflect phase (episode encoding, Hebbian)."""


# ── LLM ─────────────────────────────────────────────────────────────────────


class LLMError(SovyxError):
    """Error in the LLM router or providers."""


class ProviderUnavailableError(LLMError):
    """LLM provider is down, timed out, or returned an error."""


class CostLimitExceededError(LLMError):
    """Daily or per-conversation cost budget exceeded."""


class CircuitOpenError(LLMError):
    """Circuit breaker is open for this provider (too many failures)."""


class TokenBudgetExceededError(LLMError):
    """Request would exceed the model's token context window."""


# ── Context ─────────────────────────────────────────────────────────────────


class ContextError(SovyxError):
    """Error in context assembly."""


class TokenBudgetError(ContextError):
    """Token budget allocation failed (context window too small)."""


class ContextAssemblyError(ContextError):
    """Failed to assemble context for the LLM."""


# ── Bridge ──────────────────────────────────────────────────────────────────


class BridgeError(SovyxError):
    """Error in the communication bridge."""


class ChannelConnectionError(BridgeError):
    """Failed to connect to a channel (Telegram, Discord)."""


class ChannelSendError(BridgeError):
    """Failed to send a message through a channel."""


class MessageRoutingError(BridgeError):
    """Failed to route a message to the correct handler."""


# ── Mind ────────────────────────────────────────────────────────────────────


class MindError(SovyxError):
    """Error in mind definition or management."""


class MindNotFoundError(MindError):
    """Mind with given ID does not exist."""

    mind_id: str = ""

    def __init__(
        self,
        message: str = "",
        *,
        mind_id: str = "",
        context: dict[str, object] | None = None,
    ) -> None:
        self.mind_id = mind_id
        ctx = dict(context or {})
        if mind_id:
            ctx["mind_id"] = mind_id
        msg = message or f"Mind not found: {mind_id}"
        super().__init__(msg, context=ctx)


class MindConfigError(MindError):
    """Mind configuration (mind.yaml) is invalid."""


class PersonalityError(MindError):
    """Error in personality engine (OCEAN, prompt generation)."""


# ── CLI ─────────────────────────────────────────────────────────────────────


class CLIError(SovyxError):
    """Error in the CLI layer."""


# ── Plugin ──────────────────────────────────────────────────────────────────


class PluginError(SovyxError):
    """Error in the plugin system."""


class PluginLoadError(PluginError):
    """Failed to load or initialize a plugin."""


class PluginCrashError(PluginError):
    """Plugin crashed during execution."""


class CloudError(SovyxError):
    """Error in cloud/SaaS subsystem."""


class VoiceError(SovyxError):
    """Error in voice pipeline subsystem."""
