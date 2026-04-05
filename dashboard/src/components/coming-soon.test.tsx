import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ComingSoon, TabPlaceholder } from "./coming-soon";

describe("ComingSoon", () => {
  it("renders title and default version badge", () => {
    render(<ComingSoon title="Voice" />);
    expect(screen.getByText("Voice")).toBeInTheDocument();
    expect(screen.getByText("Coming in v1.0")).toBeInTheDocument();
  });

  it("renders custom version", () => {
    render(<ComingSoon title="Feature" version="v2.0" />);
    expect(screen.getByText("Coming in v2.0")).toBeInTheDocument();
  });

  it("renders description", () => {
    render(<ComingSoon title="X" description="A cool feature" />);
    expect(screen.getByText("A cool feature")).toBeInTheDocument();
  });
});

describe("TabPlaceholder", () => {
  it("renders label with version", () => {
    render(<TabPlaceholder label="Plugins" />);
    expect(screen.getByText("Plugins — coming in v1.0")).toBeInTheDocument();
  });
});
