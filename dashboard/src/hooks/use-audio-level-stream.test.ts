/**
 * Tests for :func:`useAudioLevelStream`.
 */
import { renderHook, act, waitFor } from "@testing-library/react";
import { useAudioLevelStream } from "./use-audio-level-stream";

// ── Mock WebSocket ──

type WsHandler<T = unknown> = ((ev: T) => void) | null;

interface MockMessageEvent {
  data: string;
}

interface MockCloseEvent {
  code: number;
  reason: string;
}

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: WsHandler<MockCloseEvent> = null;
  onerror: (() => void) | null = null;
  onmessage: WsHandler<MockMessageEvent> = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close(): void {
    this.readyState = MockWebSocket.CLOSED;
  }

  send(_: string): void {}

  // Test helpers
  receive(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) });
  }

  closeFromServer(code: number, reason: string): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code, reason });
  }
}

beforeEach(() => {
  MockWebSocket.instances = [];
  sessionStorage.clear();
  sessionStorage.setItem("sovyx_token", "tok");
  // @ts-expect-error — overriding the global for this test.
  globalThis.WebSocket = MockWebSocket;
});

afterEach(() => {
  vi.clearAllTimers();
});

describe("useAudioLevelStream", () => {
  it("connects and surfaces ready + level frames", async () => {
    const { result } = renderHook(() =>
      useAudioLevelStream({ deviceId: 0, enabled: true }),
    );

    // The hook must have opened exactly one socket.
    expect(MockWebSocket.instances).toHaveLength(1);
    const ws = MockWebSocket.instances[0];
    expect(ws.url).toContain("/api/voice/test/input");
    expect(ws.url).toContain("token=tok");
    expect(ws.url).toContain("device_id=0");

    // Server emits Ready → Level.
    act(() => {
      ws.receive({
        v: 1,
        t: "ready",
        device_id: 0,
        device_name: "MyMic",
        sample_rate: 16000,
        channels: 1,
      });
    });
    await waitFor(() => expect(result.current.state).toBe("ready"));
    expect(result.current.ready?.device_name).toBe("MyMic");

    act(() => {
      ws.receive({
        v: 1,
        t: "level",
        rms_db: -30,
        peak_db: -20,
        hold_db: -18,
        clipping: false,
        vad_trigger: true,
      });
    });
    await waitFor(() => expect(result.current.state).toBe("streaming"));
    expect(result.current.level?.rms_db).toBe(-30);
    expect(result.current.level?.vad_trigger).toBe(true);
  });

  it("surfaces error frames with machine-readable code", async () => {
    const { result } = renderHook(() =>
      useAudioLevelStream({ deviceId: 0 }),
    );
    const ws = MockWebSocket.instances[0];
    act(() => {
      ws.receive({
        v: 1,
        t: "error",
        code: "device_busy",
        detail: "held",
        retryable: false,
      });
    });
    await waitFor(() => expect(result.current.state).toBe("error"));
    expect(result.current.errorCode).toBe("device_busy");
    expect(result.current.errorDetail).toBe("held");
  });

  it("does not reconnect on terminal 4xxx close code", async () => {
    const { result } = renderHook(() =>
      useAudioLevelStream({ deviceId: 0 }),
    );
    const ws = MockWebSocket.instances[0];
    act(() => {
      ws.closeFromServer(4010, "disabled");
    });
    await waitFor(() => expect(result.current.state).toBe("error"));
    // No new socket has been opened.
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(result.current.errorCode).toBe("disabled");
  });

  it("reconnects on transient 1006 up to the attempt budget", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      useAudioLevelStream({ deviceId: 0 }),
    );
    const ws = MockWebSocket.instances[0];
    act(() => {
      ws.closeFromServer(1006, "abnormal");
    });
    // Advance past the initial 500 ms backoff.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    expect(MockWebSocket.instances.length).toBeGreaterThan(1);
    expect(result.current.state).toBe("connecting");
    vi.useRealTimers();
  });

  it("drops malformed payloads without crashing the hook", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() =>
      useAudioLevelStream({ deviceId: 0 }),
    );
    const ws = MockWebSocket.instances[0];
    // Missing required fields on `level`.
    act(() => {
      ws.receive({ v: 1, t: "level", rms_db: -30 });
    });
    // State stays connecting (no good frame has arrived yet).
    await waitFor(() => expect(result.current.state).toBe("connecting"));
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  it("does not open a socket when disabled", () => {
    renderHook(() =>
      useAudioLevelStream({ deviceId: 0, enabled: false }),
    );
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it("tears down the socket on unmount", () => {
    const { unmount } = renderHook(() =>
      useAudioLevelStream({ deviceId: 0 }),
    );
    expect(MockWebSocket.instances).toHaveLength(1);
    unmount();
    expect(MockWebSocket.instances[0].readyState).toBe(MockWebSocket.CLOSED);
  });
});
