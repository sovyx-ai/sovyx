"""Sovyx Observability — logging, metrics, tracing, health, SLOs, alerts.

Lazy imports to break circular dependency:
  engine.events → observability.logging → __init__ → alerts → engine.events
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovyx.observability._clamp_fields import (
        ClampFieldsProcessor as ClampFieldsProcessor,
    )
    from sovyx.observability._exception_serializer import (
        ExceptionTreeProcessor as ExceptionTreeProcessor,
    )
    from sovyx.observability._exception_serializer import (
        build_cause_chain as build_cause_chain,
    )
    from sovyx.observability._exception_serializer import (
        serialize_exception as serialize_exception,
    )
    from sovyx.observability.alerts import (
        Alert as Alert,
    )
    from sovyx.observability.alerts import (
        AlertFired as AlertFired,
    )
    from sovyx.observability.alerts import (
        AlertManager as AlertManager,
    )
    from sovyx.observability.alerts import (
        AlertRule as AlertRule,
    )
    from sovyx.observability.alerts import (
        AlertSeverity as AlertSeverity,
    )
    from sovyx.observability.alerts import (
        create_default_alert_manager as create_default_alert_manager,
    )
    from sovyx.observability.async_handler import (
        AsyncQueueHandler as AsyncQueueHandler,
    )
    from sovyx.observability.async_handler import (
        BackgroundLogWriter as BackgroundLogWriter,
    )
    from sovyx.observability.envelope import (
        EnvelopeProcessor as EnvelopeProcessor,
    )
    from sovyx.observability.health import (
        CheckResult as CheckResult,
    )
    from sovyx.observability.health import (
        CheckStatus as CheckStatus,
    )
    from sovyx.observability.health import (
        HealthCheck as HealthCheck,
    )
    from sovyx.observability.health import (
        HealthRegistry as HealthRegistry,
    )
    from sovyx.observability.health import (
        create_default_registry as create_default_registry,
    )
    from sovyx.observability.health import (
        create_offline_registry as create_offline_registry,
    )
    from sovyx.observability.logging import (
        bind_request_context as bind_request_context,
    )
    from sovyx.observability.logging import (
        bound_request_context as bound_request_context,
    )
    from sovyx.observability.logging import (
        clear_request_context as clear_request_context,
    )
    from sovyx.observability.logging import (
        get_logger as get_logger,
    )
    from sovyx.observability.logging import (
        get_request_context as get_request_context,
    )
    from sovyx.observability.logging import (
        runtime_get_level as runtime_get_level,
    )
    from sovyx.observability.logging import (
        runtime_set_level as runtime_set_level,
    )
    from sovyx.observability.logging import (
        setup_logging as setup_logging,
    )
    from sovyx.observability.logging import (
        shutdown_logging as shutdown_logging,
    )
    from sovyx.observability.metrics import (
        MetricsRegistry as MetricsRegistry,
    )
    from sovyx.observability.metrics import (
        collect_json as collect_json,
    )
    from sovyx.observability.metrics import (
        get_metrics as get_metrics,
    )
    from sovyx.observability.metrics import (
        setup_metrics as setup_metrics,
    )
    from sovyx.observability.metrics import (
        teardown_metrics as teardown_metrics,
    )
    from sovyx.observability.pii import (
        PIIRedactor as PIIRedactor,
    )
    from sovyx.observability.ringbuffer import (
        RingBufferHandler as RingBufferHandler,
    )
    from sovyx.observability.ringbuffer import (
        install_crash_hooks as install_crash_hooks,
    )
    from sovyx.observability.sampling import (
        SamplingProcessor as SamplingProcessor,
    )
    from sovyx.observability.schema import (
        ENVELOPE_FIELDS as ENVELOPE_FIELDS,
    )
    from sovyx.observability.schema import (
        KNOWN_EVENTS as KNOWN_EVENTS,
    )
    from sovyx.observability.schema import (
        SCHEMA_VERSION as SCHEMA_VERSION,
    )
    from sovyx.observability.schema import (
        LogEntry as LogEntry,
    )
    from sovyx.observability.schema import (
        validate_entry as validate_entry,
    )
    from sovyx.observability.slo import (
        SLODefinition as SLODefinition,
    )
    from sovyx.observability.slo import (
        SLOMonitor as SLOMonitor,
    )
    from sovyx.observability.slo import (
        SLOReport as SLOReport,
    )
    from sovyx.observability.slo import (
        SLOStatus as SLOStatus,
    )
    from sovyx.observability.slo import (
        SLOTracker as SLOTracker,
    )
    from sovyx.observability.slo import (
        create_default_monitor as create_default_monitor,
    )
    from sovyx.observability.tracing import (
        SovyxTracer as SovyxTracer,
    )
    from sovyx.observability.tracing import (
        get_tracer as get_tracer,
    )
    from sovyx.observability.tracing import (
        setup_tracing as setup_tracing,
    )
    from sovyx.observability.tracing import (
        teardown_tracing as teardown_tracing,
    )

_SUBMODULE_MAP: dict[str, tuple[str, ...]] = {
    "sovyx.observability.alerts": (
        "Alert",
        "AlertFired",
        "AlertManager",
        "AlertRule",
        "AlertSeverity",
        "create_default_alert_manager",
    ),
    "sovyx.observability.async_handler": ("AsyncQueueHandler", "BackgroundLogWriter"),
    "sovyx.observability.health": (
        "CheckResult",
        "CheckStatus",
        "HealthCheck",
        "HealthRegistry",
        "create_default_registry",
        "create_offline_registry",
    ),
    "sovyx.observability.logging": (
        "bind_request_context",
        "bound_request_context",
        "clear_request_context",
        "get_logger",
        "get_request_context",
        "runtime_get_level",
        "runtime_set_level",
        "setup_logging",
        "shutdown_logging",
    ),
    "sovyx.observability.metrics": (
        "MetricsRegistry",
        "collect_json",
        "get_metrics",
        "setup_metrics",
        "teardown_metrics",
    ),
    "sovyx.observability._clamp_fields": ("ClampFieldsProcessor",),
    "sovyx.observability._exception_serializer": (
        "ExceptionTreeProcessor",
        "build_cause_chain",
        "serialize_exception",
    ),
    "sovyx.observability.envelope": ("EnvelopeProcessor",),
    "sovyx.observability.pii": ("PIIRedactor",),
    "sovyx.observability.ringbuffer": ("RingBufferHandler", "install_crash_hooks"),
    "sovyx.observability.sampling": ("SamplingProcessor",),
    "sovyx.observability.schema": (
        "ENVELOPE_FIELDS",
        "KNOWN_EVENTS",
        "SCHEMA_VERSION",
        "LogEntry",
        "validate_entry",
    ),
    "sovyx.observability.slo": (
        "SLODefinition",
        "SLOMonitor",
        "SLOReport",
        "SLOStatus",
        "SLOTracker",
        "create_default_monitor",
    ),
    "sovyx.observability.tracing": (
        "SovyxTracer",
        "get_tracer",
        "setup_tracing",
        "teardown_tracing",
    ),
}

_NAME_TO_MODULE: dict[str, str] = {}
for _mod, _names in _SUBMODULE_MAP.items():
    for _n in _names:
        _NAME_TO_MODULE[_n] = _mod


def __getattr__(name: str) -> object:
    if name in _NAME_TO_MODULE:
        mod = importlib.import_module(_NAME_TO_MODULE[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ENVELOPE_FIELDS",
    "KNOWN_EVENTS",
    "SCHEMA_VERSION",
    "Alert",
    "AlertFired",
    "AlertManager",
    "AlertRule",
    "AlertSeverity",
    "AsyncQueueHandler",
    "BackgroundLogWriter",
    "CheckResult",
    "CheckStatus",
    "ClampFieldsProcessor",
    "EnvelopeProcessor",
    "ExceptionTreeProcessor",
    "HealthCheck",
    "HealthRegistry",
    "LogEntry",
    "MetricsRegistry",
    "PIIRedactor",
    "RingBufferHandler",
    "SLODefinition",
    "SamplingProcessor",
    "SLOMonitor",
    "SLOReport",
    "SLOStatus",
    "SLOTracker",
    "SovyxTracer",
    "bind_request_context",
    "bound_request_context",
    "build_cause_chain",
    "clear_request_context",
    "collect_json",
    "create_default_alert_manager",
    "create_default_monitor",
    "create_default_registry",
    "create_offline_registry",
    "get_logger",
    "get_metrics",
    "get_request_context",
    "get_tracer",
    "install_crash_hooks",
    "runtime_get_level",
    "runtime_set_level",
    "serialize_exception",
    "setup_logging",
    "setup_metrics",
    "setup_tracing",
    "shutdown_logging",
    "teardown_metrics",
    "teardown_tracing",
    "validate_entry",
]
