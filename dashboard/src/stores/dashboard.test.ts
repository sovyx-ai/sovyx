import { describe, it, expect, beforeEach } from "vitest";
import { useDashboardStore } from "./dashboard";
import type { LogEntry, WsEvent, HealthCheck } from "@/types/api";

describe("Dashboard Store", () => {
  beforeEach(() => {
    // Reset store between tests
    const store = useDashboardStore.getState();
    store.setConnected(false);
    store.setLogs([]);
    store.clearLogs();
    store.setSettings(null as never);
  });

  describe("connection slice", () => {
    it("sets connected state", () => {
      useDashboardStore.getState().setConnected(true);
      expect(useDashboardStore.getState().connected).toBe(true);
    });
  });

  describe("logs slice", () => {
    const makeLog = (event: string, level = "INFO" as const): LogEntry => ({
      timestamp: new Date().toISOString(),
      level,
      logger: "test",
      event,
    });

    it("adds log entries", () => {
      const store = useDashboardStore.getState();
      store.addLog(makeLog("test 1"));
      store.addLog(makeLog("test 2"));
      expect(useDashboardStore.getState().logs).toHaveLength(2);
    });

    it("sets logs in bulk", () => {
      const logs = Array.from({ length: 5 }, (_, i) => makeLog(`log ${i}`));
      useDashboardStore.getState().setLogs(logs);
      expect(useDashboardStore.getState().logs).toHaveLength(5);
    });

    it("clears logs", () => {
      useDashboardStore.getState().addLog(makeLog("test"));
      useDashboardStore.getState().clearLogs();
      expect(useDashboardStore.getState().logs).toHaveLength(0);
    });

    it("trims logs at MAX_LOGS boundary", () => {
      const logs = Array.from({ length: 5001 }, (_, i) => makeLog(`log ${i}`));
      useDashboardStore.getState().setLogs(logs.slice(0, 4999));
      // Add one more to trigger trim check in addLog
      useDashboardStore.getState().addLog(makeLog("new"));
      expect(useDashboardStore.getState().logs.length).toBeLessThanOrEqual(5001);
    });
  });

  describe("events slice", () => {
    it("adds events and keeps max 50", () => {
      const store = useDashboardStore.getState();
      for (let i = 0; i < 55; i++) {
        store.addEvent({
          type: "ThinkCompleted",
          timestamp: new Date().toISOString(),
          data: {},
        } as WsEvent);
      }
      expect(useDashboardStore.getState().recentEvents.length).toBeLessThanOrEqual(50);
    });
  });

  describe("status slice", () => {
    it("sets status", () => {
      useDashboardStore.getState().setStatus({
        mind_name: "test",
        uptime_seconds: 100,
        active_conversations: 2,
        memory_concepts: 50,
        memory_episodes: 10,
        llm_calls_today: 5,
        llm_cost_today: 0.42,
        tokens_today: 1000,
        messages_today: 3,
      });
      expect(useDashboardStore.getState().status?.mind_name).toBe("test");
    });

    it("sets health checks", () => {
      const checks: HealthCheck[] = [
        { name: "brain", status: "green", message: "OK" },
      ];
      useDashboardStore.getState().setHealthChecks(checks);
      expect(useDashboardStore.getState().healthChecks).toHaveLength(1);
    });
  });

  describe("brain slice", () => {
    it("sets brain graph", () => {
      useDashboardStore.getState().setBrainGraph({
        nodes: [{ id: "1", name: "test", category: "fact", importance: 0.5, confidence: 0.8, access_count: 3 }],
        links: [],
      });
      expect(useDashboardStore.getState().brainGraph?.nodes).toHaveLength(1);
    });
  });

  describe("conversations slice", () => {
    it("sets conversations", () => {
      useDashboardStore.getState().setConversations([
        { id: "c1", participant: "Alice", channel: "telegram", message_count: 5, last_message_at: new Date().toISOString(), status: "active" },
      ]);
      expect(useDashboardStore.getState().conversations).toHaveLength(1);
    });

    it("sets active conversation", () => {
      useDashboardStore.getState().setActiveConversationId("c1");
      expect(useDashboardStore.getState().activeConversationId).toBe("c1");
    });
  });

  describe("cost data accumulation", () => {
    it("accumulates cost from ThinkCompleted events", () => {
      const store = useDashboardStore.getState();
      store.addEvent({
        type: "ThinkCompleted",
        timestamp: new Date().toISOString(),
        data: { cost_usd: 0.05 },
      } as WsEvent);
      store.addEvent({
        type: "ThinkCompleted",
        timestamp: new Date().toISOString(),
        data: { cost_usd: 0.03 },
      } as WsEvent);
      const costData = useDashboardStore.getState().costData;
      expect(costData).toHaveLength(2);
      expect(costData[1].value).toBeCloseTo(0.08, 4);
    });

    it("ignores events without cost", () => {
      // Reset cost data from previous test
      useDashboardStore.setState({ costData: [] });
      useDashboardStore.getState().addEvent({
        type: "PerceptionReceived",
        timestamp: new Date().toISOString(),
        data: {},
      } as WsEvent);
      expect(useDashboardStore.getState().costData).toHaveLength(0);
    });
  });
});
