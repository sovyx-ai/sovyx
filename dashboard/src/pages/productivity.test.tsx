import { describe, it, expect } from "vitest";
import { render, screen } from "@/test/test-utils";
import ProductivityPage from "./productivity";

describe("ProductivityPage", () => {
  it("renders page title", () => {
    render(<ProductivityPage />);
    expect(screen.getByText("Daily Productivity")).toBeInTheDocument();
  });

  it("renders description", () => {
    render(<ProductivityPage />);
    expect(screen.getByText(/Morning briefings/)).toBeInTheDocument();
  });

  it("renders feature list with expected items", () => {
    render(<ProductivityPage />);
    expect(screen.getByText("Morning briefing")).toBeInTheDocument();
    expect(screen.getByText("Task manager")).toBeInTheDocument();
    expect(screen.getByText("Habit tracker")).toBeInTheDocument();
    expect(screen.getByText("Daily journal")).toBeInTheDocument();
    expect(screen.getByText("Calendar sync")).toBeInTheDocument();
  });

  it("shows v1.0 version badge", () => {
    render(<ProductivityPage />);
    expect(screen.getByText("Coming in v1.0")).toBeInTheDocument();
  });
});
