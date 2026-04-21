"""OpenTelemetry OTLP exporter — Phase 11 Task 11.8 of IMPL-OBSERVABILITY-001.

Default OFF behind ``observability.otel.enabled``. When enabled, replaces
the in-process no-op ``TracerProvider`` with a real :class:`TracerProvider`
that batches spans to an OTLP/gRPC endpoint (typically the OpenTelemetry
Collector at ``localhost:4317``) and tags every span with the standard
``service.name`` / ``service.version`` / ``deployment.environment`` /
``host.name`` / ``process.pid`` resource attributes.

The OTLP exporter and httpx auto-instrumentation packages are *optional* —
the engine never imports them at module load. ``pip install sovyx[otel]``
adds:

- ``opentelemetry-exporter-otlp`` (required)
- ``opentelemetry-instrumentation-httpx`` (toggled by
  :attr:`ObservabilityOtelConfig.instrument_httpx`)

Lifecycle is owned by :class:`OtelExporter`. ``start()`` installs the
provider; the bootstrap registers the exporter in ``_closables`` so
``stop()`` is awaited on shutdown to flush in-flight spans and join the
``BatchSpanProcessor`` worker thread.

Usage from bootstrap::

    if engine_config.observability.otel.enabled:
        from sovyx.observability.otel import OtelExporter

        exporter = OtelExporter(engine_config.observability.otel)
        exporter.start()
        _closables.append(exporter)
        registry.register_instance(OtelExporter, exporter)
"""

from __future__ import annotations

import asyncio
import os
import socket
from typing import TYPE_CHECKING, Any

from sovyx import __version__
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.config import ObservabilityOtelConfig

logger = get_logger(__name__)


class OtelExporterUnavailableError(RuntimeError):
    """Raised when ``observability.otel.enabled=True`` but the OTLP exporter package is missing.

    Operators see this on startup with a clear remediation hint:
    ``pip install sovyx[otel]``. We never silently degrade enabled OTel
    to no-op tracing — a misconfigured exporter would otherwise hide
    distributed-tracing gaps until production debugging.
    """


def _build_resource_attributes(
    *,
    deployment_environment: str,
) -> dict[str, str | int]:
    """Build the OTel ``Resource`` attribute dict.

    Follows the OTel semantic conventions:
        * ``service.name`` — fixed ``"sovyx"`` so trace queries can group
          regardless of host.
        * ``service.version`` — engine version from ``importlib.metadata``.
        * ``deployment.environment`` — operator-supplied (dev / staging /
          prod) so the same trace backend can hold multiple environments.
        * ``host.name`` — ``socket.gethostname()``; used for fleet scoping.
        * ``process.pid`` — the daemon PID, useful for correlating a trace
          to a specific process restart in the logs.
    """
    return {
        "service.name": "sovyx",
        "service.version": __version__,
        "deployment.environment": deployment_environment,
        "host.name": socket.gethostname(),
        "process.pid": os.getpid(),
    }


class OtelExporter:
    """Owns the lifetime of the OTLP TracerProvider + auto-instrumentation.

    Construction is cheap — no OTel imports happen until :meth:`start`.
    That keeps the optional ``opentelemetry-exporter-otlp`` package
    out of the engine's hot path; environments that don't enable OTel
    never load the SDK exporter modules.

    Args:
        config: The :class:`ObservabilityOtelConfig` block to drive
            endpoint/insecure/instrumentation flags from.
    """

    def __init__(self, config: ObservabilityOtelConfig) -> None:
        self._config = config
        self._provider: Any | None = None
        self._instrumentors: list[Any] = []

    def start(self) -> None:
        """Install the OTLP-backed TracerProvider as the global tracer.

        Raises:
            OtelExporterUnavailableError: When ``opentelemetry-exporter-otlp``
                is not installed. Prints a remediation hint pointing at
                ``pip install sovyx[otel]``.
        """
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError as exc:
            msg = (
                "OpenTelemetry OTLP exporter is not installed. "
                "Disable observability.otel.enabled or install the optional "
                "dependency: pip install sovyx[otel]"
            )
            raise OtelExporterUnavailableError(msg) from exc

        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from sovyx.observability.tracing import (
            _BATCH_MAX_EXPORT_SIZE,
            _BATCH_MAX_QUEUE_SIZE,
            _BATCH_SCHEDULE_DELAY_MILLIS,
            _reset_tracer_provider_latch,
        )

        resource = Resource.create(
            _build_resource_attributes(
                deployment_environment=self._config.deployment_environment,
            ),
        )
        exporter = OTLPSpanExporter(
            endpoint=self._config.endpoint,
            insecure=self._config.insecure,
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=_BATCH_MAX_QUEUE_SIZE,
                schedule_delay_millis=_BATCH_SCHEDULE_DELAY_MILLIS,
                max_export_batch_size=_BATCH_MAX_EXPORT_SIZE,
            ),
        )

        # OTel guards ``set_tracer_provider`` with a one-shot latch — without
        # this reset, anything that touched the global tracer earlier (tests,
        # plugins, another bootstrap pass) would silently win and our OTLP
        # provider would never publish.
        _reset_tracer_provider_latch()
        trace.set_tracer_provider(provider)
        self._provider = provider

        if self._config.instrument_httpx:
            self._maybe_instrument_httpx()

        logger.info(
            "otel_exporter_started",
            endpoint=self._config.endpoint,
            insecure=self._config.insecure,
            deployment_environment=self._config.deployment_environment,
            instrument_httpx=bool(self._instrumentors),
        )

    def _maybe_instrument_httpx(self) -> None:
        """Attach the httpx auto-instrumentor if the package is installed.

        Soft-fails: a missing ``opentelemetry-instrumentation-httpx`` is
        logged at INFO and skipped rather than raising — operators may
        legitimately want the OTLP exporter without the auto-instrumentor.
        """
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        except ImportError:
            logger.info(
                "otel_httpx_instrumentation_skipped",
                reason="opentelemetry-instrumentation-httpx not installed",
                hint="pip install opentelemetry-instrumentation-httpx",
            )
            return
        instrumentor = HTTPXClientInstrumentor()
        instrumentor.instrument()
        self._instrumentors.append(instrumentor)

    async def stop(self) -> None:
        """Flush in-flight spans and tear the provider down.

        Async wrapper around the sync OTel shutdown path so the bootstrap
        ``_closables`` cleanup chain (which awaits every entry) can drive
        it. Spawns the blocking ``provider.shutdown()`` on a worker
        thread because BatchSpanProcessor's shutdown joins its export
        worker and may issue a final OTLP HTTP/gRPC round trip.
        """
        await asyncio.to_thread(self._sync_shutdown)

    def _sync_shutdown(self) -> None:
        """Synchronous teardown — uninstrument + flush + shutdown the provider.

        Idempotent: calling twice (e.g., from both ``stop()`` and a manual
        cleanup path) leaves the second call as a no-op.
        """
        for inst in self._instrumentors:
            try:
                inst.uninstrument()
            except Exception:  # noqa: BLE001 — uninstrument failures must not block shutdown.
                logger.warning(
                    "otel_uninstrument_failed",
                    instrumentor=type(inst).__name__,
                    exc_info=True,
                )
        self._instrumentors = []

        provider = self._provider
        self._provider = None
        if provider is not None:
            try:
                provider.shutdown()
            except Exception:  # noqa: BLE001 — shutdown failures must not raise from cleanup.
                logger.warning(
                    "otel_provider_shutdown_failed",
                    exc_info=True,
                )


__all__ = ["OtelExporter", "OtelExporterUnavailableError"]
