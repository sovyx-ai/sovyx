import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ComingSoon } from "./coming-soon";

describe("ComingSoon", () => {
  it("renders title and default version badge", () => {
    render(<ComingSoon title="Voice Pipeline" />);
    expect(screen.getByText("Voice Pipeline")).toBeInTheDocument();
    expect(screen.getByText("Available in v1.0")).toBeInTheDocument();
  });

  it("renders custom version", () => {
    render(<ComingSoon title="Feature" version="v2.0" />);
    expect(screen.getByText("Available in v2.0")).toBeInTheDocument();
  });

  it("renders description", () => {
    render(<ComingSoon title="X" description="A cool feature" />);
    expect(screen.getByText("A cool feature")).toBeInTheDocument();
  });

  it("renders feature checklist", () => {
    render(
      <ComingSoon
        title="Voice"
        features={["Pipeline status", "STT model selector", "Wake word config"]}
      />,
    );
    expect(screen.getByText("Pipeline status")).toBeInTheDocument();
    expect(screen.getByText("STT model selector")).toBeInTheDocument();
    expect(screen.getByText("Wake word config")).toBeInTheDocument();
  });
});
