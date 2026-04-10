/**
 * About page tests — POLISH-16.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@/test/test-utils";
import AboutPage from "./about";

describe("AboutPage", () => {
  it("renders page title", () => {
    render(<AboutPage />);
    expect(screen.getByText("Sovyx")).toBeInTheDocument();
  });

  it("renders version and license section", () => {
    render(<AboutPage />);
    expect(screen.getByText("Version & License")).toBeInTheDocument();
    expect(screen.getByText("AGPL-3.0")).toBeInTheDocument();
  });

  it("renders system section", () => {
    render(<AboutPage />);
    expect(screen.getByText("System")).toBeInTheDocument();
  });

  it("renders links section with GitHub, Website and PyPI", () => {
    render(<AboutPage />);
    expect(screen.getByText("Links")).toBeInTheDocument();
    const github = screen.getByText("GitHub");
    expect(github.closest("a")).toHaveAttribute("href", "https://github.com/sovyx-ai/sovyx");
    const website = screen.getByText("Website");
    expect(website.closest("a")).toHaveAttribute("href", "https://sovyx.ai");
    const pypi = screen.getByText("PyPI");
    expect(pypi.closest("a")).toHaveAttribute("href", "https://pypi.org/project/sovyx/");
  });

  it("renders footer tagline", () => {
    render(<AboutPage />);
    expect(screen.getByText("Your mind, your data, your sovereignty.")).toBeInTheDocument();
  });

  it("shows fallback version when status is null", () => {
    render(<AboutPage />);
    expect(screen.getByText(/v0\.1\.0/)).toBeInTheDocument();
  });
});
