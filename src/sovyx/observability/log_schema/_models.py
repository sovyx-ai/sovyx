"""Generated pydantic models for every cataloged log event.

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

__all__ = [
    "VoiceAudioFrame",
    "VoiceVadFrame",
    "VoiceVadStateChange",
    "VoiceWakeScore",
    "VoiceWakeDetected",
    "VoiceSttRequest",
    "VoiceSttResponse",
    "VoiceTtsSynthStart",
    "VoiceTtsSynthEnd",
    "VoiceTtsChunk",
    "VoiceTtsChunkPlayed",
    "VoiceDeaf",
    "VoiceFrameDrop",
    "VoiceBargeInDetected",
    "VoiceApoDetected",
    "VoiceApoBypass",
    "VoiceStreamOpened",
    "VoiceDeviceHotplug",
    "PluginInvokeStart",
    "PluginInvokeEnd",
    "PluginHttpFetch",
    "PluginFsAccess",
    "LlmRequestStart",
    "LlmRequestEnd",
    "BrainQuery",
    "BrainEpisodeEncoded",
    "BridgeSend",
    "BridgeReceive",
    "WsConnect",
    "HttpRequest",
    "ConfigValueResolved",
    "ConfigValueChanged",
    "LicenseValidated",
    "AuditPermissionChange",
    "MetaCanaryTick",
    "MetaAuditTick",
    "EVENT_REGISTRY",
]


class VoiceAudioFrame(LogEvent):
    """Per-frame audio capture telemetry from voice/audio.py."""

    event_name: ClassVar[str] = "voice.audio.frame"
    event: Literal["voice.audio.frame"] = "voice.audio.frame"
    voice_frames: int = Field(..., alias="voice.frames")
    voice_sample_rate: int = Field(..., alias="voice.sample_rate")
    voice_rms: float = Field(..., alias="voice.rms")
    voice_peak: float | None = Field(default=None, alias="voice.peak")
    voice_dropped: int | None = Field(default=None, alias="voice.dropped")


class VoiceVadFrame(LogEvent):
    """Silero VAD per-frame probability + rolling RMS window."""

    event_name: ClassVar[str] = "voice.vad.frame"
    event: Literal["voice.vad.frame"] = "voice.vad.frame"
    voice_probability: float = Field(..., alias="voice.probability")
    voice_rms: float = Field(..., alias="voice.rms")
    voice_state: str = Field(..., alias="voice.state")
    voice_onset_threshold: float = Field(..., alias="voice.onset_threshold")
    voice_offset_threshold: float = Field(..., alias="voice.offset_threshold")


class VoiceVadStateChange(LogEvent):
    """VAD finite-state-machine transition (silence ↔ speech)."""

    event_name: ClassVar[str] = "voice.vad.state_change"
    event: Literal["voice.vad.state_change"] = "voice.vad.state_change"
    voice_from_state: str = Field(..., alias="voice.from_state")
    voice_to_state: str = Field(..., alias="voice.to_state")
    voice_probability: float = Field(..., alias="voice.probability")
    voice_rms: float = Field(..., alias="voice.rms")
    voice_onset_threshold: float = Field(..., alias="voice.onset_threshold")
    voice_offset_threshold: float = Field(..., alias="voice.offset_threshold")
    voice_prob_window: list[float] = Field(..., alias="voice.prob_window")
    voice_rms_window: list[float] = Field(..., alias="voice.rms_window")


class VoiceWakeScore(LogEvent):
    """OpenWakeWord score per inference window."""

    event_name: ClassVar[str] = "voice.wake.score"
    event: Literal["voice.wake.score"] = "voice.wake.score"
    voice_score: float = Field(..., alias="voice.score")
    voice_threshold: float = Field(..., alias="voice.threshold")
    voice_stage2_threshold: float = Field(..., alias="voice.stage2_threshold")
    voice_cooldown_ms_remaining: int = Field(..., alias="voice.cooldown_ms_remaining")
    voice_state: str = Field(..., alias="voice.state")
    voice_model_name: str = Field(..., alias="voice.model_name")


class VoiceWakeDetected(LogEvent):
    """Wake-word fired and the orchestrator armed STT."""

    event_name: ClassVar[str] = "voice.wake.detected"
    event: Literal["voice.wake.detected"] = "voice.wake.detected"
    voice_score: float = Field(..., alias="voice.score")
    voice_model_name: str = Field(..., alias="voice.model_name")
    voice_stage1_threshold: float = Field(..., alias="voice.stage1_threshold")
    voice_stage2_threshold: float = Field(..., alias="voice.stage2_threshold")
    voice_transcription: str = Field(..., alias="voice.transcription")
    voice_window_frames: int = Field(..., alias="voice.window_frames")


class VoiceSttRequest(LogEvent):
    """STT request submitted (local Moonshine or cloud provider)."""

    event_name: ClassVar[str] = "voice.stt.request"
    event: Literal["voice.stt.request"] = "voice.stt.request"
    voice_model: str = Field(..., alias="voice.model")
    voice_provider: str = Field(..., alias="voice.provider")
    voice_language: str = Field(..., alias="voice.language")
    voice_audio_ms: int = Field(..., alias="voice.audio_ms")
    voice_sample_rate: int = Field(..., alias="voice.sample_rate")


class VoiceSttResponse(LogEvent):
    """STT result returned with transcript + confidence."""

    event_name: ClassVar[str] = "voice.stt.response"
    event: Literal["voice.stt.response"] = "voice.stt.response"
    voice_model: str = Field(..., alias="voice.model")
    voice_provider: str = Field(..., alias="voice.provider")
    voice_language: str = Field(..., alias="voice.language")
    voice_audio_ms: int = Field(..., alias="voice.audio_ms")
    voice_latency_ms: float = Field(..., alias="voice.latency_ms")
    voice_confidence: float = Field(..., alias="voice.confidence")
    voice_text_chars: int = Field(..., alias="voice.text_chars")
    voice_transcript: str = Field(..., alias="voice.transcript")


class VoiceTtsSynthStart(LogEvent):
    """TTS synthesis kicked off (Kokoro or Piper)."""

    event_name: ClassVar[str] = "voice.tts.synth.start"
    event: Literal["voice.tts.synth.start"] = "voice.tts.synth.start"
    voice_model: str = Field(..., alias="voice.model")
    voice_text_chars: int = Field(..., alias="voice.text_chars")
    voice_engine: str = Field(..., alias="voice.engine")


class VoiceTtsSynthEnd(LogEvent):
    """TTS synthesis finished — total chunks + wall-clock duration."""

    event_name: ClassVar[str] = "voice.tts.synth.end"
    event: Literal["voice.tts.synth.end"] = "voice.tts.synth.end"
    voice_model: str = Field(..., alias="voice.model")
    voice_total_ms: float = Field(..., alias="voice.total_ms")
    voice_total_chunks: int = Field(..., alias="voice.total_chunks")


class VoiceTtsChunk(LogEvent):
    """Per-chunk TTS generation timing (audio_ms vs wall-clock ms)."""

    event_name: ClassVar[str] = "voice.tts.chunk"
    event: Literal["voice.tts.chunk"] = "voice.tts.chunk"
    voice_chunk_index: int = Field(..., alias="voice.chunk_index")
    voice_text_chars: int = Field(..., alias="voice.text_chars")
    voice_audio_ms: float = Field(..., alias="voice.audio_ms")
    voice_generation_ms: float = Field(..., alias="voice.generation_ms")
    voice_model: str = Field(..., alias="voice.model")
    voice_voice: str = Field(..., alias="voice.voice")
    voice_sample_rate: int = Field(..., alias="voice.sample_rate")
    voice_speaker_id: int | None = Field(default=None, alias="voice.speaker_id")


class VoiceTtsChunkPlayed(LogEvent):
    """TTS chunk dequeued from the output buffer and played to PortAudio."""

    event_name: ClassVar[str] = "voice.tts.chunk.played"
    event: Literal["voice.tts.chunk.played"] = "voice.tts.chunk.played"
    voice_chunk_index: int = Field(..., alias="voice.chunk_index")
    voice_playback_latency_ms: float = Field(..., alias="voice.playback_latency_ms")
    voice_output_queue_depth: int = Field(..., alias="voice.output_queue_depth")


class VoiceDeaf(LogEvent):
    """Capture stream is wedged — VAD probability stayed below floor."""

    event_name: ClassVar[str] = "voice.deaf"
    event: Literal["voice.deaf"] = "voice.deaf"
    voice_mind_id: str = Field(..., alias="voice.mind_id")
    voice_state: str = Field(..., alias="voice.state")
    voice_consecutive_deaf_warnings: int = Field(..., alias="voice.consecutive_deaf_warnings")
    voice_threshold: int = Field(..., alias="voice.threshold")
    voice_max_vad_probability: float = Field(..., alias="voice.max_vad_probability")
    voice_frames_processed: int = Field(..., alias="voice.frames_processed")
    voice_voice_clarity_active: bool = Field(..., alias="voice.voice_clarity_active")


class VoiceFrameDrop(LogEvent):
    """PortAudio reported a missing/late capture frame."""

    event_name: ClassVar[str] = "voice.frame.drop"
    event: Literal["voice.frame.drop"] = "voice.frame.drop"
    voice_stream_id: str = Field(..., alias="voice.stream_id")
    voice_expected_frame_index: int = Field(..., alias="voice.expected_frame_index")
    voice_missing_frame_index: int = Field(..., alias="voice.missing_frame_index")
    voice_gap_ms: float = Field(..., alias="voice.gap_ms")


class VoiceBargeInDetected(LogEvent):
    """User started speaking while TTS was playing — playback was interrupted."""

    event_name: ClassVar[str] = "voice.barge_in.detected"
    event: Literal["voice.barge_in.detected"] = "voice.barge_in.detected"
    voice_mind_id: str = Field(..., alias="voice.mind_id")
    voice_frames_sustained: int = Field(..., alias="voice.frames_sustained")
    voice_prob: float = Field(..., alias="voice.prob")
    voice_threshold_frames: int = Field(..., alias="voice.threshold_frames")
    voice_output_was_playing: bool = Field(..., alias="voice.output_was_playing")


class VoiceApoDetected(LogEvent):
    """Windows capture-side APO (Voice Clarity etc.) was found on the active endpoint."""

    event_name: ClassVar[str] = "voice.apo.detected"
    event: Literal["voice.apo.detected"] = "voice.apo.detected"
    voice_device_id: str = Field(..., alias="voice.device_id")
    voice_apo_name: str = Field(..., alias="voice.apo_name")
    voice_endpoint_guid: str = Field(..., alias="voice.endpoint_guid")
    voice_enabled: bool = Field(..., alias="voice.enabled")


class VoiceApoBypass(LogEvent):
    """Coordinator switched the capture stream out of the APO chain."""

    event_name: ClassVar[str] = "voice.apo.bypass"
    event: Literal["voice.apo.bypass"] = "voice.apo.bypass"
    voice_mind_id: str = Field(..., alias="voice.mind_id")
    voice_consecutive_deaf_warnings: int = Field(..., alias="voice.consecutive_deaf_warnings")
    voice_threshold: int = Field(..., alias="voice.threshold")
    voice_voice_clarity_active: bool = Field(..., alias="voice.voice_clarity_active")
    voice_auto_bypass_enabled: bool = Field(..., alias="voice.auto_bypass_enabled")
    voice_strategy_name: str | None = Field(default=None, alias="voice.strategy_name")
    voice_attempt_index: int | None = Field(default=None, alias="voice.attempt_index")
    voice_verdict: str | None = Field(default=None, alias="voice.verdict")


class VoiceStreamOpened(LogEvent):
    """PortAudio capture stream opened with negotiated format."""

    event_name: ClassVar[str] = "voice.stream.opened"
    event: Literal["voice.stream.opened"] = "voice.stream.opened"
    voice_stream_id: str = Field(..., alias="voice.stream_id")
    voice_device_id: str = Field(..., alias="voice.device_id")
    voice_mode: str = Field(..., alias="voice.mode")
    voice_sample_rate: int = Field(..., alias="voice.sample_rate")
    voice_channel_count: int = Field(..., alias="voice.channel_count")


class VoiceDeviceHotplug(LogEvent):
    """OS audio endpoint changed (arrival / removal / default switch)."""

    event_name: ClassVar[str] = "voice.device.hotplug"
    event: Literal["voice.device.hotplug"] = "voice.device.hotplug"
    voice_device_id: str = Field(..., alias="voice.device_id")
    voice_device_name: str = Field(..., alias="voice.device_name")
    voice_event_type: str = Field(..., alias="voice.event_type")
    voice_endpoint_guid: str = Field(..., alias="voice.endpoint_guid")


class PluginInvokeStart(LogEvent):
    """PluginManager.invoke() entered — tool dispatched to the sandbox."""

    event_name: ClassVar[str] = "plugin.invoke.start"
    event: Literal["plugin.invoke.start"] = "plugin.invoke.start"
    plugin_id: str = Field(...)
    plugin_tool_name: str = Field(..., alias="plugin.tool_name")
    plugin_args_preview: str = Field(..., alias="plugin.args_preview")
    plugin_timeout_s: float = Field(..., alias="plugin.timeout_s")


class PluginInvokeEnd(LogEvent):
    """Plugin tool returned (success or error) — duration + health snapshot."""

    event_name: ClassVar[str] = "plugin.invoke.end"
    event: Literal["plugin.invoke.end"] = "plugin.invoke.end"
    plugin_id: str = Field(...)
    plugin_tool_name: str = Field(..., alias="plugin.tool_name")
    plugin_duration_ms: int = Field(..., alias="plugin.duration_ms")
    plugin_success: bool = Field(..., alias="plugin.success")
    plugin_result_preview: str | None = Field(default=None, alias="plugin.result_preview")
    plugin_health_consecutive_failures: int = Field(
        ..., alias="plugin.health.consecutive_failures"
    )
    plugin_health_active_tasks: int = Field(..., alias="plugin.health.active_tasks")
    plugin_error: str | None = Field(default=None, alias="plugin.error")


class PluginHttpFetch(LogEvent):
    """SandboxedHttpClient.request() — request log + paired response log."""

    event_name: ClassVar[str] = "plugin.http.fetch"
    event: Literal["plugin.http.fetch"] = "plugin.http.fetch"
    plugin_id: str = Field(...)
    plugin_http_method: str = Field(..., alias="plugin.http.method")
    plugin_http_url_host_only: str = Field(..., alias="plugin.http.url_host_only")
    plugin_http_headers_redacted: bool = Field(..., alias="plugin.http.headers_redacted")
    plugin_http_body_bytes: int = Field(..., alias="plugin.http.body_bytes")
    plugin_http_allowed_domain: bool = Field(..., alias="plugin.http.allowed_domain")
    plugin_http_rate_limited: bool = Field(..., alias="plugin.http.rate_limited")
    plugin_http_status: int | None = Field(default=None, alias="plugin.http.status")
    plugin_http_response_bytes: int | None = Field(
        default=None, alias="plugin.http.response_bytes"
    )
    plugin_http_latency_ms: float | None = Field(default=None, alias="plugin.http.latency_ms")
    plugin_http_attempt: int | None = Field(default=None, alias="plugin.http.attempt")


class PluginFsAccess(LogEvent):
    """SandboxedFsAccess read or write — path is relative to the plugin sandbox root."""

    event_name: ClassVar[str] = "plugin.fs.access"
    event: Literal["plugin.fs.access"] = "plugin.fs.access"
    plugin_id: str = Field(...)
    plugin_fs_path_relative: str = Field(..., alias="plugin.fs.path_relative")
    plugin_fs_bytes: int = Field(..., alias="plugin.fs.bytes")
    plugin_fs_binary: bool | None = Field(default=None, alias="plugin.fs.binary")
    plugin_fs_mode: str | None = Field(default=None, alias="plugin.fs.mode")


class LlmRequestStart(LogEvent):
    """LLMRouter dispatched a request to a provider."""

    event_name: ClassVar[str] = "llm.request.start"
    event: Literal["llm.request.start"] = "llm.request.start"
    llm_provider: str = Field(..., alias="llm.provider")
    llm_model: str = Field(..., alias="llm.model")
    llm_tokens_in: int = Field(..., alias="llm.tokens_in")
    llm_context_tokens: int = Field(..., alias="llm.context_tokens")
    llm_system_tokens: int = Field(..., alias="llm.system_tokens")


class LlmRequestEnd(LogEvent):
    """LLMRouter received the provider response — tokens, latency, cost."""

    event_name: ClassVar[str] = "llm.request.end"
    event: Literal["llm.request.end"] = "llm.request.end"
    llm_provider: str = Field(..., alias="llm.provider")
    llm_model: str = Field(..., alias="llm.model")
    llm_tokens_in: int = Field(..., alias="llm.tokens_in")
    llm_tokens_out: int = Field(..., alias="llm.tokens_out")
    llm_duration_ms: float = Field(..., alias="llm.duration_ms")
    llm_cost_usd: float = Field(..., alias="llm.cost_usd")
    llm_stop_reason: str = Field(..., alias="llm.stop_reason")


class BrainQuery(LogEvent):
    """BrainService retrieval — start log + completion log share the event name."""

    event_name: ClassVar[str] = "brain.query"
    event: Literal["brain.query"] = "brain.query"
    brain_k: int = Field(..., alias="brain.k")
    brain_filter: str = Field(..., alias="brain.filter")
    brain_query_len: int = Field(..., alias="brain.query_len")
    brain_latency_ms: int | None = Field(default=None, alias="brain.latency_ms")
    brain_result_count: int | None = Field(default=None, alias="brain.result_count")
    brain_top_score: float | None = Field(default=None, alias="brain.top_score")
    brain_search_mode: str | None = Field(default=None, alias="brain.search_mode")


class BrainEpisodeEncoded(LogEvent):
    """Reflect phase wrote a new episode + its concept extractions."""

    event_name: ClassVar[str] = "brain.episode.encoded"
    event: Literal["brain.episode.encoded"] = "brain.episode.encoded"
    brain_episode_id: str = Field(..., alias="brain.episode_id")
    brain_top_concept: str = Field(..., alias="brain.top_concept")
    brain_novelty: float = Field(..., alias="brain.novelty")
    brain_concepts_extracted: int = Field(..., alias="brain.concepts_extracted")


class BridgeSend(LogEvent):
    """Outbound message dispatched on a bridge channel (telegram, signal, …)."""

    event_name: ClassVar[str] = "bridge.send"
    event: Literal["bridge.send"] = "bridge.send"
    bridge_channel_type: str = Field(..., alias="bridge.channel_type")
    bridge_message_id: str = Field(..., alias="bridge.message_id")
    bridge_recipient_hash: str = Field(..., alias="bridge.recipient_hash")
    bridge_message_bytes: int = Field(..., alias="bridge.message_bytes")


class BridgeReceive(LogEvent):
    """Inbound message accepted from a bridge channel."""

    event_name: ClassVar[str] = "bridge.receive"
    event: Literal["bridge.receive"] = "bridge.receive"
    bridge_channel_type: str = Field(..., alias="bridge.channel_type")
    bridge_message_id: str = Field(..., alias="bridge.message_id")
    bridge_sender_hash: str = Field(..., alias="bridge.sender_hash")
    bridge_message_bytes: int = Field(..., alias="bridge.message_bytes")


class WsConnect(LogEvent):
    """Dashboard WebSocket client connected."""

    event_name: ClassVar[str] = "ws.connect"
    event: Literal["ws.connect"] = "ws.connect"
    net_client: str = Field(..., alias="net.client")
    net_active_count: int = Field(..., alias="net.active_count")


class HttpRequest(LogEvent):
    """Dashboard HTTP request — method, path, status, latency."""

    event_name: ClassVar[str] = "http.request"
    event: Literal["http.request"] = "http.request"
    net_method: str = Field(..., alias="net.method")
    net_path: str = Field(..., alias="net.path")
    net_client: str = Field(..., alias="net.client")
    net_request_bytes: int = Field(..., alias="net.request_bytes")
    net_status_code: int | None = Field(default=None, alias="net.status_code")
    net_response_bytes: int | None = Field(default=None, alias="net.response_bytes")
    net_latency_ms: int | None = Field(default=None, alias="net.latency_ms")
    net_failed: bool | None = Field(default=None, alias="net.failed")
    net_error_type: str | None = Field(default=None, alias="net.error_type")


class ConfigValueResolved(LogEvent):
    """Startup cascade emitted the resolved value for one EngineConfig field."""

    event_name: ClassVar[str] = "config.value.resolved"
    event: Literal["config.value.resolved"] = "config.value.resolved"
    cfg_field: str = Field(..., alias="cfg.field")
    cfg_source: str = Field(..., alias="cfg.source")
    cfg_value: str = Field(..., alias="cfg.value")
    cfg_env_key: str | None = Field(default=None, alias="cfg.env_key")


class ConfigValueChanged(LogEvent):
    """Runtime config mutation (dashboard or RPC) — audit trail."""

    event_name: ClassVar[str] = "config.value.changed"
    event: Literal["config.value.changed"] = "config.value.changed"
    audit_field: str = Field(..., alias="audit.field")
    audit_old: str | None = Field(default=None, alias="audit.old")
    audit_new: str | None = Field(default=None, alias="audit.new")
    audit_actor: str = Field(..., alias="audit.actor")
    audit_request_id: str | None = Field(default=None, alias="audit.request_id")
    audit_source: str = Field(..., alias="audit.source")


class LicenseValidated(LogEvent):
    """LicenseValidator accepted (or refused) the JWT — tier + expiry surfaced."""

    event_name: ClassVar[str] = "license.validated"
    event: Literal["license.validated"] = "license.validated"
    license_subject_hash: str = Field(..., alias="license.subject_hash")
    license_tier: str = Field(..., alias="license.tier")
    license_expiry: int = Field(..., alias="license.expiry")
    license_minds_max: int = Field(..., alias="license.minds_max")
    license_feature_count: int = Field(..., alias="license.feature_count")
    license_grace_days_remaining: int | None = Field(
        default=None, alias="license.grace_days_remaining"
    )
    license_expired_for_seconds: int | None = Field(
        default=None, alias="license.expired_for_seconds"
    )


class AuditPermissionChange(LogEvent):
    """Plugin permission denied or escalated — emitted on permission."""

    event_name: ClassVar[str] = "audit.permission_change"
    event: Literal["audit.permission_change"] = "audit.permission_change"
    plugin_id: str = Field(...)
    plugin_tool_name: str = Field(..., alias="plugin.tool_name")
    plugin_permission_attempted_resource: str | None = Field(
        default=None, alias="plugin.permission.attempted_resource"
    )
    plugin_permission_required: list[str] | None = Field(
        default=None, alias="plugin.permission.required"
    )
    plugin_permission_detail: str = Field(..., alias="plugin.permission.detail")


class MetaCanaryTick(LogEvent):
    """Synthetic heartbeat — confirms the logging pipeline is reachable end-to-end."""

    event_name: ClassVar[str] = "meta.canary.tick"
    event: Literal["meta.canary.tick"] = "meta.canary.tick"
    meta_tick_id: int = Field(..., alias="meta.tick_id")
    meta_timestamp: str = Field(..., alias="meta.timestamp")
    meta_lag_ms: float = Field(..., alias="meta.lag_ms")


class MetaAuditTick(LogEvent):
    """Audit-of-auditor — verifies the tamper chain is advancing."""

    event_name: ClassVar[str] = "meta.audit.tick"
    event: Literal["meta.audit.tick"] = "meta.audit.tick"
    meta_tick_id: int = Field(..., alias="meta.tick_id")
    meta_audit_entries_count: int = Field(..., alias="meta.audit_entries_count")
    meta_chain_hash: str = Field(..., alias="meta.chain_hash")


EVENT_REGISTRY: dict[str, type[LogEvent]] = {
    VoiceAudioFrame.event_name: VoiceAudioFrame,
    VoiceVadFrame.event_name: VoiceVadFrame,
    VoiceVadStateChange.event_name: VoiceVadStateChange,
    VoiceWakeScore.event_name: VoiceWakeScore,
    VoiceWakeDetected.event_name: VoiceWakeDetected,
    VoiceSttRequest.event_name: VoiceSttRequest,
    VoiceSttResponse.event_name: VoiceSttResponse,
    VoiceTtsSynthStart.event_name: VoiceTtsSynthStart,
    VoiceTtsSynthEnd.event_name: VoiceTtsSynthEnd,
    VoiceTtsChunk.event_name: VoiceTtsChunk,
    VoiceTtsChunkPlayed.event_name: VoiceTtsChunkPlayed,
    VoiceDeaf.event_name: VoiceDeaf,
    VoiceFrameDrop.event_name: VoiceFrameDrop,
    VoiceBargeInDetected.event_name: VoiceBargeInDetected,
    VoiceApoDetected.event_name: VoiceApoDetected,
    VoiceApoBypass.event_name: VoiceApoBypass,
    VoiceStreamOpened.event_name: VoiceStreamOpened,
    VoiceDeviceHotplug.event_name: VoiceDeviceHotplug,
    PluginInvokeStart.event_name: PluginInvokeStart,
    PluginInvokeEnd.event_name: PluginInvokeEnd,
    PluginHttpFetch.event_name: PluginHttpFetch,
    PluginFsAccess.event_name: PluginFsAccess,
    LlmRequestStart.event_name: LlmRequestStart,
    LlmRequestEnd.event_name: LlmRequestEnd,
    BrainQuery.event_name: BrainQuery,
    BrainEpisodeEncoded.event_name: BrainEpisodeEncoded,
    BridgeSend.event_name: BridgeSend,
    BridgeReceive.event_name: BridgeReceive,
    WsConnect.event_name: WsConnect,
    HttpRequest.event_name: HttpRequest,
    ConfigValueResolved.event_name: ConfigValueResolved,
    ConfigValueChanged.event_name: ConfigValueChanged,
    LicenseValidated.event_name: LicenseValidated,
    AuditPermissionChange.event_name: AuditPermissionChange,
    MetaCanaryTick.event_name: MetaCanaryTick,
    MetaAuditTick.event_name: MetaAuditTick,
}
