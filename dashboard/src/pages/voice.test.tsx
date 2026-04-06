import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import VoicePage from "./voice";

describe("VoicePage", () => {
  it("renders page title", () => {
    render(<VoicePage />);
    expect(
      screen.getByRole("heading", { name: "Voice Pipeline", level: 1 }),
    ).toBeInTheDocument();
  });

  it("renders coming soon card", () => {
    render(<VoicePage />);
    expect(screen.getByTestId("coming-soon-card")).toBeInTheDocument();
  });

  it("renders feature list with expected items", () => {
    render(<VoicePage />);
    expect(screen.getByTestId("feature-list")).toBeInTheDocument();
    expect(screen.getByText("Pipeline status")).toBeInTheDocument();
    expect(screen.getByText("STT/TTS model selector")).toBeInTheDocument();
    expect(screen.getByText("Wake word config")).toBeInTheDocument();
    expect(screen.getByText("Latency gauge")).toBeInTheDocument();
    expect(screen.getByText("Voice test")).toBeInTheDocument();
  });

  it("shows v1.0 version badge", () => {
    render(<VoicePage />);
    expect(screen.getByText("Available in v1.0")).toBeInTheDocument();
  });
});
