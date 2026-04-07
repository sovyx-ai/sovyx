import { describe, it, expect } from "vitest";
import { render, screen } from "@/test/test-utils";
import PluginsPage from "./plugins";

describe("PluginsPage", () => {
  it("renders page title", () => {
    render(<PluginsPage />);
    expect(screen.getByText("Plugin Marketplace")).toBeInTheDocument();
  });

  it("renders description", () => {
    render(<PluginsPage />);
    expect(screen.getByText(/Extend your Mind/)).toBeInTheDocument();
  });

  it("renders feature list with expected items", () => {
    render(<PluginsPage />);
    expect(screen.getByText("Search & browse plugins")).toBeInTheDocument();
    expect(screen.getByText("Install and update")).toBeInTheDocument();
    expect(screen.getByText("Per-plugin configuration")).toBeInTheDocument();
    expect(screen.getByText("Plugin analytics")).toBeInTheDocument();
    expect(screen.getByText("Sandbox status")).toBeInTheDocument();
  });

  it("shows v1.0 version badge", () => {
    render(<PluginsPage />);
    expect(screen.getByText("Coming in v1.0")).toBeInTheDocument();
  });
});
