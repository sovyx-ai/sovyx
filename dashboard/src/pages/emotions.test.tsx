import { describe, it, expect } from "vitest";
import { render, screen } from "@/test/test-utils";
import EmotionsPage from "./emotions";

describe("EmotionsPage", () => {
  it("renders page title", () => {
    render(<EmotionsPage />);
    expect(screen.getByText("Emotional Intelligence")).toBeInTheDocument();
  });

  it("renders description", () => {
    render(<EmotionsPage />);
    expect(screen.getByText(/Your Mind feels/)).toBeInTheDocument();
  });

  it("renders feature list with expected items", () => {
    render(<EmotionsPage />);
    expect(screen.getByText("Current mood indicator")).toBeInTheDocument();
    expect(screen.getByText("PAD dimension gauges")).toBeInTheDocument();
    expect(screen.getByText("Emotion timeline")).toBeInTheDocument();
    expect(screen.getByText("Trigger history")).toBeInTheDocument();
  });

  it("shows v1.0 version badge", () => {
    render(<EmotionsPage />);
    expect(screen.getByText("Coming in v1.0")).toBeInTheDocument();
  });
});
