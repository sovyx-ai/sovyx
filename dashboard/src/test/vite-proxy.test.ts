/**
 * Regression guard for the dev-server proxy config.
 *
 * The voice-setup wizard's live meter connects to
 * ``/api/voice/test/input`` over WebSocket. If the ``/api`` proxy rule
 * is missing ``ws: true``, Vite forwards the upgrade request as plain
 * HTTP — the browser opens a socket, never receives LevelFrames, and
 * the VU bar renders a static green block. This test locks that in.
 */
import { describe, it, expect } from "vitest";
import viteConfig from "../../vite.config";

describe("vite dev-server proxy", () => {
  it("enables websocket upgrade on /api so the voice-test meter works", () => {
    // The config is UserConfig; narrow to the shape we need.
    const proxy = viteConfig.server?.proxy as
      | Record<string, { ws?: boolean; target?: string } | string>
      | undefined;
    expect(proxy).toBeDefined();
    const apiRule = proxy?.["/api"];
    expect(apiRule).toBeDefined();
    if (typeof apiRule === "string") {
      throw new Error(
        "/api proxy must be an object with ws:true — string shorthand doesn't proxy WebSockets",
      );
    }
    expect(apiRule?.ws).toBe(true);
  });

  it("keeps /ws WebSocket proxy for the main dashboard stream", () => {
    const proxy = viteConfig.server?.proxy as
      | Record<string, { ws?: boolean; target?: string } | string>
      | undefined;
    const wsRule = proxy?.["/ws"];
    expect(wsRule).toBeDefined();
    if (typeof wsRule === "string") {
      throw new Error("/ws proxy must be an object with ws:true");
    }
    expect(wsRule?.ws).toBe(true);
  });
});
