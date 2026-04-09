"""Sovyx Observability — logging, metrics, tracing, health, SLOs, alerts.

Lazy imports to break circular dependency:
  engine.events → observability.logging → __init__ → alerts → engine.events
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
        setup_logging as setup_logging,
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
        "setup_logging",
    ),
    "sovyx.observability.metrics": (
        "MetricsRegistry",
        "collect_json",
        "get_metrics",
        "setup_metrics",
        "teardown_metrics",
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
    "Alert",
    "AlertFired",
    "AlertManager",
    "AlertRule",
    "AlertSeverity",
    "CheckResult",
    "CheckStatus",
    "HealthCheck",
    "HealthRegistry",
    "MetricsRegistry",
    "SLODefinition",
    "SLOMonitor",
    "SLOReport",
    "SLOStatus",
    "SLOTracker",
    "SovyxTracer",
    "create_default_alert_manager",
    "create_default_monitor",
    "bind_request_context",
    "bound_request_context",
    "clear_request_context",
    "collect_json",
    "create_default_registry",
    "create_offline_registry",
    "get_logger",
    "get_metrics",
    "get_request_context",
    "get_tracer",
    "setup_logging",
    "setup_metrics",
    "setup_tracing",
    "teardown_metrics",
    "teardown_tracing",
]
