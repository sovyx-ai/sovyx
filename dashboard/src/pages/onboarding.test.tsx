/**
 * OnboardingPage tests — focused on the v0.31.6 T3.2 (M3.c) wire-up
 * that surfaces ``voice_configured: false`` from the backend's
 * ``POST /api/onboarding/complete`` response into the dashboard's
 * ``voiceWarning`` store slot.
 *
 * Mission: ``MISSION-voice-v0_31_6-paranoid-closure-2026-05-08.md``
 * §Phase 3 T3.2.
 *
 * The test exercises the ``handleSkipAll`` path (Step 1 → "I'll
 * configure manually" button) since both ``handleComplete`` and
 * ``handleSkipAll`` use IDENTICAL wire-up logic — the same
 * ``api.post(..., { schema: OnboardingCompleteResponseSchema })``
 * call followed by the ``setVoiceWarning`` decision. Driving Step 1
 * is significantly simpler than scaffolding Step 5 (which requires
 * the live FirstChatStep's chat thread).
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";
import userEvent from "@testing-library/user-event";
import OnboardingPage from "./onboarding";
import { useDashboardStore } from "@/stores/dashboard";
// v0.32.0 BT.B.1: the page now reads /api/onboarding/state via the
// shared module-level singleton in ``useResolvedMindId`` /
// ``useOnboardingState``. The singleton caches the resolved snapshot
// across re-renders, which is correct for production but wrong for
// tests — every ``it()`` block needs a fresh fetch. Reset between
// tests via the test-only escape hatch.
import { __resetResolvedMindIdCacheForTests } from "@/hooks/use-resolved-mind-id";

vi.mock("@/lib/api", () => {
  return {
    api: {
      get: vi.fn(),
      post: vi.fn(),
    },
    apiFetch: vi.fn(),
    isAbortError: () => false,
    getToken: () => "test-token",
    BASE_URL: "",
    setToken: vi.fn(),
    clearToken: vi.fn(),
  };
});

import { api } from "@/lib/api";

const mockApi = api as unknown as {
  get: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
};

// Stage Step 1 of the onboarding page (provider_configured: false →
// no fast-forward) so the "I'll configure manually" button is visible.
function stageStep1(): void {
  mockApi.get.mockResolvedValue({
    complete: false,
    mind_name: "TestMind",
    mind_id: "test-mind",
    provider_configured: false,
    default_provider: "",
    default_model: "",
    ollama_available: false,
    ollama_models: [],
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  // Reset the BT.B.1 singleton so every test starts from a clean
  // "not yet fetched" state. Without this, the second ``it()`` block
  // would observe the cached snapshot from the first.
  __resetResolvedMindIdCacheForTests();
  // Reset voiceWarning between tests (in-memory zustand state survives
  // by default, so we must explicitly clear it).
  useDashboardStore.setState({ voiceWarning: null });
});

describe("OnboardingPage — voice_configured wire-up", () => {
  it("sets voiceWarning when backend returns voice_configured: false", async () => {
    const user = userEvent.setup();
    stageStep1();
    mockApi.post.mockResolvedValueOnce({
      ok: true,
      voice_configured: false,
    });

    render(<OnboardingPage />);
    // Wait for initial state fetch to land us on Step 1.
    const skipButton = await screen.findByText(/I'll configure manually/i);
    await user.click(skipButton);

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith(
        "/api/onboarding/complete",
        {},
        expect.objectContaining({ schema: expect.any(Object) }),
      ),
    );
    await waitFor(() =>
      expect(useDashboardStore.getState().voiceWarning).toEqual({
        kind: "voice_not_configured",
      }),
    );
  });

  it("leaves voiceWarning null when backend returns voice_configured: true", async () => {
    const user = userEvent.setup();
    stageStep1();
    mockApi.post.mockResolvedValueOnce({
      ok: true,
      voice_configured: true,
    });

    render(<OnboardingPage />);
    const skipButton = await screen.findByText(/I'll configure manually/i);
    await user.click(skipButton);

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    expect(useDashboardStore.getState().voiceWarning).toBeNull();
  });

  it("leaves voiceWarning null when backend omits voice_configured (pre-v0.31.4)", async () => {
    const user = userEvent.setup();
    stageStep1();
    mockApi.post.mockResolvedValueOnce({ ok: true });

    render(<OnboardingPage />);
    const skipButton = await screen.findByText(/I'll configure manually/i);
    await user.click(skipButton);

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    expect(useDashboardStore.getState().voiceWarning).toBeNull();
  });

  it("leaves voiceWarning null when the backend call rejects (best-effort)", async () => {
    const user = userEvent.setup();
    stageStep1();
    mockApi.post.mockRejectedValueOnce(new Error("network down"));

    render(<OnboardingPage />);
    const skipButton = await screen.findByText(/I'll configure manually/i);
    await user.click(skipButton);

    await waitFor(() => expect(mockApi.post).toHaveBeenCalled());
    // Network error → best-effort path; warning stays null + navigation
    // still fires. We assert only the warning side-effect here; navigation
    // is handled by react-router and out of scope for this slice test.
    expect(useDashboardStore.getState().voiceWarning).toBeNull();
  });
});

// Slice-level integration: pin the store contract that the page depends on.
describe("voiceWarning slice", () => {
  beforeEach(() => {
    useDashboardStore.setState({ voiceWarning: null });
  });

  it("initial state is null", () => {
    expect(useDashboardStore.getState().voiceWarning).toBeNull();
  });

  it("setVoiceWarning sets the warning", () => {
    useDashboardStore
      .getState()
      .setVoiceWarning({ kind: "voice_not_configured" });
    expect(useDashboardStore.getState().voiceWarning).toEqual({
      kind: "voice_not_configured",
    });
  });

  it("clearVoiceWarning resets to null", () => {
    useDashboardStore
      .getState()
      .setVoiceWarning({ kind: "voice_not_configured" });
    useDashboardStore.getState().clearVoiceWarning();
    expect(useDashboardStore.getState().voiceWarning).toBeNull();
  });

  it("setVoiceWarning(null) clears the warning", () => {
    useDashboardStore
      .getState()
      .setVoiceWarning({ kind: "voice_not_configured" });
    useDashboardStore.getState().setVoiceWarning(null);
    expect(useDashboardStore.getState().voiceWarning).toBeNull();
  });
});
