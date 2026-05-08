import type { StateCreator } from "zustand";
import type { DashboardState } from "../dashboard";

const STORAGE_KEY = "sovyx_onboarding";

/**
 * Warning surfaced post-onboarding-completion when the daemon defensively
 * reports that voice was requested but did NOT come up (registry malfunction
 * or auto-resume failure). Backend contract: ``voice_configured: false`` in
 * the ``POST /api/onboarding/complete`` response means "operator enabled
 * voice during onboarding but the pipeline is not registered" — a tree-falls-
 * in-the-forest signal until v0.31.6 wired this up. The warning persists
 * across the navigation from /onboarding to / via Zustand in-memory state
 * (no localStorage per CLAUDE.md auth-token rule + no need to survive reload).
 *
 * Mission: ``MISSION-voice-v0_31_6-paranoid-closure-2026-05-08.md`` §Phase 3
 * T3.2 (M3.c). Backend source: v0.31.4 GAP 8 closure
 * (``src/sovyx/dashboard/routes/onboarding.py`` § ``complete_onboarding``).
 */
export type VoiceWarning = { kind: "voice_not_configured" };

/** Read dismissed flag from localStorage (false if absent or invalid). */
function readDismissed(): boolean {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return false;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed === "object" && parsed !== null && "dismissed" in parsed) {
      return (parsed as { dismissed: unknown }).dismissed === true;
    }
    return false;
  } catch {
    return false;
  }
}

/** Persist dismissed flag to localStorage. */
function writeDismissed(dismissed: boolean): void {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ dismissed, ...(dismissed ? { completedAt: new Date().toISOString() } : {}) }),
    );
  } catch {
    // localStorage unavailable (e.g., private browsing quota exceeded) — silently ignore.
  }
}

export interface OnboardingSlice {
  /** Whether the user has manually dismissed the onboarding guide. */
  onboardingDismissed: boolean;
  /** Dismiss (or un-dismiss) the onboarding guide. Persists to localStorage. */
  setOnboardingDismissed: (v: boolean) => void;
  /**
   * Active voice-related warning surfaced after onboarding completion. ``null``
   * when no warning is pending. Set by ``handleComplete`` in
   * ``pages/onboarding.tsx`` when the backend reports ``voice_configured:
   * false``; consumed by the post-onboarding home page (``pages/overview.tsx``).
   * In-memory only (zustand state) — survives navigation but not full reload.
   */
  voiceWarning: VoiceWarning | null;
  /** Set the voice warning (use ``null`` to clear). */
  setVoiceWarning: (warning: VoiceWarning | null) => void;
  /** Convenience clearer used by the banner Dismiss button. */
  clearVoiceWarning: () => void;
}

export const createOnboardingSlice: StateCreator<
  DashboardState,
  [],
  [],
  OnboardingSlice
> = (set) => ({
  onboardingDismissed: readDismissed(),
  setOnboardingDismissed: (v) => {
    writeDismissed(v);
    set({ onboardingDismissed: v });
  },
  voiceWarning: null,
  setVoiceWarning: (warning) => set({ voiceWarning: warning }),
  clearVoiceWarning: () => set({ voiceWarning: null }),
});
