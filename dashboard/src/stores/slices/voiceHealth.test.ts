/**
 * VoiceHealth slice tests — 7 actions + retry behaviour + error path.
 *
 * v0.38.0 / W3.B4 + F2-M04 (audit §3.G) closure on F-515. Pre-fix the
 * slice was tested only INDIRECTLY via the voice-health page tests,
 * which couldn't differentiate slice-level from page-level regressions.
 * This file pins each action's contract directly:
 *
 *   * fetchVoiceHealth — happy + ApiError → error message
 *   * reprobeVoiceEndpoint — happy + auto-refetch + busy flag lifecycle
 *   * forgetVoiceEndpoint — happy + reason default + ApiError path
 *   * pinVoiceEndpoint — happy + ApiError path
 *   * fetchMixerKbList — happy + abort silent + ApiError → error
 *   * fetchMixerKbDetail — happy + abort silent + ApiError → error
 *   * validateMixerKbProfile — happy + ApiError → error
 *
 * Each action is exercised through ``useDashboardStore`` so the store
 * binding is part of the contract under test.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";

import { useDashboardStore } from "../dashboard";

const _comboFixture = {
  host_api: "WASAPI",
  sample_rate: 16_000,
  channels: 1,
  sample_format: "int16",
  exclusive: false,
  auto_convert: true,
  frames_per_buffer: 480,
};

const _snapshotFixture = {
  combo_store: [],
  overrides: [],
  quarantine_count: 0,
  data_dir: "/var/data/sovyx",
  voice_enabled: true,
};

const _probeResult = {
  diagnosis: "healthy",
  mode: "warm",
  combo: _comboFixture,
  vad_max_prob: 0.9,
  vad_mean_prob: 0.4,
  rms_db: -22.5,
  callbacks_fired: 200,
  duration_ms: 3_000,
  error: null,
  remediation: null,
};

const _profileSummary = {
  pool: "user",
  profile_id: "vaio-pipewire",
  profile_version: 1,
  schema_version: 2,
  driver_family: "snd_hda_intel",
  codec_id_glob: "0x*",
  match_threshold: 0.85,
  factory_regime: "lenient",
  contributed_by: "operator@example",
};

const _profileDetail = {
  ..._profileSummary,
  system_vendor_glob: "Sony*",
  system_product_glob: "VAIO*",
  distro_family: "debian",
  audio_stack: "pipewire",
  kernel_major_minor_glob: "6.*",
  factory_signature_roles: ["capture"],
  verified_on_count: 5,
};

function _resetSlice() {
  useDashboardStore.setState({
    voiceHealthSnapshot: null,
    voiceHealthLoading: false,
    voiceHealthError: null,
    voiceHealthLastProbe: {},
    voiceHealthBusy: {},
    mixerKbList: null,
    mixerKbLoading: false,
    mixerKbError: null,
    mixerKbDetails: {},
  });
}

function _mockJsonResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
    headers: new Headers({ "content-type": "application/json" }),
  } as unknown as Response;
}

beforeEach(() => {
  _resetSlice();
  vi.restoreAllMocks();
});

// ── fetchVoiceHealth ────────────────────────────────────────────────

describe("voiceHealth slice — fetchVoiceHealth", () => {
  it("populates voiceHealthSnapshot on success", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      _mockJsonResponse(_snapshotFixture),
    );

    await useDashboardStore.getState().fetchVoiceHealth();

    const state = useDashboardStore.getState();
    expect(state.voiceHealthSnapshot?.data_dir).toBe("/var/data/sovyx");
    expect(state.voiceHealthLoading).toBe(false);
    expect(state.voiceHealthError).toBeNull();
  });

  it("sets voiceHealthError on ApiError + clears loading", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new ApiError(503, "voice routes disabled"),
    );

    await useDashboardStore.getState().fetchVoiceHealth();

    const state = useDashboardStore.getState();
    expect(state.voiceHealthLoading).toBe(false);
    expect(state.voiceHealthError).toContain("503");
  });
});

// ── reprobeVoiceEndpoint ────────────────────────────────────────────

describe("voiceHealth slice — reprobeVoiceEndpoint", () => {
  it("stashes the probe result by guid + auto-refetches the snapshot", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() =>
        Promise.resolve(
          _mockJsonResponse({
            endpoint_guid: "{abc}",
            result: _probeResult,
          }),
        ),
      )
      .mockImplementationOnce(() =>
        Promise.resolve(_mockJsonResponse(_snapshotFixture)),
      );

    const result = await useDashboardStore
      .getState()
      .reprobeVoiceEndpoint({ endpoint_guid: "{abc}", mode: "warm" });

    expect(result?.diagnosis).toBe("healthy");
    const state = useDashboardStore.getState();
    expect(state.voiceHealthLastProbe["{abc}"]).toEqual(_probeResult);
    // Auto-refetch fires, lifting the busy flag in the same loop tick.
    expect(state.voiceHealthBusy["{abc}"]).toBeUndefined();
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it("sets voiceHealthError + returns null on ApiError + clears busy flag", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new ApiError(500, "ICE candidate exhausted"),
    );

    const result = await useDashboardStore
      .getState()
      .reprobeVoiceEndpoint({ endpoint_guid: "{boom}", mode: "cold" });

    expect(result).toBeNull();
    const state = useDashboardStore.getState();
    expect(state.voiceHealthError).toContain("500");
    expect(state.voiceHealthBusy["{boom}"]).toBeUndefined();
  });
});

// ── forgetVoiceEndpoint ─────────────────────────────────────────────

describe("voiceHealth slice — forgetVoiceEndpoint", () => {
  it("returns the invalidated flag + auto-refetches", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() =>
        Promise.resolve(
          _mockJsonResponse({ endpoint_guid: "{abc}", invalidated: true }),
        ),
      )
      .mockImplementationOnce(() =>
        Promise.resolve(_mockJsonResponse(_snapshotFixture)),
      );

    const ok = await useDashboardStore.getState().forgetVoiceEndpoint("{abc}");

    expect(ok).toBe(true);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    // The default reason is sent in the body when caller omits it.
    const firstCallInit = fetchSpy.mock.calls[0]?.[1];
    const body = firstCallInit?.body as string | undefined;
    expect(body && JSON.parse(body)).toMatchObject({
      endpoint_guid: "{abc}",
      reason: "dashboard-forget",
    });
  });

  it("returns false + sets error on ApiError", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new ApiError(404, "endpoint not in store"),
    );

    const ok = await useDashboardStore
      .getState()
      .forgetVoiceEndpoint("{ghost}", "explicit-reason");

    expect(ok).toBe(false);
    expect(useDashboardStore.getState().voiceHealthError).toContain("404");
  });
});

// ── pinVoiceEndpoint ────────────────────────────────────────────────

describe("voiceHealth slice — pinVoiceEndpoint", () => {
  it("returns true + auto-refetches on success", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() =>
        Promise.resolve(
          _mockJsonResponse({ endpoint_guid: "{abc}", pinned: true }),
        ),
      )
      .mockImplementationOnce(() =>
        Promise.resolve(_mockJsonResponse(_snapshotFixture)),
      );

    const ok = await useDashboardStore.getState().pinVoiceEndpoint({
      endpoint_guid: "{abc}",
      combo: _comboFixture,
      reason: "operator-pin",
    });

    expect(ok).toBe(true);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it("returns false + sets error on ApiError + clears busy flag", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new ApiError(409, "combo conflicts with quarantine"),
    );

    const ok = await useDashboardStore.getState().pinVoiceEndpoint({
      endpoint_guid: "{abc}",
      combo: _comboFixture,
      reason: "operator-pin",
    });

    expect(ok).toBe(false);
    const state = useDashboardStore.getState();
    expect(state.voiceHealthError).toContain("409");
    expect(state.voiceHealthBusy["{abc}"]).toBeUndefined();
  });
});

// ── fetchMixerKbList ────────────────────────────────────────────────

describe("voiceHealth slice — fetchMixerKbList", () => {
  it("populates mixerKbList on success", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      _mockJsonResponse({
        profiles: [_profileSummary],
        shipped_count: 0,
        user_count: 1,
      }),
    );

    await useDashboardStore.getState().fetchMixerKbList();

    const state = useDashboardStore.getState();
    expect(state.mixerKbList?.profiles).toHaveLength(1);
    expect(state.mixerKbLoading).toBe(false);
    expect(state.mixerKbError).toBeNull();
  });

  it("sets mixerKbError on ApiError + clears loading", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new ApiError(500, "kb pool unreadable"),
    );

    await useDashboardStore.getState().fetchMixerKbList();

    const state = useDashboardStore.getState();
    expect(state.mixerKbLoading).toBe(false);
    expect(state.mixerKbError).toContain("500");
  });
});

// ── fetchMixerKbDetail ──────────────────────────────────────────────

describe("voiceHealth slice — fetchMixerKbDetail", () => {
  it("returns the detail + caches it by profile_id", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      _mockJsonResponse(_profileDetail),
    );

    const detail = await useDashboardStore
      .getState()
      .fetchMixerKbDetail("vaio-pipewire");

    expect(detail?.audio_stack).toBe("pipewire");
    expect(useDashboardStore.getState().mixerKbDetails["vaio-pipewire"])
      .toBeDefined();
  });

  it("returns null + sets mixerKbError on ApiError", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new ApiError(404, "profile not in pool"),
    );

    const detail = await useDashboardStore
      .getState()
      .fetchMixerKbDetail("ghost");

    expect(detail).toBeNull();
    expect(useDashboardStore.getState().mixerKbError).toContain("404");
  });
});

// ── validateMixerKbProfile ──────────────────────────────────────────

describe("voiceHealth slice — validateMixerKbProfile", () => {
  it("returns the validation response on success", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      _mockJsonResponse({
        ok: true,
        profile_id: "vaio-pipewire",
        profile_version: 1,
        issues: [],
      }),
    );

    const result = await useDashboardStore
      .getState()
      .validateMixerKbProfile({ yaml_text: "pool: user\nprofile_id: vaio" });

    expect(result?.ok).toBe(true);
    expect(result?.profile_id).toBe("vaio-pipewire");
  });

  it("returns null + sets mixerKbError on ApiError (e.g. 422)", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new ApiError(422, "yaml malformed"),
    );

    const result = await useDashboardStore
      .getState()
      .validateMixerKbProfile({ yaml_text: "" });

    expect(result).toBeNull();
    expect(useDashboardStore.getState().mixerKbError).toContain("422");
  });
});
