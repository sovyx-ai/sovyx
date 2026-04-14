import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import { MetricChart } from "./metric-chart";

// Recharts pulls in ResizeObserver (called with `new`) and offsetWidth APIs
// that jsdom lacks. Stub a class so the chart can mount.
beforeAll(() => {
  class MockResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
});

describe("MetricChart", () => {
  it("renders the title", () => {
    render(<MetricChart title="Cost" data={[]} />);
    expect(screen.getByText("Cost")).toBeInTheDocument();
  });

  it("shows the empty state animation when no data", () => {
    const { container } = render(<MetricChart title="Cost" data={[]} />);
    // Empty state container rendered with fixed h-[140px]
    expect(container.querySelector(".h-\\[140px\\]")).not.toBeNull();
  });

  it("accepts a custom className on the outer wrapper", () => {
    const { container } = render(
      <MetricChart title="X" data={[]} className="my-custom-chart" />,
    );
    const wrapper = container.firstChild as HTMLElement;
    expect(wrapper.className).toContain("my-custom-chart");
  });
});
