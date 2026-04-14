import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { LineChart, Line } from "recharts";
import { ChartContainer, type ChartConfig } from "./chart";

// Recharts needs ResizeObserver — jsdom lacks it. It's invoked with `new`,
// so a constructor (class) rather than a plain mock fn is required.
beforeAll(() => {
  class MockResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
});

const config: ChartConfig = {
  value: { label: "Value", color: "var(--chart-1)" },
};

describe("ChartContainer", () => {
  it("renders a data-slot=chart element with generated chart id", () => {
    const { container } = render(
      <ChartContainer config={config}>
        <LineChart data={[{ time: 1, value: 1 }]}>
          <Line dataKey="value" />
        </LineChart>
      </ChartContainer>,
    );
    const slot = container.querySelector("[data-slot='chart']");
    expect(slot).not.toBeNull();
    expect(slot?.getAttribute("data-chart")).toMatch(/^chart-/);
  });

  it("injects the chart-style block when the config has colors", () => {
    const { container } = render(
      <ChartContainer config={config}>
        <LineChart data={[{ time: 1, value: 1 }]}>
          <Line dataKey="value" />
        </LineChart>
      </ChartContainer>,
    );
    expect(container.querySelector("style")).not.toBeNull();
  });
});
