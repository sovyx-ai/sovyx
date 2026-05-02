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

/**
 * Log entry schema — mirrors the structlog JSON envelope plus the
 * Phase 1/2 observability fields (saga_id, cause_id, span_id, …) and
 * the Phase 8.4 diagnosis hints. All envelope fields are optional so
 * the dashboard tolerates pre-Phase-1 deployments. ``catchall`` keeps
 * unknown keys without failing validation.
 */
export const LogEntrySchema = z
  .object({
    timestamp: z.string(),
    level: LogLevelSchema,
    logger: z.string(),
    event: z.string(),
    schema_version: z.string().optional(),
    process_id: z.number().int().optional(),
    pid: z.number().int().optional(),
    host: z.string().optional(),
    sovyx_version: z.string().optional(),
    saga_id: z.string().nullable().optional(),
    cause_id: z.string().nullable().optional(),
    span_id: z.string().nullable().optional(),
    sequence_no: z.number().int().optional(),
    diagnosis_hint: z.string().optional(),
    diagnosis_severity: z.string().optional(),
    diagnosis_runbook_url: z.string().optional(),
    snippet: z.string().nullable().optional(),
    content: z.string().optional(),
    message: z.string().optional(),
  })
  .catchall(z.unknown());

export const LogsResponseSchema = z.object({
  entries: z.array(LogEntrySchema),
});

/** GET /api/logs/search response — FTS5-backed query. */
export const LogSearchResponseSchema = z.object({
  query: z.string(),
  filters: z.object({
    level: z.string().nullable(),
    logger: z.string().nullable(),
    saga_id: z.string().nullable(),
    since: z.string().nullable(),
    until: z.string().nullable(),
  }),
  count: z.number().int(),
  entries: z.array(LogEntrySchema),
});

/** GET /api/logs/sagas/{saga_id} response. */
export const SagaResponseSchema = z.object({
  saga_id: z.string(),
  entries: z.array(LogEntrySchema),
});

/** Causality DAG node — used as the base shape for edges as well. */
export const CausalityNodeSchema = z.object({
  id: z.string().nullable(),
  event: z.string().nullable(),
  logger: z.string().nullable(),
  timestamp: z.string(),
  level: z.string(),
});

/** Causality edge — extends node with cause_id parent pointer. */
export const CausalityEdgeSchema = CausalityNodeSchema.extend({
  cause_id: z.string().nullable(),
});

/** GET /api/logs/sagas/{saga_id}/causality response. */
export const CausalityGraphResponseSchema = z.object({
  saga_id: z.string(),
  edges: z.array(CausalityEdgeSchema),
});

/** One step of the localized narrative (P8.3). */
export const NarrativeStepSchema = z.object({
  timestamp: z.string(),
  text: z.string(),
  event: z.string().optional(),
});

/** GET /api/logs/sagas/{saga_id}/story response. */
export const NarrativeResponseSchema = z.object({
  saga_id: z.string(),
  story: z.string(),
  locale: z.enum(["pt-BR", "en-US"]),
  steps: z.array(NarrativeStepSchema).optional(),
});

/** Anomaly event names emitted by AnomalyDetector (Phase 8.1). */
export const AnomalyKindSchema = z.enum([
  "anomaly.first_occurrence",
  "anomaly.latency_spike",
  "anomaly.error_rate_spike",
  "anomaly.memory_growth",
]);

/** GET /api/logs/anomalies response. */
export const AnomaliesResponseSchema = z.object({
  count: z.number().int(),
  entries: z.array(LogEntrySchema),
});

/** WS /api/logs/stream — batch frame. */
export const LogStreamBatchSchema = z.object({
  type: z.literal("batch"),
  entries: z.array(LogEntrySchema),
});

/** WS /api/logs/stream — error frame. */
export const LogStreamErrorSchema = z.object({
  type: z.literal("error"),
  message: z.string(),
});

/** Discriminated union over all log-stream frames. */
export const LogStreamFrameSchema = z.discriminatedUnion("type", [
  LogStreamBatchSchema,
  LogStreamErrorSchema,
]);

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
  "obsidian",
  "grok",
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

// ── Voice device test ──

export const VoiceTestErrorCodeSchema = z.enum([
  "device_not_found",
  "device_busy",
  "device_disappeared",
  "permission_denied",
  "unsupported_samplerate",
  "unsupported_channels",
  "unsupported_format",
  "buffer_size_invalid",
  "pipeline_active",
  "rate_limited",
  "disabled",
  "replaced_by_newer_session",
  "internal_error",
  "invalid_request",
  "tts_unavailable",
  "models_not_downloaded",
  "job_not_found",
  "job_expired",
]);

export const VoiceTestFrameTypeSchema = z.enum(["level", "error", "closed", "ready"]);

export const VoiceTestCloseReasonSchema = z.enum([
  "client_disconnect",
  "server_shutdown",
  "device_changed",
  "session_replaced",
  "device_error",
]);

export const VoiceTestReadyFrameSchema = z.object({
  v: z.number().int(),
  t: z.literal("ready"),
  device_id: z.number().int().nullable(),
  device_name: z.string(),
  sample_rate: z.number().int(),
  channels: z.number().int(),
});

export const VoiceTestLevelFrameSchema = z.object({
  v: z.number().int(),
  t: z.literal("level"),
  rms_db: z.number(),
  peak_db: z.number(),
  hold_db: z.number(),
  clipping: z.boolean(),
  vad_trigger: z.boolean(),
});

export const VoiceTestErrorFrameSchema = z.object({
  v: z.number().int(),
  t: z.literal("error"),
  code: VoiceTestErrorCodeSchema,
  detail: z.string(),
  retryable: z.boolean(),
});

export const VoiceTestClosedFrameSchema = z.object({
  v: z.number().int(),
  t: z.literal("closed"),
  reason: VoiceTestCloseReasonSchema,
});

export const VoiceTestFrameSchema = z.discriminatedUnion("t", [
  VoiceTestReadyFrameSchema,
  VoiceTestLevelFrameSchema,
  VoiceTestErrorFrameSchema,
  VoiceTestClosedFrameSchema,
]);

export const VoiceTestDeviceInfoSchema = z.object({
  index: z.number().int(),
  name: z.string(),
  is_default: z.boolean(),
  max_input_channels: z.number().int(),
  max_output_channels: z.number().int(),
  default_samplerate: z.number().int(),
});

export const VoiceTestDevicesResponseSchema = z.object({
  ok: z.boolean(),
  protocol_version: z.number().int(),
  input_devices: z.array(VoiceTestDeviceInfoSchema),
  output_devices: z.array(VoiceTestDeviceInfoSchema),
});

export const VoiceTestOutputJobSchema = z.object({
  ok: z.boolean(),
  job_id: z.string(),
  status: z.string(),
});

export const VoiceTestOutputResultSchema = z.object({
  ok: z.boolean(),
  job_id: z.string(),
  status: z.string(),
  code: VoiceTestErrorCodeSchema.nullable().optional(),
  detail: z.string().nullable().optional(),
  phrase: z.string().nullable().optional(),
  synthesis_ms: z.number().nullable().optional(),
  playback_ms: z.number().nullable().optional(),
  peak_db: z.number().nullable().optional(),
});

export const VoiceTestErrorResponseSchema = z.object({
  ok: z.boolean(),
  code: VoiceTestErrorCodeSchema,
  detail: z.string(),
  missing_models: z.array(z.string()).nullable().optional(),
});

// ── Voice models — disk status + background download ──

export const VoiceModelDiskStatusSchema = z.object({
  name: z.string(),
  category: z.string(),
  description: z.string(),
  installed: z.boolean(),
  path: z.string(),
  size_mb: z.number(),
  expected_size_mb: z.number(),
  download_available: z.boolean(),
});

export const VoiceModelsStatusResponseSchema = z.object({
  model_dir: z.string(),
  all_installed: z.boolean(),
  missing_count: z.number().int(),
  missing_download_mb: z.number(),
  models: z.array(VoiceModelDiskStatusSchema),
});

export const VoiceModelDownloadProgressSchema = z.object({
  task_id: z.string(),
  status: z.enum(["running", "done", "error"]),
  total_models: z.number().int(),
  completed_models: z.number().int(),
  current_model: z.string().nullable(),
  error: z.string().nullable(),
  error_code: z
    .enum([
      "cooldown",
      "all_mirrors_exhausted",
      "checksum_mismatch",
      "network",
      "unknown",
    ])
    .nullable()
    .optional(),
  retry_after_seconds: z.number().int().nullable().optional(),
});

export const VoiceCatalogEntrySchema = z.object({
  id: z.string(),
  display_name: z.string(),
  language: z.string(),
  gender: z.enum(["female", "male"]),
});

export const VoiceCatalogResponseSchema = z.object({
  supported_languages: z.array(z.string()),
  by_language: z.record(z.string(), z.array(VoiceCatalogEntrySchema)),
  recommended_per_language: z.record(z.string(), z.string()),
});

// ── Voice capture APO diagnostics ──

export const CaptureApoEndpointSchema = z.object({
  endpoint_id: z.string(),
  endpoint_name: z.string(),
  enumerator: z.string(),
  fx_binding_count: z.number().int(),
  known_apos: z.array(z.string()),
  raw_clsids: z.array(z.string()),
  voice_clarity_active: z.boolean(),
  is_active_device: z.boolean(),
});

export const CaptureDiagnosticsResponseSchema = z.object({
  platform_supported: z.boolean(),
  active_device_name: z.string().nullable(),
  active_endpoint: z
    .object({
      endpoint_id: z.string(),
      endpoint_name: z.string(),
      known_apos: z.array(z.string()),
      voice_clarity_active: z.boolean(),
    })
    .nullable(),
  voice_clarity_active: z.boolean(),
  any_voice_clarity_active: z.boolean(),
  endpoints: z.array(CaptureApoEndpointSchema),
  fix_suggestion: z.string().nullable(),
  error: z.string().optional(),
});

export const CaptureExclusiveResponseSchema = z.object({
  ok: z.boolean(),
  enabled: z.boolean(),
  persisted: z.boolean(),
  applied_immediately: z.boolean(),
});

// ── Linux ALSA mixer diagnostics + remediation ──

export const LinuxMixerControlSchema = z.object({
  name: z.string(),
  min_raw: z.number().int(),
  max_raw: z.number().int(),
  current_raw: z.number().int(),
  current_db: z.number().nullable(),
  max_db: z.number().nullable(),
  is_boost_control: z.boolean(),
  saturation_risk: z.boolean(),
  asymmetric: z.boolean(),
});

export const LinuxMixerCardSchema = z.object({
  card_index: z.number().int(),
  card_id: z.string(),
  card_longname: z.string(),
  aggregated_boost_db: z.number(),
  saturation_warning: z.boolean(),
  controls: z.array(LinuxMixerControlSchema),
});

export const LinuxMixerDiagnosticsResponseSchema = z.object({
  platform_supported: z.boolean(),
  amixer_available: z.boolean(),
  snapshots: z.array(LinuxMixerCardSchema),
  aggregated_boost_db_ceiling: z.number(),
  saturation_ratio_ceiling: z.number(),
  reset_enabled_by_default: z.boolean(),
});

export const LinuxMixerResetResponseSchema = z.object({
  ok: z.boolean(),
  reason: z.string().optional(),
  reason_code: z.string().optional(),
  detail: z.string().optional(),
  card_index: z.number().int().optional(),
  card_id: z.string().optional(),
  card_longname: z.string().optional(),
  candidate_card_indexes: z.array(z.number().int()).optional(),
  applied_controls: z.array(z.tuple([z.string(), z.number().int()])).optional(),
  reverted_controls: z.array(z.tuple([z.string(), z.number().int()])).optional(),
});

// v1.3 §4.6 L6 — boot preflight warning carried on
// /api/voice/status.preflight_warnings and as the WebSocket
// "voice_preflight_warning" payload. Shape mirrors
// ``BootPreflightWarningsStore.snapshot()`` on the backend.
export const PreflightWarningSchema = z.object({
  code: z.string(),
  severity: z.string().optional(),
  hint: z.string().optional(),
  details: z.record(z.string(), z.unknown()).optional(),
});

// ── Voice Capture Health (L7, ADR §4.7) ──

/** T6.20 — `GET /api/voice/service-health` aggregated readiness response.
 *
 * ``user_remediation`` (T6.12) is the operator-facing hint string mapped
 * from ``last_diagnosis``. ``null`` when no actionable hint applies.
 */
export const VoiceServiceHealthResponseSchema = z.object({
  ready: z.boolean(),
  reason: z.enum([
    "ok",
    "voice_disabled",
    "engine_not_running",
    "voice_pipeline_not_registered",
    "last_diagnosis_unhealthy",
  ]),
  last_diagnosis: z.string().nullable(),
  watchdog_state: z.string().nullable(),
  user_remediation: z.string().nullable(),
});

export const VoiceHealthComboSchema = z.object({
  host_api: z.string(),
  sample_rate: z.number().int(),
  channels: z.number().int(),
  sample_format: z.string(),
  exclusive: z.boolean(),
  auto_convert: z.boolean(),
  frames_per_buffer: z.number().int(),
});

export const VoiceHealthRemediationHintSchema = z.object({
  code: z.string(),
  severity: z.enum(["info", "warn", "error"]),
  cli_action: z.string().nullable(),
});

export const VoiceHealthProbeHistoryEntrySchema = z.object({
  ts: z.string(),
  mode: z.string(),
  diagnosis: z.string(),
  vad_max_prob: z.number().nullable(),
  rms_db: z.number(),
  duration_ms: z.number().int(),
});

export const VoiceHealthProbeResultSchema = z.object({
  diagnosis: z.string(),
  mode: z.string(),
  combo: VoiceHealthComboSchema,
  vad_max_prob: z.number().nullable(),
  vad_mean_prob: z.number().nullable(),
  rms_db: z.number(),
  callbacks_fired: z.number().int(),
  duration_ms: z.number().int(),
  error: z.string().nullable(),
  remediation: VoiceHealthRemediationHintSchema.nullable(),
});

export const VoiceHealthComboEntrySchema = z.object({
  endpoint_guid: z.string(),
  device_friendly_name: z.string(),
  device_interface_name: z.string(),
  device_class: z.string(),
  endpoint_fxproperties_sha: z.string(),
  winning_combo: VoiceHealthComboSchema,
  validated_at: z.string(),
  validation_mode: z.string(),
  vad_max_prob_at_validation: z.number().nullable(),
  vad_mean_prob_at_validation: z.number().nullable(),
  rms_db_at_validation: z.number(),
  probe_duration_ms: z.number().int(),
  detected_apos_at_validation: z.array(z.string()),
  cascade_attempts_before_success: z.number().int(),
  boots_validated: z.number().int(),
  last_boot_validated: z.string(),
  last_boot_diagnosis: z.string(),
  probe_history: z.array(VoiceHealthProbeHistoryEntrySchema),
  pinned: z.boolean(),
  needs_revalidation: z.boolean(),
});

export const VoiceHealthOverrideEntrySchema = z.object({
  endpoint_guid: z.string(),
  device_friendly_name: z.string(),
  pinned_combo: VoiceHealthComboSchema,
  pinned_at: z.string(),
  pinned_by: z.string(),
  reason: z.string(),
});

export const VoiceHealthSnapshotResponseSchema = z.object({
  combo_store: z.array(VoiceHealthComboEntrySchema),
  overrides: z.array(VoiceHealthOverrideEntrySchema),
  quarantine_count: z.number().int(),
  data_dir: z.string(),
  voice_enabled: z.boolean(),
});

export const VoiceHealthQuarantineEntrySchema = z.object({
  endpoint_guid: z.string(),
  device_friendly_name: z.string(),
  device_interface_name: z.string(),
  host_api: z.string(),
  added_at_monotonic: z.number(),
  expires_at_monotonic: z.number(),
  seconds_until_expiry: z.number(),
  reason: z.string(),
});

export const VoiceHealthQuarantineSnapshotResponseSchema = z.object({
  entries: z.array(VoiceHealthQuarantineEntrySchema),
  count: z.number().int(),
});

export const VoiceHealthReprobeResponseSchema = z.object({
  endpoint_guid: z.string(),
  result: VoiceHealthProbeResultSchema,
});

export const VoiceHealthForgetResponseSchema = z.object({
  endpoint_guid: z.string(),
  invalidated: z.boolean(),
});

// ── voice-linux-cascade-root-fix T9 — alternative-device banner ──
//
// Backend returns HTTP 503 with ``error: "capture_device_contended"``
// when every candidate failed because a session manager holds the
// hardware. The dashboard renders a banner with clickable chips for
// each alternative; clicking one dispatches a new ``/api/voice/enable``
// with ``input_device_name`` / ``input_device`` pinned to that device.
export const VoiceDeviceKindSchema = z.enum([
  "hardware",
  "session_manager_virtual",
  "os_default",
  "unknown",
]);

export const VoiceAlternativeDeviceSchema = z.object({
  index: z.number().int(),
  name: z.string(),
  host_api: z.string(),
  kind: VoiceDeviceKindSchema,
  max_input_channels: z.number().int(),
  default_samplerate: z.number().int(),
});

export const VoiceCaptureDeviceContendedErrorSchema = z.object({
  ok: z.literal(false),
  error: z.literal("capture_device_contended"),
  detail: z.string(),
  device: z.union([z.number(), z.string(), z.null()]),
  host_api: z.union([z.string(), z.null()]),
  suggested_actions: z.array(z.string()),
  contending_process_hint: z.union([z.string(), z.null()]).optional(),
  alternative_devices: z.array(VoiceAlternativeDeviceSchema),
});

export const VoiceHealthPinResponseSchema = z.object({
  endpoint_guid: z.string(),
  pinned: z.boolean(),
});

// ── Mixer KB (Sprint 4 dashboard workflow) ──────────────────────────

export const MixerKbProfileSummarySchema = z.object({
  pool: z.string(),
  profile_id: z.string(),
  profile_version: z.number().int(),
  schema_version: z.number().int(),
  driver_family: z.string(),
  codec_id_glob: z.string(),
  match_threshold: z.number(),
  factory_regime: z.string(),
  contributed_by: z.string(),
});

export const MixerKbProfileDetailSchema = MixerKbProfileSummarySchema.extend({
  system_vendor_glob: z.string().nullable(),
  system_product_glob: z.string().nullable(),
  distro_family: z.string().nullable(),
  audio_stack: z.string().nullable(),
  kernel_major_minor_glob: z.string().nullable(),
  factory_signature_roles: z.array(z.string()),
  verified_on_count: z.number().int(),
});

export const MixerKbListResponseSchema = z.object({
  profiles: z.array(MixerKbProfileSummarySchema),
  shipped_count: z.number().int(),
  user_count: z.number().int(),
});

export const MixerKbValidationIssueSchema = z.object({
  loc: z.string(),
  msg: z.string(),
});

export const MixerKbValidateResponseSchema = z.object({
  ok: z.boolean(),
  profile_id: z.string().nullable(),
  profile_version: z.number().int().nullable(),
  issues: z.array(MixerKbValidationIssueSchema),
});

// ── Platform Diagnostics — GET /api/voice/platform-diagnostics ──

export const MicPermissionStatusSchema = z.enum([
  "granted",
  "denied",
  "unknown",
]);

export const PlatformMicPermissionPayloadSchema = z.object({
  status: MicPermissionStatusSchema,
  machine_value: z.string().nullable(),
  user_value: z.string().nullable(),
  notes: z.array(z.string()),
  remediation_hint: z.string(),
});

export const PipeWireStatusTokenSchema = z.enum([
  "active",
  "absent",
  "degraded",
  "unknown",
]);

export const PipeWirePayloadSchema = z.object({
  status: PipeWireStatusTokenSchema,
  socket_present: z.boolean(),
  pactl_available: z.boolean(),
  pactl_info_ok: z.boolean(),
  server_name: z.string().nullable(),
  modules_loaded: z.array(z.string()),
  echo_cancel_loaded: z.boolean(),
  notes: z.array(z.string()),
});

export const UcmStatusTokenSchema = z.enum([
  "available",
  "absent",
  "no_ucm_for_card",
  "unknown",
]);

export const UcmPayloadSchema = z.object({
  status: UcmStatusTokenSchema,
  card_id: z.string(),
  alsaucm_available: z.boolean(),
  verbs: z.array(z.string()),
  active_verb: z.string().nullable(),
  notes: z.array(z.string()),
});

export const WindowsServiceStateTokenSchema = z.enum([
  "running",
  "stopped",
  "start_pending",
  "stop_pending",
  "paused",
  "unknown",
  "not_found",
]);

export const WindowsServicePayloadSchema = z.object({
  name: z.string(),
  state: WindowsServiceStateTokenSchema,
  raw_state: z.string(),
  notes: z.array(z.string()),
});

export const WindowsAudioServicePayloadSchema = z.object({
  audiosrv: WindowsServicePayloadSchema,
  audio_endpoint_builder: WindowsServicePayloadSchema,
  all_healthy: z.boolean(),
  degraded_services: z.array(z.string()),
});

export const EtwEventLevelTokenSchema = z.enum([
  "critical",
  "error",
  "warning",
  "information",
  "verbose",
  "unknown",
]);

export const EtwEventPayloadSchema = z.object({
  channel: z.string(),
  level: EtwEventLevelTokenSchema,
  event_id: z.number().int(),
  timestamp_iso: z.string(),
  provider: z.string(),
  description: z.string(),
});

export const EtwChannelPayloadSchema = z.object({
  channel: z.string(),
  events: z.array(EtwEventPayloadSchema),
  lookback_seconds: z.number().int(),
  notes: z.array(z.string()),
});

export const HalPluginCategoryTokenSchema = z.enum([
  "virtual_audio",
  "audio_enhancement",
  "vendor",
  "unknown",
]);

export const HalPluginPayloadSchema = z.object({
  bundle_name: z.string(),
  path: z.string(),
  category: HalPluginCategoryTokenSchema,
  friendly_label: z.string(),
});

export const HalPayloadSchema = z.object({
  plugins: z.array(HalPluginPayloadSchema),
  notes: z.array(z.string()),
  virtual_audio_active: z.boolean(),
  audio_enhancement_active: z.boolean(),
});

export const BluetoothAudioProfileTokenSchema = z.enum([
  "a2dp",
  "hfp",
  "unknown",
]);

export const BluetoothDevicePayloadSchema = z.object({
  name: z.string(),
  address: z.string(),
  profile: BluetoothAudioProfileTokenSchema,
  is_input_capable: z.boolean(),
  is_output_capable: z.boolean(),
});

export const BluetoothPayloadSchema = z.object({
  devices: z.array(BluetoothDevicePayloadSchema),
  notes: z.array(z.string()),
});

export const EntitlementVerdictTokenSchema = z.enum([
  "present",
  "absent",
  "unsigned",
  "unknown",
]);

export const CodeSigningPayloadSchema = z.object({
  verdict: EntitlementVerdictTokenSchema,
  executable_path: z.string(),
  notes: z.array(z.string()),
  remediation_hint: z.string(),
});

export const PlatformLinuxBranchSchema = z.object({
  pipewire: PipeWirePayloadSchema,
  alsa_ucm: UcmPayloadSchema,
});

export const PlatformWindowsBranchSchema = z.object({
  audio_service: WindowsAudioServicePayloadSchema,
  etw_audio_events: z.array(EtwChannelPayloadSchema),
});

export const PlatformMacOSBranchSchema = z.object({
  hal_plugins: HalPayloadSchema,
  bluetooth: BluetoothPayloadSchema,
  code_signing: CodeSigningPayloadSchema,
});

export const PlatformTokenSchema = z.enum([
  "linux",
  "win32",
  "darwin",
  "other",
]);

export const PlatformDiagnosticsResponseSchema = z.object({
  platform: PlatformTokenSchema,
  mic_permission: PlatformMicPermissionPayloadSchema,
  linux: PlatformLinuxBranchSchema.nullable(),
  windows: PlatformWindowsBranchSchema.nullable(),
  macos: PlatformMacOSBranchSchema.nullable(),
});

// ── Voice Windows Paranoid Mission (v0.24.0 → v0.26.0) ──────────────
// CaptureRestartFrame — observability layer for capture-task restarts.
// Mirrors src/sovyx/voice/pipeline/_frame_types.py::CaptureRestartFrame.
//
// Phase 3 / T3.12 (v0.26.0 prep, 2026-04-29): ALL payload fields
// promoted from .optional() to required after T32 wire-up shipped
// (commits a21d8bf + ab2720f) and demonstrated in the production
// payload. The Python dataclass at _frame_types.py defines defaults
// (empty strings, 0 ints) for every field — dataclasses.asdict()
// always produces them — so the wire contract is "fields are
// always present even when their values are zero/empty". Required
// schema rejects payloads that drop fields, catching backend
// regression at the safeParse boundary instead of letting None
// propagate into the dashboard's TypeScript layer.

export const CaptureRestartReasonSchema = z.enum([
  "device_changed",
  "apo_degraded",
  "overflow",
  "manual",
]);

export const CaptureRestartFrameSchema = z.object({
  // PipelineFrame base fields:
  frame_type: z.literal("CaptureRestart"),
  timestamp_monotonic: z.number(),
  utterance_id: z.string(),
  // CaptureRestart payload (T3.12 — all required, defaults to
  // empty-string / 0 in the Python dataclass; always serialised).
  restart_reason: CaptureRestartReasonSchema.or(z.string()),
  old_host_api: z.string(),
  new_host_api: z.string(),
  old_device_id: z.string(),
  new_device_id: z.string(),
  old_signal_processing_mode: z.string(),
  new_signal_processing_mode: z.string(),
  recovery_latency_ms: z.number().int().nonnegative(),
  bypass_tier: z.number().int().min(0).max(3),
});

// ``GET /api/voice/restart-history?limit=N`` payload — bounded list of
// CaptureRestartFrame entries from the orchestrator's frame ring buffer.
// T33 (commit 773a2ff) wired the endpoint to the real ``frame_history``
// payload; T3.12 promotes ``limit`` + ``total`` to required since the
// endpoint always populates them post-T33.
export const VoiceRestartHistoryResponseSchema = z.object({
  frames: z.array(CaptureRestartFrameSchema),
  limit: z.number().int().positive(),
  total: z.number().int().nonnegative(),
});

// ``GET /api/voice/bypass-tier-status`` — current bypass-tier health
// snapshot (Tier 1 RAW / Tier 2 host_api_rotate / Tier 3 WASAPI excl).
// v0.26.0 wire-up (commit 2dbe913): the endpoint reads a deterministic
// ``BypassTierSnapshot`` mirror that always populates every counter, so
// the schema promotes counters from .optional() to required. Backend
// regression (e.g. dropped field) is now caught at the zod boundary
// in CI rather than silent at runtime.
// ``current_bypass_tier`` stays nullable+optional — coordinator-side
// engaged-tier tracking is staged for a follow-up commit (the v0.26.0
// snapshot always emits the key but with value ``null``).
export const VoiceBypassTierStatusResponseSchema = z.object({
  current_bypass_tier: z.number().int().min(0).max(3).nullable().optional(),
  tier1_raw_attempted: z.number().int().nonnegative(),
  tier1_raw_succeeded: z.number().int().nonnegative(),
  tier2_host_api_rotate_attempted: z.number().int().nonnegative(),
  tier2_host_api_rotate_succeeded: z.number().int().nonnegative(),
  tier3_wasapi_exclusive_attempted: z.number().int().nonnegative(),
  tier3_wasapi_exclusive_succeeded: z.number().int().nonnegative(),
});

// ``GET /api/voice/quality-snapshot`` — Phase 4 / T4.26 + T4.37.
// Single read of the rolling SNR buffer + noise-floor drift state
// + AGC2 verdict counters. ``dnsmos_extras_installed`` distinguishes
// "true MOS available" (T4.21+ extras) from "SNR-proxy MOS only".
export const VoiceQualityVerdictSchema = z.enum([
  "excellent",
  "good",
  "degraded",
  "poor",
  "no_signal",
]);

export const VoiceNoiseFloorBlockSchema = z.object({
  short_avg_db: z.number().nullable(),
  long_avg_db: z.number().nullable(),
  drift_db: z.number().nullable(),
  ready: z.boolean(),
  short_sample_count: z.number().int().nonnegative(),
  long_sample_count: z.number().int().nonnegative(),
});

export const VoiceAgc2BlockSchema = z.object({
  frames_processed: z.number().int().nonnegative(),
  frames_silenced: z.number().int().nonnegative(),
  frames_vad_silenced: z.number().int().nonnegative(),
  current_gain_db: z.number(),
  speech_level_dbfs: z.number(),
});

export const VoiceQualitySnapshotResponseSchema = z.object({
  snr_p50_db: z.number().nullable(),
  snr_sample_count: z.number().int().nonnegative(),
  snr_verdict: VoiceQualityVerdictSchema,
  noise_floor: VoiceNoiseFloorBlockSchema,
  agc2: VoiceAgc2BlockSchema.nullable(),
  dnsmos_extras_installed: z.boolean(),
});

/* ── Mind management runtime schemas ──
 *
 * Phase 8 / T8.21 dashboard endpoints:
 *   POST /api/mind/{mind_id}/forget          (step 5)
 *   POST /api/mind/{mind_id}/retention/prune (step 6)
 *
 * Pass ``{ schema: ForgetMindResponseSchema }`` to ``api.post`` so
 * malformed responses are caught at the boundary instead of
 * silently corrupting downstream UI state.
 */

export const ForgetMindResponseSchema = z.object({
  mind_id: z.string(),
  concepts_purged: z.number().int().nonnegative(),
  relations_purged: z.number().int().nonnegative(),
  episodes_purged: z.number().int().nonnegative(),
  concept_embeddings_purged: z.number().int().nonnegative(),
  episode_embeddings_purged: z.number().int().nonnegative(),
  conversation_imports_purged: z.number().int().nonnegative(),
  consolidation_log_purged: z.number().int().nonnegative(),
  conversations_purged: z.number().int().nonnegative(),
  conversation_turns_purged: z.number().int().nonnegative(),
  daily_stats_purged: z.number().int().nonnegative(),
  consent_ledger_purged: z.number().int().nonnegative(),
  total_brain_rows_purged: z.number().int().nonnegative(),
  total_conversations_rows_purged: z.number().int().nonnegative(),
  total_system_rows_purged: z.number().int().nonnegative(),
  total_rows_purged: z.number().int().nonnegative(),
  dry_run: z.boolean(),
});

export const PruneRetentionResponseSchema = z.object({
  mind_id: z.string(),
  cutoff_utc: z.string(),
  episodes_purged: z.number().int().nonnegative(),
  conversations_purged: z.number().int().nonnegative(),
  conversation_turns_purged: z.number().int().nonnegative(),
  daily_stats_purged: z.number().int().nonnegative(),
  consolidation_log_purged: z.number().int().nonnegative(),
  consent_ledger_purged: z.number().int().nonnegative(),
  /**
   * Per-surface horizon (days) actually applied. ``0`` means the
   * surface was skipped (retention disabled for it).
   */
  effective_horizons: z.record(z.string(), z.number().int().nonnegative()),
  total_brain_rows_purged: z.number().int().nonnegative(),
  total_conversations_rows_purged: z.number().int().nonnegative(),
  total_system_rows_purged: z.number().int().nonnegative(),
  total_rows_purged: z.number().int().nonnegative(),
  dry_run: z.boolean(),
});

/* ── Voice setup wizard runtime schemas — Phase 7 / T7.21-T7.24 ── */

export const WizardDeviceInfoSchema = z.object({
  device_id: z.string(),
  name: z.string(),
  friendly_name: z.string(),
  max_input_channels: z.number().int().nonnegative(),
  default_sample_rate: z.number().int().nonnegative(),
  is_default: z.boolean(),
  diagnosis_hint: z.string(),
});

export const WizardDevicesResponseSchema = z.object({
  devices: z.array(WizardDeviceInfoSchema),
  total_count: z.number().int().nonnegative(),
  default_device_id: z.string().nullable(),
});

export const WizardTestResultResponseSchema = z.object({
  session_id: z.string(),
  success: z.boolean(),
  duration_actual_s: z.number().nonnegative(),
  sample_rate_hz: z.number().int().nonnegative(),
  level_rms_dbfs: z.number().nullable(),
  level_peak_dbfs: z.number().nullable(),
  snr_db: z.number().nullable(),
  clipping_detected: z.boolean(),
  silent_capture: z.boolean(),
  diagnosis: z.string(),
  diagnosis_hint: z.string(),
  recorded_at_utc: z.string(),
  error: z.string().nullable(),
});

export const WizardDiagnosticResponseSchema = z.object({
  ready: z.boolean(),
  voice_clarity_active: z.boolean(),
  active_device_name: z.string().nullable(),
  platform: z.string(),
  recommendations: z.array(z.string()),
});
