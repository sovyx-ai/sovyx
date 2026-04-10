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
  | "ChannelConnected"
  | "ChannelDisconnected"
  | "ChatMessage";

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
}

/** Local chat message for the thread UI */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  mind_id?: string;
}

// ── Channels ──

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

/** Safety guardrails */
export interface SafetyConfig {
  child_safe_mode: boolean;
  financial_confirmation: boolean;
  content_filter: ContentFilter;
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
