import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import { StatusDot, healthStatusToState } from "./status-dot";
import type { StatusDotState, HealthStatus } from "./status-dot";

describe("StatusDot", () => {
  const states: StatusDotState[] = ["online", "idle", "thinking", "dreaming", "error", "offline"];

  it.each(states)("renders %s state with correct aria-label", (status) => {
    render(<StatusDot status={status} />);
    const dot = screen.getByRole("status");
    expect(dot).toBeInTheDocument();
    expect(dot).toHaveAttribute("aria-label");
  });

  it("shows label when showLabel is true", () => {
    render(<StatusDot status="online" showLabel />);
    expect(screen.getByText("Online")).toBeInTheDocument();
  });

  it("hides label by default", () => {
    render(<StatusDot status="online" />);
    expect(screen.queryByText("Online")).not.toBeInTheDocument();
  });

  it("applies correct size classes", () => {
    const { rerender } = render(<StatusDot status="online" size="sm" />);
    expect(screen.getByRole("status")).toHaveClass("size-1.5");

    rerender(<StatusDot status="online" size="lg" />);
    expect(screen.getByRole("status")).toHaveClass("size-2.5");
  });

  it("applies animation class for pulsing states", () => {
    render(<StatusDot status="thinking" />);
    const dot = screen.getByRole("status");
    expect(dot.className).toContain("animate-");
  });

  it("does not animate static states", () => {
    render(<StatusDot status="error" />);
    const dot = screen.getByRole("status");
    expect(dot.className).not.toContain("animate-");
  });
});

describe("healthStatusToState", () => {
  it.each<[HealthStatus, StatusDotState]>([
    ["green", "online"],
    ["yellow", "idle"],
    ["red", "error"],
  ])("maps %s → %s", (input, expected) => {
    expect(healthStatusToState(input)).toBe(expected);
  });
});
