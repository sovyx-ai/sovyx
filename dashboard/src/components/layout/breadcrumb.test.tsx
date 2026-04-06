import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { Breadcrumb } from "./breadcrumb";

function renderAtPath(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Breadcrumb />
    </MemoryRouter>,
  );
}

describe("Breadcrumb accessibility", () => {
  it("has nav with aria-label", () => {
    renderAtPath("/");
    const nav = screen.getByRole("navigation", { name: /breadcrumb/i });
    expect(nav).toBeInTheDocument();
  });

  it("marks current page with aria-current", () => {
    renderAtPath("/logs");
    const current = screen.getByText("Logs");
    expect(current).toHaveAttribute("aria-current", "page");
  });

  it("hides separator from screen readers", () => {
    renderAtPath("/settings");
    const separators = screen.getAllByText("/");
    for (const sep of separators) {
      expect(sep).toHaveAttribute("aria-hidden", "true");
    }
  });

  it("shows Not Found for unknown routes with aria-current", () => {
    renderAtPath("/unknown-route");
    const notFound = screen.getByText("Not Found");
    expect(notFound).toHaveAttribute("aria-current", "page");
  });

  it("resolves trailing slash to correct route", () => {
    renderAtPath("/about/");
    expect(screen.getByText("About")).toBeInTheDocument();
    expect(screen.queryByText("Not Found")).not.toBeInTheDocument();
  });

  it("resolves nested path to parent route", () => {
    renderAtPath("/conversations/abc-123");
    expect(screen.getByText("Conversations")).toBeInTheDocument();
    expect(screen.queryByText("Not Found")).not.toBeInTheDocument();
  });
});
