import type { StateCreator } from "zustand";
import type { DashboardState } from "../dashboard";

const STORAGE_KEY = "sovyx_onboarding";

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
});
