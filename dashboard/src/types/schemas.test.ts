import { describe, expect, it } from "vitest";
import {
  CaptureRestartFrameSchema,
  CaptureRestartReasonSchema,
  VoiceBypassTierStatusResponseSchema,
  VoiceRestartHistoryResponseSchema,
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
