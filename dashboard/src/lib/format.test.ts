import { describe, it, expect } from "vitest";
import { formatUptime, formatCost, formatNumber, formatTimeAgo } from "./format";

describe("formatUptime", () => {
  it("formats seconds", () => {
    expect(formatUptime(45)).toBe("45s");
  });

  it("formats minutes", () => {
    expect(formatUptime(120)).toBe("2m");
    expect(formatUptime(3599)).toBe("59m");
  });

  it("formats hours and minutes", () => {
    expect(formatUptime(7200)).toBe("2h 0m");
    expect(formatUptime(7500)).toBe("2h 5m");
  });

  it("formats days and hours", () => {
    expect(formatUptime(86400)).toBe("1d 0h");
    expect(formatUptime(180000)).toBe("2d 2h");
  });
});

describe("formatCost", () => {
  it("formats zero", () => {
    expect(formatCost(0)).toBe("$0.00");
  });

  it("formats tiny amounts with 4 decimals", () => {
    expect(formatCost(0.0042)).toBe("$0.0042");
  });

  it("formats sub-dollar with 3 decimals", () => {
    expect(formatCost(0.123)).toBe("$0.123");
  });

  it("formats dollar amounts with 2 decimals", () => {
    expect(formatCost(12.5)).toBe("$12.50");
  });
});

describe("formatNumber", () => {
  it("formats with locale separators", () => {
    expect(formatNumber(1234567)).toBe("1,234,567");
  });

  it("formats small numbers", () => {
    expect(formatNumber(42)).toBe("42");
  });
});

describe("formatTimeAgo", () => {
  it("formats recent as 'just now'", () => {
    const now = new Date().toISOString();
    expect(formatTimeAgo(now)).toBe("just now");
  });

  it("formats minutes ago", () => {
    const date = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    expect(formatTimeAgo(date)).toBe("5m ago");
  });

  it("formats hours ago", () => {
    const date = new Date(Date.now() - 3 * 3600 * 1000).toISOString();
    expect(formatTimeAgo(date)).toBe("3h ago");
  });

  it("formats days ago", () => {
    const date = new Date(Date.now() - 2 * 86400 * 1000).toISOString();
    expect(formatTimeAgo(date)).toBe("2d ago");
  });
});
