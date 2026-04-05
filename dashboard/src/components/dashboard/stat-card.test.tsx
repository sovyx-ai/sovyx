import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatCard } from "./stat-card";

describe("StatCard accessibility", () => {
  it("has role=group with aria-label from title", () => {
    render(<StatCard title="Engine Status" value="Online" />);
    const group = screen.getByRole("group", { name: "Engine Status" });
    expect(group).toBeInTheDocument();
  });

  it("status dot has role=status and aria-label", () => {
    render(<StatCard title="Test" value="42" status="green" />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-label", "Healthy");
  });

  it("status dot shows Error for red", () => {
    render(<StatCard title="Test" value="0" status="red" />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-label", "Error");
  });

  it("status dot shows Warning for yellow", () => {
    render(<StatCard title="Test" value="0" status="yellow" />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-label", "Warning");
  });

  it("value has aria-live=polite for updates", () => {
    const { container } = render(<StatCard title="Messages" value="156" />);
    const liveRegion = container.querySelector("[aria-live='polite']");
    expect(liveRegion).toBeInTheDocument();
    expect(liveRegion).toHaveTextContent("156");
  });

  it("icon is hidden from screen readers", () => {
    render(
      <StatCard title="Cost" value="$1.23" icon={<span data-testid="icon">💰</span>} />,
    );
    const iconWrapper = screen.getByTestId("icon").parentElement;
    expect(iconWrapper).toHaveAttribute("aria-hidden", "true");
  });

  it("trend has descriptive aria-label", () => {
    render(
      <StatCard title="Revenue" value="$500" trend={{ value: 12, label: "vs yesterday" }} />,
    );
    const trend = screen.getByLabelText("Up 12% vs yesterday");
    expect(trend).toBeInTheDocument();
  });

  it("negative trend uses Down in aria-label", () => {
    render(
      <StatCard title="Errors" value="3" trend={{ value: -5, label: "this week" }} />,
    );
    const trend = screen.getByLabelText("Down 5% this week");
    expect(trend).toBeInTheDocument();
  });
});
