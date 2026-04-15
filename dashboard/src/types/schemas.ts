/**
 * Sovyx Dashboard — runtime zod schemas mirroring api.ts.
 *
 * These schemas validate server responses at the edge in lib/api.ts.
 * On schema drift the validator logs a warning (safeParse + warn) but
 * still returns the payload — the backend is the source of truth and we
 * prefer resilience over hard-fail in production. Useful to catch
 * silent contract breaks in staging.
 *
 * When you add / change a type in `api.ts`, add / change its schema here.
 */

import { z } from "zod";

// ── Primitives ──

export const HealthStatusSchema = z.enum(["green", "yellow", "red"]);
export const RelationTypeSchema = z.enum([
  "related_to",
  "part_of",
  "causes",
  "contradicts",
  "example_of",
  "temporal",
  "emotional",
]);
export const ConceptCategorySchema = z.enum([
  "fact",
  "preference",
  "entity",
  "skill",
  "belief",
  "event",
  "relationship",
]);
export const LogLevelSchema = z.enum([
  "DEBUG",
  "INFO",
  "WARNING",
  "ERROR",
  "CRITICAL",
]);
export const TimelineEntryTypeSchema = z.enum([
  "conversation",
  "message",
  "concepts_learned",
  "episode_encoded",
  "consolidation",
]);
export const PermissionRiskSchema = z.enum(["low", "medium", "high"]);
export const PluginStatusSchema = z.enum(["active", "disabled", "error"]);

// ── Health ──

export const HealthCheckSchema = z.object({
  name: z.string(),
  status: HealthStatusSchema,
  message: z.string(),
  latency_ms: z.number().optional(),
});

export const HealthResponseSchema = z.object({
  overall: HealthStatusSchema,
  checks: z.array(HealthCheckSchema),
});

// ── Status ──

export const CostHistoryEntrySchema = z.object({
  time: z.number(),
  cost: z.number(),
  model: z.string(),
  cumulative: z.number(),
});

export const SystemStatusSchema = z.object({
  version: z.string(),
  uptime_seconds: z.number(),
  mind_name: z.string(),
  active_conversations: z.number(),
  memory_concepts: z.number(),
  memory_episodes: z.number(),
  llm_cost_today: z.number(),
  llm_calls_today: z.number(),
  tokens_today: z.number(),
  messages_today: z.number(),
  cost_history: z.array(CostHistoryEntrySchema).optional(),
  timezone: z.string().optional(),
  today_date: z.string().optional(),
  has_lifetime_activity: z.boolean().optional(),
});

// ── Conversations ──

export const MessageSchema = z.object({
  id: z.string(),
  role: z.enum(["user", "assistant"]),
  content: z.string(),
  timestamp: z.string(),
});

export const ConversationSchema = z.object({
  id: z.string(),
  participant: z.string(),
  participant_name: z.string().optional(),
  channel: z.string(),
  message_count: z.number(),
  last_message_at: z.string(),
  status: z.enum(["active", "closed"]),
});

export const ConversationsResponseSchema = z.object({
  conversations: z.array(ConversationSchema),
});

export const ConversationDetailResponseSchema = z.object({
  conversation_id: z.string(),
  messages: z.array(MessageSchema),
});

// ── Brain ──

export const BrainNodeSchema = z.object({
  id: z.string(),
  name: z.string(),
  category: ConceptCategorySchema,
  importance: z.number(),
  confidence: z.number(),
  access_count: z.number(),
});

export const BrainLinkSchema = z.object({
  source: z.string(),
  target: z.string(),
  relation_type: RelationTypeSchema,
  weight: z.number(),
});

export const BrainGraphSchema = z.object({
  nodes: z.array(BrainNodeSchema),
  links: z.array(BrainLinkSchema),
});

export const BrainSearchResultSchema = z.object({
  id: z.string(),
  name: z.string(),
  category: ConceptCategorySchema,
  importance: z.number(),
  confidence: z.number(),
  access_count: z.number(),
  score: z.number(),
  match_type: z.enum(["text", "vector"]).optional(),
});

export const BrainSearchResponseSchema = z.object({
  results: z.array(BrainSearchResultSchema),
  query: z.string(),
});

// ── Logs ──

export const LogEntrySchema = z
  .object({
    timestamp: z.string(),
    level: LogLevelSchema,
    logger: z.string(),
    event: z.string(),
  })
  .catchall(z.unknown());

export const LogsResponseSchema = z.object({
  entries: z.array(LogEntrySchema),
});

// ── Activity Timeline ──

export const TimelineEntrySchema = z.object({
  type: TimelineEntryTypeSchema,
  timestamp: z.string(),
  data: z.record(z.string(), z.unknown()),
});

export const TimelineResponseSchema = z.object({
  entries: z.array(TimelineEntrySchema),
  meta: z.object({
    hours: z.number(),
    limit: z.number(),
    total_before_limit: z.number(),
    sources: z.record(z.string(), z.number()),
  }),
});

// ── Stats ──

export const DailyStatsSchema = z.object({
  date: z.string(),
  cost: z.number(),
  messages: z.number(),
  llm_calls: z.number(),
  tokens: z.number(),
  conversations: z.number().optional(),
  cost_by_provider: z.record(z.string(), z.number()).optional(),
  cost_by_model: z.record(z.string(), z.number()).optional(),
  is_live: z.boolean().optional(),
});

export const StatsTotalsSchema = z.object({
  cost: z.number(),
  messages: z.number(),
  llm_calls: z.number(),
  tokens: z.number(),
  days_active: z.number(),
});

export const StatsMonthSchema = z.object({
  cost: z.number(),
  messages: z.number(),
  llm_calls: z.number(),
  tokens: z.number(),
});

export const StatsHistoryResponseSchema = z.object({
  days: z.array(DailyStatsSchema),
  totals: StatsTotalsSchema,
  current_month: StatsMonthSchema,
});

// ── Chat ──

export const ChatResponseSchema = z.object({
  response: z.string(),
  conversation_id: z.string(),
  mind_id: z.string(),
  timestamp: z.string().optional(),
  tags: z.array(z.string()).optional(),
});

// ── Conversation Imports ──

export const ConversationImportPlatformSchema = z.enum([
  "chatgpt",
  "claude",
  "gemini",
]);

export const ConversationImportStateSchema = z.enum([
  "pending",
  "parsing",
  "processing",
  "completed",
  "failed",
]);

export const StartConversationImportResponseSchema = z.object({
  job_id: z.string(),
  platform: ConversationImportPlatformSchema,
  conversations_total: z.number(),
});

export const ConversationImportProgressSchema = z.object({
  job_id: z.string(),
  platform: ConversationImportPlatformSchema,
  state: ConversationImportStateSchema,
  conversations_total: z.number(),
  conversations_processed: z.number(),
  conversations_skipped: z.number(),
  episodes_created: z.number(),
  concepts_learned: z.number(),
  warnings: z.array(z.string()),
  error: z.string().nullable(),
  elapsed_ms: z.number(),
});

// ── Plugins ──

export const PluginPermissionSchema = z.object({
  permission: z.string(),
  risk: PermissionRiskSchema,
  description: z.string(),
});

export const PluginHealthSchema = z.object({
  consecutive_failures: z.number(),
  disabled: z.boolean(),
  last_error: z.string(),
  active_tasks: z.number(),
});

export const PluginToolSummarySchema = z.object({
  name: z.string(),
  description: z.string(),
});

export const PluginToolDetailSchema = PluginToolSummarySchema.extend({
  parameters: z.record(z.string(), z.unknown()),
  requires_confirmation: z.boolean(),
  timeout_seconds: z.number(),
});

export const PluginInfoSchema = z.object({
  name: z.string(),
  version: z.string(),
  description: z.string(),
  status: PluginStatusSchema,
  tools_count: z.number(),
  tools: z.array(PluginToolSummarySchema),
  permissions: z.array(PluginPermissionSchema),
  health: PluginHealthSchema,
  category: z.string(),
  tags: z.array(z.string()),
  icon_url: z.string(),
  pricing: z.string(),
});

export const PluginsResponseSchema = z.object({
  available: z.boolean(),
  plugins: z.array(PluginInfoSchema),
  total: z.number(),
  active: z.number(),
  disabled: z.number(),
  error: z.number(),
  total_tools: z.number(),
});

/**
 * PluginManifestData is loosely validated — the manifest comes from
 * third-party plugin code and the dashboard treats unknown fields as
 * data to display. `passthrough()` preserves extra keys.
 */
export const PluginManifestDataSchema = z
  .object({
    name: z.string(),
    version: z.string(),
    description: z.string(),
    author: z.string(),
  })
  .loose();

export const PluginDetailSchema = z.object({
  name: z.string(),
  version: z.string(),
  description: z.string(),
  status: PluginStatusSchema,
  tools: z.array(PluginToolDetailSchema),
  permissions: z.array(PluginPermissionSchema),
  health: PluginHealthSchema,
  manifest: z.union([PluginManifestDataSchema, z.object({}).loose()]),
});

export const PluginActionResponseSchema = z.object({
  ok: z.boolean(),
  plugin: z.string(),
  status: z.string(),
  error: z.string().optional(),
});
