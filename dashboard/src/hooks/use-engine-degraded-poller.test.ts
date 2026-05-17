/**
 * Vitest cohort for useEngineDegradedPoller + ackComposite.
 *
 * Mission C4 §T1.13 §9.1 row "useEngineDegradedPoller" — focused
 * tests on the thin wrapper around useApiPoller. Mocks useApiPoller
 * so the underlying network primitives are not exercised here (those
 * live in use-api-poller.test.ts). Phase 3 §T3.7 added the
 * ``ackComposite`` POST helper — covered by the dedicated test
 * suite below.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

import {
  ENGINE_DEGRADED_POLL_INTERVAL_MS,
  ackComposite,
  useEngineDegradedPoller,
} from "./use-engine-degraded-poller";

const mockUseApiPoller = vi.fn();
const mockApiPost = vi.fn();

vi.mock("@/hooks/use-api-poller", () => ({
  useApiPoller: (options: unknown) => {
    mockUseApiPoller(options);
    return { data: null, error: "ok", consecutive5xx: 0 };
  },
}));

vi.mock("@/lib/api", () => ({
  api: {
    post: (path: string, body?: unknown) => mockApiPost(path, body),
  },
}));

describe("useEngineDegradedPoller", () => {
  beforeEach(() => {
    mockUseApiPoller.mockClear();
  });

  it("exports the 5-second baseline interval constant", () => {
    expect(ENGINE_DEGRADED_POLL_INTERVAL_MS).toBe(5000);
  });

  it("calls useApiPoller with the /api/engine/degraded endpoint", () => {
    renderHook(() => useEngineDegradedPoller());
    expect(mockUseApiPoller).toHaveBeenCalledWith(
      expect.objectContaining({ endpoint: "/api/engine/degraded" }),
    );
  });

  it("passes the 5-second baseline interval to the underlying poller", () => {
    renderHook(() => useEngineDegradedPoller());
    expect(mockUseApiPoller).toHaveBeenCalledWith(
      expect.objectContaining({
        baselineIntervalMs: ENGINE_DEGRADED_POLL_INTERVAL_MS,
      }),
    );
  });

  it("defaults enabled=true when no option is provided", () => {
    renderHook(() => useEngineDegradedPoller());
    expect(mockUseApiPoller).toHaveBeenCalledWith(
      expect.objectContaining({ enabled: true }),
    );
  });

  it("forwards explicit enabled=false to the underlying poller", () => {
    renderHook(() => useEngineDegradedPoller({ enabled: false }));
    expect(mockUseApiPoller).toHaveBeenCalledWith(
      expect.objectContaining({ enabled: false }),
    );
  });

  it("passes a warnTag for the one-shot degraded-state console.warn", () => {
    renderHook(() => useEngineDegradedPoller());
    expect(mockUseApiPoller).toHaveBeenCalledWith(
      expect.objectContaining({ warnTag: "engine_degraded_poller_degraded" }),
    );
  });
});


describe("ackComposite (Phase 3 §T3.7)", () => {
  beforeEach(() => {
    mockApiPost.mockClear();
    mockApiPost.mockResolvedValue({ ok: true });
  });

  it("POSTs to /api/voice/degraded/ack with composite reason", async () => {
    await ackComposite();
    expect(mockApiPost).toHaveBeenCalledWith(
      "/api/voice/degraded/ack",
      expect.objectContaining({ reason: "composite" }),
    );
  });

  it("passes default ttl_sec=3600 when no argument provided", async () => {
    await ackComposite();
    expect(mockApiPost).toHaveBeenCalledWith(
      "/api/voice/degraded/ack",
      expect.objectContaining({ ttl_sec: 3600 }),
    );
  });

  it("forwards explicit ttl_sec to the POST body", async () => {
    await ackComposite(7200);
    expect(mockApiPost).toHaveBeenCalledWith(
      "/api/voice/degraded/ack",
      expect.objectContaining({ ttl_sec: 7200 }),
    );
  });

  it("propagates the api.post resolution to callers", async () => {
    const expected = { ok: true, reasons_acked: ["voice.x"] };
    mockApiPost.mockResolvedValueOnce(expected);
    const result = await ackComposite();
    expect(result).toEqual(expected);
  });
});
