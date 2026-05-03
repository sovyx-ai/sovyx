/**
 * Wake-word slice tests — Mission MISSION-wake-word-ui-2026-05-03 §T3 (D5).
 *
 * Validates: initial state, fetchPerMindStatus (success + error paths),
 * toggleMind (optimistic update + 200 reconcile + 422 rollback +
 * 500 rollback). The slice is the single source of truth for the
 * v0.29.0 React component (T4) so the contract tests pin every
 * branch the UI consumes.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";

import type { WakeWordPerMindStatus } from "@/types/api";

import { useDashboardStore } from "../dashboard";

// ── Mock data ─────────────────────────────────────────────────────────

const HEALTHY_MIND: WakeWordPerMindStatus = {
  mind_id: "aria",
  wake_word: "Aria",
  voice_language: "en",
  wake_word_enabled: true,
  runtime_registered: true,
  model_path: "/data/wake_word_models/pretrained/aria.onnx",
  resolution_strategy: "exact",
  last_error: null,
};

const BROKEN_MIND: WakeWordPerMindStatus = {
  mind_id: "lucia",
  wake_word: "Lucia",
  voice_language: "pt-BR",
  wake_word_enabled: true,
  runtime_registered: false,
  model_path: null,
  resolution_strategy: "none",
  last_error:
    "No ONNX model resolved for wake word 'Lucia' ... train via `sovyx voice train-wake-word`",
};

const DISABLED_MIND: WakeWordPerMindStatus = {
  mind_id: "joao",
  wake_word: "Joao",
  voice_language: "en",
  wake_word_enabled: false,
  runtime_registered: false,
  model_path: null,
  resolution_strategy: null,
  last_error: null,
};

function _resetWakeWordState() {
  useDashboardStore.setState({
    perMindStatus: [],
    wakeWordLoading: false,
    wakeWordError: null,
  });
}

beforeEach(() => {
  _resetWakeWordState();
  vi.restoreAllMocks();
});

// ── Initial state ─────────────────────────────────────────────────────

describe("wakeWord slice — initial state", () => {
  it("starts with empty perMindStatus and no error", () => {
    const state = useDashboardStore.getState();
    expect(state.perMindStatus).toEqual([]);
    expect(state.wakeWordLoading).toBe(false);
    expect(state.wakeWordError).toBeNull();
  });
});

// ── clearWakeWordError ────────────────────────────────────────────────

describe("wakeWord slice — clearWakeWordError", () => {
  it("clears the error field", () => {
    useDashboardStore.setState({ wakeWordError: "boom" });
    useDashboardStore.getState().clearWakeWordError();
    expect(useDashboardStore.getState().wakeWordError).toBeNull();
  });
});

// ── fetchPerMindStatus ────────────────────────────────────────────────

describe("wakeWord slice — fetchPerMindStatus", () => {
  it("sets loading and populates perMindStatus on success", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ minds: [HEALTHY_MIND, BROKEN_MIND, DISABLED_MIND] }),
    } as Response);

    await useDashboardStore.getState().fetchPerMindStatus();

    const state = useDashboardStore.getState();
    expect(state.perMindStatus).toHaveLength(3);
    expect(state.wakeWordLoading).toBe(false);
    expect(state.wakeWordError).toBeNull();
  });

  it("sets error when network fails", async () => {
    // ``mockRejectedValue`` (not Once) — api.ts retries idempotent
    // verbs up to 2× on network errors, so a single rejection would
    // get masked by a real-fetch URL-parse on retry.
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Network down"));

    await useDashboardStore.getState().fetchPerMindStatus();

    const state = useDashboardStore.getState();
    expect(state.wakeWordLoading).toBe(false);
    expect(state.wakeWordError).toContain("Network");
  });

  it("clears prior error at start of fetch", async () => {
    useDashboardStore.setState({ wakeWordError: "prior failure" });
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ minds: [] }),
    } as Response);

    await useDashboardStore.getState().fetchPerMindStatus();

    expect(useDashboardStore.getState().wakeWordError).toBeNull();
  });
});

// ── toggleMind: happy path (optimistic + reconcile) ──────────────────

describe("wakeWord slice — toggleMind happy path", () => {
  it("optimistically updates wake_word_enabled on click + refetches on 200", async () => {
    // Seed: aria currently disabled.
    useDashboardStore.setState({
      perMindStatus: [{ ...HEALTHY_MIND, wake_word_enabled: false, runtime_registered: false }],
    });

    const fetchSpy = vi.spyOn(globalThis, "fetch");
    // First call: POST /toggle returns 200.
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: () =>
        Promise.resolve({
          mind_id: "aria",
          enabled: true,
          persisted: true,
          applied_immediately: true,
          hot_apply_detail: null,
        }),
    } as Response);
    // Second call: refetch returns the reconciled state.
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ minds: [HEALTHY_MIND] }),
    } as Response);

    await useDashboardStore.getState().toggleMind("aria", true);

    const state = useDashboardStore.getState();
    // After reconcile, runtime_registered=true (the backend hot-applied).
    expect(state.perMindStatus[0].wake_word_enabled).toBe(true);
    expect(state.perMindStatus[0].runtime_registered).toBe(true);
    expect(state.wakeWordError).toBeNull();
  });
});

// ── toggleMind: rollback on 422 (NONE strategy) ──────────────────────

describe("wakeWord slice — toggleMind 422 rollback", () => {
  it("rolls back optimistic update + populates error with detail message", async () => {
    // Seed: aria currently disabled.
    const initial = [{ ...HEALTHY_MIND, wake_word_enabled: false, runtime_registered: false }];
    useDashboardStore.setState({ perMindStatus: initial });

    const remediation =
      "No ONNX model resolved for wake word 'Aria' ... train via `sovyx voice train-wake-word`";
    // api.ts reads error responses via response.text() then ApiError
    // parses the body as JSON internally. The mock MUST expose
    // ``text()`` (not ``json()``) — different code path from success.
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 422,
      text: () => Promise.resolve(JSON.stringify({ detail: remediation })),
    } as Response);

    await useDashboardStore.getState().toggleMind("aria", true);

    const state = useDashboardStore.getState();
    // Rollback: aria's wake_word_enabled is still false.
    expect(state.perMindStatus[0].wake_word_enabled).toBe(false);
    // Error message surfaces the resolver's remediation text.
    expect(state.wakeWordError).toBe(remediation);
  });
});

// ── toggleMind: rollback on 500 ──────────────────────────────────────

describe("wakeWord slice — toggleMind 500 rollback", () => {
  it("rolls back + populates error with backend message", async () => {
    const initial = [{ ...HEALTHY_MIND, wake_word_enabled: false }];
    useDashboardStore.setState({ perMindStatus: initial });

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 500,
      text: () =>
        Promise.resolve(JSON.stringify({ detail: "ConfigEditor write failed: ENOSPC" })),
    } as Response);

    await useDashboardStore.getState().toggleMind("aria", true);

    const state = useDashboardStore.getState();
    expect(state.perMindStatus[0].wake_word_enabled).toBe(false); // rollback
    expect(state.wakeWordError).toContain("ConfigEditor");
  });
});

// ── toggleMind: unknown mind ─────────────────────────────────────────

describe("wakeWord slice — toggleMind unknown mind", () => {
  it("sets error without firing the network call", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    await useDashboardStore.getState().toggleMind("nonexistent", true);

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(useDashboardStore.getState().wakeWordError).toContain("nonexistent");
  });
});
