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
 * - Plus arbitrary extra key-value pairs
 */
export interface LogEntry {
  timestamp: string;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  logger: string; // module path (structlog uses "logger", not "module")
  event: string; // message text (structlog uses "event", not "message")
  [key: string]: unknown; // extra structured fields
}

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

export interface VoiceModelDownloadProgress {
  task_id: string;
  status: VoiceModelDownloadStatus;
  total_models: number;
  completed_models: number;
  current_model: string | null;
  error: string | null;
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
