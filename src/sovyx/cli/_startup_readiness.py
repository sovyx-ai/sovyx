"""Mission OX-1.A — Startup Readiness Console.

Prints a 6-line readiness summary at the end of ``sovyx start`` when
``EngineConfig.ox1.startup_readiness_enabled`` is True. Reads existing
primitives only; never writes; never raises.

Surfaces five facts that today live in structured logs only and are
invisible to an operator who runs the daemon in a terminal:

1. Engine session id (``SERVICE_INSTANCE_ID``, per-boot UUID — already
   on every log envelope under the OTel-canonical
   ``service.instance.id`` key, surfaced here as the operator-friendly
   ``engine_session_id``).
2. LLM provider discovery verdict (``DiscoveryVerdict`` —
   ``FULLY_AVAILABLE`` / ``NO_PROVIDER_CONFIGURED`` /
   ``OLLAMA_UNREACHABLE`` / …).
3. Voice device + ``voice_enabled`` flag.
4. Brain embedding model readiness.
5. Degraded axis count + active operator-ack count (from
   :class:`EngineDegradedStore` + :class:`OperatorAcksStore`).

Closes adversarial-operator scenarios 7 + 8 from
``docs-internal/MISSION-OX1-OPERATOR-EXPERIENCE-RESEARCH-2026-05-23.md``
§4.5 (LLM verdict invisible at boot; voice auto-resume silent failure).

Pure additive: gated behind ``SOVYX_OX1__STARTUP_READINESS_ENABLED``
(default False per ``feedback_staged_adoption``). Never mutates state.
Never propagates exceptions — readiness print must not crash the
daemon.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.envelope import SERVICE_INSTANCE_ID

if TYPE_CHECKING:
    from rich.console import Console

    from sovyx.engine.registry import ServiceRegistry
    from sovyx.mind.config import MindConfig


_SESSION_ID_DISPLAY_LEN: int = 12
"""How many leading hex chars of the boot UUID to render. Matches the
``request_id`` truncation used in
:func:`sovyx.observability.logging.bind_request_context` so operators
can cross-reference the same prefix in logs."""


_VERDICT_STYLE: dict[str, str] = {
    "fully_available": "green",
    "partial_health": "yellow",
    "default_model_unavailable": "yellow",
    "all_providers_unhealthy": "red",
    "cloud_key_invalid": "red",
    "ollama_no_models": "yellow",
    "ollama_unreachable": "red",
    "no_provider_configured": "red",
}


async def print_startup_readiness(
    console: Console,
    registry: ServiceRegistry,
    mind_config: MindConfig,
) -> None:
    """Print the 6-line readiness summary.

    All five primitive reads are independently guarded — a missing
    subsystem (e.g. brain not registered in a CLI-only smoke test)
    degrades to ``unknown`` rather than raising.

    Args:
        console: Rich console used by ``sovyx start``.
        registry: Service registry resolved during bootstrap.
        mind_config: The :class:`MindConfig` already loaded by the
            start command — passed in to avoid a second YAML read.
    """
    session_id = SERVICE_INSTANCE_ID[:_SESSION_ID_DISPLAY_LEN]
    llm_line = await _llm_line(registry)
    voice_line = _voice_line(mind_config)
    embedding_line = await _embedding_line(registry)
    degraded_line = await _degraded_line(registry)

    console.print(f"\n[bold]Startup readiness[/bold]  [dim]session={session_id}[/dim]")
    console.print(f"  LLM:       {llm_line}")
    console.print(f"  Voice:     {voice_line}")
    console.print(f"  Embedding: {embedding_line}")
    console.print(f"  Degraded:  {degraded_line}")
    console.print(
        "[dim]  Hint: `sovyx doctor` for detail · "
        "see `MISSION-OX1-...md` §8 for the readiness contract.[/dim]\n"
    )


async def _llm_line(registry: ServiceRegistry) -> str:
    """Render the LLM-provider verdict line.

    Reads from :class:`LLMRouter.discovery_report`. Returns
    ``[yellow]unknown[/yellow]`` when the router is not registered or
    has not yet received a discovery report (pre-first-tick).
    """
    try:
        from sovyx.llm.router import LLMRouter  # noqa: PLC0415

        router = await registry.resolve(LLMRouter)
    except Exception:  # noqa: BLE001 — readiness print must never raise
        return "[yellow]unknown[/yellow]  [dim](router not registered)[/dim]"

    report = router.discovery_report
    if report is None:
        return "[yellow]unknown[/yellow]  [dim](no discovery report yet)[/dim]"

    verdict = report.verdict.value
    style = _VERDICT_STYLE.get(verdict, "yellow")
    return (
        f"[{style}]{verdict}[/{style}]  "
        f"[dim]({report.available_count}/{report.configured_count} providers, "
        f"default={report.default_provider or '—'})[/dim]"
    )


def _voice_line(mind_config: MindConfig) -> str:
    """Render the voice-subsystem line from the loaded :class:`MindConfig`.

    Reads ``voice_enabled`` + ``voice.input_device_name`` (resolved by
    ``sovyx voice setup`` if the operator ran the wizard). Best-effort:
    missing fields render as ``—``.
    """
    if not getattr(mind_config, "voice_enabled", False):
        return "[dim]disabled[/dim]"

    voice = getattr(mind_config, "voice", None)
    device = getattr(voice, "input_device_name", None) if voice is not None else None
    if not device:
        device = "—"
    return f"[green]enabled[/green]  [dim](device={device})[/dim]"


async def _embedding_line(registry: ServiceRegistry) -> str:
    """Render the brain-embedding readiness line.

    Reads :attr:`BrainService.embedding_model_ready`. Cognitive-loop
    operates in degraded mode when this is False (anti-pattern #44).
    """
    try:
        from sovyx.brain.service import BrainService  # noqa: PLC0415

        brain = await registry.resolve(BrainService)
    except Exception:  # noqa: BLE001 — readiness print must never raise
        return "[yellow]unknown[/yellow]  [dim](brain not registered)[/dim]"

    ready = bool(getattr(brain, "embedding_model_ready", False))
    if ready:
        return "[green]ready[/green]"
    return "[yellow]not ready[/yellow]  [dim](FTS5 fallback active)[/dim]"


async def _degraded_line(registry: ServiceRegistry) -> str:
    """Render the degraded-axes + active-acks line.

    Reads :meth:`EngineDegradedStore.snapshot` (count of distinct axes)
    + :meth:`OperatorAcksStore.list_active_acks` (count of operator
    acks not yet expired). Both are bounded primitives (≤32 entries
    each); the lookup is cheap.
    """
    axis_count = 0
    ack_count = 0

    try:
        from sovyx.engine._degraded_store import EngineDegradedStore  # noqa: PLC0415

        store = await registry.resolve(EngineDegradedStore)
        axis_count = len({e.axis for e in store.snapshot()})
    except Exception:  # noqa: BLE001 — readiness print must never raise
        pass

    try:
        from sovyx.engine._operator_acks_store import OperatorAcksStore  # noqa: PLC0415

        acks = await registry.resolve(OperatorAcksStore)
        ack_count = len(await acks.list_active_acks())
    except Exception:  # noqa: BLE001 — readiness print must never raise
        pass

    if axis_count == 0:
        axis_part = "[green]0 axes[/green]"
    elif axis_count <= 2:
        axis_part = f"[yellow]{axis_count} axes[/yellow]"
    else:
        axis_part = f"[red]{axis_count} axes[/red]"

    ack_part = (
        "[dim](no active acks)[/dim]"
        if ack_count == 0
        else f"[dim]({ack_count} active ack{'s' if ack_count != 1 else ''})[/dim]"
    )
    return f"{axis_part}  {ack_part}"


__all__ = ["print_startup_readiness"]
