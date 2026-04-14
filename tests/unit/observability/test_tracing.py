"""Tests for sovyx.observability.tracing — OTel spans for cognitive pipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry.sdk.trace.export import (
    SpanExporter,
    SpanExportResult,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan

from sovyx.observability.tracing import (
    SovyxTracer,
    get_tracer,
    setup_tracing,
    teardown_tracing,
)

# ── In-memory span collector ───────────────────────────────────────────────


class InMemoryExporter(SpanExporter):
    """Collects finished spans in a list for test assertions."""

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Any) -> SpanExportResult:  # noqa: ANN401
        """Store exported spans."""
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """No-op."""

    def force_flush(self, timeout_millis: int = 0) -> bool:
        """No-op flush."""
        return True

    def clear(self) -> None:
        """Clear collected spans."""
        self.spans.clear()


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_otel_trace() -> None:
    """Reset OTel global tracer state between tests."""
    from opentelemetry.trace import _TRACER_PROVIDER_SET_ONCE

    yield  # type: ignore[misc]
    teardown_tracing()
    _TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]


@pytest.fixture()
def exporter() -> InMemoryExporter:
    """Fresh in-memory span exporter."""
    return InMemoryExporter()


@pytest.fixture()
def tracer(exporter: InMemoryExporter) -> SovyxTracer:
    """Set up tracing with in-memory exporter.

    ``batch=False`` forces synchronous span export so assertions on
    ``exporter.spans`` right after a ``with`` block see the finished
    span without having to wait on BatchSpanProcessor's worker thread.
    Production code uses the default ``batch=True``.
    """
    return setup_tracing(exporters=[exporter], batch=False)


# ── Setup / Teardown ────────────────────────────────────────────────────────


class TestSetupTeardown:
    """Tracing lifecycle."""

    def test_setup_returns_sovyx_tracer(self, exporter: InMemoryExporter) -> None:
        t = setup_tracing(exporters=[exporter], batch=False)
        assert isinstance(t, SovyxTracer)

    def test_get_tracer_returns_sovyx_tracer(
        self,
        tracer: SovyxTracer,
    ) -> None:
        t = get_tracer()
        assert isinstance(t, SovyxTracer)

    def test_teardown_cleans_up(self, exporter: InMemoryExporter) -> None:
        setup_tracing(exporters=[exporter], batch=False)
        teardown_tracing()
        # Should not raise; get_tracer returns no-op based tracer
        t = get_tracer()
        assert isinstance(t, SovyxTracer)

    def test_setup_without_exporters(self) -> None:
        """No exporters = spans created but not exported."""
        t = setup_tracing()
        assert isinstance(t, SovyxTracer)

    def test_batch_true_installs_batch_processor(
        self,
        exporter: InMemoryExporter,
    ) -> None:
        """Production default — batch=True wires BatchSpanProcessor."""
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        setup_tracing(exporters=[exporter], batch=True)
        # _active_provider is set by setup_tracing; its _active_span_processor
        # (or _span_processors in recent OTel) must contain a BatchSpanProcessor.
        from sovyx.observability import tracing as _tracing

        provider = _tracing._active_provider
        assert provider is not None
        # TracerProvider exposes span processors via the _active_span_processor
        # attribute, which wraps a list internally. Walk both public-ish paths.
        processors = getattr(provider, "_active_span_processor", None) or getattr(
            provider, "_span_processors", ()
        )
        # MultiSpanProcessor wraps its children in `_span_processors`.
        children = getattr(processors, "_span_processors", None)
        if children is None and hasattr(processors, "__iter__"):
            children = list(processors)
        assert children is not None
        assert any(isinstance(p, BatchSpanProcessor) for p in children)
        # Clean up so other tests don't see the batch-wired provider.
        teardown_tracing()


# ── Cognitive Spans ─────────────────────────────────────────────────────────


class TestCognitiveSpan:
    """start_cognitive_span creates correct spans."""

    def test_creates_span_with_phase(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_cognitive_span("perceive"):
            pass

        assert len(exporter.spans) == 1
        span = exporter.spans[0]
        assert span.name == "cognitive.perceive"
        attrs = dict(span.attributes or {})
        assert attrs["sovyx.cognitive.phase"] == "perceive"

    def test_includes_mind_id_and_conversation_id(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_cognitive_span(
            "think",
            mind_id="nyx",
            conversation_id="conv-42",
        ):
            pass

        attrs = dict(exporter.spans[0].attributes or {})
        assert attrs["sovyx.mind_id"] == "nyx"
        assert attrs["sovyx.conversation_id"] == "conv-42"

    def test_extra_attributes(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_cognitive_span(
            "act",
            person_name="Guipe",
        ):
            pass

        attrs = dict(exporter.spans[0].attributes or {})
        assert attrs["sovyx.person_name"] == "Guipe"

    def test_omits_empty_mind_id(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_cognitive_span("attend"):
            pass

        attrs = dict(exporter.spans[0].attributes or {})
        assert "sovyx.mind_id" not in attrs

    def test_span_set_attribute_inside(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        """Can add attributes inside the context manager."""
        with tracer.start_cognitive_span("reflect") as span:
            span.set_attribute("sovyx.tokens_in", 500)
            span.set_attribute("sovyx.tokens_out", 150)

        attrs = dict(exporter.spans[0].attributes or {})
        assert attrs["sovyx.tokens_in"] == 500
        assert attrs["sovyx.tokens_out"] == 150

    def test_span_records_exception(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        """Spans auto-record exceptions when body raises."""
        with (
            pytest.raises(ValueError, match="test error"),
            tracer.start_cognitive_span("think"),
        ):
            raise ValueError("test error")

        span = exporter.spans[0]
        # OTel records the exception as an event
        events = span.events
        assert len(events) >= 1
        assert events[0].name == "exception"

    def test_all_phases(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        """All cognitive phases create valid spans."""
        phases = ["perceive", "attend", "think", "act", "reflect", "consolidate"]
        for phase in phases:
            with tracer.start_cognitive_span(phase):
                pass

        assert len(exporter.spans) == 6
        names = [s.name for s in exporter.spans]
        for phase in phases:
            assert f"cognitive.{phase}" in names


# ── LLM Spans ──────────────────────────────────────────────────────────────


class TestLLMSpan:
    """start_llm_span for provider calls."""

    def test_creates_llm_span(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_llm_span(provider="anthropic", model="claude-sonnet"):
            pass

        span = exporter.spans[0]
        assert span.name == "llm.call"
        attrs = dict(span.attributes or {})
        assert attrs["sovyx.llm.provider"] == "anthropic"
        assert attrs["sovyx.llm.model"] == "claude-sonnet"

    def test_set_response_attributes(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_llm_span(provider="openai") as span:
            span.set_attribute("sovyx.llm.tokens_in", 1000)
            span.set_attribute("sovyx.llm.tokens_out", 200)
            span.set_attribute("sovyx.llm.cost_usd", 0.003)

        attrs = dict(exporter.spans[0].attributes or {})
        assert attrs["sovyx.llm.tokens_in"] == 1000
        assert attrs["sovyx.llm.cost_usd"] == 0.003

    def test_omits_empty_provider(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_llm_span():
            pass

        attrs = dict(exporter.spans[0].attributes or {})
        assert "sovyx.llm.provider" not in attrs

    def test_extra_llm_attributes(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_llm_span(provider="ollama", temperature=0.7):
            pass

        attrs = dict(exporter.spans[0].attributes or {})
        assert attrs["sovyx.llm.temperature"] == 0.7


# ── Brain Spans ─────────────────────────────────────────────────────────────


class TestBrainSpan:
    """start_brain_span for brain operations."""

    def test_search_span(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_brain_span("search", query="python asyncio"):
            pass

        span = exporter.spans[0]
        assert span.name == "brain.search"
        attrs = dict(span.attributes or {})
        assert attrs["sovyx.brain.query"] == "python asyncio"

    def test_store_concept_span(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_brain_span("store_concept", concept_id="c-123"):
            pass

        assert exporter.spans[0].name == "brain.store_concept"

    def test_spreading_activation_span(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_brain_span("spreading_activation", depth=3):
            pass

        attrs = dict(exporter.spans[0].attributes or {})
        assert attrs["sovyx.brain.depth"] == 3


# ── Context Assembly Spans ──────────────────────────────────────────────────


class TestContextSpan:
    """start_context_span for context assembly."""

    def test_context_assembly_span(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_context_span(slot_count=6, budget=4096):
            pass

        span = exporter.spans[0]
        assert span.name == "context.assembly"
        attrs = dict(span.attributes or {})
        assert attrs["sovyx.context.slot_count"] == 6
        assert attrs["sovyx.context.budget"] == 4096


# ── Generic Spans ───────────────────────────────────────────────────────────


class TestGenericSpan:
    """start_span for arbitrary operations."""

    def test_generic_span(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        with tracer.start_span("bridge.telegram.send", chat_id="12345"):
            pass

        span = exporter.spans[0]
        assert span.name == "bridge.telegram.send"
        attrs = dict(span.attributes or {})
        assert attrs["sovyx.chat_id"] == "12345"


# ── Span Hierarchy ──────────────────────────────────────────────────────────


class TestSpanHierarchy:
    """Nested spans create parent-child relationships."""

    def test_nested_spans(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        """Child spans reference parent's span context."""
        with tracer.start_cognitive_span("think", mind_id="nyx"):  # noqa: SIM117
            with tracer.start_llm_span(provider="anthropic"):
                pass

        assert len(exporter.spans) == 2
        # LLM span finished first (inner), cognitive span finished second (outer)
        llm_span = exporter.spans[0]
        think_span = exporter.spans[1]

        assert think_span.name == "cognitive.think"
        assert llm_span.name == "llm.call"

        # LLM span should be child of think span
        assert llm_span.parent is not None
        assert llm_span.parent.span_id == think_span.context.span_id

    def test_full_pipeline_hierarchy(
        self,
        tracer: SovyxTracer,
        exporter: InMemoryExporter,
    ) -> None:
        """Simulates a full cognitive loop with nested spans."""
        with tracer.start_span("cognitive.loop"):
            with tracer.start_cognitive_span("perceive"):
                pass
            with tracer.start_cognitive_span("attend"):
                pass
            with tracer.start_cognitive_span("think"):
                with tracer.start_context_span():
                    pass
                with tracer.start_llm_span(provider="anthropic"):
                    pass
            with tracer.start_cognitive_span("act"):
                pass
            with tracer.start_cognitive_span("reflect"):  # noqa: SIM117
                with tracer.start_brain_span("encode_episode"):
                    pass

        # 9 spans total: loop + 5 phases + context + llm + brain
        assert len(exporter.spans) == 9

        # Root span is the last to finish
        root = exporter.spans[-1]
        assert root.name == "cognitive.loop"
        assert root.parent is None

        # All others are descendants
        for span in exporter.spans[:-1]:
            assert span.parent is not None


# ── No-op safety ────────────────────────────────────────────────────────────


class TestNoOpSafety:
    """get_tracer works even without setup (no-op tracer)."""

    def test_get_tracer_without_setup(self) -> None:
        """Should not raise — uses OTel no-op tracer."""
        t = get_tracer()
        with t.start_cognitive_span("think"):
            pass  # no-op, no error

    def test_noop_span_attribute_safe(self) -> None:
        """Setting attributes on no-op spans doesn't raise."""
        t = get_tracer()
        with t.start_llm_span(provider="test") as span:
            span.set_attribute("key", "value")
