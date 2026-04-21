"""Sovyx failure-signature dictionary + structlog ErrorEnricher.

A catalogue of known-bad log entry shapes (exception type × event-name
prefix × optional message regex) mapped to a one-line operator
diagnosis hint, severity, and optional runbook URL. The
:class:`ErrorEnricher` structlog processor walks the catalogue for
every WARNING+ entry and, on first match, decorates the entry with
``diagnosis_hint``, ``diagnosis_severity``, and ``diagnosis_runbook_url``
fields so the dashboard's NarrativePanel and the operator's tail can
both surface the same actionable advice.

Coverage target (plan §8.4): ≥80% of historical ERROR entries should
match a signature. New incidents should be added to ``_SIGNATURES``
in the same commit as the bug-fix, so the next person hitting the
same failure gets the hint immediately.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import MutableMapping

Severity = Literal["info", "warning", "critical"]
"""Operator-facing severity for the matched diagnosis."""


class FailureSignature:
    """One known-bad pattern + the actionable hint to surface.

    Match semantics: every key in ``match_fields`` must be present
    on the entry. ``str`` values are compared with ``==``; compiled
    regex values are tested with :py:meth:`re.Pattern.search`. The
    ``exc_type`` key matches the structlog-serialized exception class
    name (``event_dict["exc_type"]`` after :class:`ExceptionTreeProcessor`).
    """

    __slots__ = ("hint", "match_fields", "runbook_url", "severity")

    def __init__(
        self,
        match_fields: dict[str, str | re.Pattern[str]],
        hint: str,
        severity: Severity,
        runbook_url: str | None = None,
    ) -> None:
        self.match_fields = match_fields
        self.hint = hint
        self.severity = severity
        self.runbook_url = runbook_url

    def matches(self, entry: "MutableMapping[str, Any]") -> bool:
        """Return ``True`` iff *entry* satisfies every ``match_fields`` clause."""
        for key, expected in self.match_fields.items():
            value = entry.get(key)
            if value is None:
                return False
            if isinstance(expected, re.Pattern):
                if not isinstance(value, str) or not expected.search(value):
                    return False
            elif value != expected:
                return False
        return True


# ── Pre-compiled regex helpers ──────────────────────────────────────


def _re(pattern: str) -> re.Pattern[str]:
    """Compile *pattern* with sensible defaults for diagnosis matching."""
    return re.compile(pattern)


# ── Signature catalogue ─────────────────────────────────────────────
#
# Ordering matters: the first signature that matches wins. Put more
# specific signatures (extra match_fields) BEFORE generic catch-alls
# of the same exception family so the more-actionable hint surfaces.

_SIGNATURES: list[FailureSignature] = [
    # ── Voice / Audio (8) ──
    FailureSignature(
        {"exc_type": "EOFError", "event": _re(r"^voice\.stt\.")},
        "Audio device disconnected mid-stream — check audio.device.removed "
        "events earlier in the saga and the audio.apo.bypassed verdict.",
        "warning",
        "docs/runbooks/voice-stt-eof.md",
    ),
    FailureSignature(
        {"exc_type": "OSError", "event": _re(r"^audio\.stream\.")},
        "WASAPI stream failed — usually the APO chain corrupted the signal; "
        "run `sovyx doctor voice_capture_apo`.",
        "warning",
        "docs/runbooks/audio-stream-failure.md",
    ),
    FailureSignature(
        {"exc_type": "TimeoutError", "event": _re(r"^voice\.tts\.")},
        "TTS model overloaded — check voice.output_queue.depth (>3 = backpressure).",
        "warning",
        None,
    ),
    FailureSignature(
        {
            "exc_type": "RuntimeError",
            "event": _re(r"^voice\.vad\.silero"),
            "message": _re(r"max_speech_prob"),
        },
        "VAD signal destroyed upstream — likely Voice Clarity APO; "
        "see CLAUDE.md anti-pattern #21 (WASAPI exclusive bypass).",
        "critical",
        "docs/runbooks/vad-deaf-apo.md",
    ),
    FailureSignature(
        {"exc_type": "OSError", "event": _re(r"^audio\.kernel_reset")},
        "USB-audio kernel reset detected — wedged driver; the voice/health "
        "coordinator (v0.20.4) handles recovery without rebooting the kernel.",
        "warning",
        None,
    ),
    FailureSignature(
        {
            "exc_type": "FileNotFoundError",
            "event": _re(r"^voice\.(stt|tts)\.model_load"),
        },
        "ONNX model missing — run `sovyx voice models download` or check "
        "data_dir/models/ for the expected filename.",
        "critical",
        None,
    ),
    FailureSignature(
        {"exc_type": _re(r"OnnxRuntime"), "event": _re(r"^voice\.")},
        "ONNX session corrupted — re-download via `sovyx voice models verify`.",
        "critical",
        None,
    ),
    FailureSignature(
        {"event": "voice.heartbeat.deaf"},
        "Wake-word deaf for N heartbeats — auto-fix triggers when "
        "voice_clarity_autofix=True; check voice.apo.bypass_triggered "
        "in the next saga.",
        "warning",
        "docs/runbooks/wake-word-deaf.md",
    ),
    # ── LLM Router (5) ──
    FailureSignature(
        {"exc_type": "ConnectionResetError", "event": _re(r"^llm\.")},
        "Provider connection reset — check llm.circuit.opened "
        "(open=skip provider, half_open=retry carefully).",
        "warning",
        None,
    ),
    FailureSignature(
        {"exc_type": "ReadTimeout", "event": _re(r"^llm\.route\.")},
        "Provider read timeout — usually a hidden rate-limit; check "
        "llm.rate_limit.observed in the 60s before this entry.",
        "warning",
        None,
    ),
    FailureSignature(
        {"exc_type": "JSONDecodeError", "event": _re(r"^llm\.tool_call\.")},
        "Provider returned invalid JSON in a tool-call — model fallback "
        "recommended; check llm.tool_call.malformed.",
        "warning",
        None,
    ),
    FailureSignature(
        {"exc_type": "ValueError", "event": _re(r"^llm\.budget\.")},
        "Token budget exceeded — verify llm.budget.consumed_pct; tune via "
        "EngineConfig.tuning.llm.budget_*.",
        "warning",
        "docs/runbooks/llm-budget.md",
    ),
    FailureSignature(
        {"exc_type": "RuntimeError", "event": "llm.fallback.exhausted"},
        "All providers failed — system in degraded mode; check the last "
        "llm.route.attempt per provider for root cause.",
        "critical",
        "docs/runbooks/llm-degraded.md",
    ),
    # ── Plugin Sandbox (4) ──
    FailureSignature(
        {"exc_type": "PermissionDeniedError", "event": _re(r"^plugin\.fs\.")},
        "Plugin tried FS access outside the sandbox — check plugin.fs.denied "
        "for the attempted path; review plugin manifest.",
        "warning",
        "docs/security.md#sandbox",
    ),
    FailureSignature(
        {
            "exc_type": "ConnectError",
            "event": _re(r"^plugin\.http\."),
        },
        "Plugin tried a blocked domain — check plugin.http.denied; add the "
        "domain to allowed_domains in the manifest if legitimate.",
        "warning",
        "docs/security.md#sandbox-http",
    ),
    FailureSignature(
        {"exc_type": "MemoryError", "event": _re(r"^plugin\.execute\.")},
        "Plugin exceeded sandbox memory — check plugin.lifecycle.rss_mb; "
        "reduce the plugin's batch size.",
        "warning",
        None,
    ),
    FailureSignature(
        {"exc_type": "TimeoutError", "event": _re(r"^plugin\.execute\.")},
        "Plugin timeout (default 30s) — check plugin.lifecycle.duration_ms; "
        "optimize the plugin or raise plugin_timeout_s.",
        "warning",
        None,
    ),
    # ── Brain / Persistence (4) ──
    FailureSignature(
        {"exc_type": "MemoryError", "event": _re(r"^brain\.")},
        "Brain consolidation memory pressure — check brain.consolidation.tick "
        "size; reduce brain_consolidation_batch.",
        "warning",
        None,
    ),
    FailureSignature(
        {
            "exc_type": "OperationalError",
            "event": _re(r"^persistence\."),
            "message": _re(r"database is locked"),
        },
        "SQLite WAL contention — check persistence.pool.wait_ms p99; "
        "investigate long-running transactions.",
        "warning",
        "docs/runbooks/sqlite-wal.md",
    ),
    FailureSignature(
        {"exc_type": "IntegrityError", "event": "brain.episode.encode"},
        "Concept embedding collision — likely a race; check "
        "brain.embedding.cache.hit_rate.",
        "warning",
        None,
    ),
    FailureSignature(
        {
            "exc_type": "OSError",
            "event": _re(r"^persistence\.migration\."),
            "message": _re(r"no space left"),
        },
        "Disk full during migration — abort + rollback; check system.disk.free_pct.",
        "critical",
        "docs/runbooks/disk-full.md",
    ),
    # ── Cognitive / Engine (4) ──
    FailureSignature(
        {
            "exc_type": "RuntimeError",
            "event": _re(r"^cognitive\.phase\."),
            "message": _re(r"budget exhausted"),
        },
        "Phase budget exhausted — check cognitive.budget.consumed; tune "
        "EngineConfig.tuning.cognitive.phase_budget_ms.",
        "warning",
        None,
    ),
    FailureSignature(
        {"exc_type": "CancelledError", "event": _re(r"^cognitive\.loop\.")},
        "Loop cancelled — verify engine.shutdown.requested precedes (expected) "
        "vs spurious cancel (bug).",
        "info",
        None,
    ),
    FailureSignature(
        {
            "exc_type": "RuntimeError",
            "event": _re(r"^engine\.bootstrap\."),
            "message": _re(r"service not registered"),
        },
        "Service registry incomplete — race condition during bootstrap; check "
        "the order of register_* calls.",
        "critical",
        "docs/runbooks/bootstrap-race.md",
    ),
    FailureSignature(
        {
            "exc_type": "KeyError",
            "event": _re(r"^engine\.event_bus\."),
            "message": _re(r"no handler"),
        },
        "Event has no subscriber — likely a typo in the event name; check "
        "KNOWN_EVENTS in observability/schema.py.",
        "warning",
        None,
    ),
    # ── Bridge / Dashboard (2) ──
    FailureSignature(
        {"exc_type": "WebSocketDisconnect", "event": _re(r"^dashboard\.ws\.")},
        "Dashboard WS dropped — expected on mobile or network change; check "
        "dashboard.ws.reconnect.attempted.",
        "info",
        None,
    ),
    FailureSignature(
        {
            "exc_type": "JSONDecodeError",
            "event": _re(r"^bridge\.(telegram|signal)\."),
        },
        "Bridge payload malformed — provider may have changed schema; check "
        "the provider's changelog.",
        "warning",
        None,
    ),
]


class ErrorEnricher:
    """Structlog processor: tag WARNING+ entries with diagnosis hints.

    Wire AFTER :class:`PIIRedactor` (so we don't try to match against
    raw PII) and BEFORE :class:`ClampFieldsProcessor` (so the hint
    isn't truncated). The processor never raises — a malformed entry
    or an unexpected field type just falls through unmatched.
    """

    __slots__ = ("_signatures",)

    # Levels at which we attempt enrichment. WARNING is included so
    # high-signal warnings (rate-limit incoming, circuit half_open) get
    # the same diagnostic context that ERROR/CRITICAL entries do.
    _ENRICH_LEVELS = frozenset({"warning", "error", "critical"})

    def __init__(
        self, signatures: list[FailureSignature] | None = None
    ) -> None:
        self._signatures = signatures if signatures is not None else _SIGNATURES

    def __call__(
        self,
        _logger: Any,  # noqa: ANN401 — opaque structlog logger reference.
        _method_name: str,
        event_dict: "MutableMapping[str, Any]",
    ) -> "MutableMapping[str, Any]":
        """Add ``diagnosis_*`` fields to *event_dict* on first signature match."""
        level = event_dict.get("level")
        if not isinstance(level, str) or level.lower() not in self._ENRICH_LEVELS:
            return event_dict

        # Don't double-enrich — the same entry can pass through the
        # chain multiple times in test code that re-runs setup_logging.
        if "diagnosis_hint" in event_dict:
            return event_dict

        for sig in self._signatures:
            try:
                if sig.matches(event_dict):
                    event_dict["diagnosis_hint"] = sig.hint
                    event_dict["diagnosis_severity"] = sig.severity
                    if sig.runbook_url is not None:
                        event_dict["diagnosis_runbook_url"] = sig.runbook_url
                    return event_dict
            except (re.error, TypeError):
                # A bad regex or unexpected type in event_dict must not
                # break the entire log pipeline; skip the signature.
                continue
        return event_dict


def get_default_signatures() -> list[FailureSignature]:
    """Return the read-only default catalogue (exposed for tests)."""
    return list(_SIGNATURES)


__all__ = [
    "ErrorEnricher",
    "FailureSignature",
    "Severity",
    "get_default_signatures",
]
