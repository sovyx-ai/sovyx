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


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for event, (description, payload) in EVENTS.items():
        schema = build_schema(event, description, payload)
        path = OUT_DIR / f"{event}.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        written += 1
    print(f"wrote {written} schemas to {OUT_DIR}")


if __name__ == "__main__":
    main()
