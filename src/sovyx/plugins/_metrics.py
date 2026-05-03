"""Plugin observability helpers — T05 of pre-wake-word-hardening mission (2026-05-02).

Before this module, plugin observability was log-event-only — zero
structured metrics. The voice → cognitive loop → tool-call pathway
made plugin behaviour invisible in dashboards. T05 added 4 instruments
on the global ``MetricsRegistry``; this module wraps them in
``record_*`` helpers per the existing pattern in
``voice/health/_metrics.py`` so call sites stay tidy.

Usage from inside the plugin subsystem::

    from sovyx.plugins._metrics import (
        record_tool_executed,
        record_tool_latency,
        record_sandbox_denial,
        record_auto_disabled,
    )

    record_tool_executed(plugin="weather", tool="get_weather", outcome="ok")
    record_tool_latency(plugin="weather", tool="get_weather", duration_ms=42.5)
    record_sandbox_denial(plugin="evil", layer="http")
    record_auto_disabled(plugin="evil", reason="permission_denials_exceeded")

Cardinality discipline: every ``plugin`` / ``tool`` value is operator-
controlled but bounded (7 official + operator-installed; ~6 tools per
plugin on average per audit R07). ``outcome`` / ``layer`` / ``reason``
are closed-set StrEnum-like values pinned by this module so future
contributors don't accidentally introduce unbounded label values.
"""

from __future__ import annotations

from typing import Literal

from sovyx.observability.metrics import get_metrics

# Closed-set label values — explicit literal types so type-checker
# rejects accidental free-form strings at the call site.
ToolOutcome = Literal["ok", "error"]
SandboxLayer = Literal["ast", "import", "http", "fs", "permission"]
AutoDisableReason = Literal[
    "consecutive_failures",
    "permission_denials_exceeded",
    "other",
]


def record_tool_executed(
    *,
    plugin: str,
    tool: str,
    outcome: ToolOutcome,
) -> None:
    """Record a plugin tool execution outcome.

    Increments ``sovyx.plugins.tool_executed`` Counter with attributes
    ``plugin``, ``tool``, ``outcome``. Companion to
    :func:`record_tool_latency` — call BOTH on every execution to
    populate the count + latency time-series.
    """
    instrument = getattr(get_metrics(), "plugins_tool_executed", None)
    if instrument is None:
        return
    instrument.add(
        1,
        attributes={"plugin": plugin, "tool": tool, "outcome": outcome},
    )


def record_tool_latency(
    *,
    plugin: str,
    tool: str,
    duration_ms: float,
) -> None:
    """Record per-tool execution latency in milliseconds.

    Records a ``sovyx.plugins.tool_latency_ms`` Histogram observation
    with attributes ``plugin``, ``tool``. Recorded for every
    successful AND failed execution — the outcome distinction is
    on the :func:`record_tool_executed` counter side.
    """
    instrument = getattr(get_metrics(), "plugins_tool_latency_ms", None)
    if instrument is None:
        return
    instrument.record(
        duration_ms,
        attributes={"plugin": plugin, "tool": tool},
    )


def record_sandbox_denial(
    *,
    plugin: str,
    layer: SandboxLayer,
) -> None:
    """Record a sandbox denial — per-layer security visibility.

    Increments ``sovyx.plugins.sandbox_denial`` Counter with attributes
    ``plugin``, ``layer``. Emitted by each of the 5 sandbox layers when
    a plugin attempts an action it isn't permitted to take.
    """
    instrument = getattr(get_metrics(), "plugins_sandbox_denial", None)
    if instrument is None:
        return
    instrument.add(1, attributes={"plugin": plugin, "layer": layer})


def record_auto_disabled(
    *,
    plugin: str,
    reason: AutoDisableReason,
) -> None:
    """Record a plugin auto-disable event.

    Increments ``sovyx.plugins.auto_disabled`` Counter with attributes
    ``plugin``, ``reason``. The 2 in-codebase reasons:

    * ``consecutive_failures`` — manager-side health threshold tripped
      (``manager.py:578-586``)
    * ``permission_denials_exceeded`` — enforcer-side
      ``max_denials`` (default 10) tripped
      (``permissions.py:243-250``)
    """
    instrument = getattr(get_metrics(), "plugins_auto_disabled", None)
    if instrument is None:
        return
    instrument.add(1, attributes={"plugin": plugin, "reason": reason})
