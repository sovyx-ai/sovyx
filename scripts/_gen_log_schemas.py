"""Generator for the canonical log-event schema catalog (P11.1).

Output path: ``src/sovyx/observability/log_schema/<event>.json``.

The catalog ships as package data (see ``pyproject.toml``
``[tool.hatch.build.targets.wheel]``) so:

* the CI gate ``scripts/check_log_schemas.py`` can load it via
  ``importlib.resources.files("sovyx.observability.log_schema")``,
* downstream consumers that pip-install Sovyx get the contract
  without having to fetch a separate artifact,
* the schemas live next to the code they describe.

The IMPL plan §16 Task 11.1 originally suggested
``docs-internal/log_schema/`` — that directory is gitignored, so the
schemas would never reach CI. Use this generator as the source of
truth: edit the EVENTS table below and re-run

    uv run python scripts/_gen_log_schemas.py

A diff in the JSON files without a matching diff here is a bypass of
the contract — reject in review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "sovyx"
    / "observability"
    / "log_schema"
)

# JSON-Schema Draft 2020-12 fragment — envelope fields injected by
# EnvelopeProcessor. Every event schema embeds these as ``required``.
ENVELOPE_REQUIRED = [
    "timestamp",
    "level",
    "logger",
    "event",
    "schema_version",
    "process_id",
    "host",
    "sovyx_version",
    "sequence_no",
]

ENVELOPE_PROPERTIES: dict[str, dict[str, Any]] = {
    "timestamp": {"type": "string", "format": "date-time"},
    "level": {"enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]},
    "logger": {"type": "string", "minLength": 1},
    "schema_version": {"const": "1.0.0"},
    "process_id": {"type": "integer", "minimum": 1},
    "pid": {"type": "integer", "minimum": 1},
    "host": {"type": "string", "minLength": 1},
    "sovyx_version": {"type": "string", "minLength": 1},
    "sequence_no": {"type": "integer", "minimum": 0},
    "saga_id": {"type": ["string", "null"]},
    "cause_id": {"type": ["string", "null"]},
    "span_id": {"type": ["string", "null"]},
}


# Type tokens used in the table below — kept terse so the table stays
# scannable.
T_STR = {"type": "string"}
T_INT = {"type": "integer"}
T_FLOAT = {"type": "number"}
T_BOOL = {"type": "boolean"}
T_LIST_NUM = {"type": "array", "items": {"type": "number"}}
T_LIST_STR = {"type": "array", "items": {"type": "string"}}


# Each entry: event name → (description, payload-fields dict).
# Payload-field value is (jsonschema-type, required: bool).
EVENTS: dict[str, tuple[str, dict[str, tuple[dict[str, Any], bool]]]] = {
    # ── Voice (Phase 3) ─────────────────────────────────────────────
    "voice.audio.frame": (
        "Per-frame audio capture telemetry from voice/audio.py.",
        {
            "voice.frames": (T_INT, True),
            "voice.sample_rate": (T_INT, True),
            "voice.rms": (T_FLOAT, True),
            "voice.peak": (T_FLOAT, False),
            "voice.dropped": (T_INT, False),
        },
    ),
    "voice.vad.frame": (
        "Silero VAD per-frame probability + rolling RMS window.",
        {
            "voice.probability": (T_FLOAT, True),
            "voice.rms": (T_FLOAT, True),
            "voice.state": (T_STR, True),
            "voice.onset_threshold": (T_FLOAT, True),
            "voice.offset_threshold": (T_FLOAT, True),
        },
    ),
    "voice.vad.state_change": (
        "VAD finite-state-machine transition (silence ↔ speech).",
        {
            "voice.from_state": (T_STR, True),
            "voice.to_state": (T_STR, True),
            "voice.probability": (T_FLOAT, True),
            "voice.rms": (T_FLOAT, True),
            "voice.onset_threshold": (T_FLOAT, True),
            "voice.offset_threshold": (T_FLOAT, True),
            "voice.prob_window": (T_LIST_NUM, True),
            "voice.rms_window": (T_LIST_NUM, True),
        },
    ),
    "voice.wake.score": (
        "OpenWakeWord score per inference window.",
        {
            "voice.score": (T_FLOAT, True),
            "voice.threshold": (T_FLOAT, True),
            "voice.stage2_threshold": (T_FLOAT, True),
            "voice.cooldown_ms_remaining": (T_INT, True),
            "voice.state": (T_STR, True),
            "voice.model_name": (T_STR, True),
        },
    ),
    "voice.wake.detected": (
        "Wake-word fired and the orchestrator armed STT.",
        {
            "voice.score": (T_FLOAT, True),
            "voice.model_name": (T_STR, True),
            "voice.stage1_threshold": (T_FLOAT, True),
            "voice.stage2_threshold": (T_FLOAT, True),
            "voice.transcription": (T_STR, True),
            "voice.window_frames": (T_INT, True),
        },
    ),
    "voice.stt.request": (
        "STT request submitted (local Moonshine or cloud provider).",
        {
            "voice.model": (T_STR, True),
            "voice.provider": (T_STR, True),
            "voice.language": (T_STR, True),
            "voice.audio_ms": (T_INT, True),
            "voice.sample_rate": (T_INT, True),
        },
    ),
    "voice.stt.response": (
        "STT result returned with transcript + confidence.",
        {
            "voice.model": (T_STR, True),
            "voice.provider": (T_STR, True),
            "voice.language": (T_STR, True),
            "voice.audio_ms": (T_INT, True),
            "voice.latency_ms": (T_FLOAT, True),
            "voice.confidence": (T_FLOAT, True),
            "voice.text_chars": (T_INT, True),
            "voice.transcript": (T_STR, True),
        },
    ),
    "voice.tts.synth.start": (
        "TTS synthesis kicked off (Kokoro or Piper).",
        {
            "voice.model": (T_STR, True),
            "voice.text_chars": (T_INT, True),
            "voice.engine": (T_STR, True),
        },
    ),
    "voice.tts.synth.end": (
        "TTS synthesis finished — total chunks + wall-clock duration.",
        {
            "voice.model": (T_STR, True),
            "voice.total_ms": (T_FLOAT, True),
            "voice.total_chunks": (T_INT, True),
        },
    ),
    "voice.tts.chunk": (
        "Per-chunk TTS generation timing (audio_ms vs wall-clock ms).",
        {
            "voice.chunk_index": (T_INT, True),
            "voice.text_chars": (T_INT, True),
            "voice.audio_ms": (T_FLOAT, True),
            "voice.generation_ms": (T_FLOAT, True),
            "voice.model": (T_STR, True),
            "voice.voice": (T_STR, True),
            "voice.sample_rate": (T_INT, True),
            "voice.speaker_id": (T_INT, False),
        },
    ),
    "voice.tts.chunk.played": (
        "TTS chunk dequeued from the output buffer and played to PortAudio.",
        {
            "voice.chunk_index": (T_INT, True),
            "voice.playback_latency_ms": (T_FLOAT, True),
            "voice.output_queue_depth": (T_INT, True),
        },
    ),
    "voice.deaf": (
        "Capture stream is wedged — VAD probability stayed below floor.",
        {
            "voice.mind_id": (T_STR, True),
            "voice.state": (T_STR, True),
            "voice.consecutive_deaf_warnings": (T_INT, True),
            "voice.threshold": (T_INT, True),
            "voice.max_vad_probability": (T_FLOAT, True),
            "voice.frames_processed": (T_INT, True),
            "voice.voice_clarity_active": (T_BOOL, True),
        },
    ),
    "voice.frame.drop": (
        "PortAudio reported a missing/late capture frame.",
        {
            "voice.stream_id": (T_STR, True),
            "voice.expected_frame_index": (T_INT, True),
            "voice.missing_frame_index": (T_INT, True),
            "voice.gap_ms": (T_FLOAT, True),
        },
    ),
    "voice.barge_in.detected": (
        "User started speaking while TTS was playing — playback was interrupted.",
        {
            "voice.mind_id": (T_STR, True),
            "voice.frames_sustained": (T_INT, True),
            "voice.prob": (T_FLOAT, True),
            "voice.threshold_frames": (T_INT, True),
            "voice.output_was_playing": (T_BOOL, True),
        },
    ),
    "voice.apo.detected": (
        "Windows capture-side APO (Voice Clarity etc.) was found on the active endpoint.",
        {
            "voice.device_id": (T_STR, True),
            "voice.apo_name": (T_STR, True),
            "voice.endpoint_guid": (T_STR, True),
            "voice.enabled": (T_BOOL, True),
        },
    ),
    "voice.apo.bypass": (
        "Coordinator switched the capture stream out of the APO chain.",
        {
            "voice.mind_id": (T_STR, True),
            "voice.consecutive_deaf_warnings": (T_INT, True),
            "voice.threshold": (T_INT, True),
            "voice.voice_clarity_active": (T_BOOL, True),
            "voice.auto_bypass_enabled": (T_BOOL, True),
            "voice.strategy_name": (T_STR, False),
            "voice.attempt_index": (T_INT, False),
            "voice.verdict": (T_STR, False),
        },
    ),
    "voice.stream.opened": (
        "PortAudio capture stream opened with negotiated format.",
        {
            "voice.stream_id": (T_STR, True),
            "voice.device_id": (T_STR, True),
            "voice.mode": (T_STR, True),
            "voice.sample_rate": (T_INT, True),
            "voice.channel_count": (T_INT, True),
        },
    ),
    "voice.device.hotplug": (
        "OS audio endpoint changed (arrival / removal / default switch).",
        {
            "voice.device_id": (T_STR, True),
            "voice.device_name": (T_STR, True),
            "voice.event_type": (T_STR, True),
            "voice.endpoint_guid": (T_STR, True),
        },
    ),
    # ── Plugins (Phase 5) ──────────────────────────────────────────
    "plugin.invoke.start": (
        "PluginManager.invoke() entered — tool dispatched to the sandbox.",
        {
            "plugin_id": (T_STR, True),
            "plugin.tool_name": (T_STR, True),
            "plugin.args_preview": (T_STR, True),
            "plugin.timeout_s": (T_FLOAT, True),
        },
    ),
    "plugin.invoke.end": (
        "Plugin tool returned (success or error) — duration + health snapshot.",
        {
            "plugin_id": (T_STR, True),
            "plugin.tool_name": (T_STR, True),
            "plugin.duration_ms": (T_INT, True),
            "plugin.success": (T_BOOL, True),
            "plugin.result_preview": (T_STR, False),
            "plugin.health.consecutive_failures": (T_INT, True),
            "plugin.health.active_tasks": (T_INT, True),
            "plugin.error": (T_STR, False),
        },
    ),
    "plugin.http.fetch": (
        "SandboxedHttpClient.request() — request log + paired response log.",
        {
            "plugin_id": (T_STR, True),
            "plugin.http.method": (T_STR, True),
            "plugin.http.url_host_only": (T_STR, True),
            "plugin.http.headers_redacted": (T_BOOL, True),
            "plugin.http.body_bytes": (T_INT, True),
            "plugin.http.allowed_domain": (T_BOOL, True),
            "plugin.http.rate_limited": (T_BOOL, True),
            "plugin.http.status": (T_INT, False),
            "plugin.http.response_bytes": (T_INT, False),
            "plugin.http.latency_ms": (T_FLOAT, False),
            "plugin.http.attempt": (T_INT, False),
        },
    ),
    "plugin.fs.access": (
        "SandboxedFsAccess read or write — path is relative to the plugin sandbox root.",
        {
            "plugin_id": (T_STR, True),
            "plugin.fs.path_relative": (T_STR, True),
            "plugin.fs.bytes": (T_INT, True),
            "plugin.fs.binary": (T_BOOL, False),
            "plugin.fs.mode": (T_STR, False),
        },
    ),
    # ── LLM / brain / bridge / dashboard (Phase 7) ─────────────────
    "llm.request.start": (
        "LLMRouter dispatched a request to a provider.",
        {
            "llm.provider": (T_STR, True),
            "llm.model": (T_STR, True),
            "llm.tokens_in": (T_INT, True),
            "llm.context_tokens": (T_INT, True),
            "llm.system_tokens": (T_INT, True),
        },
    ),
    "llm.request.end": (
        "LLMRouter received the provider response — tokens, latency, cost.",
        {
            "llm.provider": (T_STR, True),
            "llm.model": (T_STR, True),
            "llm.tokens_in": (T_INT, True),
            "llm.tokens_out": (T_INT, True),
            "llm.duration_ms": (T_FLOAT, True),
            "llm.cost_usd": (T_FLOAT, True),
            "llm.stop_reason": (T_STR, True),
        },
    ),
    "brain.query": (
        "BrainService retrieval — start log + completion log share the event name.",
        {
            "brain.k": (T_INT, True),
            "brain.filter": (T_STR, True),
            "brain.query_len": (T_INT, True),
            "brain.latency_ms": (T_INT, False),
            "brain.result_count": (T_INT, False),
            "brain.top_score": (T_FLOAT, False),
            "brain.search_mode": (T_STR, False),
        },
    ),
    "brain.episode.encoded": (
        "Reflect phase wrote a new episode + its concept extractions.",
        {
            "brain.episode_id": (T_STR, True),
            "brain.top_concept": (T_STR, True),
            "brain.novelty": (T_FLOAT, True),
            "brain.concepts_extracted": (T_INT, True),
        },
    ),
    "bridge.send": (
        "Outbound message dispatched on a bridge channel (telegram, signal, …).",
        {
            "bridge.channel_type": (T_STR, True),
            "bridge.message_id": (T_STR, True),
            "bridge.recipient_hash": (T_STR, True),
            "bridge.message_bytes": (T_INT, True),
        },
    ),
    "bridge.receive": (
        "Inbound message accepted from a bridge channel.",
        {
            "bridge.channel_type": (T_STR, True),
            "bridge.message_id": (T_STR, True),
            "bridge.sender_hash": (T_STR, True),
            "bridge.message_bytes": (T_INT, True),
        },
    ),
    "ws.connect": (
        "Dashboard WebSocket client connected.",
        {
            "net.client": (T_STR, True),
            "net.active_count": (T_INT, True),
        },
    ),
    "http.request": (
        "Dashboard HTTP request — method, path, status, latency.",
        {
            "net.method": (T_STR, True),
            "net.path": (T_STR, True),
            "net.client": (T_STR, True),
            "net.request_bytes": (T_INT, True),
            "net.status_code": (T_INT, False),
            "net.response_bytes": (T_INT, False),
            "net.latency_ms": (T_INT, False),
            "net.failed": (T_BOOL, False),
            "net.error_type": (T_STR, False),
        },
    ),
    # ── Config / audit / meta (Phase 9 + 11+) ──────────────────────
    "config.value.resolved": (
        "Startup cascade emitted the resolved value for one EngineConfig field.",
        {
            "cfg.field": (T_STR, True),
            "cfg.source": (T_STR, True),
            "cfg.value": (T_STR, True),
            "cfg.env_key": (T_STR, False),
        },
    ),
    "config.value.changed": (
        "Runtime config mutation (dashboard or RPC) — audit trail.",
        {
            "audit.field": (T_STR, True),
            "audit.old": (T_STR, False),
            "audit.new": (T_STR, False),
            "audit.actor": (T_STR, True),
            "audit.request_id": (T_STR, False),
            "audit.source": (T_STR, True),
        },
    ),
    "license.validated": (
        "LicenseValidator accepted (or refused) the JWT — tier + expiry surfaced.",
        {
            "license.subject_hash": (T_STR, True),
            "license.tier": (T_STR, True),
            "license.expiry": (T_INT, True),
            "license.minds_max": (T_INT, True),
            "license.feature_count": (T_INT, True),
            "license.grace_days_remaining": (T_INT, False),
            "license.expired_for_seconds": (T_INT, False),
        },
    ),
    "audit.permission_change": (
        "Plugin permission denied or escalated — emitted on permission.",
        {
            "plugin_id": (T_STR, True),
            "plugin.tool_name": (T_STR, True),
            "plugin.permission.attempted_resource": (T_STR, False),
            "plugin.permission.required": (T_LIST_STR, False),
            "plugin.permission.detail": (T_STR, True),
        },
    ),
    "meta.canary.tick": (
        "Synthetic heartbeat — confirms the logging pipeline is reachable end-to-end.",
        {
            "meta.tick_id": (T_INT, True),
            "meta.timestamp": (T_STR, True),
            "meta.lag_ms": (T_FLOAT, True),
        },
    ),
    "meta.audit.tick": (
        "Audit-of-auditor — verifies the tamper chain is advancing.",
        {
            "meta.tick_id": (T_INT, True),
            "meta.audit_entries_count": (T_INT, True),
            "meta.chain_hash": (T_STR, True),
        },
    ),
}


# ── Pydantic model generation (P11.3) ──────────────────────────────
#
# The same EVENTS table that drives the JSON catalog also drives a
# typed-model module (``src/sovyx/observability/log_schema/_models.py``).
# Each event becomes a ``LogEvent`` subclass with strictly-typed payload
# fields (``Field(..., alias="voice.probability")``). The KNOWN_EVENTS
# registry in ``observability/schema.py`` is built from that module.
#
# Two sources of truth would rot — the table here is the only place a
# new event is declared. JSON files + pydantic models are byproducts.

# Map the JSON-Schema type tokens above → annotated Python types for
# the generated pydantic models. Adding a new T_* token requires both
# this map AND the JSON token map above to stay in sync.
_PY_TYPE_BY_TOKEN: dict[str, str] = {
    json.dumps(T_STR, sort_keys=True): "str",
    json.dumps(T_INT, sort_keys=True): "int",
    json.dumps(T_FLOAT, sort_keys=True): "float",
    json.dumps(T_BOOL, sort_keys=True): "bool",
    json.dumps(T_LIST_NUM, sort_keys=True): "list[float]",
    json.dumps(T_LIST_STR, sort_keys=True): "list[str]",
}


def _python_type_for(field_schema: dict[str, Any]) -> str:
    """Return the annotated Python type for a payload field schema."""
    key = json.dumps(field_schema, sort_keys=True)
    py_type = _PY_TYPE_BY_TOKEN.get(key)
    if py_type is None:
        raise ValueError(
            f"unsupported payload type token in generator — extend _PY_TYPE_BY_TOKEN: {field_schema!r}"
        )
    return py_type


def _class_name_for(event: str) -> str:
    """Convert a dotted event name (``voice.vad.frame``) → CapWords (``VoiceVadFrame``).

    Underscores inside segments are also treated as word boundaries, so
    ``voice.vad.state_change`` → ``VoiceVadStateChange`` (not the half-pep8
    ``VoiceVadState_change`` that ``str.capitalize`` would produce).
    """
    pieces = event.replace(".", "_").split("_")
    return "".join(piece.capitalize() for piece in pieces if piece)


def _attr_name_for(field: str) -> str:
    """Convert a dotted payload-field name to a Python attribute (``voice.probability`` → ``voice_probability``)."""
    return field.replace(".", "_")


def build_models_module(events: dict[str, tuple[str, dict[str, tuple[dict[str, Any], bool]]]]) -> str:
    """Emit the generated ``_models.py`` source as a single string."""
    classes: list[str] = []
    registry_lines: list[str] = []

    for event, (description, payload) in events.items():
        cls_name = _class_name_for(event)
        registry_lines.append(f'    {cls_name}.event_name: {cls_name},')

        body: list[str] = [
            f'class {cls_name}(LogEvent):',
            f'    """{description}"""',
            "",
            f'    event_name: ClassVar[str] = "{event}"',
            f'    event: Literal["{event}"] = "{event}"',
        ]

        for field_name, (field_schema, is_required) in payload.items():
            attr = _attr_name_for(field_name)
            py_type = _python_type_for(field_schema)
            if is_required:
                if field_name == attr:
                    body.append(f"    {attr}: {py_type} = Field(...)")
                else:
                    body.append(f'    {attr}: {py_type} = Field(..., alias="{field_name}")')
            else:
                if field_name == attr:
                    body.append(f"    {attr}: {py_type} | None = None")
                else:
                    body.append(
                        f'    {attr}: {py_type} | None = Field(default=None, alias="{field_name}")'
                    )

        classes.append("\n".join(body))

    header = '''"""Generated pydantic models for every cataloged log event.

DO NOT EDIT — regenerate via ``uv run python scripts/_gen_log_schemas.py``.

Each class subclasses :class:`sovyx.observability.schema.LogEvent` and
declares strictly-typed payload fields with JSON aliases (e.g.
``voice.probability``). The :data:`EVENT_REGISTRY` below maps each
canonical event name to its model class — :data:`KNOWN_EVENTS` in
``observability/schema.py`` re-exports it.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from sovyx.observability.schema import LogEvent
'''
    all_lines = [f'    "{_class_name_for(name)}",' for name in events]
    all_lines.append('    "EVENT_REGISTRY",')
    all_block = "__all__ = [\n" + "\n".join(all_lines) + "\n]\n"

    registry_block = (
        "EVENT_REGISTRY: dict[str, type[LogEvent]] = {\n"
        + "\n".join(registry_lines)
        + "\n}\n"
    )

    parts = [header, all_block, "", "\n\n\n".join(classes), "", registry_block]
    return "\n".join(parts)


def build_schema(event: str, description: str, payload: dict[str, tuple[dict[str, Any], bool]]) -> dict[str, Any]:
    properties: dict[str, Any] = dict(ENVELOPE_PROPERTIES)
    properties["event"] = {"const": event}
    required = list(ENVELOPE_REQUIRED)

    for field_name, (field_schema, is_required) in payload.items():
        properties[field_name] = field_schema
        if is_required:
            required.append(field_name)

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://sovyx.dev/log_schema/{event}.json",
        "title": event,
        "description": description,
        "type": "object",
        "required": required,
        "properties": properties,
        "additionalProperties": True,
    }


# ── docs/observability.md auto-gen block (P11.4) ───────────────────
#
# The prose in docs/observability.md is hand-written, but the events
# catalog table is auto-generated from EVENTS so a phase can't add an
# event without the doc staying in sync. Regeneration rewrites only
# the block between these markers — everything outside is preserved.

DOCS_PATH = Path(__file__).resolve().parent.parent / "docs" / "observability.md"
_DOCS_BEGIN = "<!-- BEGIN AUTO-GENERATED EVENTS TABLE — do not edit by hand -->"
_DOCS_END = "<!-- END AUTO-GENERATED EVENTS TABLE -->"


def _docs_table_rows(
    events: dict[str, tuple[str, dict[str, tuple[dict[str, Any], bool]]]],
) -> str:
    """Render the catalog as a GitHub-flavoured markdown table."""
    lines = [
        "| Event | Description | Required payload | Optional payload |",
        "|---|---|---|---|",
    ]
    for event, (description, payload) in events.items():
        required = sorted(field for field, (_, is_req) in payload.items() if is_req)
        optional = sorted(field for field, (_, is_req) in payload.items() if not is_req)
        required_cell = ", ".join(f"`{name}`" for name in required) or "—"
        optional_cell = ", ".join(f"`{name}`" for name in optional) or "—"
        lines.append(
            f"| `{event}` | {description} | {required_cell} | {optional_cell} |"
        )
    return "\n".join(lines)


def render_docs_table(events: dict[str, tuple[str, dict[str, tuple[dict[str, Any], bool]]]]) -> str:
    """Return the full auto-generated block (markers included)."""
    return "\n".join(
        [
            _DOCS_BEGIN,
            "",
            f"_{len(events)} canonical events. Regenerate via "
            "`uv run python scripts/_gen_log_schemas.py`._",
            "",
            _docs_table_rows(events),
            "",
            _DOCS_END,
        ]
    )


def write_docs_table(
    events: dict[str, tuple[str, dict[str, tuple[dict[str, Any], bool]]]],
) -> bool:
    """Rewrite the auto-gen block in ``DOCS_PATH``.

    Returns True if the file existed and was rewritten, False if the doc
    is missing (first-time wiring — the generator does not create it).
    """
    if not DOCS_PATH.exists():
        return False
    current = DOCS_PATH.read_text(encoding="utf-8")
    if _DOCS_BEGIN not in current or _DOCS_END not in current:
        raise ValueError(
            f"{DOCS_PATH} is missing the auto-gen markers "
            f"({_DOCS_BEGIN!r} / {_DOCS_END!r}) — add them around the "
            "events catalog section and re-run."
        )
    before, _, rest = current.partition(_DOCS_BEGIN)
    _, _, after = rest.partition(_DOCS_END)
    new_doc = f"{before}{render_docs_table(events)}{after}"
    DOCS_PATH.write_text(new_doc, encoding="utf-8")
    return True


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for event, (description, payload) in EVENTS.items():
        schema = build_schema(event, description, payload)
        path = OUT_DIR / f"{event}.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        written += 1
    print(f"wrote {written} schemas to {OUT_DIR}")

    models_path = OUT_DIR / "_models.py"
    models_path.write_text(build_models_module(EVENTS), encoding="utf-8")
    print(f"wrote pydantic models for {len(EVENTS)} events to {models_path}")

    if write_docs_table(EVENTS):
        print(f"refreshed catalog table in {DOCS_PATH}")
    else:
        print(f"{DOCS_PATH} not found — docs catalog not refreshed")


if __name__ == "__main__":
    main()
