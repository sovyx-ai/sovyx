import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { PageTransition } from "./page-transition";

describe("PageTransition", () => {
  it("renders children", () => {
    render(
      <PageTransition>
        <p>Hello</p>
      </PageTransition>,
    );

    expect(screen.getByText("Hello")).toBeInTheDocument();
  });

  it("wraps children in animation container", () => {
    render(
      <PageTransition>
        <p>Content</p>
      </PageTransition>,
    );

    const wrapper = screen.getByText("Content").parentElement;
    expect(wrapper).toHaveClass("animate-page-in");
  });
});
