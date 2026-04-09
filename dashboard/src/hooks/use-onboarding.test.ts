/**
 * Tests for useOnboardingProgress hook.
 *
 * Covers: step detection from store data, state transitions,
 * boundary conditions, dismissed persistence, null/missing data handling.
 *
 * The hook derives state from SystemStatus + HealthCheck[] — never stores
 * step completion. Only `dismissed` is persisted (via onboarding slice).
 */
import { renderHook, act } from "@testing-library/react";
import { useOnboardingProgress } from "./use-onboarding";
import { useDashboardStore } from "@/stores/dashboard";
import type { SystemStatus, HealthCheck } from "@/types/api";

// ── Helpers ──

/** Minimal valid SystemStatus with all counters at zero. */
function makeStatus(overrides: Partial<SystemStatus> = {}): SystemStatus {
  return {
    version: "0.5.25",
    uptime_seconds: 3600,
    mind_name: "test-mind",
    active_conversations: 0,
    memory_concepts: 0,
    memory_episodes: 0,
    llm_cost_today: 0,
    llm_calls_today: 0,
    tokens_today: 0,
    messages_today: 0,
    ...overrides,
  };
}

/** Health check for LLM provider with given status. */
function llmHealth(status: "green" | "yellow" | "red"): HealthCheck {
  return { name: "llm_provider", status, message: `LLM is ${status}` };
}

function resetStore(): void {
  localStorage.removeItem("sovyx_onboarding");
  useDashboardStore.setState({
    status: null,
    healthChecks: [],
    onboardingDismissed: false,
  });
}

beforeEach(resetStore);

// ════════════════════════════════════════════════════════
// NULL / LOADING STATE
// ════════════════════════════════════════════════════════
describe("null/loading state", () => {
  it("returns all pending when status is null", () => {
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("pending");
    expect(result.current.step2).toBe("pending");
    expect(result.current.step3).toBe("pending");
    expect(result.current.completedCount).toBe(0);
    expect(result.current.allDone).toBe(false);
  });

  it("showBanner is true when status null and not dismissed", () => {
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.showBanner).toBe(true);
    expect(result.current.showAliveCard).toBe(false);
  });
});

// ════════════════════════════════════════════════════════
// STEP 1: LLM CONFIGURED
// ════════════════════════════════════════════════════════
describe("step 1 — LLM configured", () => {
  it("done when llm_provider health is green", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("done");
  });

  it("done when llm_provider health is yellow (degraded but working)", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("yellow")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("done");
  });

  it("pending when llm_provider health is red", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("red")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("pending");
  });

  it("done when llm_calls_today > 0 (proves LLM works regardless of health)", () => {
    useDashboardStore.setState({
      status: makeStatus({ llm_calls_today: 1 }),
      healthChecks: [llmHealth("red")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("done");
  });

  it("done when llm_calls_today > 0 and no health checks at all", () => {
    useDashboardStore.setState({
      status: makeStatus({ llm_calls_today: 5 }),
      healthChecks: [],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("done");
  });

  it("pending when healthChecks is empty and llm_calls_today is 0", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("pending");
  });

  it("done when health check name is 'LLM Providers' (observability format)", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [{ name: "LLM Providers", status: "green", message: "1 provider(s) available" }],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("done");
  });

  it("ignores non-llm health checks", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [
        { name: "database", status: "green", message: "OK" },
        { name: "disk", status: "green", message: "OK" },
      ],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("pending");
  });
});

// ════════════════════════════════════════════════════════
// STEP 2: FIRST MESSAGE SENT
// ════════════════════════════════════════════════════════
describe("step 2 — first message sent", () => {
  it("done when memory_concepts > 0", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 1 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step2).toBe("done");
  });

  it("done when messages_today > 0", () => {
    useDashboardStore.setState({
      status: makeStatus({ messages_today: 1 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step2).toBe("done");
  });

  it("active when step1 done but no interaction yet", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step2).toBe("active");
  });

  it("pending when step1 is still pending", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("red")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("pending");
    expect(result.current.step2).toBe("pending");
  });
});

// ════════════════════════════════════════════════════════
// STEP 3: MIND GROWING (5+ concepts)
// ════════════════════════════════════════════════════════
describe("step 3 — mind growing", () => {
  it("done when memory_concepts >= 5", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 5, messages_today: 1 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step3).toBe("done");
  });

  it("NOT done when memory_concepts = 4 (boundary)", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 4, messages_today: 1 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step3).toBe("active");
  });

  it("done when memory_concepts = 5 (exact boundary)", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 5, messages_today: 1 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step3).toBe("done");
  });

  it("active when step2 done but concepts < 5", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 3, messages_today: 1 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step3).toBe("active");
  });

  it("pending when step2 is still pending", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("red")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step3).toBe("pending");
  });

  it("done with large concept count (no overflow)", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 999999, messages_today: 1000 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step3).toBe("done");
    expect(result.current.allDone).toBe(true);
  });
});

// ════════════════════════════════════════════════════════
// COMPLETED COUNT & ALL DONE
// ════════════════════════════════════════════════════════
describe("completedCount and allDone", () => {
  it("0 completed when all pending", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("red")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.completedCount).toBe(0);
    expect(result.current.allDone).toBe(false);
  });

  it("1 completed when only step1 done", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.completedCount).toBe(1);
    expect(result.current.allDone).toBe(false);
  });

  it("2 completed when step1 + step2 done", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 2, messages_today: 1 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.completedCount).toBe(2);
    expect(result.current.allDone).toBe(false);
  });

  it("3 completed when all done", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 10, messages_today: 5 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.completedCount).toBe(3);
    expect(result.current.allDone).toBe(true);
  });
});

// ════════════════════════════════════════════════════════
// SHOW BANNER / SHOW ALIVE CARD
// ════════════════════════════════════════════════════════
describe("showBanner and showAliveCard", () => {
  it("showBanner true when steps incomplete and not dismissed", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("green")],
      onboardingDismissed: false,
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.showBanner).toBe(true);
    expect(result.current.showAliveCard).toBe(false);
  });

  it("showBanner false when dismissed", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("green")],
      onboardingDismissed: true,
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.showBanner).toBe(false);
    expect(result.current.showAliveCard).toBe(false);
  });

  it("showAliveCard true when allDone and not dismissed", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 10, messages_today: 5 }),
      healthChecks: [llmHealth("green")],
      onboardingDismissed: false,
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.showBanner).toBe(false);
    expect(result.current.showAliveCard).toBe(true);
  });

  it("showAliveCard false when allDone but dismissed", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 10, messages_today: 5 }),
      healthChecks: [llmHealth("green")],
      onboardingDismissed: true,
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.showBanner).toBe(false);
    expect(result.current.showAliveCard).toBe(false);
  });

  it("neither shown when dismissed (regardless of step state)", () => {
    useDashboardStore.setState({
      status: null,
      healthChecks: [],
      onboardingDismissed: true,
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.showBanner).toBe(false);
    expect(result.current.showAliveCard).toBe(false);
  });
});

// ════════════════════════════════════════════════════════
// DISMISS ACTION
// ════════════════════════════════════════════════════════
describe("dismiss", () => {
  it("setDismissed(true) hides banner", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("green")],
      onboardingDismissed: false,
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.showBanner).toBe(true);

    act(() => {
      result.current.setDismissed(true);
    });

    expect(result.current.showBanner).toBe(false);
    expect(result.current.dismissed).toBe(true);
  });

  it("setDismissed(false) restores banner", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("green")],
      onboardingDismissed: true,
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.showBanner).toBe(false);

    act(() => {
      result.current.setDismissed(false);
    });

    expect(result.current.showBanner).toBe(true);
    expect(result.current.dismissed).toBe(false);
  });
});

// ════════════════════════════════════════════════════════
// REACTIVITY (store changes → hook updates)
// ════════════════════════════════════════════════════════
describe("reactivity", () => {
  it("updates when status changes in store", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step2).toBe("active");

    // Simulate first message
    act(() => {
      useDashboardStore.setState({
        status: makeStatus({ messages_today: 1 }),
      });
    });

    expect(result.current.step2).toBe("done");
    expect(result.current.step3).toBe("active");
  });

  it("updates when healthChecks change in store", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("red")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("pending");

    // LLM configured
    act(() => {
      useDashboardStore.setState({
        healthChecks: [llmHealth("green")],
      });
    });

    expect(result.current.step1).toBe("done");
    expect(result.current.step2).toBe("active");
  });

  it("full progression: pending → 1 done → 2 done → all done", () => {
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("red")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.completedCount).toBe(0);

    // Step 1: configure LLM
    act(() => {
      useDashboardStore.setState({ healthChecks: [llmHealth("green")] });
    });
    expect(result.current.completedCount).toBe(1);

    // Step 2: send message
    act(() => {
      useDashboardStore.setState({ status: makeStatus({ messages_today: 1 }) });
    });
    expect(result.current.completedCount).toBe(2);
    expect(result.current.showBanner).toBe(true);

    // Step 3: mind grows
    act(() => {
      useDashboardStore.setState({
        status: makeStatus({ memory_concepts: 7, messages_today: 3 }),
      });
    });
    expect(result.current.completedCount).toBe(3);
    expect(result.current.allDone).toBe(true);
    expect(result.current.showBanner).toBe(false);
    expect(result.current.showAliveCard).toBe(true);
  });
});

// ════════════════════════════════════════════════════════
// EDGE CASES
// ════════════════════════════════════════════════════════
describe("edge cases", () => {
  it("step2 done via memory_concepts alone (messages_today = 0)", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 1, messages_today: 0 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step2).toBe("done");
  });

  it("step2 done via messages_today alone (memory_concepts = 0)", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 0, messages_today: 1 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step2).toBe("done");
  });

  it("step1 active is not a valid state (step1 is first — either pending or done)", () => {
    // Step 1 can never be "active" because there's no step before it.
    // When status is present and step1 isn't done, it should be pending.
    useDashboardStore.setState({
      status: makeStatus(),
      healthChecks: [llmHealth("red")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    // Step 1 is the first step — if not done, all are pending, not "active"
    expect(result.current.step1).toBe("pending");
  });

  it("handles simultaneous step completion (e.g., concepts=10 on first render)", () => {
    useDashboardStore.setState({
      status: makeStatus({ memory_concepts: 10, messages_today: 5, llm_calls_today: 3 }),
      healthChecks: [llmHealth("green")],
    });
    const { result } = renderHook(() => useOnboardingProgress());
    expect(result.current.step1).toBe("done");
    expect(result.current.step2).toBe("done");
    expect(result.current.step3).toBe("done");
    expect(result.current.allDone).toBe(true);
  });
});
