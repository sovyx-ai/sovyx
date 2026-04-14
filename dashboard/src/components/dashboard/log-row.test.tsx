import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { LogRow } from "./log-row";
import type { LogEntry } from "@/types/api";

function mk(overrides: Partial<LogEntry> = {}): LogEntry {
  return {
    timestamp: new Date("2026-01-01T12:00:00Z").toISOString(),
    level: "INFO",
    logger: "sovyx.test",
    event: "hello world",
    ...overrides,
  };
}

describe("LogRow", () => {
  it("renders level, logger and event", () => {
    render(<LogRow entry={mk()} />);
    expect(screen.getByText("hello world")).toBeInTheDocument();
    expect(screen.getByText("sovyx.test")).toBeInTheDocument();
    expect(screen.getByText(/INFO/)).toBeInTheDocument();
  });

  it("does not show expand-click affordance when there are no extra fields", () => {
    const { container } = render(<LogRow entry={mk()} />);
    const row = container.firstChild as HTMLElement;
    expect(row.className).not.toContain("cursor-pointer");
  });

  it("expands to show extra structured fields when clicked", () => {
    const entry = mk({ request_id: "abc123", user_id: 42 });
    const { container } = render(<LogRow entry={entry} />);
    const row = container.firstChild as HTMLElement;
    expect(row.className).toContain("cursor-pointer");
    expect(container.querySelector("pre")).toBeNull();
    fireEvent.click(row);
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toContain("abc123");
    expect(pre?.textContent).toContain("42");
  });
});
