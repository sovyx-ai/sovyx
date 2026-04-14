import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import { HealthGrid } from "./health-grid";
import type { HealthCheck } from "@/types/api";

function mk(name: string, status: HealthCheck["status"], message = ""): HealthCheck {
  return { name, status, message };
}

describe("HealthGrid", () => {
  it("renders each check with its name", () => {
    render(
      <HealthGrid
        checks={[mk("Disk", "green"), mk("RAM", "yellow"), mk("LLM", "red")]}
      />,
    );
    expect(screen.getByText("Disk")).toBeInTheDocument();
    expect(screen.getByText("RAM")).toBeInTheDocument();
    expect(screen.getByText("LLM")).toBeInTheDocument();
  });

  it("exposes an accessible status role per check", () => {
    render(<HealthGrid checks={[mk("Disk", "green"), mk("RAM", "yellow")]} />);
    const checks = screen.getAllByRole("status");
    // Each check is role=status, plus one extra StatusDot in the header
    expect(checks.length).toBeGreaterThanOrEqual(2);
  });

  it("handles empty checks array without crashing", () => {
    const { container } = render(<HealthGrid checks={[]} />);
    expect(container.firstChild).not.toBeNull();
  });
});
