import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { NeuralMesh } from "./neural-mesh";

describe("NeuralMesh", () => {
  it("renders a non-interactive, aria-hidden background", () => {
    const { container } = render(<NeuralMesh />);
    const root = container.firstChild as HTMLElement;
    expect(root.getAttribute("aria-hidden")).toBe("true");
    expect(root.className).toContain("pointer-events-none");
  });

  it("renders the expected background layers", () => {
    const { container } = render(<NeuralMesh />);
    // Implementation detail but useful regression guard: multiple layered divs
    expect(container.querySelectorAll("div").length).toBeGreaterThan(2);
  });
});
