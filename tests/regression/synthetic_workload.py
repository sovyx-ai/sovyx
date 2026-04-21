"""Deterministic log workload — fixture for the §23.2 noise gate.

Emits a frozen, reproducible sequence of cataloged log events that mirrors
a single end-to-end Sovyx interaction (voice wake → STT → brain query →
LLM round-trip → plugin call → TTS → bridge response). The set of events
and their per-event counts are constants of this module; the only thing
that changes between runs is the wall-clock timestamp injected by the
envelope processor.

Consumed by:

  * ``scripts/check_log_noise.py`` — runs this workload, counts entries
    by ``(event, logger)``, and fails CI if the totals drift more than
    the budget allows from ``benchmarks/log_noise_baseline.json``.
  * ``scripts/update_log_noise_baseline.py`` — runs the same workload
    and re-writes the baseline JSON when a phase intentionally adds new
    events (commit body must justify the bump).

Design constraints:

  * **Deterministic counts** — no loops driven by random or wall-clock,
    no conditional emits. The same call yields the same multiset of
    ``(event, logger)`` pairs every time.
  * **No real IO** — payloads are literals; nothing touches the network,
    audio devices, LLM providers, or the brain DB.
  * **Real pipeline** — events go through the production
    :func:`setup_logging` chain (envelope, PII redactor, JSON renderer)
    so the captured JSONL exercises every processor a CI regression
    would otherwise hide.
  * **Sampling disabled** — ``ObservabilitySamplingConfig`` rates set to
    1 so high-frequency events (``voice.audio.frame``, ``voice.vad.frame``,
    ``voice.wake.score``) are never dropped by the SamplingProcessor;
    a flapping count would make the gate noisy.

The emit sequence is a single conceptual saga repeated three times to
verify reproducibility — the noise gate's ratio check would catch a
single-emit drift that the baseline's absolute counts would not.
"""

from __future__ import annotations

import argparse
import contextlib
import logging as _stdlib_logging
import logging.handlers as _stdlib_handlers
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

# Number of times the conceptual saga is replayed end-to-end. Three
# iterations keeps the workload lightweight (~100 entries) while still
# exposing a regression where one emit site started firing twice per
# saga instead of once — the per-event ratio gate fires at 50% drift.
_SAGA_ITERATIONS: int = 3

# Logger name conventions mirror production: each subsystem owns the
# events it emits, so the noise gate detects "developer added a new
# logger.info to module X" granularity, not just "global volume up".
_LOGGER_VOICE = "sovyx.voice"
_LOGGER_PLUGIN = "sovyx.plugins"
_LOGGER_LLM = "sovyx.llm.router"
_LOGGER_BRAIN = "sovyx.brain"
_LOGGER_BRIDGE = "sovyx.bridge"
_LOGGER_DASHBOARD = "sovyx.dashboard"
_LOGGER_CONFIG = "sovyx.engine.config"
_LOGGER_LICENSE = "sovyx.license"
_LOGGER_AUDIT = "sovyx.audit"
_LOGGER_META = "sovyx.observability.meta"


# ── Per-saga emit recipe ──────────────────────────────────────────────
# Each tuple is (logger_name, event, payload). Payload uses canonical
# dotted aliases so the EVENT_REGISTRY validators recognise them. The
# values are intentionally fixed integers/floats — never wall-clock or
# random — so the resulting JSONL byte-shape is reproducible.
_PER_SAGA_EVENTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    # --- voice intake ---------------------------------------------------
    (
        _LOGGER_VOICE,
        "voice.stream.opened",
        {
            "voice.stream_id": "synth-stream-1",
            "voice.device_id": "synth-mic-0",
            "voice.mode": "shared",
            "voice.sample_rate": 16000,
            "voice.channel_count": 1,
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.audio.frame",
        {
            "voice.frames": 320,
            "voice.sample_rate": 16000,
            "voice.rms": 0.012,
            "voice.peak": 0.035,
            "voice.dropped": 0,
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.vad.frame",
        {
            "voice.probability": 0.84,
            "voice.rms": 0.018,
            "voice.state": "speech",
            "voice.onset_threshold": 0.5,
            "voice.offset_threshold": 0.35,
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.vad.state_change",
        {
            "voice.from_state": "silence",
            "voice.to_state": "speech",
            "voice.probability": 0.91,
            "voice.rms": 0.022,
            "voice.onset_threshold": 0.5,
            "voice.offset_threshold": 0.35,
            "voice.prob_window": [0.4, 0.6, 0.85, 0.91],
            "voice.rms_window": [0.01, 0.014, 0.02, 0.022],
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.wake.score",
        {
            "voice.score": 0.62,
            "voice.threshold": 0.5,
            "voice.stage2_threshold": 0.7,
            "voice.cooldown_ms_remaining": 0,
            "voice.state": "armed",
            "voice.model_name": "alexa_v0.1",
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.wake.detected",
        {
            "voice.score": 0.88,
            "voice.model_name": "alexa_v0.1",
            "voice.stage1_threshold": 0.5,
            "voice.stage2_threshold": 0.7,
            "voice.transcription": "alexa",
            "voice.window_frames": 16,
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.stt.request",
        {
            "voice.model": "moonshine-base",
            "voice.provider": "local",
            "voice.language": "en",
            "voice.audio_ms": 1800,
            "voice.sample_rate": 16000,
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.stt.response",
        {
            "voice.model": "moonshine-base",
            "voice.provider": "local",
            "voice.language": "en",
            "voice.audio_ms": 1800,
            "voice.latency_ms": 312.0,
            "voice.confidence": 0.94,
            "voice.text_chars": 22,
            "voice.transcript": "what's the weather today",
        },
    ),
    # --- brain retrieval ------------------------------------------------
    (
        _LOGGER_BRAIN,
        "brain.query",
        {
            "brain.k": 8,
            "brain.filter": "kind:episode",
            "brain.query_len": 22,
            "brain.latency_ms": 14,
            "brain.result_count": 5,
            "brain.top_score": 0.71,
            "brain.search_mode": "hybrid",
        },
    ),
    # --- LLM round-trip ------------------------------------------------
    (
        _LOGGER_LLM,
        "llm.request.start",
        {
            "llm.provider": "anthropic",
            "llm.model": "claude-haiku-4-5-20251001",
            "llm.tokens_in": 412,
            "llm.context_tokens": 380,
            "llm.system_tokens": 32,
        },
    ),
    (
        _LOGGER_LLM,
        "llm.request.end",
        {
            "llm.provider": "anthropic",
            "llm.model": "claude-haiku-4-5-20251001",
            "llm.tokens_in": 412,
            "llm.tokens_out": 96,
            "llm.duration_ms": 740.0,
            "llm.cost_usd": 0.0034,
            "llm.stop_reason": "end_turn",
        },
    ),
    # --- plugin invocation --------------------------------------------
    (
        _LOGGER_PLUGIN,
        "plugin.invoke.start",
        {
            "plugin.id": "weather",
            "plugin.tool_name": "get_forecast",
            "plugin.args_preview": "{'city': 'Lisbon'}",
            "plugin.timeout_s": 10.0,
        },
    ),
    (
        _LOGGER_PLUGIN,
        "plugin.http.fetch",
        {
            "plugin.id": "weather",
            "plugin.http.method": "GET",
            "plugin.http.url_host_only": "api.weather.example",
            "plugin.http.headers_redacted": True,
            "plugin.http.body_bytes": 0,
            "plugin.http.allowed_domain": True,
            "plugin.http.rate_limited": False,
            "plugin.http.status": 200,
            "plugin.http.response_bytes": 612,
            "plugin.http.latency_ms": 88.0,
            "plugin.http.attempt": 1,
        },
    ),
    (
        _LOGGER_PLUGIN,
        "plugin.fs.access",
        {
            "plugin.id": "weather",
            "plugin.fs.path_relative": "cache/forecast.json",
            "plugin.fs.bytes": 612,
            "plugin.fs.binary": False,
            "plugin.fs.mode": "read",
        },
    ),
    (
        _LOGGER_PLUGIN,
        "plugin.invoke.end",
        {
            "plugin.id": "weather",
            "plugin.tool_name": "get_forecast",
            "plugin.duration_ms": 102,
            "plugin.success": True,
            "plugin.result_preview": "Sunny, 22C",
            "plugin.health.consecutive_failures": 0,
            "plugin.health.active_tasks": 0,
        },
    ),
    # --- brain consolidation ------------------------------------------
    (
        _LOGGER_BRAIN,
        "brain.episode.encoded",
        {
            "brain.episode_id": "ep-synth-0001",
            "brain.top_concept": "weather",
            "brain.novelty": 0.31,
            "brain.concepts_extracted": 4,
        },
    ),
    # --- TTS playback -------------------------------------------------
    (
        _LOGGER_VOICE,
        "voice.tts.synth.start",
        {
            "voice.model": "kokoro-v1",
            "voice.text_chars": 64,
            "voice.engine": "kokoro",
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.tts.chunk",
        {
            "voice.chunk_index": 0,
            "voice.text_chars": 32,
            "voice.audio_ms": 920.0,
            "voice.generation_ms": 180.0,
            "voice.model": "kokoro-v1",
            "voice.voice": "af",
            "voice.sample_rate": 24000,
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.tts.chunk.played",
        {
            "voice.chunk_index": 0,
            "voice.playback_latency_ms": 12.0,
            "voice.output_queue_depth": 1,
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.tts.synth.end",
        {
            "voice.model": "kokoro-v1",
            "voice.total_ms": 920.0,
            "voice.total_chunks": 1,
        },
    ),
    # --- bridge round-trip --------------------------------------------
    (
        _LOGGER_BRIDGE,
        "bridge.receive",
        {
            "bridge.channel_type": "telegram",
            "bridge.message_id": "tg-msg-1",
            "bridge.sender_hash": "sha256:abcd",
            "bridge.message_bytes": 22,
        },
    ),
    (
        _LOGGER_BRIDGE,
        "bridge.send",
        {
            "bridge.channel_type": "telegram",
            "bridge.message_id": "tg-msg-2",
            "bridge.recipient_hash": "sha256:abcd",
            "bridge.message_bytes": 64,
        },
    ),
    # --- dashboard surface --------------------------------------------
    (
        _LOGGER_DASHBOARD,
        "ws.connect",
        {
            "net.client": "127.0.0.1",
            "net.active_count": 1,
        },
    ),
    (
        _LOGGER_DASHBOARD,
        "http.request",
        {
            "net.method": "GET",
            "net.path": "/api/status",
            "net.client": "127.0.0.1",
            "net.request_bytes": 0,
            "net.status_code": 200,
            "net.response_bytes": 412,
            "net.latency_ms": 6,
        },
    ),
)


# ── One-shot startup-cascade events ──────────────────────────────────
# Emitted ONCE per workload run (not per-saga) — they correspond to
# bootstrap-time emits in the real daemon and their absence/duplication
# would itself be a regression.
_STARTUP_EVENTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        _LOGGER_CONFIG,
        "config.value.resolved",
        {
            "cfg.field": "log.level",
            "cfg.source": "default",
            "cfg.value": "INFO",
            "cfg.env_key": "SOVYX_LOG__LEVEL",
        },
    ),
    (
        _LOGGER_CONFIG,
        "config.value.changed",
        {
            "audit.field": "log.level",
            "audit.old": "INFO",
            "audit.new": "DEBUG",
            "audit.actor": "dashboard:operator",
            "audit.source": "dashboard",
        },
    ),
    (
        _LOGGER_LICENSE,
        "license.validated",
        {
            "license.subject_hash": "sha256:1111",
            "license.tier": "personal",
            "license.expiry": 4_102_444_800,
            "license.minds_max": 3,
            "license.feature_count": 12,
        },
    ),
    (
        _LOGGER_AUDIT,
        "audit.permission_change",
        {
            "plugin.id": "weather",
            "plugin.tool_name": "get_forecast",
            "plugin.permission.detail": "granted: http.fetch",
        },
    ),
    (
        _LOGGER_META,
        "meta.canary.tick",
        {
            "meta.tick_id": 1,
            "meta.timestamp": "2026-04-20T00:00:00Z",
            "meta.lag_ms": 0.5,
        },
    ),
    (
        _LOGGER_META,
        "meta.audit.tick",
        {
            "meta.tick_id": 1,
            "meta.audit_entries_count": 4,
            "meta.chain_hash": "sha256:cccc",
        },
    ),
    # --- rare voice-anomaly events emitted once for catalog coverage --
    (
        _LOGGER_VOICE,
        "voice.deaf",
        {
            "voice.mind_id": "synth-mind",
            "voice.state": "armed",
            "voice.consecutive_deaf_warnings": 1,
            "voice.threshold": 5,
            "voice.max_vad_probability": 0.001,
            "voice.frames_processed": 480,
            "voice.voice_clarity_active": False,
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.frame.drop",
        {
            "voice.stream_id": "synth-stream-1",
            "voice.expected_frame_index": 41,
            "voice.missing_frame_index": 42,
            "voice.gap_ms": 32.0,
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.barge_in.detected",
        {
            "voice.mind_id": "synth-mind",
            "voice.frames_sustained": 4,
            "voice.prob": 0.82,
            "voice.threshold_frames": 3,
            "voice.output_was_playing": True,
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.apo.detected",
        {
            "voice.device_id": "synth-mic-0",
            "voice.apo_name": "VocaEffectPack",
            "voice.endpoint_guid": "{00000000-0000-0000-0000-000000000001}",
            "voice.enabled": True,
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.apo.bypass",
        {
            "voice.mind_id": "synth-mind",
            "voice.consecutive_deaf_warnings": 5,
            "voice.threshold": 5,
            "voice.voice_clarity_active": True,
            "voice.auto_bypass_enabled": True,
            "voice.strategy_name": "wasapi-exclusive",
            "voice.attempt_index": 1,
            "voice.verdict": "ok",
        },
    ),
    (
        _LOGGER_VOICE,
        "voice.device.hotplug",
        {
            "voice.device_id": "synth-mic-0",
            "voice.device_name": "Synth Mic",
            "voice.event_type": "arrival",
            "voice.endpoint_guid": "{00000000-0000-0000-0000-000000000001}",
        },
    ),
)


def _iter_workload_events() -> Iterable[tuple[str, str, dict[str, Any]]]:
    """Yield the full (logger, event, payload) sequence for one run.

    Order: every startup event once, then the per-saga sequence
    repeated ``_SAGA_ITERATIONS`` times. The order does not affect
    the noise gate (which counts, not orders) but a stable order
    makes manual JSONL inspection easier when debugging a baseline
    drift.
    """
    yield from _STARTUP_EVENTS
    for _ in range(_SAGA_ITERATIONS):
        yield from _PER_SAGA_EVENTS


def _drain_handlers() -> None:
    """Close every file handler so Windows releases the rotating-log lock."""
    for parent in (
        _stdlib_logging.getLogger(),
        _stdlib_logging.getLogger("sovyx.audit"),
    ):
        for handler in list(parent.handlers):
            with contextlib.suppress(Exception):
                handler.flush()
            with contextlib.suppress(Exception):
                handler.close()
            with contextlib.suppress(Exception):
                parent.removeHandler(handler)


def _silence_console() -> None:
    """Detach console StreamHandlers so emits don't pollute stdout."""
    root = _stdlib_logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _stdlib_logging.StreamHandler) and not isinstance(
            handler,
            _stdlib_logging.FileHandler | _stdlib_handlers.RotatingFileHandler,
        ):
            root.removeHandler(handler)


def run_workload(out_path: Path) -> int:
    """Emit the full deterministic workload through the production pipeline.

    Args:
        out_path: Path to the rotating-file JSONL the workload writes to.
            The parent directory is created if absent.

    Returns:
        The number of (event, logger) emissions issued. This is a sanity
        check for the gate — if the count returned does not equal the
        sum of baseline counts, the JSONL was truncated or sampling fired.
    """
    from sovyx.engine.config import (
        LoggingConfig,
        ObservabilityConfig,
        ObservabilityFeaturesConfig,
        ObservabilityPIIConfig,
        ObservabilitySamplingConfig,
    )
    from sovyx.observability.logging import get_logger, setup_logging, shutdown_logging

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Sampling rates set to 1 so the SamplingProcessor never drops a
    # workload event — counts must be reproducible.
    obs_cfg = ObservabilityConfig(
        features=ObservabilityFeaturesConfig(
            async_queue=False,
            pii_redaction=True,
            schema_validation=True,
            saga_propagation=False,
            voice_telemetry=True,
            startup_cascade=False,
            plugin_introspection=True,
            anomaly_detection=False,
            tamper_chain=False,
            metrics_exporter=False,
        ),
        pii=ObservabilityPIIConfig(),
        sampling=ObservabilitySamplingConfig(
            audio_frame_rate=1,
            vad_frame_rate=1,
            wake_word_score_rate=1,
        ),
    )
    logging_cfg = LoggingConfig(
        level="INFO",
        console_format="json",
        log_file=out_path,
    )

    shutdown_logging(timeout=2.0)
    _drain_handlers()
    setup_logging(logging_cfg, obs_cfg, data_dir=out_path.parent)
    _silence_console()

    emit_count = 0
    loggers: dict[str, Any] = {}
    for logger_name, event, payload in _iter_workload_events():
        log = loggers.get(logger_name) or loggers.setdefault(logger_name, get_logger(logger_name))
        log.info(event, **payload)
        emit_count += 1

    shutdown_logging(timeout=5.0)
    _drain_handlers()
    return emit_count


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m tests.regression.synthetic_workload --out <path>``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Path the workload writes its JSONL output to",
    )
    args = parser.parse_args(argv)
    emitted = run_workload(args.out)
    print(f"OK: emitted {emitted} entries to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
