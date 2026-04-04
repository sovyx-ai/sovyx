/**
 * Sovyx Dashboard — API types
 * Mirrors the FastAPI response schemas.
 */

// ── Health ──

export type HealthStatus = "GREEN" | "YELLOW" | "RED";

export interface HealthCheck {
  name: string;
  status: HealthStatus;
  message: string;
  latency_ms?: number;
}

// ── Status ──

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

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  channel: string;
  timestamp: string;
}

export interface Conversation {
  id: string;
  participant: string;
  channel: string;
  message_count: number;
  last_message_at: string;
  messages?: Message[];
}

// ── Brain ──

export interface BrainNode {
  id: string;
  label: string;
  category: "semantic" | "episodic" | "procedural";
  strength: number;
  last_accessed: string;
}

export interface BrainEdge {
  source: string;
  target: string;
  weight: number;
}

export interface BrainGraph {
  nodes: BrainNode[];
  edges: BrainEdge[];
}

// ── Logs ──

export interface LogEntry {
  timestamp: string;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  module: string;
  message: string;
  metadata?: Record<string, unknown>;
}

// ── WebSocket Events ──

export type WsEventType =
  | "conversation.message"
  | "cognitive.transition"
  | "brain.concept_created"
  | "health.alert"
  | "llm.response"
  | "log.entry";

export interface WsEvent<T = unknown> {
  type: WsEventType;
  timestamp: string;
  payload: T;
}

// ── Settings ──

export interface OceanPersonality {
  openness: number;
  conscientiousness: number;
  extraversion: number;
  agreeableness: number;
  neuroticism: number;
}

export interface ChannelConfig {
  name: string;
  connected: boolean;
  details?: string;
}

export interface Settings {
  mind_name: string;
  log_level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  data_dir: string;
  personality: OceanPersonality;
  channels: ChannelConfig[];
}
