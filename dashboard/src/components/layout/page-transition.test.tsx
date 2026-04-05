import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Test with reduced motion
describe("PageTransition reduced motion", () => {
  it("renders children regardless of motion preference", async () => {
    // Mock useReducedMotion to return true
    vi.doMock("framer-motion", () => ({
      motion: {
        div: ({ children, ...props }: Record<string, unknown>) => {
          const { variants: _v, initial: _i, animate: _a, exit: _e, transition: _t, ...rest } = props;
          return <div data-testid="motion-div" {...rest}>{children}</div>;
        },
      },
      useReducedMotion: () => true,
    }));

    const { PageTransition } = await import("./page-transition");

    render(
      <PageTransition>
        <p>Hello</p>
      </PageTransition>,
    );

    expect(screen.getByText("Hello")).toBeInTheDocument();
  });
});
