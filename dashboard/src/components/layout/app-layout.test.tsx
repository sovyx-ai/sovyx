import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { AppLayout } from "./app-layout";

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

  it("sets document.title based on route (WCAG 2.4.2)", () => {
    render(
      <MemoryRouter initialEntries={["/brain"]}>
        <AppLayout />
      </MemoryRouter>,
    );
    expect(document.title).toBe("Brain — Sovyx");
  });

  it("falls back to 'Sovyx' for unknown routes", () => {
    render(
      <MemoryRouter initialEntries={["/nonexistent"]}>
        <AppLayout />
      </MemoryRouter>,
    );
    expect(document.title).toBe("Sovyx");
  });
});
