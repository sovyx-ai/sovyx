"""Tests for sovyx.engine.errors — full error hierarchy."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sovyx.engine.errors import (
    ActionError,
    AttentionError,
    BootstrapError,
    BrainError,
    BridgeError,
    ChannelConnectionError,
    ChannelSendError,
    CircuitOpenError,
    CLIError,
    CognitiveError,
    ConceptNotFoundError,
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    ConsolidationError,
    ContextAssemblyError,
    ContextError,
    CostLimitExceededError,
    DatabaseConnectionError,
    EmbeddingError,
    EngineError,
    EpisodeNotFoundError,
    HealthCheckError,
    LifecycleError,
    LLMError,
    MessageRoutingError,
    MigrationError,
    MindConfigError,
    MindError,
    MindNotFoundError,
    PerceptionError,
    PersistenceError,
    PersonalityError,
    PluginCrashError,
    PluginError,
    PluginLoadError,
    ProviderUnavailableError,
    ReflectionError,
    SchemaError,
    SearchError,
    ServiceNotRegisteredError,
    ShutdownError,
    SovyxError,
    ThinkError,
    TokenBudgetError,
    TokenBudgetExceededError,
    TransactionError,
)

# ── All error classes for parametrized tests ──


ALL_ERRORS: list[type[SovyxError]] = [
    SovyxError,
    EngineError,
    BootstrapError,
    ShutdownError,
    ServiceNotRegisteredError,
    LifecycleError,
    HealthCheckError,
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    PersistenceError,
    DatabaseConnectionError,
    MigrationError,
    SchemaError,
    TransactionError,
    BrainError,
    ConceptNotFoundError,
    EpisodeNotFoundError,
    EmbeddingError,
    SearchError,
    ConsolidationError,
    CognitiveError,
    PerceptionError,
    AttentionError,
    ThinkError,
    ActionError,
    ReflectionError,
    LLMError,
    ProviderUnavailableError,
    CostLimitExceededError,
    CircuitOpenError,
    TokenBudgetExceededError,
    ContextError,
    TokenBudgetError,
    ContextAssemblyError,
    BridgeError,
    ChannelConnectionError,
    ChannelSendError,
    MessageRoutingError,
    MindError,
    MindNotFoundError,
    MindConfigError,
    PersonalityError,
    CLIError,
    PluginError,
    PluginLoadError,
    PluginCrashError,
]


class TestErrorInstantiation:
    """Every error class is instantiable with message and optional context."""

    @pytest.mark.parametrize("error_cls", ALL_ERRORS)
    def test_instantiate_with_message(self, error_cls: type[SovyxError]) -> None:
        err = error_cls("something went wrong")
        assert str(err) == "something went wrong"
        assert err.context == {}

    @pytest.mark.parametrize("error_cls", ALL_ERRORS)
    def test_instantiate_with_context(self, error_cls: type[SovyxError]) -> None:
        ctx = {"key": "value", "count": 42}
        err = error_cls("failed", context=ctx)
        assert str(err) == "failed"
        assert err.context == ctx

    @pytest.mark.parametrize("error_cls", ALL_ERRORS)
    def test_is_exception(self, error_cls: type[SovyxError]) -> None:
        err = error_cls("test")
        assert isinstance(err, Exception)
        assert isinstance(err, SovyxError)


class TestHierarchy:
    """Inheritance tree is correct."""

    # Engine
    def test_engine_tree(self) -> None:
        for cls in [
            BootstrapError,
            ShutdownError,
            ServiceNotRegisteredError,
            LifecycleError,
            HealthCheckError,
        ]:
            assert issubclass(cls, EngineError)
            assert issubclass(cls, SovyxError)

    # Config
    def test_config_tree(self) -> None:
        for cls in [ConfigNotFoundError, ConfigValidationError]:
            assert issubclass(cls, ConfigError)
            assert issubclass(cls, SovyxError)

    # Persistence
    def test_persistence_tree(self) -> None:
        for cls in [DatabaseConnectionError, MigrationError, SchemaError, TransactionError]:
            assert issubclass(cls, PersistenceError)
            assert issubclass(cls, SovyxError)

    # Brain
    def test_brain_tree(self) -> None:
        for cls in [
            ConceptNotFoundError,
            EpisodeNotFoundError,
            EmbeddingError,
            SearchError,
            ConsolidationError,
        ]:
            assert issubclass(cls, BrainError)
            assert issubclass(cls, SovyxError)

    # Cognitive
    def test_cognitive_tree(self) -> None:
        for cls in [PerceptionError, AttentionError, ThinkError, ActionError, ReflectionError]:
            assert issubclass(cls, CognitiveError)
            assert issubclass(cls, SovyxError)

    # LLM
    def test_llm_tree(self) -> None:
        for cls in [
            ProviderUnavailableError,
            CostLimitExceededError,
            CircuitOpenError,
            TokenBudgetExceededError,
        ]:
            assert issubclass(cls, LLMError)
            assert issubclass(cls, SovyxError)

    # Context
    def test_context_tree(self) -> None:
        for cls in [TokenBudgetError, ContextAssemblyError]:
            assert issubclass(cls, ContextError)
            assert issubclass(cls, SovyxError)

    # Bridge
    def test_bridge_tree(self) -> None:
        for cls in [ChannelConnectionError, ChannelSendError, MessageRoutingError]:
            assert issubclass(cls, BridgeError)
            assert issubclass(cls, SovyxError)

    # Mind
    def test_mind_tree(self) -> None:
        for cls in [MindNotFoundError, MindConfigError, PersonalityError]:
            assert issubclass(cls, MindError)
            assert issubclass(cls, SovyxError)

    # CLI
    def test_cli_tree(self) -> None:
        assert issubclass(CLIError, SovyxError)

    # Plugin
    def test_plugin_tree(self) -> None:
        for cls in [PluginLoadError, PluginCrashError]:
            assert issubclass(cls, PluginError)
            assert issubclass(cls, SovyxError)

    # Cross-domain isolation
    def test_domains_isolated(self) -> None:
        """Errors from different domains don't inherit from each other."""
        assert not issubclass(BrainError, EngineError)
        assert not issubclass(LLMError, BrainError)
        assert not issubclass(CognitiveError, PersistenceError)
        assert not issubclass(BridgeError, LLMError)
        assert not issubclass(MindError, CognitiveError)


class TestContextDefault:
    """Context defaults to empty dict, not None."""

    def test_default_context_is_empty_dict(self) -> None:
        err = SovyxError("test")
        assert err.context is not None
        assert err.context == {}
        assert isinstance(err.context, dict)

    def test_contexts_are_independent(self) -> None:
        """Each error gets its own context dict (no shared mutable default)."""
        err1 = SovyxError("a")
        err2 = SovyxError("b")
        err1.context["key"] = "val"
        assert "key" not in err2.context


class TestDocstrings:
    """Every error class has a docstring."""

    @pytest.mark.parametrize("error_cls", ALL_ERRORS)
    def test_has_docstring(self, error_cls: type[SovyxError]) -> None:
        assert error_cls.__doc__ is not None
        assert len(error_cls.__doc__.strip()) > 0


class TestRaiseAndCatch:
    """Errors can be raised and caught at various hierarchy levels."""

    def test_catch_specific(self) -> None:
        with pytest.raises(ConceptNotFoundError):
            raise ConceptNotFoundError("id=abc")

    def test_catch_domain(self) -> None:
        with pytest.raises(BrainError):
            raise ConceptNotFoundError("id=abc")

    def test_catch_base(self) -> None:
        with pytest.raises(SovyxError):
            raise ProviderUnavailableError("anthropic down")

    def test_context_preserved_on_catch(self) -> None:
        try:
            raise DatabaseConnectionError(
                "connection refused",
                context={"host": "localhost", "port": 5432},
            )
        except PersistenceError as exc:
            assert exc.context["host"] == "localhost"
            assert exc.context["port"] == 5432


class TestPropertyBased:
    """Property-based tests for robustness."""

    @given(st.text(min_size=0, max_size=1000))
    def test_any_message_works(self, message: str) -> None:
        err = SovyxError(message)
        assert str(err) == message

    @given(
        st.text(min_size=1, max_size=100),
        st.dictionaries(st.text(min_size=1, max_size=50), st.integers()),
    )
    def test_any_context_works(self, message: str, context: dict[str, int]) -> None:
        err = SovyxError(message, context=context)
        assert err.context == context
