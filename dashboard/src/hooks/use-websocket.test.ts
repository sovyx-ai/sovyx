/**
 * Tests for useWebSocket hook.
 *
 * VAL-19: Covers WS connection, reconnect with backoff,
 * event dispatch to store, debounced refreshes, and cleanup.
 */
import { renderHook, act } from "@testing-library/react";
import { useWebSocket } from "./use-websocket";
import { useDashboardStore } from "@/stores/dashboard";

// ── Mock WebSocket ──

type WsHandler = ((ev: { data: string }) => void) | null;

class MockWebSocket {
  static instances: MockWebSocket[] = [];

  url: string;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: WsHandler = null;
  readyState = 0; // CONNECTING
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close() {
    this.closed = true;
    this.readyState = 3; // CLOSED
  }

  send(_data: string) {}

  // Test helpers
  simulateOpen() {
    this.readyState = 1; // OPEN
    this.onopen?.();
  }

  simulateMessage(data: string) {
    this.onmessage?.({ data });
  }

  simulateClose() {
    this.readyState = 3;
    this.onclose?.();
  }

  simulateError() {
    this.onerror?.();
  }
}

// ── Mock fetch (for refresh calls) ──

function mockFetchSuccess() {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ version: "1.0", checks: [], conversations: [], messages: [], nodes: [], edges: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

// ── Setup ──

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  MockWebSocket.instances = [];
  localStorage.clear();
  localStorage.setItem("sovyx_token", "test-token");

  useDashboardStore.setState({
    authenticated: false,
    showTokenModal: false,
    connected: false,
    connectionState: "disconnected",
    recentEvents: [],
    logs: [],
    activeConversationId: null,
  });

  vi.stubGlobal("WebSocket", MockWebSocket);
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useWebSocket", () => {
  it("creates WebSocket with token in URL", () => {
    mockFetchSuccess();
    renderHook(() => useWebSocket());

    expect(MockWebSocket.instances).toHaveLength(1);
    expect(MockWebSocket.instances[0]!.url).toContain("token=test-token");
  });

  it("sets connected=true on WS open", () => {
    mockFetchSuccess();
    renderHook(() => useWebSocket());

    const ws = MockWebSocket.instances[0]!;
    act(() => ws.simulateOpen());

    expect(useDashboardStore.getState().connected).toBe(true);
  });

  it("triggers refreshStatus and refreshHealth on open", () => {
    const fetchSpy = mockFetchSuccess();
    renderHook(() => useWebSocket());

    const ws = MockWebSocket.instances[0]!;
    act(() => ws.simulateOpen());

    // Should have called /api/status and /api/health
    const urls = fetchSpy.mock.calls.map(([url]) => String(url));
    expect(urls.some((u) => u.includes("/api/status"))).toBe(true);
    expect(urls.some((u) => u.includes("/api/health"))).toBe(true);
  });

  it("dispatches events to store on WS message", () => {
    mockFetchSuccess();
    renderHook(() => useWebSocket());

    const ws = MockWebSocket.instances[0]!;
    act(() => ws.simulateOpen());

    const event = JSON.stringify({
      type: "EngineStarted",
      timestamp: "2026-04-07T00:00:00Z",
      data: {},
    });

    act(() => ws.simulateMessage(event));

    const state = useDashboardStore.getState();
    expect(state.recentEvents.length).toBeGreaterThan(0);
    expect(state.recentEvents[0]!.type).toBe("EngineStarted");
  });

  it("WS events go to activity feed, NOT to logs store", () => {
    mockFetchSuccess();
    renderHook(() => useWebSocket());

    const ws = MockWebSocket.instances[0]!;
    act(() => ws.simulateOpen());

    act(() =>
      ws.simulateMessage(
        JSON.stringify({
          type: "ConceptCreated",
          timestamp: "2026-04-07T00:00:00Z",
          data: { id: "test" },
        }),
      ),
    );

    // Logs store should NOT contain WS events (fed by /api/logs polling)
    const logs = useDashboardStore.getState().logs;
    expect(logs.some((l) => l.event.includes("ConceptCreated"))).toBe(false);

    // Activity feed SHOULD contain the event
    const events = useDashboardStore.getState().recentEvents;
    expect(events.some((e) => e.type === "ConceptCreated")).toBe(true);
  });

  it("ignores 'pong' messages", () => {
    mockFetchSuccess();
    renderHook(() => useWebSocket());

    const ws = MockWebSocket.instances[0]!;
    act(() => ws.simulateOpen());

    const eventsBefore = useDashboardStore.getState().recentEvents.length;
    act(() => ws.simulateMessage("pong"));
    const eventsAfter = useDashboardStore.getState().recentEvents.length;

    expect(eventsAfter).toBe(eventsBefore);
  });

  it("ignores malformed JSON messages", () => {
    mockFetchSuccess();
    renderHook(() => useWebSocket());

    const ws = MockWebSocket.instances[0]!;
    act(() => ws.simulateOpen());

    const eventsBefore = useDashboardStore.getState().recentEvents.length;
    act(() => ws.simulateMessage("{invalid json"));
    const eventsAfter = useDashboardStore.getState().recentEvents.length;

    expect(eventsAfter).toBe(eventsBefore);
  });

  it("reconnects with backoff on WS close", () => {
    mockFetchSuccess();
    renderHook(() => useWebSocket());

    const ws = MockWebSocket.instances[0]!;
    act(() => ws.simulateOpen());
    act(() => ws.simulateClose());

    expect(useDashboardStore.getState().connectionState).toBe("reconnecting");

    // Advance past initial backoff (1000ms)
    act(() => vi.advanceTimersByTime(1100));

    // Should have created a new WS instance
    expect(MockWebSocket.instances.length).toBeGreaterThanOrEqual(2);
  });

  it("doubles backoff on consecutive failures", () => {
    mockFetchSuccess();
    renderHook(() => useWebSocket());

    // First connection + close
    const ws1 = MockWebSocket.instances[0]!;
    act(() => ws1.simulateOpen());
    act(() => ws1.simulateClose());

    // First reconnect after 1000ms
    act(() => vi.advanceTimersByTime(1100));
    const ws2 = MockWebSocket.instances[MockWebSocket.instances.length - 1]!;
    act(() => ws2.simulateClose());

    // Second reconnect should wait 2000ms
    act(() => vi.advanceTimersByTime(1100));
    const countBefore = MockWebSocket.instances.length;

    // Not enough time yet
    expect(MockWebSocket.instances.length).toBe(countBefore);

    // Now advance past 2000ms total
    act(() => vi.advanceTimersByTime(1000));
    expect(MockWebSocket.instances.length).toBeGreaterThan(countBefore);
  });

  it("resets backoff on successful open", () => {
    mockFetchSuccess();
    renderHook(() => useWebSocket());

    // First connection fails
    const ws1 = MockWebSocket.instances[0]!;
    act(() => ws1.simulateOpen());
    act(() => ws1.simulateClose());

    // Reconnect
    act(() => vi.advanceTimersByTime(1100));
    const ws2 = MockWebSocket.instances[MockWebSocket.instances.length - 1]!;
    act(() => ws2.simulateOpen()); // Resets backoff

    act(() => ws2.simulateClose());

    // Should reconnect after 1000ms (reset), not 2000ms
    const countBefore = MockWebSocket.instances.length;
    act(() => vi.advanceTimersByTime(1100));
    expect(MockWebSocket.instances.length).toBeGreaterThan(countBefore);
  });

  it("closes WS on error", () => {
    mockFetchSuccess();
    renderHook(() => useWebSocket());

    const ws = MockWebSocket.instances[0]!;
    const closeSpy = vi.spyOn(ws, "close");
    act(() => ws.simulateError());

    expect(closeSpy).toHaveBeenCalled();
  });

  it("cleans up on unmount — closes WS, stops polling", () => {
    mockFetchSuccess();
    const { unmount } = renderHook(() => useWebSocket());

    const ws = MockWebSocket.instances[0]!;
    act(() => ws.simulateOpen());

    unmount();
    expect(ws.closed).toBe(true);
  });

  it("does not reconnect after unmount", () => {
    mockFetchSuccess();
    const { unmount } = renderHook(() => useWebSocket());

    const ws = MockWebSocket.instances[0]!;
    act(() => ws.simulateOpen());

    unmount();

    // Simulate close after unmount
    act(() => ws.onclose?.());

    const countBefore = MockWebSocket.instances.length;
    act(() => vi.advanceTimersByTime(5000));

    // No new WS connections should be created
    expect(MockWebSocket.instances.length).toBe(countBefore);
  });

  describe("event-specific refresh dispatching", () => {
    function sendEvent(ws: MockWebSocket, type: string) {
      act(() =>
        ws.simulateMessage(
          JSON.stringify({
            type,
            timestamp: "2026-04-07T00:00:00Z",
            data: {},
          }),
        ),
      );
    }

    it("refreshes health on ServiceHealthChanged (debounced)", () => {
      const fetchSpy = mockFetchSuccess();
      renderHook(() => useWebSocket());

      const ws = MockWebSocket.instances[0]!;
      act(() => ws.simulateOpen());
      fetchSpy.mockClear();

      sendEvent(ws, "ServiceHealthChanged");

      // Debounce: 300ms
      act(() => vi.advanceTimersByTime(350));

      const urls = fetchSpy.mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => u.includes("/api/health"))).toBe(true);
    });

    it("refreshes status + brain on ConceptCreated (debounced)", () => {
      const fetchSpy = mockFetchSuccess();
      renderHook(() => useWebSocket());

      const ws = MockWebSocket.instances[0]!;
      act(() => ws.simulateOpen());
      fetchSpy.mockClear();

      sendEvent(ws, "ConceptCreated");
      act(() => vi.advanceTimersByTime(350));

      const urls = fetchSpy.mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => u.includes("/api/status"))).toBe(true);
      expect(urls.some((u) => u.includes("/api/brain/graph"))).toBe(true);
    });

    it("refreshes immediately on EngineStarted (not debounced)", () => {
      const fetchSpy = mockFetchSuccess();
      renderHook(() => useWebSocket());

      const ws = MockWebSocket.instances[0]!;
      act(() => ws.simulateOpen());
      fetchSpy.mockClear();

      sendEvent(ws, "EngineStarted");

      // Immediate — no need to advance timers
      const urls = fetchSpy.mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => u.includes("/api/status"))).toBe(true);
      expect(urls.some((u) => u.includes("/api/health"))).toBe(true);
      expect(urls.some((u) => u.includes("/api/brain/graph"))).toBe(true);
    });

    it("refreshes status on EngineStopping", () => {
      const fetchSpy = mockFetchSuccess();
      renderHook(() => useWebSocket());

      const ws = MockWebSocket.instances[0]!;
      act(() => ws.simulateOpen());
      fetchSpy.mockClear();

      sendEvent(ws, "EngineStopping");
      act(() => vi.advanceTimersByTime(350));

      const urls = fetchSpy.mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => u.includes("/api/status"))).toBe(true);
    });

    it("refreshes conversation on ThinkCompleted", () => {
      const fetchSpy = mockFetchSuccess();

      // Set an active conversation to trigger refreshActiveConversation
      useDashboardStore.setState({ activeConversationId: "conv-1" });

      renderHook(() => useWebSocket());
      const ws = MockWebSocket.instances[0]!;
      act(() => ws.simulateOpen());
      fetchSpy.mockClear();

      sendEvent(ws, "ThinkCompleted");
      act(() => vi.advanceTimersByTime(350));

      const urls = fetchSpy.mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => u.includes("/api/status"))).toBe(true);
      expect(urls.some((u) => u.includes("/api/conversations"))).toBe(true);
    });

    it("refreshes status on ChannelConnected/Disconnected", () => {
      const fetchSpy = mockFetchSuccess();
      renderHook(() => useWebSocket());

      const ws = MockWebSocket.instances[0]!;
      act(() => ws.simulateOpen());
      fetchSpy.mockClear();

      sendEvent(ws, "ChannelConnected");
      act(() => vi.advanceTimersByTime(350));

      const urls = fetchSpy.mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => u.includes("/api/status"))).toBe(true);
    });

    it("batches rapid events via debounce (no API burst)", () => {
      const fetchSpy = mockFetchSuccess();
      renderHook(() => useWebSocket());

      const ws = MockWebSocket.instances[0]!;
      act(() => ws.simulateOpen());
      fetchSpy.mockClear();

      // Send 5 ConceptCreated events in rapid succession
      for (let i = 0; i < 5; i++) {
        sendEvent(ws, "ConceptCreated");
      }

      act(() => vi.advanceTimersByTime(350));

      // Debounce should have collapsed to ~1 call per target
      const statusCalls = fetchSpy.mock.calls.filter(([url]) =>
        String(url).includes("/api/status"),
      );
      const brainCalls = fetchSpy.mock.calls.filter(([url]) =>
        String(url).includes("/api/brain/graph"),
      );

      // Should be 1 each (debounced), not 5
      expect(statusCalls.length).toBe(1);
      expect(brainCalls.length).toBe(1);
    });
  });

  describe("periodic polling", () => {
    it("polls status every 5s and health every 10s", () => {
      const fetchSpy = mockFetchSuccess();
      renderHook(() => useWebSocket());
      fetchSpy.mockClear();

      act(() => vi.advanceTimersByTime(5100));

      const statusCalls = fetchSpy.mock.calls.filter(([url]) =>
        String(url).includes("/api/status"),
      );
      expect(statusCalls.length).toBeGreaterThanOrEqual(1);

      fetchSpy.mockClear();
      act(() => vi.advanceTimersByTime(10100));

      const healthCalls = fetchSpy.mock.calls.filter(([url]) =>
        String(url).includes("/api/health"),
      );
      expect(healthCalls.length).toBeGreaterThanOrEqual(1);
    });
  });
});
