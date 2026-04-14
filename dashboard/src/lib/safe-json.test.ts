import { describe, it, expect } from "vitest";
import { safeStringify } from "./safe-json";

describe("safeStringify", () => {
  it("serializes plain objects with 2-space indent", () => {
    const s = safeStringify({ a: 1, b: "x" });
    expect(s).toContain('"a": 1');
    expect(s).toContain('"b": "x"');
  });

  it("redacts values under secret-looking keys (token)", () => {
    const s = safeStringify({ name: "ok", token: "abcd-1234" });
    expect(s).toContain('"name": "ok"');
    expect(s).toContain('"token": "[REDACTED]"');
    expect(s).not.toContain("abcd-1234");
  });

  it("redacts api_key, password, secret, authorization, cookie", () => {
    const s = safeStringify({
      api_key: "key",
      password: "pw",
      secret: "sh",
      authorization: "Bearer xyz",
      cookie: "session=abc",
      private_key: "pk",
    });
    expect(s.match(/\[REDACTED\]/g)?.length).toBe(6);
  });

  it("passes through non-secret keys unchanged", () => {
    const s = safeStringify({ name: "plugin", version: "1.0.0" });
    expect(s).not.toContain("[REDACTED]");
  });

  it("redacts nested secret keys", () => {
    const s = safeStringify({ config: { auth_token: "xyz" } });
    expect(s).toContain("[REDACTED]");
    expect(s).not.toContain("xyz");
  });

  it("clamps output beyond maxLength and appends suffix", () => {
    const big = "a".repeat(10_000);
    const s = safeStringify({ payload: big }, { maxLength: 200 });
    expect(s.length).toBeLessThan(260);
    expect(s).toContain("clamped,");
    expect(s).toContain("more chars");
  });

  it("returns full output when under maxLength", () => {
    const s = safeStringify({ n: 1 }, { maxLength: 1_000 });
    expect(s).not.toContain("clamped");
  });

  it("falls back to placeholder for circular structures", () => {
    const cyc: Record<string, unknown> = { name: "x" };
    cyc.self = cyc;
    expect(safeStringify(cyc)).toBe("[unserializable]");
  });

  it("returns empty string for undefined value", () => {
    expect(safeStringify(undefined)).toBe("");
  });
});
