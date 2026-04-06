/**
 * Store slices unit tests — cover uncovered branches.
 *
 * Ref: DASH-37
 */
import { describe, it, expect } from "vitest";
import { useDashboardStore } from "../dashboard";

describe("auth slice", () => {
  it("setAuthenticated updates state", () => {
    useDashboardStore.getState().setAuthenticated(true);
    expect(useDashboardStore.getState().authenticated).toBe(true);
    useDashboardStore.getState().setAuthenticated(false);
    expect(useDashboardStore.getState().authenticated).toBe(false);
  });

  it("setShowTokenModal toggles modal", () => {
    useDashboardStore.getState().setShowTokenModal(true);
    expect(useDashboardStore.getState().showTokenModal).toBe(true);
    useDashboardStore.getState().setShowTokenModal(false);
    expect(useDashboardStore.getState().showTokenModal).toBe(false);
  });
});

describe("brain slice", () => {
  it("setBrainGraph stores graph data", () => {
    const graph = {
      nodes: [{ id: "1", name: "Test", category: "fact" as const, importance: 0.5, confidence: 0.8, access_count: 3 }],
      links: [],
    };
    useDashboardStore.getState().setBrainGraph(graph);
    expect(useDashboardStore.getState().brainGraph).toEqual(graph);
  });
});

describe("conversations slice", () => {
  it("setActiveMessages stores messages", () => {
    const msgs = [
      { id: "m1", role: "user" as const, content: "hello", timestamp: "2026-01-01T00:00:00Z" },
    ];
    useDashboardStore.getState().setActiveMessages(msgs);
    expect(useDashboardStore.getState().activeMessages).toEqual(msgs);
  });
});

describe("logs slice", () => {
  it("respects MAX_LOGS limit", () => {
    const store = useDashboardStore.getState();
    // Clear first
    store.clearLogs();
    // Add more than MAX_LOGS (5000)
    for (let i = 0; i < 5010; i++) {
      store.addLog({
        timestamp: `2026-01-01T00:00:${String(i % 60).padStart(2, "0")}Z`,
        level: "INFO",
        logger: "test",
        event: `log-${i}`,
      });
    }
    expect(useDashboardStore.getState().logs.length).toBeLessThanOrEqual(5000);
  });
});
