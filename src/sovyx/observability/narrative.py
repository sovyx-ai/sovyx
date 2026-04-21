"""Sovyx saga-narrative builder — pt-BR / en-US storyline from logs.

Reads JSON-line log files, filters entries belonging to a saga, and
renders them into a chronological storyline a human operator can read
top-to-bottom to understand "what happened during this conversation /
voice turn / cognitive cycle".

Public entry point: :func:`build_user_journey`.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Literal

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = get_logger(__name__)


Locale = Literal["pt-BR", "en-US"]
"""Supported narrative locales. Default ``pt-BR`` matches the dashboard UI."""

_TemplateFn = "Callable[[dict[str, Any]], str]"


# ── Field accessors with defensive fallbacks ────────────────────────


def _f(entry: dict[str, Any], key: str, default: object = "?") -> Any:  # noqa: ANN401
    """Return entry[key] or default — narrative must never KeyError."""
    val = entry.get(key, default)
    if val is None:
        return default
    return val


def _short_saga(saga_id: str) -> str:
    """Truncate saga UUID to 8 chars for compact headers."""
    return saga_id[:8] if len(saga_id) > 8 else saga_id  # noqa: PLR2004


# ── pt-BR templates (default) ───────────────────────────────────────


def _ptbr_templates() -> dict[str, _TemplateFn]:
    """Build the pt-BR narrative dispatch table.

    Lazy-evaluated so the template list isn't paid for at import time
    when only en-US callers run.
    """
    return {
        # Voice/Audio (10)
        "voice.wake_word.detected": lambda e: (
            f"Wake-word '{_f(e, 'voice.phrase', _f(e, 'phrase', '?'))}' "
            f"detectado (score={float(_f(e, 'voice.score', _f(e, 'score', 0.0))):.2f})"
        ),
        "voice.vad.state_changed": lambda e: (
            f"VAD: {_f(e, 'voice.from_state', _f(e, 'from_state'))} → "
            f"{_f(e, 'voice.to_state', _f(e, 'to_state'))} "
            f"(prob={float(_f(e, 'voice.prob', _f(e, 'prob', 0.0))):.2f})"
        ),
        "voice.stt.requested": lambda e: (
            f"STT iniciado ({_f(e, 'voice.audio_ms', _f(e, 'audio_ms', '?'))}ms de áudio capturado)"
        ),
        "voice.stt.response": lambda e: (
            f'Transcrição: "{_f(e, "voice.text", _f(e, "text", ""))}" '
            f"({_f(e, 'voice.latency_ms', _f(e, 'latency_ms', '?'))}ms, "
            f"conf={float(_f(e, 'voice.confidence', _f(e, 'confidence', 0.0))):.2f})"
        ),
        "voice.tts.requested": lambda e: (
            f"TTS solicitado ({_f(e, 'voice.chars', _f(e, 'chars', '?'))} chars, "
            f"voice={_f(e, 'voice.voice', _f(e, 'voice', '?'))})"
        ),
        "voice.tts.chunk_emitted": lambda e: (
            f"TTS chunk {_f(e, 'voice.chunk_n', _f(e, 'chunk_n', '?'))} "
            f"({_f(e, 'voice.audio_ms', _f(e, 'audio_ms', '?'))}ms áudio)"
        ),
        "voice.tts.completed": lambda e: (
            f"TTS concluído ({_f(e, 'voice.total_ms', _f(e, 'total_ms', '?'))}ms, "
            f"{_f(e, 'voice.chunks', _f(e, 'chunks', '?'))} chunks)"
        ),
        "voice.barge_in.triggered": lambda _e: "Barge-in detectado — TTS interrompido",
        "voice.heartbeat.deaf": lambda e: (
            f"⚠ Heartbeat surdo (count={_f(e, 'voice.deaf_count', _f(e, 'count', '?'))})"
            " — investigando APO"
        ),
        "voice.apo.bypass_triggered": lambda _e: (
            "Auto-fix: WASAPI exclusive ativado para bypass APO"
        ),
        # Cognitive Loop (6)
        "cognitive.perceive.completed": lambda e: (
            f"Percepção completa ({_f(e, 'cognitive.inputs', _f(e, 'inputs', '?'))} inputs)"
        ),
        "cognitive.attend.completed": lambda e: (
            f"Atenção: {_f(e, 'cognitive.focus_topic', _f(e, 'focus_topic', '?'))} "
            f"(relevance={float(_f(e, 'cognitive.relevance', _f(e, 'relevance', 0.0))):.2f})"
        ),
        "cognitive.think.completed": lambda e: (
            f"Pensamento concluído "
            f"({_f(e, 'cognitive.tokens', _f(e, 'tokens', '?'))} tokens deliberation)"
        ),
        "cognitive.act.completed": lambda e: (
            f"Ação executada: {_f(e, 'cognitive.action_type', _f(e, 'action_type', '?'))}"
        ),
        "cognitive.reflect.completed": lambda e: (
            "Reflexão: "
            f"{_f(e, 'cognitive.episodes_encoded', _f(e, 'episodes_encoded', '?'))}"
            " episódios codificados"
        ),
        "cognitive.phase.completed": lambda e: (
            f"Fase {_f(e, 'cognitive.phase', _f(e, 'phase', '?'))} terminou em "
            f"{_f(e, 'cognitive.latency_ms', _f(e, 'latency_ms', '?'))}ms"
        ),
        # LLM Router (5)
        "llm.route.attempt": lambda e: (
            f"Tentando provider {_f(e, 'llm.provider', '?')}/{_f(e, 'llm.model', '?')}"
        ),
        "llm.route.succeeded": lambda e: (
            f"✓ {_f(e, 'llm.provider', '?')}/{_f(e, 'llm.model', '?')} respondeu em "
            f"{_f(e, 'llm.latency_ms', '?')}ms "
            f"({_f(e, 'llm.tokens_in', '?')}→{_f(e, 'llm.tokens_out', '?')})"
        ),
        "llm.route.fallback": lambda e: (
            f"✗ {_f(e, 'llm.from_provider', '?')} falhou: "
            f"{_f(e, 'llm.reason', _f(e, 'llm.error', '?'))} "
            f"(próximo fallback: {_f(e, 'llm.to_provider', '?')})"
        ),
        "llm.tool_call.executed": lambda e: (
            f"Tool call: {_f(e, 'llm.tool_name', '?')}("
            f"{_f(e, 'llm.args_summary', '')}) → {_f(e, 'llm.result_summary', '?')}"
        ),
        "llm.budget.warning": lambda e: (
            f"⚠ Budget {_f(e, 'llm.budget_type', '?')} em "
            f"{_f(e, 'llm.consumed_pct', '?')}% "
            f"({_f(e, 'llm.consumed', '?')}/{_f(e, 'llm.limit', '?')})"
        ),
        # Brain (4)
        "brain.episode.encoded": lambda e: (
            f"Episódio codificado (concept={_f(e, 'brain.top_concept', '?')}, "
            f"novelty={float(_f(e, 'brain.novelty', _f(e, 'brain.score', 0.0))):.2f})"
        ),
        "brain.search.completed": lambda e: (
            f"Recall: {_f(e, 'brain.result_count', '?')} memórias relevantes "
            f"({_f(e, 'brain.latency_ms', '?')}ms)"
        ),
        "brain.consolidation.tick": lambda e: (
            f"Consolidação: {_f(e, 'brain.promoted', '?')} concepts mesclados, "
            f"{_f(e, 'brain.pruned', '?')} pruned"
        ),
        "brain.concept.created": lambda e: (
            f"Novo conceito: '{_f(e, 'brain.name', _f(e, 'name', '?'))}' "
            f"(parent={_f(e, 'brain.parent_name', _f(e, 'parent', '?'))})"
        ),
        # Plugin (3)
        "plugin.execute.started": lambda e: (
            f"Plugin {_f(e, 'plugin.id', '?')}.{_f(e, 'plugin.tool', '?')} iniciado"
        ),
        "plugin.execute.completed": lambda e: (
            f"Plugin {_f(e, 'plugin.id', '?')}.{_f(e, 'plugin.tool', '?')} concluído em "
            f"{_f(e, 'plugin.latency_ms', '?')}ms"
        ),
        "plugin.execute.failed": lambda e: (
            f"✗ Plugin {_f(e, 'plugin.id', '?')}.{_f(e, 'plugin.tool', '?')} "
            f"falhou: {_f(e, 'plugin.error', _f(e, 'error', '?'))}"
        ),
        # Saga lifecycle (3)
        "saga.started": lambda e: (
            f"═══ Saga {_short_saga(str(_f(e, 'saga_id', '?')))} iniciada "
            f"({_f(e, 'saga.trigger', _f(e, 'trigger', '?'))}) ═══"
        ),
        "saga.completed": lambda e: (
            f"═══ Saga concluída em {_f(e, 'saga.duration_ms', '?')}ms "
            f"({_f(e, 'saga.phases_count', '?')} fases) ═══"
        ),
        "saga.failed": lambda e: (
            f"═══ ✗ Saga falhou em {_f(e, 'saga.phase_name', '?')}: "
            f"{_f(e, 'saga.reason', _f(e, 'reason', '?'))} ═══"
        ),
    }


# ── en-US templates ─────────────────────────────────────────────────


def _enus_templates() -> dict[str, _TemplateFn]:
    """English mirror of the pt-BR table — same keys, equivalent prose."""
    return {
        # Voice/Audio
        "voice.wake_word.detected": lambda e: (
            f"Wake-word '{_f(e, 'voice.phrase', _f(e, 'phrase', '?'))}' "
            f"detected (score={float(_f(e, 'voice.score', _f(e, 'score', 0.0))):.2f})"
        ),
        "voice.vad.state_changed": lambda e: (
            f"VAD: {_f(e, 'voice.from_state', _f(e, 'from_state'))} → "
            f"{_f(e, 'voice.to_state', _f(e, 'to_state'))} "
            f"(prob={float(_f(e, 'voice.prob', _f(e, 'prob', 0.0))):.2f})"
        ),
        "voice.stt.requested": lambda e: (
            f"STT started ({_f(e, 'voice.audio_ms', _f(e, 'audio_ms', '?'))}ms of audio captured)"
        ),
        "voice.stt.response": lambda e: (
            f'Transcript: "{_f(e, "voice.text", _f(e, "text", ""))}" '
            f"({_f(e, 'voice.latency_ms', _f(e, 'latency_ms', '?'))}ms, "
            f"conf={float(_f(e, 'voice.confidence', _f(e, 'confidence', 0.0))):.2f})"
        ),
        "voice.tts.requested": lambda e: (
            f"TTS requested ({_f(e, 'voice.chars', _f(e, 'chars', '?'))} chars, "
            f"voice={_f(e, 'voice.voice', _f(e, 'voice', '?'))})"
        ),
        "voice.tts.chunk_emitted": lambda e: (
            f"TTS chunk {_f(e, 'voice.chunk_n', _f(e, 'chunk_n', '?'))} "
            f"({_f(e, 'voice.audio_ms', _f(e, 'audio_ms', '?'))}ms audio)"
        ),
        "voice.tts.completed": lambda e: (
            f"TTS done ({_f(e, 'voice.total_ms', _f(e, 'total_ms', '?'))}ms, "
            f"{_f(e, 'voice.chunks', _f(e, 'chunks', '?'))} chunks)"
        ),
        "voice.barge_in.triggered": lambda _e: "Barge-in detected — TTS interrupted",
        "voice.heartbeat.deaf": lambda e: (
            f"⚠ Deaf heartbeat (count={_f(e, 'voice.deaf_count', _f(e, 'count', '?'))})"
            " — investigating APO"
        ),
        "voice.apo.bypass_triggered": lambda _e: (
            "Auto-fix: WASAPI exclusive engaged for APO bypass"
        ),
        # Cognitive Loop
        "cognitive.perceive.completed": lambda e: (
            f"Perceive complete ({_f(e, 'cognitive.inputs', _f(e, 'inputs', '?'))} inputs)"
        ),
        "cognitive.attend.completed": lambda e: (
            f"Attend: {_f(e, 'cognitive.focus_topic', _f(e, 'focus_topic', '?'))} "
            f"(relevance={float(_f(e, 'cognitive.relevance', _f(e, 'relevance', 0.0))):.2f})"
        ),
        "cognitive.think.completed": lambda e: (
            "Think complete "
            f"({_f(e, 'cognitive.tokens', _f(e, 'tokens', '?'))} tokens deliberation)"
        ),
        "cognitive.act.completed": lambda e: (
            f"Act executed: {_f(e, 'cognitive.action_type', _f(e, 'action_type', '?'))}"
        ),
        "cognitive.reflect.completed": lambda e: (
            "Reflect: "
            f"{_f(e, 'cognitive.episodes_encoded', _f(e, 'episodes_encoded', '?'))} "
            "episodes encoded"
        ),
        "cognitive.phase.completed": lambda e: (
            f"Phase {_f(e, 'cognitive.phase', _f(e, 'phase', '?'))} ended in "
            f"{_f(e, 'cognitive.latency_ms', _f(e, 'latency_ms', '?'))}ms"
        ),
        # LLM Router
        "llm.route.attempt": lambda e: (
            f"Trying provider {_f(e, 'llm.provider', '?')}/{_f(e, 'llm.model', '?')}"
        ),
        "llm.route.succeeded": lambda e: (
            f"✓ {_f(e, 'llm.provider', '?')}/{_f(e, 'llm.model', '?')} replied in "
            f"{_f(e, 'llm.latency_ms', '?')}ms "
            f"({_f(e, 'llm.tokens_in', '?')}→{_f(e, 'llm.tokens_out', '?')})"
        ),
        "llm.route.fallback": lambda e: (
            f"✗ {_f(e, 'llm.from_provider', '?')} failed: "
            f"{_f(e, 'llm.reason', _f(e, 'llm.error', '?'))} "
            f"(next fallback: {_f(e, 'llm.to_provider', '?')})"
        ),
        "llm.tool_call.executed": lambda e: (
            f"Tool call: {_f(e, 'llm.tool_name', '?')}("
            f"{_f(e, 'llm.args_summary', '')}) → {_f(e, 'llm.result_summary', '?')}"
        ),
        "llm.budget.warning": lambda e: (
            f"⚠ Budget {_f(e, 'llm.budget_type', '?')} at "
            f"{_f(e, 'llm.consumed_pct', '?')}% "
            f"({_f(e, 'llm.consumed', '?')}/{_f(e, 'llm.limit', '?')})"
        ),
        # Brain
        "brain.episode.encoded": lambda e: (
            f"Episode encoded (concept={_f(e, 'brain.top_concept', '?')}, "
            f"novelty={float(_f(e, 'brain.novelty', _f(e, 'brain.score', 0.0))):.2f})"
        ),
        "brain.search.completed": lambda e: (
            f"Recall: {_f(e, 'brain.result_count', '?')} relevant memories "
            f"({_f(e, 'brain.latency_ms', '?')}ms)"
        ),
        "brain.consolidation.tick": lambda e: (
            f"Consolidation: {_f(e, 'brain.promoted', '?')} concepts merged, "
            f"{_f(e, 'brain.pruned', '?')} pruned"
        ),
        "brain.concept.created": lambda e: (
            f"New concept: '{_f(e, 'brain.name', _f(e, 'name', '?'))}' "
            f"(parent={_f(e, 'brain.parent_name', _f(e, 'parent', '?'))})"
        ),
        # Plugin
        "plugin.execute.started": lambda e: (
            f"Plugin {_f(e, 'plugin.id', '?')}.{_f(e, 'plugin.tool', '?')} started"
        ),
        "plugin.execute.completed": lambda e: (
            f"Plugin {_f(e, 'plugin.id', '?')}.{_f(e, 'plugin.tool', '?')} done in "
            f"{_f(e, 'plugin.latency_ms', '?')}ms"
        ),
        "plugin.execute.failed": lambda e: (
            f"✗ Plugin {_f(e, 'plugin.id', '?')}.{_f(e, 'plugin.tool', '?')} "
            f"failed: {_f(e, 'plugin.error', _f(e, 'error', '?'))}"
        ),
        # Saga lifecycle
        "saga.started": lambda e: (
            f"═══ Saga {_short_saga(str(_f(e, 'saga_id', '?')))} started "
            f"({_f(e, 'saga.trigger', _f(e, 'trigger', '?'))}) ═══"
        ),
        "saga.completed": lambda e: (
            f"═══ Saga done in {_f(e, 'saga.duration_ms', '?')}ms "
            f"({_f(e, 'saga.phases_count', '?')} phases) ═══"
        ),
        "saga.failed": lambda e: (
            f"═══ ✗ Saga failed at {_f(e, 'saga.phase_name', '?')}: "
            f"{_f(e, 'saga.reason', _f(e, 'reason', '?'))} ═══"
        ),
    }


# Cache the dispatch tables — both are immutable after first build,
# so subsequent build_user_journey() calls share them.
_PTBR_CACHE: dict[str, _TemplateFn] | None = None
_ENUS_CACHE: dict[str, _TemplateFn] | None = None


def _templates_for(locale: Locale) -> dict[str, _TemplateFn]:
    """Return (and cache) the dispatch table for *locale*."""
    global _PTBR_CACHE, _ENUS_CACHE  # noqa: PLW0603
    if locale == "pt-BR":
        if _PTBR_CACHE is None:
            _PTBR_CACHE = _ptbr_templates()
        return _PTBR_CACHE
    if _ENUS_CACHE is None:
        _ENUS_CACHE = _enus_templates()
    return _ENUS_CACHE


# ── Public entry point ──────────────────────────────────────────────


# Match a saga_id field anywhere in a JSON line. Cheap pre-filter so
# `json.loads` runs only against candidate lines — large log files
# (>100MB) common in long-running daemons would otherwise dominate
# CPU time during narrative builds.
_SAGA_PREFILTER_TEMPLATE = '"saga_id":"{}"'


def build_user_journey(
    saga_id: str,
    log_path: Path,
    *,
    locale: Locale = "pt-BR",
) -> str:
    """Render a chronological narrative of *saga_id* from JSON-line logs.

    Args:
        saga_id: The saga UUID to filter on (matches the ``saga_id`` field).
        log_path: Path to the structlog JSON log file (one entry per line).
        locale: Output language. Defaults to pt-BR.

    Returns:
        Multi-line string. One narrative line per log entry, in
        timestamp order. Events without a registered template fall back
        to ``[evento <name> @ <ts>]`` so gaps surface in the output
        instead of being silently swallowed.

    Empty saga (no matching entries) returns the literal sentinel
    ``"(no entries for saga <short_id>)"``.
    """
    templates = _templates_for(locale)
    entries = list(_iter_saga_entries(saga_id, log_path))
    if not entries:
        return f"(no entries for saga {_short_saga(saga_id)})"

    entries.sort(key=lambda e: str(e.get("timestamp", "")))
    lines: list[str] = []
    fallback_label = "[evento" if locale == "pt-BR" else "[event"
    for entry in entries:
        event_name = entry.get("event")
        if not isinstance(event_name, str):
            continue
        renderer = templates.get(event_name)
        if renderer is None:
            ts = entry.get("timestamp", "?")
            lines.append(f"{fallback_label} {event_name} @ {ts}]")
            continue
        try:
            lines.append(renderer(entry))
        except (KeyError, TypeError, ValueError) as exc:
            # A renderer crash must never break the whole story — log
            # the failure for the maintainer and emit a fallback line.
            logger.debug(
                "narrative.render_failed",
                event=event_name,
                error=str(exc),
            )
            lines.append(f"{fallback_label} {event_name} (render error)]")
    return "\n".join(lines)


def _iter_saga_entries(saga_id: str, log_path: Path) -> "list[dict[str, Any]]":
    """Stream-parse *log_path* and yield entries with matching saga_id.

    Handles the common modes the structlog file handler produces:
    one JSON object per line. Malformed lines (rotation tear, partial
    flush) are silently skipped; the whole-file parse must never abort
    on a single bad row.
    """
    if not log_path.exists():
        return []
    needle = _SAGA_PREFILTER_TEMPLATE.format(_escape_for_substring(saga_id))
    out: list[dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or needle not in line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                if parsed.get("saga_id") == saga_id:
                    out.append(parsed)
    except OSError as exc:
        logger.warning("narrative.log_read_failed", path=str(log_path), error=str(exc))
        return []
    return out


_SUBSTRING_ESCAPE_RE = re.compile(r'([\\"])')


def _escape_for_substring(saga_id: str) -> str:
    """Escape backslashes and quotes so the prefilter substring matches."""
    return _SUBSTRING_ESCAPE_RE.sub(r"\\\1", saga_id)


__all__ = [
    "Locale",
    "build_user_journey",
]
