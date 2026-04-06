import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ComingSoon, TabPlaceholder } from "./coming-soon";

describe("ComingSoon", () => {
  it("renders title and default version badge", () => {
    render(<ComingSoon title="Voice" />);
    expect(screen.getByText("Voice")).toBeInTheDocument();
    expect(screen.getByText("Available in v1.0")).toBeInTheDocument();
  });

  it("renders custom version badge text", () => {
    render(<ComingSoon title="Feature" versionBadge="v2.0" />);
    expect(screen.getByText("Available in v2.0")).toBeInTheDocument();
  });

  it("renders description", () => {
    render(<ComingSoon title="X" description="A cool feature" />);
    expect(screen.getByText("A cool feature")).toBeInTheDocument();
  });

  it("renders feature checklist", () => {
    const features = ["Pipeline status", "STT selector", "Wake word"];
    render(<ComingSoon title="Voice" features={features} />);
    expect(screen.getByTestId("feature-list")).toBeInTheDocument();
    for (const feat of features) {
      expect(screen.getByText(feat)).toBeInTheDocument();
    }
  });

  it("renders no feature list when features is empty", () => {
    render(<ComingSoon title="Empty" features={[]} />);
    expect(screen.queryByTestId("feature-list")).not.toBeInTheDocument();
  });

  it("renders custom icon", () => {
    render(
      <ComingSoon
        title="Test"
        icon={<span data-testid="custom-icon">🎤</span>}
      />,
    );
    expect(screen.getByTestId("custom-icon")).toBeInTheDocument();
  });

  it("applies custom className", () => {
    render(<ComingSoon title="Test" className="extra-class" />);
    expect(screen.getByTestId("coming-soon-card")).toHaveClass("extra-class");
  });
});

describe("TabPlaceholder", () => {
  it("renders label with version", () => {
    render(<TabPlaceholder label="Plugins" />);
    expect(screen.getByText("Plugins — coming in v1.0")).toBeInTheDocument();
  });

  it("renders custom version", () => {
    render(<TabPlaceholder label="Auth" version="v2.0" />);
    expect(screen.getByText("Auth — coming in v2.0")).toBeInTheDocument();
  });
});
