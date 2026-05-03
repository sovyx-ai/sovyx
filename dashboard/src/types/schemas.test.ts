import { describe, expect, it } from "vitest";
import {
  CancelJobResponseSchema,
  CaptureRestartFrameSchema,
  CaptureRestartReasonSchema,
  ForgetMindResponseSchema,
  PruneRetentionResponseSchema,
  TrainingJobDetailResponseSchema,
  TrainingJobsResponseSchema,
  TrainingJobStatusSchema,
  TrainingJobSummarySchema,
  VoiceBypassTierStatusResponseSchema,
  VoiceRestartHistoryResponseSchema,
  WakeWordToggleRequestSchema,
  WakeWordToggleResponseSchema,
  WizardDevicesResponseSchema,
  WizardDiagnosticResponseSchema,
  WizardTestResultResponseSchema,
} from "./schemas";

// Voice Windows Paranoid Mission (v0.24.0) — pin the wire contract
// for the new CaptureRestartFrame surface so a backend rename is loud
// in CI rather than silent at runtime.

describe("CaptureRestartReasonSchema", () => {
  it("accepts every documented variant", () => {
    expect(CaptureRestartReasonSchema.parse("device_changed")).toBe(
      "device_changed",
    );
    expect(CaptureRestartReasonSchema.parse("apo_degraded")).toBe(
      "apo_degraded",
    );
    expect(CaptureRestartReasonSchema.parse("overflow")).toBe("overflow");
    expect(CaptureRestartReasonSchema.parse("manual")).toBe("manual");
  });

  it("rejects unknown variants", () => {
    expect(() => CaptureRestartReasonSchema.parse("teleport")).toThrow();
  });
});

// T3.12 — every CaptureRestartFrame field is now required at the
// zod boundary because the Python dataclass always serialises
// them (defaults to empty-string / 0). Tests use this helper to
// build a fully-defaulted frame; tests that exercise specific
// fields override only what they need.
const restartFrameDefaults = () => ({
  frame_type: "CaptureRestart" as const,
  timestamp_monotonic: 1,
  utterance_id: "",
  restart_reason: "" as const,
  old_host_api: "",
  new_host_api: "",
  old_device_id: "",
  new_device_id: "",
  old_signal_processing_mode: "",
  new_signal_processing_mode: "",
  recovery_latency_ms: 0,
  bypass_tier: 0,
});

describe("CaptureRestartFrameSchema", () => {
  it("parses a minimal default-valued frame (T3.12 contract)", () => {
    const frame = CaptureRestartFrameSchema.parse(restartFrameDefaults());
    expect(frame.frame_type).toBe("CaptureRestart");
    expect(frame.timestamp_monotonic).toBe(1);
    // All fields populated with defaults.
    expect(frame.utterance_id).toBe("");
    expect(frame.bypass_tier).toBe(0);
  });

  it("parses a full device_changed payload", () => {
    const frame = CaptureRestartFrameSchema.parse({
      ...restartFrameDefaults(),
      timestamp_monotonic: 100,
      utterance_id: "utt-1",
      restart_reason: "device_changed",
      old_host_api: "Windows WASAPI",
      new_host_api: "Windows WASAPI",
      old_device_id: "{old-guid}",
      new_device_id: "{new-guid}",
      old_signal_processing_mode: "Default",
      new_signal_processing_mode: "Default",
      recovery_latency_ms: 312,
      bypass_tier: 0,
    });
    expect(frame.restart_reason).toBe("device_changed");
    expect(frame.recovery_latency_ms).toBe(312);
    expect(frame.bypass_tier).toBe(0);
  });

  it("parses an apo_degraded payload with bypass_tier=2", () => {
    const frame = CaptureRestartFrameSchema.parse({
      ...restartFrameDefaults(),
      timestamp_monotonic: 200,
      restart_reason: "apo_degraded",
      old_host_api: "MME",
      new_host_api: "Windows WASAPI",
      old_signal_processing_mode: "Default",
      new_signal_processing_mode: "RAW",
      bypass_tier: 2,
    });
    expect(frame.bypass_tier).toBe(2);
    expect(frame.new_signal_processing_mode).toBe("RAW");
  });

  it("rejects bypass_tier outside [0, 3]", () => {
    expect(() =>
      CaptureRestartFrameSchema.parse({
        ...restartFrameDefaults(),
        bypass_tier: 4,
      }),
    ).toThrow();
    expect(() =>
      CaptureRestartFrameSchema.parse({
        ...restartFrameDefaults(),
        bypass_tier: -1,
      }),
    ).toThrow();
  });

  it("rejects negative recovery_latency_ms", () => {
    expect(() =>
      CaptureRestartFrameSchema.parse({
        ...restartFrameDefaults(),
        recovery_latency_ms: -5,
      }),
    ).toThrow();
  });

  it("rejects non-CaptureRestart frame_type", () => {
    expect(() =>
      CaptureRestartFrameSchema.parse({
        ...restartFrameDefaults(),
        frame_type: "TranscriptionFrame",
      }),
    ).toThrow();
  });

  it("rejects payloads missing required fields (T3.12 boundary)", () => {
    // Pre-T3.12 these fields were .optional() so a minimal payload
    // would pass. Post-T3.12 the schema rejects — backend regression
    // (e.g. a future refactor that drops dataclass fields) is caught
    // at the wire boundary.
    expect(() =>
      CaptureRestartFrameSchema.parse({
        frame_type: "CaptureRestart",
        timestamp_monotonic: 1,
        // missing utterance_id, restart_reason, all the new fields
      }),
    ).toThrow();
  });

  it("tolerates an unknown restart_reason string (forward-compat)", () => {
    // Backend may emit a future variant before frontend ships its
    // zod bump; the schema accepts any string fallback.
    const frame = CaptureRestartFrameSchema.parse({
      ...restartFrameDefaults(),
      restart_reason: "teleport",
    });
    expect(frame.restart_reason).toBe("teleport");
  });
});

describe("VoiceRestartHistoryResponseSchema", () => {
  it("parses the empty-state payload (T33 wired contract)", () => {
    const payload = VoiceRestartHistoryResponseSchema.parse({
      frames: [],
      limit: 50,
      total: 0,
    });
    expect(payload.frames).toEqual([]);
    expect(payload.total).toBe(0);
  });

  it("parses a populated payload with limit + total", () => {
    const payload = VoiceRestartHistoryResponseSchema.parse({
      frames: [
        {
          ...restartFrameDefaults(),
          restart_reason: "device_changed",
        },
      ],
      limit: 50,
      total: 1,
    });
    expect(payload.frames).toHaveLength(1);
    expect(payload.limit).toBe(50);
    expect(payload.total).toBe(1);
  });

  it("rejects payloads missing limit or total (T3.12 boundary)", () => {
    // Pre-T3.12 these were .optional(). Post-T3.12 the endpoint
    // always populates them so the schema rejects malformed
    // responses.
    expect(() =>
      VoiceRestartHistoryResponseSchema.parse({ frames: [] }),
    ).toThrow();
  });
});

// v0.26.0 wire-up: every counter field is now required at the zod
// boundary because the endpoint reads a deterministic ``BypassTierSnapshot``
// mirror that always populates them. Tests use this helper to build a
// fully-defaulted payload.
const bypassTierStatusDefaults = () => ({
  tier1_raw_attempted: 0,
  tier1_raw_succeeded: 0,
  tier2_host_api_rotate_attempted: 0,
  tier2_host_api_rotate_succeeded: 0,
  tier3_wasapi_exclusive_attempted: 0,
  tier3_wasapi_exclusive_succeeded: 0,
});

describe("VoiceBypassTierStatusResponseSchema", () => {
  it("parses the v0.26.0 zero-state payload (all counters present)", () => {
    const payload = VoiceBypassTierStatusResponseSchema.parse({
      ...bypassTierStatusDefaults(),
      current_bypass_tier: null,
    });
    expect(payload.tier1_raw_attempted).toBe(0);
    expect(payload.current_bypass_tier).toBeNull();
  });

  it("parses a populated tier-status payload", () => {
    const payload = VoiceBypassTierStatusResponseSchema.parse({
      ...bypassTierStatusDefaults(),
      current_bypass_tier: 1,
      tier1_raw_attempted: 5,
      tier1_raw_succeeded: 4,
      tier3_wasapi_exclusive_attempted: 1,
      tier3_wasapi_exclusive_succeeded: 1,
    });
    expect(payload.current_bypass_tier).toBe(1);
    expect(payload.tier1_raw_succeeded).toBe(4);
  });

  it("accepts current_bypass_tier=null (no bypass currently engaged)", () => {
    const payload = VoiceBypassTierStatusResponseSchema.parse({
      ...bypassTierStatusDefaults(),
      current_bypass_tier: null,
    });
    expect(payload.current_bypass_tier).toBeNull();
  });

  it("rejects current_bypass_tier outside [0, 3]", () => {
    expect(() =>
      VoiceBypassTierStatusResponseSchema.parse({
        ...bypassTierStatusDefaults(),
        current_bypass_tier: 7,
      }),
    ).toThrow();
  });

  it("rejects payloads missing required counters (v0.26.0 wire-up boundary)", () => {
    // Pre-v0.26.0 these were .optional() so a minimal {} payload would
    // pass. Post-wire-up the schema rejects — backend regression
    // (e.g. dropped field) is caught at the zod boundary in CI.
    expect(() => VoiceBypassTierStatusResponseSchema.parse({})).toThrow();
    expect(() =>
      VoiceBypassTierStatusResponseSchema.parse({
        tier1_raw_attempted: 0,
        // missing tier1_raw_succeeded + tier2/3 counters
      }),
    ).toThrow();
  });
});

// ── Phase 8 / T8.21 — mind forget + retention dashboard endpoints ─

describe("ForgetMindResponseSchema", () => {
  const valid = {
    mind_id: "aria",
    concepts_purged: 0,
    relations_purged: 0,
    episodes_purged: 0,
    concept_embeddings_purged: 0,
    episode_embeddings_purged: 0,
    conversation_imports_purged: 0,
    consolidation_log_purged: 0,
    conversations_purged: 0,
    conversation_turns_purged: 0,
    daily_stats_purged: 0,
    consent_ledger_purged: 0,
    total_brain_rows_purged: 0,
    total_conversations_rows_purged: 0,
    total_system_rows_purged: 0,
    total_rows_purged: 0,
    dry_run: true,
  };

  it("parses the dry-run zero-state payload", () => {
    expect(ForgetMindResponseSchema.parse(valid).dry_run).toBe(true);
  });

  it("parses a populated wipe payload", () => {
    const populated = {
      ...valid,
      dry_run: false,
      concepts_purged: 3,
      episodes_purged: 4,
      total_brain_rows_purged: 7,
      total_rows_purged: 7,
    };
    const parsed = ForgetMindResponseSchema.parse(populated);
    expect(parsed.concepts_purged).toBe(3);
    expect(parsed.total_rows_purged).toBe(7);
  });

  it("rejects negative counts", () => {
    expect(() =>
      ForgetMindResponseSchema.parse({ ...valid, concepts_purged: -1 }),
    ).toThrow();
  });

  it("rejects non-integer counts", () => {
    expect(() =>
      ForgetMindResponseSchema.parse({ ...valid, episodes_purged: 3.5 }),
    ).toThrow();
  });

  it("rejects payloads missing required fields", () => {
    expect(() => ForgetMindResponseSchema.parse({})).toThrow();
    const { mind_id: _omit, ...withoutMindId } = valid;
    expect(() => ForgetMindResponseSchema.parse(withoutMindId)).toThrow();
  });
});

describe("PruneRetentionResponseSchema", () => {
  const valid = {
    mind_id: "aria",
    cutoff_utc: "2026-04-01T00:00:00+00:00",
    episodes_purged: 0,
    conversations_purged: 0,
    conversation_turns_purged: 0,
    daily_stats_purged: 0,
    consolidation_log_purged: 0,
    consent_ledger_purged: 0,
    effective_horizons: {
      episodes: 30,
      conversations: 30,
      consolidation_log: 90,
      daily_stats: 365,
      consent_ledger: 0,
    },
    total_brain_rows_purged: 0,
    total_conversations_rows_purged: 0,
    total_system_rows_purged: 0,
    total_rows_purged: 0,
    dry_run: true,
  };

  it("parses a dry-run preview with default horizons", () => {
    const parsed = PruneRetentionResponseSchema.parse(valid);
    expect(parsed.effective_horizons.episodes).toBe(30);
    expect(parsed.effective_horizons.consent_ledger).toBe(0);
  });

  it("parses a real-run with mind override horizons", () => {
    const realRun = {
      ...valid,
      dry_run: false,
      effective_horizons: { ...valid.effective_horizons, episodes: 90 },
      episodes_purged: 5,
      total_brain_rows_purged: 5,
      total_rows_purged: 5,
    };
    const parsed = PruneRetentionResponseSchema.parse(realRun);
    expect(parsed.effective_horizons.episodes).toBe(90);
    expect(parsed.episodes_purged).toBe(5);
  });

  it("rejects negative horizon values", () => {
    expect(() =>
      PruneRetentionResponseSchema.parse({
        ...valid,
        effective_horizons: { episodes: -1 },
      }),
    ).toThrow();
  });

  it("rejects payloads missing cutoff_utc", () => {
    const { cutoff_utc: _omit, ...withoutCutoff } = valid;
    expect(() => PruneRetentionResponseSchema.parse(withoutCutoff)).toThrow();
  });
});

// ── MISSION-pre-wake-word-ui-hardening §T4 — wake-word toggle ──

describe("WakeWordToggleRequestSchema", () => {
  it("parses an enable request", () => {
    expect(WakeWordToggleRequestSchema.parse({ enabled: true }).enabled).toBe(
      true,
    );
  });

  it("parses a disable request", () => {
    expect(WakeWordToggleRequestSchema.parse({ enabled: false }).enabled).toBe(
      false,
    );
  });

  it("rejects payloads missing the enabled field", () => {
    expect(() => WakeWordToggleRequestSchema.parse({})).toThrow();
  });

  it("rejects non-boolean enabled values", () => {
    expect(() =>
      WakeWordToggleRequestSchema.parse({ enabled: "true" }),
    ).toThrow();
    expect(() => WakeWordToggleRequestSchema.parse({ enabled: 1 })).toThrow();
  });
});

describe("WakeWordToggleResponseSchema", () => {
  const happyPath = {
    mind_id: "aria",
    enabled: true,
    persisted: true,
    applied_immediately: true,
    hot_apply_detail: null,
  };

  it("parses the happy-path payload (applied_immediately=true, detail=null)", () => {
    const parsed = WakeWordToggleResponseSchema.parse(happyPath);
    expect(parsed.applied_immediately).toBe(true);
    expect(parsed.hot_apply_detail).toBeNull();
  });

  it("parses the cold-start payload (applied_immediately=false with detail)", () => {
    const coldStart = {
      ...happyPath,
      applied_immediately: false,
      hot_apply_detail:
        "voice subsystem not running — change persisted; will apply on next boot",
    };
    const parsed = WakeWordToggleResponseSchema.parse(coldStart);
    expect(parsed.applied_immediately).toBe(false);
    expect(parsed.hot_apply_detail).toContain("next boot");
  });

  it("parses the disable payload", () => {
    const disable = {
      mind_id: "lucia",
      enabled: false,
      persisted: true,
      applied_immediately: true,
      hot_apply_detail: null,
    };
    expect(WakeWordToggleResponseSchema.parse(disable).enabled).toBe(false);
  });

  it("rejects payloads missing required fields", () => {
    expect(() => WakeWordToggleResponseSchema.parse({})).toThrow();
    const { mind_id: _omit, ...withoutMindId } = happyPath;
    expect(() => WakeWordToggleResponseSchema.parse(withoutMindId)).toThrow();
  });

  it("rejects non-string hot_apply_detail (must be string | null)", () => {
    expect(() =>
      WakeWordToggleResponseSchema.parse({
        ...happyPath,
        hot_apply_detail: 42,
      }),
    ).toThrow();
  });
});

// ── Phase 7 / T7.21-T7.24 — voice wizard endpoints ─

describe("WizardDevicesResponseSchema", () => {
  it("parses an empty-list payload (no audio hardware)", () => {
    const parsed = WizardDevicesResponseSchema.parse({
      devices: [],
      total_count: 0,
      default_device_id: null,
    });
    expect(parsed.devices).toEqual([]);
    expect(parsed.default_device_id).toBeNull();
  });

  it("parses a populated devices payload", () => {
    const parsed = WizardDevicesResponseSchema.parse({
      devices: [
        {
          device_id: "0",
          name: "Built-in Microphone",
          friendly_name: "Built-in Microphone",
          max_input_channels: 2,
          default_sample_rate: 48000,
          is_default: true,
          diagnosis_hint: "ready",
        },
      ],
      total_count: 1,
      default_device_id: "0",
    });
    expect(parsed.devices.length).toBe(1);
    expect(parsed.devices[0].diagnosis_hint).toBe("ready");
  });

  it("rejects negative channel counts", () => {
    expect(() =>
      WizardDevicesResponseSchema.parse({
        devices: [
          {
            device_id: "0",
            name: "x",
            friendly_name: "x",
            max_input_channels: -1,
            default_sample_rate: 48000,
            is_default: false,
            diagnosis_hint: "ready",
          },
        ],
        total_count: 1,
        default_device_id: null,
      }),
    ).toThrow();
  });
});

describe("WizardTestResultResponseSchema", () => {
  const valid = {
    session_id: "abc123",
    success: true,
    duration_actual_s: 3.0,
    sample_rate_hz: 16000,
    level_rms_dbfs: -20.0,
    level_peak_dbfs: -6.0,
    snr_db: 25.0,
    clipping_detected: false,
    silent_capture: false,
    diagnosis: "ok",
    diagnosis_hint: "Microphone looks good.",
    recorded_at_utc: "2026-05-02T12:00:00+00:00",
    error: null,
  };

  it("parses a successful capture payload", () => {
    expect(WizardTestResultResponseSchema.parse(valid).diagnosis).toBe("ok");
  });

  it("parses a silent-capture payload (null levels)", () => {
    const silent = {
      ...valid,
      success: true,
      silent_capture: true,
      level_rms_dbfs: null,
      level_peak_dbfs: null,
      snr_db: null,
      diagnosis: "no_audio",
      diagnosis_hint: "No usable signal captured.",
    };
    const parsed = WizardTestResultResponseSchema.parse(silent);
    expect(parsed.silent_capture).toBe(true);
    expect(parsed.level_peak_dbfs).toBeNull();
  });

  it("parses a recorder-error payload (success=false)", () => {
    const errorPayload = {
      ...valid,
      success: false,
      level_rms_dbfs: null,
      level_peak_dbfs: null,
      snr_db: null,
      diagnosis: "device_error",
      diagnosis_hint: "Permission denied. Open System Settings...",
      error: "Permission denied",
    };
    const parsed = WizardTestResultResponseSchema.parse(errorPayload);
    expect(parsed.success).toBe(false);
    expect(parsed.error).toBe("Permission denied");
  });

  it("rejects negative duration", () => {
    expect(() =>
      WizardTestResultResponseSchema.parse({
        ...valid,
        duration_actual_s: -1.0,
      }),
    ).toThrow();
  });
});

describe("WizardDiagnosticResponseSchema", () => {
  it("parses a ready=true payload (no APOs)", () => {
    const parsed = WizardDiagnosticResponseSchema.parse({
      ready: true,
      voice_clarity_active: false,
      active_device_name: null,
      platform: "linux",
      recommendations: [],
    });
    expect(parsed.ready).toBe(true);
    expect(parsed.recommendations).toEqual([]);
  });

  it("parses a not-ready payload with Voice Clarity active", () => {
    const parsed = WizardDiagnosticResponseSchema.parse({
      ready: false,
      voice_clarity_active: true,
      active_device_name: "Razer BlackShark V2 Pro",
      platform: "win32",
      recommendations: ["Windows Voice Clarity APO is active..."],
    });
    expect(parsed.voice_clarity_active).toBe(true);
    expect(parsed.recommendations.length).toBe(1);
  });

  it("rejects payloads missing platform", () => {
    expect(() =>
      WizardDiagnosticResponseSchema.parse({
        ready: true,
        voice_clarity_active: false,
        active_device_name: null,
        recommendations: [],
      }),
    ).toThrow();
  });
});

// ── Phase 8 / T8.13 — wake-word training endpoints ─

describe("TrainingJobStatusSchema", () => {
  it("accepts every documented status", () => {
    for (const status of [
      "pending",
      "synthesizing",
      "training",
      "complete",
      "failed",
      "cancelled",
    ]) {
      expect(TrainingJobStatusSchema.parse(status)).toBe(status);
    }
  });

  it("rejects unknown status string", () => {
    expect(() => TrainingJobStatusSchema.parse("running")).toThrow();
  });
});

describe("TrainingJobSummarySchema", () => {
  const valid = {
    job_id: "lucia",
    wake_word: "Lúcia",
    mind_id: "lucia",
    language: "pt-BR",
    status: "synthesizing" as const,
    progress: 0.45,
    samples_generated: 9,
    target_samples: 20,
    started_at: "2026-05-02T12:00:00+00:00",
    updated_at: "2026-05-02T12:01:00+00:00",
    completed_at: "",
    output_path: "",
    error_summary: "",
    cancelled_signalled: false,
  };

  it("parses an in-flight summary", () => {
    expect(TrainingJobSummarySchema.parse(valid).status).toBe("synthesizing");
  });

  it("rejects progress outside [0, 1]", () => {
    expect(() =>
      TrainingJobSummarySchema.parse({ ...valid, progress: 1.5 }),
    ).toThrow();
    expect(() =>
      TrainingJobSummarySchema.parse({ ...valid, progress: -0.1 }),
    ).toThrow();
  });

  it("rejects negative samples_generated", () => {
    expect(() =>
      TrainingJobSummarySchema.parse({ ...valid, samples_generated: -1 }),
    ).toThrow();
  });
});

describe("TrainingJobsResponseSchema", () => {
  it("parses the empty-state payload", () => {
    expect(TrainingJobsResponseSchema.parse({ jobs: [], total_count: 0 })).toEqual({
      jobs: [],
      total_count: 0,
    });
  });
});

describe("TrainingJobDetailResponseSchema", () => {
  const summary = {
    job_id: "lucia",
    wake_word: "Lúcia",
    mind_id: "lucia",
    language: "pt-BR",
    status: "complete" as const,
    progress: 1.0,
    samples_generated: 20,
    target_samples: 20,
    started_at: "2026-05-02T12:00:00+00:00",
    updated_at: "2026-05-02T12:30:00+00:00",
    completed_at: "2026-05-02T12:30:00+00:00",
    output_path: "/tmp/lucia.onnx",
    error_summary: "",
    cancelled_signalled: false,
  };

  it("parses a complete-state detail with history", () => {
    const parsed = TrainingJobDetailResponseSchema.parse({
      summary,
      history: [
        { status: "pending", progress: 0, samples_generated: 0 },
        { status: "complete", progress: 1.0, samples_generated: 20 },
      ],
      history_truncated: false,
    });
    expect(parsed.summary.status).toBe("complete");
    expect(parsed.history.length).toBe(2);
  });

  it("rejects payloads missing summary", () => {
    expect(() =>
      TrainingJobDetailResponseSchema.parse({
        history: [],
        history_truncated: false,
      }),
    ).toThrow();
  });
});

describe("CancelJobResponseSchema", () => {
  it("parses a successful cancel", () => {
    expect(
      CancelJobResponseSchema.parse({
        job_id: "lucia",
        cancel_signal_written: true,
        already_terminal: false,
      }).already_terminal,
    ).toBe(false);
  });

  it("rejects payloads missing booleans", () => {
    expect(() =>
      CancelJobResponseSchema.parse({ job_id: "lucia" }),
    ).toThrow();
  });
});
