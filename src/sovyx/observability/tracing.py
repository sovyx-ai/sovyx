"""Sovyx tracing — distributed traces via OpenTelemetry.

Provides spans for the cognitive loop pipeline, LLM calls, brain searches,
and context assembly.  Each span carries Sovyx-specific attributes
(mind_id, conversation_id, provider, model, etc.).

Usage::

    from sovyx.observability.tracing import get_tracer

    tracer = get_tracer()

    with tracer.start_cognitive_span("perceive") as span:
        span.set_attribute("source", "telegram")
        result = await perceive.process(...)

    # Or use the decorator-style span:
    with tracer.start_llm_span(provider="anthropic", model="sonnet-4"):
        response = await provider.generate(...)

Design decisions:

- **Wraps OTel Tracer** — ``SovyxTracer`` delegates to a real OTel Tracer
  but adds convenience methods with Sovyx-specific attribute schemas.
- **Lazy / no-op safe** — ``get_tracer()`` returns a ``SovyxTracer`` that
  wraps whatever the current global TracerProvider gives.  If tracing is
  not configured, OTel's no-op tracer is used transparently.
- **Span hierarchy** — ``cognitive_loop`` is the root span; phase spans
  (perceive, attend, think, act, reflect) are children.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)

if TYPE_CHECKING:
    from collections.abc import Generator

# ── Module constants ────────────────────────────────────────────────────────

_TRACER_NAME = "sovyx"
# Tracks the observability API version (not the package version in pyproject.toml).
# Bump when span names, attribute schemas, or namespace conventions change.
_TRACER_VERSION = "0.2.0"

# ── BatchSpanProcessor knobs (IMPL-015) ────────────────────────────────
#
# Production default. The synchronous ``SimpleSpanProcessor`` blocks every
# span end on the exporter's HTTP round trip; under load that's a latency
# cliff. BatchSpanProcessor moves export onto a worker thread that flushes
# on a timer or when the queue fills.
#
# Values tuned for a single-node daemon under moderate traffic:
#   - max_queue_size: 2048 spans buffered in memory before back-pressure.
#   - schedule_delay_millis: 5000 ms between scheduled flushes (default
#     behaviour; kept explicit so the intent stays visible).
#   - max_export_batch_size: 512 spans per export call — balances HTTP
#     overhead against exporter memory footprint.

_BATCH_MAX_QUEUE_SIZE = 2048
_BATCH_SCHEDULE_DELAY_MILLIS = 5000
_BATCH_MAX_EXPORT_SIZE = 512

# ── SovyxTracer ────────────────────────────────────────────────────────────


class SovyxTracer:
    """Sovyx-specific tracer wrapping an OTel Tracer.

    Provides convenience methods that enforce consistent span naming
    and attribute schemas across the codebase.

    All ``start_*`` methods return context managers that yield the
    OTel ``Span`` object for adding custom attributes.
    """

    def __init__(self, tracer: trace.Tracer) -> None:
        self._tracer = tracer

    @contextlib.contextmanager
    def start_cognitive_span(
        self,
        phase: str,
        *,
        mind_id: str = "",
        conversation_id: str = "",
        **attributes: Any,  # noqa: ANN401
    ) -> Generator[trace.Span, None, None]:
        """Start a span for a cognitive loop phase.

        Args:
            phase: One of perceive, attend, think, act, reflect, consolidate.
            mind_id: The mind being served.
            conversation_id: Active conversation.
            **attributes: Extra span attributes.

        Yields:
            The active OTel Span.
        """
        span_name = f"cognitive.{phase}"
        with self._tracer.start_as_current_span(span_name) as span:
            if mind_id:
                span.set_attribute("sovyx.mind_id", mind_id)
            if conversation_id:
                span.set_attribute("sovyx.conversation_id", conversation_id)
            span.set_attribute("sovyx.cognitive.phase", phase)
            for key, value in attributes.items():
                span.set_attribute(f"sovyx.{key}", value)
            yield span

    @contextlib.contextmanager
    def start_llm_span(
        self,
        *,
        provider: str = "",
        model: str = "",
        **attributes: Any,  # noqa: ANN401
    ) -> Generator[trace.Span, None, None]:
        """Start a span for an LLM provider call.

        Args:
            provider: LLM provider name (anthropic, openai, ollama).
            model: Model identifier.
            **attributes: Extra span attributes.

        Yields:
            The active OTel Span.  Callers should set
            ``tokens_in``, ``tokens_out``, ``cost_usd`` after the call.
        """
        with self._tracer.start_as_current_span("llm.call") as span:
            if provider:
                span.set_attribute("sovyx.llm.provider", provider)
            if model:
                span.set_attribute("sovyx.llm.model", model)
            for key, value in attributes.items():
                span.set_attribute(f"sovyx.llm.{key}", value)
            yield span

    @contextlib.contextmanager
    def start_brain_span(
        self,
        operation: str,
        **attributes: Any,  # noqa: ANN401
    ) -> Generator[trace.Span, None, None]:
        """Start a span for a brain operation.

        Args:
            operation: e.g. "search", "store_concept", "encode_episode",
                "consolidate", "spreading_activation".
            **attributes: Extra span attributes.

        Yields:
            The active OTel Span.
        """
        with self._tracer.start_as_current_span(f"brain.{operation}") as span:
            for key, value in attributes.items():
                span.set_attribute(f"sovyx.brain.{key}", value)
            yield span

    @contextlib.contextmanager
    def start_context_span(
        self,
        **attributes: Any,  # noqa: ANN401
    ) -> Generator[trace.Span, None, None]:
        """Start a span for context assembly.

        Args:
            **attributes: Extra span attributes (e.g. slot_count, budget).

        Yields:
            The active OTel Span.
        """
        with self._tracer.start_as_current_span("context.assembly") as span:
            for key, value in attributes.items():
                span.set_attribute(f"sovyx.context.{key}", value)
            yield span

    @contextlib.contextmanager
    def start_span(
        self,
        name: str,
        **attributes: Any,  # noqa: ANN401
    ) -> Generator[trace.Span, None, None]:
        """Start a generic span with sovyx-prefixed attributes.

        Use for operations that don't fit the specific categories above.

        Args:
            name: Span name (e.g. "bridge.telegram.send").
            **attributes: Extra span attributes.

        Yields:
            The active OTel Span.
        """
        with self._tracer.start_as_current_span(name) as span:
            for key, value in attributes.items():
                span.set_attribute(f"sovyx.{key}", value)
            yield span


# ── Setup / Get ─────────────────────────────────────────────────────────────

_active_provider: TracerProvider | None = None


def setup_tracing(
    *,
    exporters: list[SpanExporter] | None = None,
    service_name: str = "sovyx",
    batch: bool = True,
) -> SovyxTracer:
    """Initialize the OTel tracing pipeline.

    Safe to call multiple times. If tracing was already configured (and
    possibly shut down), the prior provider is torn down and OTel's
    ``set-once`` guard is reset before the new provider is installed.
    Without this, OTel's :data:`trace._TRACER_PROVIDER_SET_ONCE` makes
    every subsequent :func:`trace.set_tracer_provider` call a silent
    no-op, so span export would stay wired to a stale (possibly
    shut-down) provider and :data:`_active_provider` would appear to
    reset to ``None`` under certain test-ordering sequences.

    Args:
        exporters: List of SpanExporters.  If ``None``, tracing is
            configured with no exporters (spans are created but not
            exported — useful for testing with ``get_finished_spans``
            on the provider).
        service_name: OTel service name attribute.
        batch: When True (the production default), spans are exported
            asynchronously via :class:`BatchSpanProcessor`
            (``max_queue_size=2048``, ``schedule_delay_millis=5000``,
            ``max_export_batch_size=512``). When False, uses
            :class:`SimpleSpanProcessor` so exporter observation is
            synchronous — tests that assert on span state immediately
            after the ``with`` block should pass ``batch=False``.

    Returns:
        A configured :class:`SovyxTracer`.
    """
    global _active_provider  # noqa: PLW0603

    from opentelemetry.sdk.resources import Resource

    # Tear down any prior provider so its exporter/worker threads stop
    # cleanly before we install the new one. Swallowed: the prior
    # provider may already be shut down or in a degraded state, and we
    # never want setup_tracing to raise on the cleanup path.
    if _active_provider is not None:
        with contextlib.suppress(Exception):
            _active_provider.shutdown()
        _active_provider = None

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if exporters:
        for exporter in exporters:
            if batch:
                provider.add_span_processor(
                    BatchSpanProcessor(
                        exporter,
                        max_queue_size=_BATCH_MAX_QUEUE_SIZE,
                        schedule_delay_millis=_BATCH_SCHEDULE_DELAY_MILLIS,
                        max_export_batch_size=_BATCH_MAX_EXPORT_SIZE,
                    ),
                )
            else:
                provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Reset OTel's set-once latch so the new provider actually replaces
    # whatever was there before. Touching the private attribute is
    # deliberate — it's the only public-ish way to re-initialise tracing
    # across a full app lifecycle (startup, test restart, shutdown).
    _reset_tracer_provider_latch()
    trace.set_tracer_provider(provider)
    _active_provider = provider

    return SovyxTracer(provider.get_tracer(_TRACER_NAME, _TRACER_VERSION))


def _reset_tracer_provider_latch() -> None:
    """Force OTel's ``_TRACER_PROVIDER_SET_ONCE`` to allow re-installation.

    OTel guards ``trace.set_tracer_provider`` with a one-shot latch so the
    first caller wins. That's fine in production where tracing is wired
    once at startup, but it makes :func:`setup_tracing` a silent no-op if
    any code path — test suite, REPL warm-up, plugin — touched OTel
    first. Resetting the latch restores "last writer wins" semantics,
    which is the behaviour :func:`setup_tracing` already promised.
    """
    with contextlib.suppress(Exception):
        trace._TRACER_PROVIDER_SET_ONCE._done = False  # noqa: SLF001


def teardown_tracing() -> None:
    """Shut down the tracing pipeline.

    Shuts down the cached provider, and — as a safety net — also calls
    shutdown on OTel's globally installed provider if it's a real
    :class:`TracerProvider` (not the no-op). This covers the case where
    :data:`_active_provider` got out of sync with OTel's global under
    unusual test-ordering / module-reload conditions. Releases OTel's
    one-shot ``_TRACER_PROVIDER_SET_ONCE`` latch so a later
    :func:`setup_tracing` can install a fresh provider — essential for
    daemon restarts and for test suites that run multiple setup/teardown
    cycles in the same interpreter.
    """
    global _active_provider  # noqa: PLW0603

    if _active_provider is not None:
        with contextlib.suppress(Exception):
            _active_provider.shutdown()
        _active_provider = None

    global_provider = trace.get_tracer_provider()
    if isinstance(global_provider, TracerProvider):
        with contextlib.suppress(Exception):
            global_provider.shutdown()

    _reset_tracer_provider_latch()


def get_tracer() -> SovyxTracer:
    """Return a SovyxTracer using the current global TracerProvider.

    Safe to call at any time — if tracing isn't configured, OTel's
    no-op tracer is returned transparently.
    """
    otel_tracer = trace.get_tracer(_TRACER_NAME, _TRACER_VERSION)
    return SovyxTracer(otel_tracer)
