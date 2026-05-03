/**
 * Sovyx Dashboard — API types
 * Mirrors the FastAPI response schemas from sovyx.dashboard.*
 *
 * FE-00a: Aligned with real backend (18 mismatches fixed).
 */

// ── Health ──

/** Backend: CheckStatus enum values (lowercase) */
export type HealthStatus = "green" | "yellow" | "red";

export interface HealthCheck {
  name: string;
  status: HealthStatus;
  message: string;
  latency_ms?: number;
}

/** GET /api/health response */
export interface HealthResponse {
  overall: HealthStatus;
  checks: HealthCheck[];
}

// ── Status ──

/** GET /api/status response — mirrors StatusSnapshot.to_dict() */
export interface SystemStatus {
  version: string;
  uptime_seconds: number;
  mind_name: string;
  active_conversations: number;
  memory_concepts: number;
  memory_episodes: number;
  llm_cost_today: number;
  llm_calls_today: number;
  tokens_today: number;
  messages_today: number;
  cost_history?: CostHistoryEntry[];
  timezone?: string;
  today_date?: string;
  has_lifetime_activity?: boolean;
}

// ── Conversations ──

/** Single message (conversation turn) — from get_conversation_messages() */
export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string; // ISO datetime (created_at from conversation_turns)
  tags?: string[];
}

/** Conversation list item — from list_conversations() */
export interface Conversation {
  id: string;
  participant: string; // person_id (UUID)
  participant_name?: string; // resolved display name from persons table
  channel: string;
  message_count: number;
  last_message_at: string; // ISO datetime
  status: "active" | "closed";
}

/** GET /api/conversations response */
export interface ConversationsResponse {
  conversations: Conversation[];
}

/**
 * GET /api/conversations/:id response
 * Backend returns {conversation_id, messages[]} — NOT {conversation, messages[]}.
 * Conversation metadata (participant, channel) comes from the list cache.
 */
export interface ConversationDetailResponse {
  conversation_id: string;
  messages: Message[];
}

// ── Brain ──

/** ConceptCategory enum from sovyx.engine.types */
export type ConceptCategory =
  | "fact"
  | "preference"
  | "entity"
  | "skill"
  | "belief"
  | "event"
  | "relationship";

/** Brain concept node — from _get_concepts() */
export interface BrainNode {
  id: string;
  name: string;
  category: ConceptCategory;
  importance: number; // 0.0-1.0
  confidence: number; // 0.0-1.0
  access_count: number;
}

/** RelationType enum from sovyx.engine.types */
export type RelationType =
  | "related_to"
  | "part_of"
  | "causes"
  | "contradicts"
  | "example_of"
  | "temporal"
  | "emotional";

/** Brain relation link — from _get_relations() */
export interface BrainLink {
  source: string;
  target: string;
  relation_type: RelationType;
  weight: number; // 0.0-1.0
}

/** GET /api/brain/graph response */
export interface BrainGraph {
  nodes: BrainNode[];
  links: BrainLink[];
}

/** Single brain search result — from /api/brain/search */
export interface BrainSearchResult {
  id: string;
  name: string;
  category: ConceptCategory;
  importance: number;
  confidence: number;
  access_count: number;
  score: number;
  match_type?: "text" | "vector";
}

/** GET /api/brain/search response */
export interface BrainSearchResponse {
  results: BrainSearchResult[];
  query: string;
}

// ── Logs ──

/**
 * Log entry from structlog JSON output.
 *
 * Structlog writes JSON lines with these fields:
 * - event: the log message (structlog convention, NOT "message")
 * - level: DEBUG/INFO/WARNING/ERROR/CRITICAL
 * - logger: dotted module path (e.g. "sovyx.brain.service")
 * - timestamp: ISO datetime
 * - Plus the observability envelope (saga_id, cause_id, span_id, …)
 *   added by `EnvelopeProcessor` when the observability subsystem is
 *   active, plus arbitrary extra key-value pairs.
 *
 * All envelope fields are OPTIONAL because the dashboard must keep
 * working against pre-Phase-1 deployments that do not emit them.
 */
export interface LogEntry {
  timestamp: string;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  logger: string;
  event: string;
  // ── Envelope (Phase 1 / Phase 2) ──
  schema_version?: string;
  process_id?: number;
  pid?: number;
  host?: string;
  sovyx_version?: string;
  saga_id?: string | null;
  cause_id?: string | null;
  span_id?: string | null;
  sequence_no?: number;
  // ── Diagnosis (Phase 8.4) ──
  diagnosis_hint?: string;
  diagnosis_severity?: string;
  diagnosis_runbook_url?: string;
  // ── FTS5 search annotations (Phase 10) ──
  snippet?: string | null;
  content?: string;
  message?: string;
  [key: string]: unknown;
}

/** GET /api/logs/search response. */
export interface LogSearchResponse {
  query: string;
  filters: {
    level: string | null;
    logger: string | null;
    saga_id: string | null;
    since: string | null;
    until: string | null;
  };
  count: number;
  entries: LogEntry[];
}

// ── Sagas ──

/**
 * GET /api/logs/sagas/{saga_id} response — every entry tagged with the saga,
 * sorted chronologically.
 */
export interface SagaResponse {
  saga_id: string;
  entries: LogEntry[];
}

/** One node in the saga causality graph. */
export interface CausalityNode {
  id: string | null;
  event: string | null;
  logger: string | null;
  timestamp: string;
  level: string;
}

/** One edge in the saga causality graph (parent pointer). */
export interface CausalityEdge extends CausalityNode {
  cause_id: string | null;
}

/** GET /api/logs/sagas/{saga_id}/causality response. */
export interface CausalityGraphResponse {
  saga_id: string;
  edges: CausalityEdge[];
}

/** One step in the rendered narrative — pre-formatted localized text. */
export interface NarrativeStep {
  timestamp: string;
  text: string;
  event?: string;
}

/** GET /api/logs/sagas/{saga_id}/story response (P8.3). */
export interface NarrativeResponse {
  saga_id: string;
  story: string;
  locale: "pt-BR" | "en-US";
  steps?: NarrativeStep[];
}

// ── Anomalies ──

/** Anomaly types emitted by the AnomalyDetector (Phase 8.1). */
export type AnomalyKind =
  | "anomaly.first_occurrence"
  | "anomaly.latency_spike"
  | "anomaly.error_rate_spike"
  | "anomaly.memory_growth";

/** GET /api/logs/anomalies response — recent anomaly.* events. */
export interface AnomaliesResponse {
  count: number;
  entries: LogEntry[];
}

/** WS /api/logs/stream batch frame. */
export interface LogStreamBatch {
  type: "batch";
  entries: LogEntry[];
}

/** WS /api/logs/stream error frame. */
export interface LogStreamError {
  type: "error";
  message: string;
}

export type LogStreamFrame = LogStreamBatch | LogStreamError;

// ── WebSocket Events ──

/**
 * WebSocket event — from DashboardEventBridge._serialize_event()
 *
 * Event types are Python class names (PascalCase), NOT dot-notation.
 * Payload is in "data" field, NOT "payload".
 */
export type WsEventType =
  | "EngineStarted"
  | "EngineStopping"
  | "ServiceHealthChanged"
  | "PerceptionReceived"
  | "ThinkCompleted"
  | "ResponseSent"
  | "ConceptCreated"
  | "EpisodeEncoded"
  | "ConsolidationCompleted"
  | "DreamCompleted"
  | "ChannelConnected"
  | "ChannelDisconnected"
  | "ChatMessage"
  | "PluginStateChanged"
  | "PluginToolExecuted"
  | "PluginAutoDisabled";

export interface WsEvent<T = Record<string, unknown>> {
  type: WsEventType;
  timestamp: string; // ISO datetime
  correlation_id: string;
  data: T;
}

// ── Activity Timeline ──

/** Entry types from /api/activity/timeline */
export type TimelineEntryType =
  | "conversation"
  | "message"
  | "concepts_learned"
  | "episode_encoded"
  | "consolidation";

/** Single timeline entry from the backend */
export interface TimelineEntry {
  type: TimelineEntryType;
  timestamp: string;
  data: Record<string, unknown>;
}

/** GET /api/activity/timeline response */
export interface TimelineResponse {
  entries: TimelineEntry[];
  meta: {
    hours: number;
    limit: number;
    total_before_limit: number;
    sources: Record<string, number>;
  };
}

/** Cost history entry from /api/status */
export interface CostHistoryEntry {
  time: number;
  cost: number;
  model: string;
  cumulative: number;
}

// ── Usage Stats ──

/** Single day's usage stats — from GET /api/stats/history */
export interface DailyStats {
  date: string;
  cost: number;
  messages: number;
  llm_calls: number;
  tokens: number;
  conversations?: number;
  cost_by_provider?: Record<string, number>;
  cost_by_model?: Record<string, number>;
  is_live?: boolean;
}

/** All-time aggregated totals */
export interface StatsTotals {
  cost: number;
  messages: number;
  llm_calls: number;
  tokens: number;
  days_active: number;
}

/** Monthly aggregated totals */
export interface StatsMonth {
  cost: number;
  messages: number;
  llm_calls: number;
  tokens: number;
}

/** GET /api/stats/history response */
export interface StatsHistoryResponse {
  days: DailyStats[];
  totals: StatsTotals;
  current_month: StatsMonth;
}

// ── Chat ──

/** POST /api/chat response */
export interface ChatResponse {
  response: string;
  conversation_id: string;
  mind_id: string;
  timestamp?: string;
  tags?: string[];
  model?: string;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  latency_ms?: number;
  provider?: string;
}

/** Local chat message for the thread UI */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  mind_id?: string;
  tags?: string[];
  model?: string;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  latency_ms?: number;
  provider?: string;
}

// ── Conversation Imports ──

/** Platform identifier for conversation-import endpoints. */
export type ConversationImportPlatform = "chatgpt" | "claude" | "gemini" | "obsidian" | "grok";

/** Lifecycle state of an import job. Mirrors backend `ImportState` enum. */
export type ConversationImportState =
  | "pending"
  | "parsing"
  | "processing"
  | "completed"
  | "failed";

/**
 * `POST /api/import/conversations` response — the job is running in
 * the background; poll `/api/import/{job_id}/progress`.
 */
export interface StartConversationImportResponse {
  job_id: string;
  platform: ConversationImportPlatform;
  conversations_total: number;
}

/** `GET /api/import/{job_id}/progress` response shape. */
export interface ConversationImportProgress {
  job_id: string;
  platform: ConversationImportPlatform;
  state: ConversationImportState;
  conversations_total: number;
  conversations_processed: number;
  conversations_skipped: number;
  episodes_created: number;
  concepts_learned: number;
  warnings: string[];
  error: string | null;
  elapsed_ms: number;
}

// ── Settings ──

/**
 * GET /api/settings response — from get_settings()
 *
 * Note: mind_name, personality (OCEAN), channels are NOT in settings.
 * They come from /api/status or future dedicated endpoints.
 */
export interface Settings {
  log_level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  log_format: string;
  log_file: string | null;
  data_dir: string;
  telemetry_enabled: boolean;
  api_enabled: boolean;
  api_host: string;
  api_port: number;
  relay_enabled: boolean;
}

// ── Mind Config ──

export type ToneType = "warm" | "neutral" | "direct" | "playful";
export type ContentFilter = "none" | "standard" | "strict";

/** Personality traits (0.0-1.0 range) */
export interface PersonalityConfig {
  tone: ToneType;
  formality: number;
  humor: number;
  assertiveness: number;
  curiosity: number;
  empathy: number;
  verbosity: number;
}

/** Big Five personality model */
export interface OceanConfig {
  openness: number;
  conscientiousness: number;
  extraversion: number;
  agreeableness: number;
  neuroticism: number;
}

/** Severity level of a safety guardrail (mirrors backend Literal). */
export type GuardrailSeverity = "critical" | "warning";

/** Single guardrail entry — shipped with defaults or user-authored. */
export interface Guardrail {
  id: string;
  rule: string;
  severity: GuardrailSeverity;
  builtin: boolean;
}

/**
 * Safety slice of the `/api/config` wire response.
 *
 * Mirrors the JSON shape emitted by `sovyx.dashboard.config.get_config`
 * — NOT the full `sovyx.mind.config.SafetyConfig` Pydantic model. The
 * backend domain model also carries `custom_rules`, `banned_topics`,
 * `shadow_mode` and `shadow_patterns`, but those are intentionally kept
 * out of `/api/config`: `custom_rules` + `banned_topics` live behind
 * `/api/safety/rules`, and `shadow_*` stay internal to the cognitive
 * loop. When a future dashboard feature needs them, add a dedicated
 * type mirroring that endpoint's shape rather than expanding this one.
 */
export interface SafetyConfig {
  child_safe_mode: boolean;
  financial_confirmation: boolean;
  content_filter: ContentFilter;
  pii_protection: boolean;
  guardrails: Guardrail[];
}

/** Brain memory system (read-only in dashboard) */
export interface BrainConfigView {
  consolidation_interval_hours: number;
  dream_time: string;
  max_concepts: number;
  forgetting_enabled: boolean;
  decay_rate: number;
  min_strength: number;
}

/** LLM provider config (read-only in dashboard) */
export interface LLMConfigView {
  default_provider: string;
  default_model: string;
  fast_model: string;
  temperature: number;
  streaming: boolean;
  budget_daily_usd: number;
  budget_per_conversation_usd: number;
}

/** GET /api/config response */
export interface MindConfigResponse {
  name: string;
  id: string;
  language: string;
  timezone: string;
  template: string;
  personality: PersonalityConfig;
  ocean: OceanConfig;
  safety: SafetyConfig;
  brain: BrainConfigView;
  llm: LLMConfigView;
}

/** PUT /api/config request — mutable sections only */
export interface MindConfigUpdate {
  personality?: Partial<PersonalityConfig>;
  ocean?: Partial<OceanConfig>;
  safety?: Partial<SafetyConfig>;
  name?: string;
  language?: string;
  timezone?: string;
}

/** PUT /api/config response */
export interface MindConfigUpdateResponse {
  ok: boolean;
  changes: Record<string, string>;
  error?: string;
}

// ── Plugins ──

/** Permission risk level — from sovyx.plugins.permissions */
export type PermissionRisk = "low" | "medium" | "high";

/** Plugin status states */
export type PluginStatus = "active" | "disabled" | "error";

/** Permission info with risk and description */
export interface PluginPermission {
  permission: string;
  risk: PermissionRisk;
  description: string;
}

/** Plugin health — from PluginManager.get_plugin_health() */
export interface PluginHealth {
  consecutive_failures: number;
  disabled: boolean;
  last_error: string;
  active_tasks: number;
}

/** Plugin tool in list view */
export interface PluginToolSummary {
  name: string;
  description: string;
}

/** Plugin tool in detail view (includes schema) */
export interface PluginToolDetail extends PluginToolSummary {
  parameters: Record<string, unknown>;
  requires_confirmation: boolean;
  timeout_seconds: number;
}

/** Plugin info — from GET /api/plugins list */
export interface PluginInfo {
  name: string;
  version: string;
  description: string;
  status: PluginStatus;
  tools_count: number;
  tools: PluginToolSummary[];
  permissions: PluginPermission[];
  health: PluginHealth;
  category: string;
  tags: string[];
  icon_url: string;
  pricing: string;
  has_setup: boolean;
}

/** Plugin manifest — serialized from backend */
export interface PluginManifestData {
  name: string;
  version: string;
  description: string;
  author: string;
  license: string;
  homepage: string;
  min_sovyx_version: string;
  permissions: string[];
  network: { allowed_domains: string[] };
  depends: Array<{ name: string; version: string }>;
  optional_depends: Array<{ name: string; version: string }>;
  events: {
    emits: Array<{ name: string; description: string }>;
    subscribes: string[];
  };
  tools: Array<{ name: string; description: string }>;
  config_schema: Record<string, unknown>;
  category: string;
  tags: string[];
  icon_url: string;
  screenshots: string[];
  pricing: string;
  price_usd: number | null;
  trial_days: number;
}

/** Plugin detail — from GET /api/plugins/:name */
export interface PluginDetail {
  name: string;
  version: string;
  description: string;
  status: PluginStatus;
  tools: PluginToolDetail[];
  permissions: PluginPermission[];
  health: PluginHealth;
  manifest: PluginManifestData | Record<string, never>;
}

/** GET /api/plugins response */
export interface PluginsResponse {
  available: boolean;
  plugins: PluginInfo[];
  total: number;
  active: number;
  disabled: number;
  error: number;
  total_tools: number;
}

/** GET /api/plugins/tools response */
export interface PluginToolsResponse {
  tools: Array<{
    plugin: string;
    name: string;
    description: string;
  }>;
}

/** POST /api/plugins/:name/enable|disable|reload response */
export interface PluginActionResponse {
  ok: boolean;
  plugin: string;
  status: string;
  error?: string;
}

/** WebSocket plugin events */
export interface PluginStateChangedEvent {
  plugin_name: string;
  from_state: string;
  to_state: string;
}

// ────────────────────────────────────────────────────────────────────────
// Voice device test
// ────────────────────────────────────────────────────────────────────────

/** Machine-readable error taxonomy for /api/voice/test/* endpoints. */
export type VoiceTestErrorCode =
  | "device_not_found"
  | "device_busy"
  | "device_disappeared"
  | "permission_denied"
  | "unsupported_samplerate"
  | "unsupported_channels"
  | "unsupported_format"
  | "buffer_size_invalid"
  | "pipeline_active"
  | "rate_limited"
  | "disabled"
  | "replaced_by_newer_session"
  | "internal_error"
  | "invalid_request"
  | "tts_unavailable"
  | "models_not_downloaded"
  | "job_not_found"
  | "job_expired";

export type VoiceTestFrameType = "level" | "error" | "closed" | "ready";

export type VoiceTestCloseReason =
  | "client_disconnect"
  | "server_shutdown"
  | "device_changed"
  | "session_replaced"
  | "device_error";

/** WebSocket envelope — emitted once per device open. */
export interface VoiceTestReadyFrame {
  v: number;
  t: "ready";
  device_id: number | null;
  device_name: string;
  sample_rate: number;
  channels: number;
}

/** WebSocket envelope — one level-meter tick. */
export interface VoiceTestLevelFrame {
  v: number;
  t: "level";
  rms_db: number;
  peak_db: number;
  hold_db: number;
  clipping: boolean;
  vad_trigger: boolean;
}

/** WebSocket envelope — structured error. */
export interface VoiceTestErrorFrame {
  v: number;
  t: "error";
  code: VoiceTestErrorCode;
  detail: string;
  retryable: boolean;
}

/** WebSocket envelope — always the last frame the server sends. */
export interface VoiceTestClosedFrame {
  v: number;
  t: "closed";
  reason: VoiceTestCloseReason;
}

export type VoiceTestFrame =
  | VoiceTestReadyFrame
  | VoiceTestLevelFrame
  | VoiceTestErrorFrame
  | VoiceTestClosedFrame;

/** One PortAudio device entry. */
export interface VoiceTestDeviceInfo {
  index: number;
  name: string;
  is_default: boolean;
  max_input_channels: number;
  max_output_channels: number;
  default_samplerate: number;
}

/** GET /api/voice/test/devices response. */
export interface VoiceTestDevicesResponse {
  ok: boolean;
  protocol_version: number;
  input_devices: VoiceTestDeviceInfo[];
  output_devices: VoiceTestDeviceInfo[];
}

/** POST /api/voice/test/output request body. */
export interface VoiceTestOutputRequest {
  device_id?: number | null;
  voice?: string | null;
  phrase_key?: string;
  language?: string;
}

/** POST /api/voice/test/output response. */
export interface VoiceTestOutputJob {
  ok: boolean;
  job_id: string;
  status: string;
}

/** GET /api/voice/test/output/:job_id response. */
export interface VoiceTestOutputResult {
  ok: boolean;
  job_id: string;
  status: string;
  code?: VoiceTestErrorCode | null;
  detail?: string | null;
  phrase?: string | null;
  synthesis_ms?: number | null;
  playback_ms?: number | null;
  peak_db?: number | null;
}

/** Shared HTTP error envelope. */
export interface VoiceTestErrorResponse {
  ok: false;
  code: VoiceTestErrorCode;
  detail: string;
  /** Registry names the UI should offer for download. Only set when
   * ``code === "models_not_downloaded"``. */
  missing_models?: string[] | null;
}

// ────────────────────────────────────────────────────────────────────────
// Voice models — disk status + background download
// ────────────────────────────────────────────────────────────────────────

/** One model's on-disk presence as reported by /api/voice/models/status. */
export interface VoiceModelDiskStatus {
  name: string;
  category: string;
  description: string;
  installed: boolean;
  path: string;
  size_mb: number;
  expected_size_mb: number;
  download_available: boolean;
}

export interface VoiceModelsStatusResponse {
  model_dir: string;
  all_installed: boolean;
  missing_count: number;
  missing_download_mb: number;
  models: VoiceModelDiskStatus[];
}

export type VoiceModelDownloadStatus = "running" | "done" | "error";

export type VoiceModelDownloadErrorCode =
  | "cooldown"
  | "all_mirrors_exhausted"
  | "checksum_mismatch"
  | "network"
  | "unknown";

export interface VoiceModelDownloadProgress {
  task_id: string;
  status: VoiceModelDownloadStatus;
  total_models: number;
  completed_models: number;
  current_model: string | null;
  error: string | null;
  error_code?: VoiceModelDownloadErrorCode | null;
  retry_after_seconds?: number | null;
}

export interface VoiceCatalogEntry {
  id: string;
  display_name: string;
  language: string;
  gender: "female" | "male";
}

export interface VoiceCatalogResponse {
  supported_languages: string[];
  by_language: Record<string, VoiceCatalogEntry[]>;
  recommended_per_language: Record<string, string>;
}

// ── Voice capture APO diagnostics ──

export interface CaptureApoEndpoint {
  endpoint_id: string;
  endpoint_name: string;
  enumerator: string;
  fx_binding_count: number;
  known_apos: string[];
  raw_clsids: string[];
  voice_clarity_active: boolean;
  is_active_device: boolean;
}

export interface CaptureDiagnosticsResponse {
  platform_supported: boolean;
  active_device_name: string | null;
  active_endpoint: {
    endpoint_id: string;
    endpoint_name: string;
    known_apos: string[];
    voice_clarity_active: boolean;
  } | null;
  voice_clarity_active: boolean;
  any_voice_clarity_active: boolean;
  endpoints: CaptureApoEndpoint[];
  fix_suggestion: string | null;
  error?: string;
}

export interface CaptureExclusiveResponse {
  ok: boolean;
  enabled: boolean;
  persisted: boolean;
  applied_immediately: boolean;
}

// ── Linux ALSA mixer diagnostics + remediation ──

export interface LinuxMixerControl {
  name: string;
  min_raw: number;
  max_raw: number;
  current_raw: number;
  current_db: number | null;
  max_db: number | null;
  is_boost_control: boolean;
  saturation_risk: boolean;
  asymmetric: boolean;
}

export interface LinuxMixerCard {
  card_index: number;
  card_id: string;
  card_longname: string;
  aggregated_boost_db: number;
  saturation_warning: boolean;
  controls: LinuxMixerControl[];
}

export interface LinuxMixerDiagnosticsResponse {
  platform_supported: boolean;
  amixer_available: boolean;
  snapshots: LinuxMixerCard[];
  aggregated_boost_db_ceiling: number;
  saturation_ratio_ceiling: number;
  reset_enabled_by_default: boolean;
}

export interface LinuxMixerResetResponse {
  ok: boolean;
  reason?: string;
  reason_code?: string;
  detail?: string;
  card_index?: number;
  card_id?: string;
  card_longname?: string;
  candidate_card_indexes?: number[];
  applied_controls?: [string, number][];
  reverted_controls?: [string, number][];
}

/**
 * v1.3 §4.6 L6 — boot preflight warning carried on
 * ``GET /api/voice/status.preflight_warnings`` and via the
 * WebSocket ``voice_preflight_warning`` event. Schema mirrors
 * ``BootPreflightWarningsStore.snapshot()`` on the backend.
 */
export interface PreflightWarning {
  code: string;
  severity?: string;
  hint?: string;
  details?: Record<string, unknown>;
}

// ────────────────────────────────────────────────────────────────────────
// Voice Capture Health Lifecycle (VCHL) — L7 REST surface (ADR §4.7)
// ────────────────────────────────────────────────────────────────────────

/** Triage labels emitted by ``sovyx.voice.health.probe``. */
export type VoiceHealthDiagnosis =
  | "healthy"
  | "muted"
  | "no_signal"
  | "low_signal"
  | "format_mismatch"
  | "apo_degraded"
  | "vad_insensitive"
  | "driver_error"
  | "device_busy"
  | "exclusive_mode_not_available"
  | "insufficient_buffer_size"
  | "invalid_sample_rate_no_auto_convert"
  | "permission_denied"
  | "permission_revoked_runtime"
  | "kernel_invalidated"
  | "stream_open_timeout"
  | "heartbeat_timeout"
  | "unknown";

export type VoiceHealthProbeMode = "cold" | "warm";

/** T6.20 — closed enum for ``GET /api/voice/service-health`` ``reason`` field.
 *
 * Stable codes for monitoring tooling (Prometheus / cron / external
 * health checks). Adding new values is fine; renaming or repurposing
 * is a breaking change to monitoring contracts.
 */
export type VoiceServiceHealthReason =
  | "ok"
  | "voice_disabled"
  | "engine_not_running"
  | "voice_pipeline_not_registered"
  | "last_diagnosis_unhealthy";

/** T6.20 — aggregated readiness snapshot for monitoring.
 *
 * ``user_remediation`` (T6.12) is the operator-facing hint string
 * mapped from ``last_diagnosis``. ``null`` when the diagnosis has no
 * actionable hint (``healthy`` / ``unknown`` / mixer-sanity family)
 * or when no diagnosis is yet stored.
 */
export interface VoiceServiceHealthResponse {
  ready: boolean;
  reason: VoiceServiceHealthReason;
  last_diagnosis: string | null;
  watchdog_state: string | null;
  user_remediation: string | null;
}

export type VoiceHealthRemediationSeverity = "info" | "warn" | "error";

export type VoiceHealthPinSource = "user" | "wizard" | "cli";

/** Audio configuration tuple that opens a capture stream (wire shape). */
export interface VoiceHealthCombo {
  host_api: string;
  sample_rate: number;
  channels: number;
  sample_format: string;
  exclusive: boolean;
  auto_convert: boolean;
  frames_per_buffer: number;
}

export interface VoiceHealthRemediationHint {
  code: string;
  severity: VoiceHealthRemediationSeverity;
  cli_action: string | null;
}

export interface VoiceHealthProbeHistoryEntry {
  ts: string;
  mode: string;
  diagnosis: string;
  vad_max_prob: number | null;
  rms_db: number;
  duration_ms: number;
}

export interface VoiceHealthProbeResult {
  diagnosis: string;
  mode: string;
  combo: VoiceHealthCombo;
  vad_max_prob: number | null;
  vad_mean_prob: number | null;
  rms_db: number;
  callbacks_fired: number;
  duration_ms: number;
  error: string | null;
  remediation: VoiceHealthRemediationHint | null;
}

export interface VoiceHealthComboEntry {
  endpoint_guid: string;
  device_friendly_name: string;
  device_interface_name: string;
  device_class: string;
  endpoint_fxproperties_sha: string;
  winning_combo: VoiceHealthCombo;
  validated_at: string;
  validation_mode: string;
  vad_max_prob_at_validation: number | null;
  vad_mean_prob_at_validation: number | null;
  rms_db_at_validation: number;
  probe_duration_ms: number;
  detected_apos_at_validation: string[];
  cascade_attempts_before_success: number;
  boots_validated: number;
  last_boot_validated: string;
  last_boot_diagnosis: string;
  probe_history: VoiceHealthProbeHistoryEntry[];
  pinned: boolean;
  needs_revalidation: boolean;
}

export interface VoiceHealthOverrideEntry {
  endpoint_guid: string;
  device_friendly_name: string;
  pinned_combo: VoiceHealthCombo;
  pinned_at: string;
  pinned_by: string;
  reason: string;
}

/** GET /api/voice/health response. */
export interface VoiceHealthSnapshotResponse {
  combo_store: VoiceHealthComboEntry[];
  overrides: VoiceHealthOverrideEntry[];
  quarantine_count: number;
  data_dir: string;
  voice_enabled: boolean;
}

/**
 * One endpoint in the §4.4.7 kernel-invalidated quarantine.
 *
 * Mirrors `sovyx.voice.health._quarantine.QuarantineEntry` + the derived
 * `seconds_until_expiry` field computed on read.
 */
export interface VoiceHealthQuarantineEntry {
  endpoint_guid: string;
  device_friendly_name: string;
  device_interface_name: string;
  host_api: string;
  added_at_monotonic: number;
  expires_at_monotonic: number;
  seconds_until_expiry: number;
  reason: string;
}

/** GET /api/voice/health/quarantine response. */
export interface VoiceHealthQuarantineSnapshotResponse {
  entries: VoiceHealthQuarantineEntry[];
  count: number;
}

// ── Mixer KB (Sprint 4 dashboard workflow) ─────────────────────────────
//
// Mirrors the Pydantic response models in
// ``src/sovyx/dashboard/routes/voice_kb.py``. ``pool`` distinguishes
// shipped (bundled with the Sovyx wheel, HIL-validated) from user
// (``~/.sovyx/mixer_kb/user/``, community-contributed, unsigned).

/** Compact profile identity for list responses. */
export interface MixerKbProfileSummary {
  pool: string;
  profile_id: string;
  profile_version: number;
  schema_version: number;
  driver_family: string;
  codec_id_glob: string;
  match_threshold: number;
  factory_regime: string;
  contributed_by: string;
}

/** Full profile surface — superset of :class:`MixerKbProfileSummary`. */
export interface MixerKbProfileDetail extends MixerKbProfileSummary {
  system_vendor_glob: string | null;
  system_product_glob: string | null;
  distro_family: string | null;
  audio_stack: string | null;
  kernel_major_minor_glob: string | null;
  factory_signature_roles: string[];
  verified_on_count: number;
}

/** GET /api/voice/health/kb/profiles response. */
export interface MixerKbListResponse {
  profiles: MixerKbProfileSummary[];
  shipped_count: number;
  user_count: number;
}

/** One validation issue — flat shape mirrors pydantic's error list. */
export interface MixerKbValidationIssue {
  loc: string;
  msg: string;
}

/** POST /api/voice/health/kb/validate request body. */
export interface MixerKbValidateRequest {
  yaml_body: string;
  filename_stem?: string | null;
}

/** POST /api/voice/health/kb/validate response. */
export interface MixerKbValidateResponse {
  ok: boolean;
  profile_id: string | null;
  profile_version: number | null;
  issues: MixerKbValidationIssue[];
}

/** POST /api/voice/health/reprobe request body. */
export interface VoiceHealthReprobeRequest {
  endpoint_guid: string;
  /** Optional — backend resolves from endpoint_guid when omitted. */
  device_index?: number;
  mode: VoiceHealthProbeMode;
  combo?: VoiceHealthCombo;
  duration_ms?: number;
}

export interface VoiceHealthReprobeResponse {
  endpoint_guid: string;
  result: VoiceHealthProbeResult;
}

export interface VoiceHealthForgetRequest {
  endpoint_guid: string;
  reason: string;
}

export interface VoiceHealthForgetResponse {
  endpoint_guid: string;
  invalidated: boolean;
}

export interface VoiceHealthPinRequest {
  endpoint_guid: string;
  device_friendly_name: string;
  combo: VoiceHealthCombo;
  source: VoiceHealthPinSource;
  reason?: string;
}

export interface VoiceHealthPinResponse {
  endpoint_guid: string;
  pinned: boolean;
}

/* ── Platform Diagnostics — GET /api/voice/platform-diagnostics ──
 *
 * Cross-OS diagnostic surface aggregating MA1/MA2/MA5/MA6 (macOS),
 * F3/F4 (Linux), WI1/WI2 (Windows) and the cross-platform mic
 * permission probe into one auth-protected payload.
 *
 * Per-OS branches are nullable — only the branch matching the host
 * `platform` is populated; the others arrive as `null`. Operators
 * should switch on `platform` before reading branch-specific fields.
 */

export type MicPermissionStatus = "granted" | "denied" | "unknown";

export interface PlatformMicPermissionPayload {
  status: MicPermissionStatus;
  machine_value: string | null;
  user_value: string | null;
  notes: string[];
  remediation_hint: string;
}

export type PipeWireStatusToken =
  | "active"
  | "absent"
  | "degraded"
  | "unknown";

export interface PipeWirePayload {
  status: PipeWireStatusToken;
  socket_present: boolean;
  pactl_available: boolean;
  pactl_info_ok: boolean;
  server_name: string | null;
  modules_loaded: string[];
  echo_cancel_loaded: boolean;
  notes: string[];
}

export type UcmStatusToken =
  | "available"
  | "absent"
  | "no_ucm_for_card"
  | "unknown";

export interface UcmPayload {
  status: UcmStatusToken;
  card_id: string;
  alsaucm_available: boolean;
  verbs: string[];
  active_verb: string | null;
  notes: string[];
}

export type WindowsServiceStateToken =
  | "running"
  | "stopped"
  | "start_pending"
  | "stop_pending"
  | "paused"
  | "unknown"
  | "not_found";

export interface WindowsServicePayload {
  name: string;
  state: WindowsServiceStateToken;
  raw_state: string;
  notes: string[];
}

export interface WindowsAudioServicePayload {
  audiosrv: WindowsServicePayload;
  audio_endpoint_builder: WindowsServicePayload;
  all_healthy: boolean;
  degraded_services: string[];
}

export type EtwEventLevelToken =
  | "critical"
  | "error"
  | "warning"
  | "information"
  | "verbose"
  | "unknown";

export interface EtwEventPayload {
  channel: string;
  level: EtwEventLevelToken;
  event_id: number;
  timestamp_iso: string;
  provider: string;
  description: string;
}

export interface EtwChannelPayload {
  channel: string;
  events: EtwEventPayload[];
  lookback_seconds: number;
  notes: string[];
}

export type HalPluginCategoryToken =
  | "virtual_audio"
  | "audio_enhancement"
  | "vendor"
  | "unknown";

export interface HalPluginPayload {
  bundle_name: string;
  path: string;
  category: HalPluginCategoryToken;
  friendly_label: string;
}

export interface HalPayload {
  plugins: HalPluginPayload[];
  notes: string[];
  virtual_audio_active: boolean;
  audio_enhancement_active: boolean;
}

export type BluetoothAudioProfileToken =
  | "a2dp"
  | "hfp"
  | "unknown";

export interface BluetoothDevicePayload {
  name: string;
  address: string;
  profile: BluetoothAudioProfileToken;
  is_input_capable: boolean;
  is_output_capable: boolean;
}

export interface BluetoothPayload {
  devices: BluetoothDevicePayload[];
  notes: string[];
}

export type EntitlementVerdictToken =
  | "present"
  | "absent"
  | "unsigned"
  | "unknown";

export interface CodeSigningPayload {
  verdict: EntitlementVerdictToken;
  executable_path: string;
  notes: string[];
  remediation_hint: string;
}

export interface PlatformLinuxBranch {
  pipewire: PipeWirePayload;
  alsa_ucm: UcmPayload;
}

export interface PlatformWindowsBranch {
  audio_service: WindowsAudioServicePayload;
  etw_audio_events: EtwChannelPayload[];
}

export interface PlatformMacOSBranch {
  hal_plugins: HalPayload;
  bluetooth: BluetoothPayload;
  code_signing: CodeSigningPayload;
}

export type PlatformToken = "linux" | "win32" | "darwin" | "other";

export interface PlatformDiagnosticsResponse {
  platform: PlatformToken;
  mic_permission: PlatformMicPermissionPayload;
  linux: PlatformLinuxBranch | null;
  windows: PlatformWindowsBranch | null;
  macos: PlatformMacOSBranch | null;
}

// ── Voice Windows Paranoid Mission (v0.24.0 → v0.26.0) ──────────────
// Compile-time TypeScript types matching the zod schemas in
// schemas.ts. Both files MUST stay in sync — the zod schema is the
// runtime validator, this file is the editor / IDE surface.

export type CaptureRestartReason =
  | "device_changed"
  | "apo_degraded"
  | "overflow"
  | "manual";

export interface CaptureRestartFrame {
  // PipelineFrame base fields:
  frame_type: "CaptureRestart";
  timestamp_monotonic: number;
  utterance_id?: string;
  // CaptureRestart payload (all optional v0.24.0):
  restart_reason?: CaptureRestartReason | string;
  old_host_api?: string;
  new_host_api?: string;
  old_device_id?: string;
  new_device_id?: string;
  old_signal_processing_mode?: string;
  new_signal_processing_mode?: string;
  recovery_latency_ms?: number;
  /** 0 when not bypass-related; 1 / 2 / 3 for Tier 1 RAW /
   *  Tier 2 host_api_rotate / Tier 3 WASAPI exclusive. */
  bypass_tier?: number;
}

export interface VoiceRestartHistoryResponse {
  frames: CaptureRestartFrame[];
  limit?: number;
  total?: number;
}

export interface VoiceBypassTierStatusResponse {
  current_bypass_tier?: number | null;
  tier1_raw_attempted: number;
  tier1_raw_succeeded: number;
  tier2_host_api_rotate_attempted: number;
  tier2_host_api_rotate_succeeded: number;
  tier3_wasapi_exclusive_attempted: number;
  tier3_wasapi_exclusive_succeeded: number;
}

/** Phase 4 / T4.26 + T4.37 — quality observables snapshot. */
export type VoiceQualityVerdict =
  | "excellent"
  | "good"
  | "degraded"
  | "poor"
  | "no_signal";

export interface VoiceNoiseFloorBlock {
  short_avg_db: number | null;
  long_avg_db: number | null;
  drift_db: number | null;
  ready: boolean;
  short_sample_count: number;
  long_sample_count: number;
}

export interface VoiceAgc2Block {
  frames_processed: number;
  frames_silenced: number;
  frames_vad_silenced: number;
  current_gain_db: number;
  speech_level_dbfs: number;
}

export interface VoiceQualitySnapshotResponse {
  snr_p50_db: number | null;
  snr_sample_count: number;
  snr_verdict: VoiceQualityVerdict;
  noise_floor: VoiceNoiseFloorBlock;
  agc2: VoiceAgc2Block | null;
  dnsmos_extras_installed: boolean;
}

/* ── Mind management — POST /api/mind/{mind_id}/forget ──
 *
 * Phase 8 / T8.21 step 5. Right-to-erasure for a single mind
 * (GDPR Art. 17 / LGPD Art. 18 VI). Wipes every per-mind row
 * across the brain DB, conversations DB, system DB, and the
 * voice consent ledger. Mind configuration is preserved.
 *
 * Defense-in-depth: ``confirm`` field MUST equal ``mind_id``
 * exactly (GitHub-style "type the name to delete" pattern).
 */

export interface ForgetMindRequest {
  /** The exact mind_id, typed verbatim by the operator. */
  confirm: string;
  /** When true, returns counts without writing. */
  dry_run?: boolean;
}

export interface ForgetMindResponse {
  mind_id: string;
  concepts_purged: number;
  relations_purged: number;
  episodes_purged: number;
  concept_embeddings_purged: number;
  episode_embeddings_purged: number;
  conversation_imports_purged: number;
  consolidation_log_purged: number;
  conversations_purged: number;
  conversation_turns_purged: number;
  daily_stats_purged: number;
  consent_ledger_purged: number;
  total_brain_rows_purged: number;
  total_conversations_rows_purged: number;
  total_system_rows_purged: number;
  total_rows_purged: number;
  dry_run: boolean;
}

/* ── Mind retention — POST /api/mind/{mind_id}/retention/prune ──
 *
 * Phase 8 / T8.21 step 6. Time-based per-mind prune (GDPR Art.
 * 5(1)(e) "storage limitation" / LGPD Art. 16). Removes only
 * records older than per-surface horizons; tombstone is
 * RETENTION_PURGE not DELETE so external auditors can
 * distinguish scheduled-policy purges from operator-invoked
 * erasures.
 *
 * No ``confirm`` field required — retention is less destructive
 * than forget; it removes only AGED records, not arbitrary rows.
 */

export interface PruneRetentionRequest {
  /** When true, returns counts without writing. Default false. */
  dry_run?: boolean;
}

export interface PruneRetentionResponse {
  mind_id: string;
  /** ISO-8601 UTC cutoff timestamp at the moment of the prune. */
  cutoff_utc: string;
  episodes_purged: number;
  conversations_purged: number;
  conversation_turns_purged: number;
  daily_stats_purged: number;
  consolidation_log_purged: number;
  consent_ledger_purged: number;
  /**
   * Per-surface horizon (days) actually applied. ``0`` means the
   * surface was skipped (retention disabled for it).
   */
  effective_horizons: Record<string, number>;
  total_brain_rows_purged: number;
  total_conversations_rows_purged: number;
  total_system_rows_purged: number;
  total_rows_purged: number;
  dry_run: boolean;
}

/* ── Mind wake-word toggle — POST /api/mind/{mind_id}/wake-word/toggle ──
 *
 * Mission ``MISSION-wake-word-runtime-wireup-2026-05-03.md`` §T3
 * shipped the endpoint in v0.28.2. Mission
 * ``MISSION-pre-wake-word-ui-hardening-2026-05-03.md`` §T1 added
 * pre-validate semantics for the v0.28.3 patch (refuse-to-persist
 * on NONE strategy → HTTP 422). T4 surfaces these types so dashboard
 * callers get compile-time safety + zod runtime validation (paired
 * schemas in ``schemas.ts``).
 *
 * Three-phase backend contract:
 *   1. Pre-validate (only when enabled=true): resolve the wake-word
 *      ONNX before persist. Returns 422 + remediation on NONE.
 *   2. Persist ``wake_word_enabled`` to ``mind.yaml`` atomically.
 *   3. Hot-apply on the live pipeline — best-effort. When the voice
 *      subsystem isn't running, ``applied_immediately=false`` and
 *      ``hot_apply_detail`` carries the cold-start reason.
 *
 * The next pipeline boot picks up the persisted YAML automatically.
 */

export interface WakeWordToggleRequest {
  /** Whether this mind requires the wake word before voice input is processed. */
  enabled: boolean;
}

export interface WakeWordToggleResponse {
  mind_id: string;
  enabled: boolean;
  /**
   * True when ``mind.yaml`` was successfully updated. Distinct from
   * ``applied_immediately``: persist is durable; hot-apply is
   * runtime-only.
   */
  persisted: boolean;
  /**
   * True when the live pipeline accepted the change (register or
   * unregister succeeded). False when (a) voice subsystem isn't
   * running yet — next boot picks up the YAML — or (b) single-mind
   * mode with no router. See ``hot_apply_detail`` when false.
   */
  applied_immediately: boolean;
  /**
   * Free-form diagnostic when ``applied_immediately`` is false.
   * Null on the happy path. Surface this directly to operators when
   * non-null — the backend produces actionable remediation text
   * (e.g. "voice subsystem not running — change persisted; will
   * apply on next boot").
   */
  hot_apply_detail: string | null;
}

/* ── Per-mind wake-word status — GET /api/voice/wake-word/status ──
 *
 * Mission ``MISSION-wake-word-ui-2026-05-03.md`` §T1+T2 (D1+D2). The
 * dashboard's per-mind wake-word section consumes this to render
 * one card per mind with toggle + status pill + error disclosure.
 *
 * Response is flat: ``{minds: [...]}``. Empty list when (a) the
 * daemon is still booting, (b) no minds are on disk. Non-empty list
 * always includes ALL minds (enabled + disabled) so operators can
 * toggle ON disabled minds without leaving the page.
 *
 * Field semantics — see backend dataclass at
 * ``voice/factory/_wake_word_wire_up.py::WakeWordPerMindStatusEntry``.
 * Strict 1:1 mirror of the pydantic model in
 * ``dashboard/routes/voice.py::WakeWordPerMindStatusItem``.
 */

export type WakeWordResolutionStrategy = "exact" | "phonetic" | "none";

export interface WakeWordPerMindStatus {
  mind_id: string;
  wake_word: string;
  voice_language: string;
  /**
   * What ``mind.yaml`` says (operator's persisted intent). Distinct
   * from ``runtime_registered`` — a mind can be configured but not
   * registered when the v0.28.3 T2 boot tolerance caught a stale-
   * config error and degraded to ``router=None``.
   */
  wake_word_enabled: boolean;
  /**
   * Whether a detector for this mind is currently in the live
   * ``WakeWordRouter``. Always false when the voice subsystem isn't
   * running OR when the boot tolerance degraded the router.
   */
  runtime_registered: boolean;
  /**
   * Resolved ``.onnx`` path on EXACT/PHONETIC strategy. Null on NONE
   * strategy or when ``wake_word_enabled`` is false (resolution
   * skipped to save ~5ms cost).
   */
  model_path: string | null;
  /**
   * Discriminated union over the three resolution strategies. Null
   * when ``wake_word_enabled`` is false (resolution skipped).
   */
  resolution_strategy: WakeWordResolutionStrategy | null;
  /**
   * Registry name that matched. For EXACT, the ASCII-folded wake
   * word (typically same as the file stem in lowercase). For
   * PHONETIC, the actual matched-file name (e.g., ``"lucia"`` for a
   * wake_word ``"Lúcia"``). Null on NONE strategy or when
   * resolution was skipped (disabled mind). Mission
   * ``MISSION-v0.29.1-tightening-2026-05-03.md`` §T1: surfaces the
   * resolver's matched-name signal so operators can see WHICH file
   * matched their diacritic / phonetic wake word.
   */
  matched_name: string | null;
  /**
   * Levenshtein-on-phonemes distance for PHONETIC matches. ``0``
   * for EXACT (no phonetic step ran). Null on NONE strategy or
   * when resolution was skipped. The backend converts the
   * resolver's ``-1`` sentinel to null at the boundary so this
   * field only carries non-negative ``number | null``.
   */
  phoneme_distance: number | null;
  /**
   * Operator-facing remediation text when ``resolution_strategy ===
   * "none"``. Null on the happy path. Surface directly to operators
   * via the dashboard error-details disclosure.
   */
  last_error: string | null;
}

export interface WakeWordPerMindStatusResponse {
  minds: WakeWordPerMindStatus[];
}

/* ── Voice setup wizard — Phase 7 / T7.21-T7.24 ──────────────
 *
 *   GET  /api/voice/wizard/devices
 *   POST /api/voice/wizard/test-record
 *   GET  /api/voice/wizard/test-result/{session_id}
 *   GET  /api/voice/wizard/diagnostic
 *
 * Frontend wizard component (T7.25-T7.30) consumes these. The
 * backend ships independently of the frontend per
 * ``feedback_staged_adoption`` — frontend is operator-pilot
 * gated.
 */

export interface WizardDeviceInfo {
  device_id: string;
  name: string;
  friendly_name: string;
  max_input_channels: number;
  default_sample_rate: number;
  is_default: boolean;
  /** ``ready`` | ``warning_low_channels`` | ``warning_high_sample_rate`` | ``error_unavailable`` */
  diagnosis_hint: string;
}

export interface WizardDevicesResponse {
  devices: WizardDeviceInfo[];
  total_count: number;
  default_device_id: string | null;
}

export interface WizardTestRecordRequest {
  device_id?: string | null;
  /** Bounded [1.0, 10.0] s; default 3.0. */
  duration_seconds?: number;
}

export interface WizardTestResultResponse {
  session_id: string;
  success: boolean;
  duration_actual_s: number;
  sample_rate_hz: number;
  level_rms_dbfs: number | null;
  level_peak_dbfs: number | null;
  snr_db: number | null;
  clipping_detected: boolean;
  silent_capture: boolean;
  /** Closed-set: ``ok`` | ``low_signal`` | ``clipping`` | ``no_audio`` | ``noisy`` | ``device_error``. */
  diagnosis: string;
  diagnosis_hint: string;
  recorded_at_utc: string;
  error: string | null;
}

export interface WizardDiagnosticResponse {
  ready: boolean;
  voice_clarity_active: boolean;
  active_device_name: string | null;
  /** ``win32`` | ``linux`` | ``darwin``. */
  platform: string;
  recommendations: string[];
}

/* ── Wake-word training — Phase 8 / T8.13 ──────────────
 *
 *   GET  /api/voice/training/jobs           — list all jobs
 *   GET  /api/voice/training/jobs/{job_id}  — detail + history
 *   POST /api/voice/training/jobs/{job_id}/cancel — touch .cancel
 *
 * Read-only observability + cancellation surface. Job creation
 * happens via the ``sovyx voice train-wake-word`` CLI — training
 * takes 30-60 minutes, blocking a dashboard request for that
 * long isn't tractable.
 */

export type TrainingJobStatus =
  | "pending"
  | "synthesizing"
  | "training"
  | "complete"
  | "failed"
  | "cancelled";

export interface TrainingJobSummary {
  job_id: string;
  wake_word: string;
  mind_id: string;
  language: string;
  status: TrainingJobStatus;
  /** 0.0 to 1.0 fractional progress within the current status phase. */
  progress: number;
  samples_generated: number;
  target_samples: number;
  started_at: string;
  updated_at: string;
  completed_at: string;
  output_path: string;
  error_summary: string;
  /** True when ``<job_dir>/.cancel`` exists. */
  cancelled_signalled: boolean;
}

export interface TrainingJobsResponse {
  jobs: TrainingJobSummary[];
  total_count: number;
}

export interface TrainingJobDetailResponse {
  summary: TrainingJobSummary;
  /** Most-recent ``limit`` progress events, oldest-first. */
  history: Record<string, string | number>[];
  history_truncated: boolean;
}

export interface CancelJobResponse {
  job_id: string;
  cancel_signal_written: boolean;
  already_terminal: boolean;
}
