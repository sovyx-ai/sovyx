/**
 * Onboarding progress hook.
 *
 * Derives step completion state from backend data (SystemStatus + HealthCheck[]).
 * No additional API calls — reads from the Zustand store which is already
 * populated by the WebSocket connection and status polling.
 *
 * Step detection (ZERO new endpoints):
 *   1. LLM configured → health check "llm_provider" is not red, OR llm_calls_today > 0
 *   2. First message sent → memory_concepts > 0 OR messages_today > 0
 *   3. Mind growing → memory_concepts >= 5
 *
 * The "active" state marks the next step to complete (visual highlight).
 * Only localStorage-persisted data: { dismissed: boolean, completedAt?: string }.
 * Step completion is DERIVED, never stored — backend is the source of truth.
 */
import { useMemo } from "react";
import { useDashboardStore } from "@/stores/dashboard";

/** Visual state for each onboarding step. */
export type StepState = "pending" | "active" | "done";

/** Threshold: number of concepts for step 3 ("Mind growing") to be done. */
const MIND_GROWING_THRESHOLD = 5;

export interface OnboardingProgress {
  /** Completion state for each step. */
  step1: StepState;
  step2: StepState;
  step3: StepState;
  /** Number of completed steps (0–3). */
  completedCount: number;
  /** True when all 3 steps are done. */
  allDone: boolean;
  /** True when the welcome banner should be visible. */
  showBanner: boolean;
  /** True when the "mind alive" card should be visible (all done, not dismissed). */
  showAliveCard: boolean;
  /** Whether the user has manually dismissed the onboarding. */
  dismissed: boolean;
  /** Dismiss or un-dismiss the onboarding guide. */
  setDismissed: (v: boolean) => void;
}

/**
 * Compute onboarding progress from live store data.
 *
 * Pure derivation — no side effects, fully memoized.
 */
export function useOnboardingProgress(): OnboardingProgress {
  const status = useDashboardStore((s) => s.status);
  const healthChecks = useDashboardStore((s) => s.healthChecks);
  const dismissed = useDashboardStore((s) => s.onboardingDismissed);
  const setDismissed = useDashboardStore((s) => s.setOnboardingDismissed);

  const steps = useMemo(() => {
    if (!status) {
      return { step1: "pending" as const, step2: "pending" as const, step3: "pending" as const };
    }

    // Step 1: LLM configured
    // Health check name varies: "llm_provider" (engine) or "LLM Providers" (observability)
    const llmHealthGreen = healthChecks.some(
      (c) => c.name.toLowerCase().includes("llm") && c.status !== "red",
    );
    const step1: StepState = llmHealthGreen || status.llm_calls_today > 0 ? "done" : "pending";

    // Step 2: First message sent (creates concepts or increments message counter)
    const hasInteraction = status.memory_concepts > 0 || status.messages_today > 0;
    const step2: StepState = hasInteraction
      ? "done"
      : step1 === "done"
        ? "active"
        : "pending";

    // Step 3: Mind growing (5+ concepts learned)
    const step3: StepState = status.memory_concepts >= MIND_GROWING_THRESHOLD
      ? "done"
      : step2 === "done"
        ? "active"
        : "pending";

    return { step1, step2, step3 };
  }, [status, healthChecks]);

  const completedCount = [steps.step1, steps.step2, steps.step3].filter(
    (s) => s === "done",
  ).length;
  const allDone = completedCount === 3;
  const showBanner = !dismissed && !allDone;
  const showAliveCard = allDone && !dismissed;

  return {
    ...steps,
    completedCount,
    allDone,
    showBanner,
    showAliveCard,
    dismissed,
    setDismissed,
  };
}
