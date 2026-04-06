import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import { StatCard, StatCardSkeleton } from "./stat-card";

describe("StatCard", () => {
  it("has role=group with aria-label from title", () => {
    render(<StatCard title="Engine Status" value="Online" />);
    const group = screen.getByRole("group", { name: "Engine Status" });
    expect(group).toBeInTheDocument();
  });

  it("renders value in aria-live region", () => {
    const { container } = render(<StatCard title="Messages" value="156" />);
    const liveRegion = container.querySelector("[aria-live='polite']");
    expect(liveRegion).toBeInTheDocument();
    expect(liveRegion).toHaveTextContent("156");
  });

  it("shows StatusDot for green status", () => {
    render(<StatCard title="Test" value="42" status="green" />);
    const dot = screen.getByRole("status");
    expect(dot).toHaveAttribute("aria-label", "Online");
  });

  it("shows StatusDot for red status", () => {
    render(<StatCard title="Test" value="0" status="red" />);
    const dot = screen.getByRole("status");
    expect(dot).toHaveAttribute("aria-label", "Error");
  });

  it("shows StatusDot for yellow status", () => {
    render(<StatCard title="Test" value="0" status="yellow" />);
    const dot = screen.getByRole("status");
    expect(dot).toHaveAttribute("aria-label", "Idle");
  });

  it("hides icon from screen readers", () => {
    render(
      <StatCard title="Cost" value="$1.23" icon={<span data-testid="icon">💰</span>} />,
    );
    const iconWrapper = screen.getByTestId("icon").parentElement;
    expect(iconWrapper).toHaveAttribute("aria-hidden", "true");
  });

  it("renders positive trend with up arrow", () => {
    render(
      <StatCard title="Revenue" value="$500" trend={{ value: 12, label: "vs yesterday" }} />,
    );
    expect(screen.getByLabelText("Up 12% vs yesterday")).toBeInTheDocument();
  });

  it("renders negative trend with down arrow", () => {
    render(
      <StatCard title="Errors" value="3" trend={{ value: -5, label: "this week" }} />,
    );
    expect(screen.getByLabelText("Down 5% this week")).toBeInTheDocument();
  });

  it("renders subtitle", () => {
    render(<StatCard title="Brain" value="1,234" subtitle="42 episodes" />);
    expect(screen.getByText("42 episodes")).toBeInTheDocument();
  });

  it("uses design tokens for styling", () => {
    const { container } = render(<StatCard title="Test" value="0" />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain("svx-color-bg-surface");
  });
});

describe("StatCardSkeleton", () => {
  it("renders with role=group and aria-label Loading", () => {
    render(<StatCardSkeleton />);
    const group = screen.getByRole("group", { name: "Loading" });
    expect(group).toBeInTheDocument();
  });

  it("uses design tokens for card styling", () => {
    const { container } = render(<StatCardSkeleton />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain("svx-color-bg-surface");
    expect(card.className).toContain("svx-color-border-default");
  });

  it("renders three shimmer bars (title, value, subtitle)", () => {
    const { container } = render(<StatCardSkeleton />);
    const shimmers = container.querySelectorAll("[class*='animate-']");
    // title shimmer + icon shimmer + value shimmer + subtitle shimmer = 4
    expect(shimmers.length).toBe(4);
  });

  it("shimmer bars use shimmer animation", () => {
    const { container } = render(<StatCardSkeleton />);
    const shimmer = container.querySelector("[class*='animate-']") as HTMLElement;
    expect(shimmer.className).toContain("shimmer");
  });

  it("shimmer bars have gradient background-image", () => {
    const { container } = render(<StatCardSkeleton />);
    const shimmer = container.querySelector("[class*='animate-']") as HTMLElement;
    expect(shimmer.style.backgroundImage).toContain("linear-gradient");
  });

  it("accepts custom className", () => {
    const { container } = render(<StatCardSkeleton className="custom-test-class" />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain("custom-test-class");
  });

  it("matches StatCard dimensions (same padding and radius)", () => {
    const { container: skelContainer } = render(<StatCardSkeleton />);
    const { container: cardContainer } = render(<StatCard title="T" value="0" />);
    const skel = skelContainer.firstChild as HTMLElement;
    const card = cardContainer.firstChild as HTMLElement;
    // Both use same radius and padding tokens
    expect(skel.className).toContain("p-4");
    expect(card.className).toContain("p-4");
    expect(skel.className).toContain("svx-radius-lg");
    expect(card.className).toContain("svx-radius-lg");
  });
});
