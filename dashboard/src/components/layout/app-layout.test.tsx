import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { AppLayout } from "./app-layout";

// Mock framer-motion to avoid animation issues in tests
vi.mock("framer-motion", () => ({
  motion: {
    div: ({ children, ...props }: Record<string, unknown>) => {
      const { variants: _v, initial: _i, animate: _a, exit: _e, transition: _t, ...rest } = props;
      return <div {...rest}>{children}</div>;
    },
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useReducedMotion: () => false,
}));

// Mock hooks
vi.mock("@/hooks/use-websocket", () => ({
  useWebSocket: () => {},
}));

vi.mock("@/stores/dashboard", () => ({
  useDashboardStore: () => null,
}));

function renderLayout() {
  return render(
    <MemoryRouter>
      <AppLayout />
    </MemoryRouter>,
  );
}

describe("AppLayout accessibility", () => {
  it("renders skip navigation link", () => {
    renderLayout();
    const skipLink = screen.getByText("Skip to main content");
    expect(skipLink).toBeInTheDocument();
    expect(skipLink).toHaveAttribute("href", "#main-content");
    expect(skipLink).toHaveClass("skip-nav");
  });

  it("renders main content landmark with id", () => {
    renderLayout();
    const main = document.getElementById("main-content");
    expect(main).toBeInTheDocument();
    expect(main?.tagName.toLowerCase()).toBe("main");
  });

  it("renders banner landmark", () => {
    renderLayout();
    expect(screen.getByRole("banner")).toBeInTheDocument();
  });

  it("notification button has aria-label", () => {
    renderLayout();
    const btn = screen.getByRole("button", { name: /notifications/i });
    expect(btn).toBeInTheDocument();
    expect(btn).toBeDisabled();
  });

  it("kbd has aria-label for command palette", () => {
    renderLayout();
    const kbd = screen.getByLabelText(/command.*k.*command palette/i);
    expect(kbd).toBeInTheDocument();
  });
});
