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
  participant: string; // person_id
  channel: string;
  message_count: number;
  last_message_at: string; // ISO datetime
  status: "active" | "closed";
}

/** GET /api/conversations response */
export interface ConversationsResponse {
  conversations: Conversation[];
}

/** GET /api/conversations/:id response */
export interface ConversationDetailResponse {
  conversation: Conversation;
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
  | "ChannelDisconnected";

export interface WsEvent<T = Record<string, unknown>> {
  type: WsEventType;
  timestamp: string; // ISO datetime
  correlation_id: string;
  data: T;
}

/** Typed payloads for specific events */
export interface ThinkCompletedData {
  tokens_in: number;
  tokens_out: number;
  model: string;
  cost_usd: number;
  latency_ms: number;
}

export interface ConceptCreatedData {
  concept_id: string;
  title: string;
  source: string;
}

export interface PerceptionReceivedData {
  source: string;
  person_id: string;
}

export interface ResponseSentData {
  channel: string;
  latency_ms: number;
}

export interface ServiceHealthChangedData {
  service: string;
  status: string;
}

export interface ConsolidationCompletedData {
  merged: number;
  pruned: number;
  strengthened: number;
  duration_s: number;
}

export interface ChannelEventData {
  channel_type: string;
  reason?: string; // only in ChannelDisconnected
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

/** PUT /api/settings request — only mutable fields */
export interface SettingsUpdate {
  log_level?: "DEBUG" | "INFO" | "WARNING" | "ERROR";
}

/** PUT /api/settings response */
export interface SettingsUpdateResponse {
  changes: Record<string, string>; // "field": "old → new"
}
