import { describe, it, expect } from "vitest";
import { render, screen } from "@/test/test-utils";
import VoicePage from "./voice";

describe("VoicePage", () => {
  it("renders page title", () => {
    render(<VoicePage />);
    expect(screen.getByText("Voice Pipeline")).toBeInTheDocument();
  });

  it("renders description", () => {
    render(<VoicePage />);
    expect(screen.getByText(/Talk to your Mind/)).toBeInTheDocument();
  });

  it("renders feature list with expected items", () => {
    render(<VoicePage />);
    expect(screen.getByText("Pipeline status monitor")).toBeInTheDocument();
    expect(screen.getByText("STT/TTS model selector")).toBeInTheDocument();
    expect(screen.getByText("Wake word configuration")).toBeInTheDocument();
    expect(screen.getByText("Latency gauge")).toBeInTheDocument();
    expect(screen.getByText("Voice test playground")).toBeInTheDocument();
  });

  it("shows v1.0 version badge", () => {
    render(<VoicePage />);
    expect(screen.getByText("Available in v1.0")).toBeInTheDocument();
  });
});
