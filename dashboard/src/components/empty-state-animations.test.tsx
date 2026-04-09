import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import {
  BrainEmptyAnimation,
  ConversationsEmptyAnimation,
  ConversationSelectAnimation,
  LogsEmptyAnimation,
  ChartEmptyAnimation,
} from "./empty-state-animations";

describe("EmptyStateAnimations", () => {
  it("BrainEmptyAnimation renders with aria-hidden", () => {
    const { container } = render(<BrainEmptyAnimation />);
    const el = container.firstElementChild as HTMLElement;
    expect(el).toHaveAttribute("aria-hidden", "true");
    expect(el).toHaveClass("empty-anim-brain");
    // Has SVG with nodes and lines
    expect(container.querySelectorAll("circle")).toHaveLength(4);
    expect(container.querySelectorAll("line")).toHaveLength(5);
  });

  it("ConversationsEmptyAnimation renders two bubbles with dots", () => {
    const { container } = render(<ConversationsEmptyAnimation />);
    const el = container.firstElementChild as HTMLElement;
    expect(el).toHaveAttribute("aria-hidden", "true");
    expect(el).toHaveClass("empty-anim-chat");
    // 2 bubbles × 3 dots = 6 dots
    expect(container.querySelectorAll(".empty-anim-chat__dot")).toHaveLength(6);
  });

  it("LogsEmptyAnimation renders terminal cursor", () => {
    const { container } = render(<LogsEmptyAnimation />);
    const el = container.firstElementChild as HTMLElement;
    expect(el).toHaveAttribute("aria-hidden", "true");
    expect(el).toHaveClass("empty-anim-terminal");
    expect(container.querySelector(".empty-anim-terminal__prompt")).toHaveTextContent("$");
    expect(container.querySelector(".empty-anim-terminal__cursor")).toBeInTheDocument();
  });

  it("ChartEmptyAnimation renders SVG pulse line", () => {
    const { container } = render(<ChartEmptyAnimation />);
    const el = container.firstElementChild as HTMLElement;
    expect(el).toHaveAttribute("aria-hidden", "true");
    expect(el).toHaveClass("empty-anim-pulse");
    expect(container.querySelector(".empty-anim-pulse__line")).toBeInTheDocument();
    expect(container.querySelector(".empty-anim-pulse__baseline")).toBeInTheDocument();
  });

  it("ConversationSelectAnimation renders static chat outline", () => {
    const { container } = render(<ConversationSelectAnimation />);
    const el = container.firstElementChild as HTMLElement;
    expect(el).toHaveAttribute("aria-hidden", "true");
    expect(el).toHaveClass("empty-anim-select");
    // SVG with bubble outline, tail, and placeholder lines
    expect(container.querySelector(".empty-anim-select__bubble")).toBeInTheDocument();
    expect(container.querySelector(".empty-anim-select__tail")).toBeInTheDocument();
    expect(container.querySelectorAll(".empty-anim-select__line")).toHaveLength(3);
  });

  it("accepts custom className", () => {
    const { container } = render(<BrainEmptyAnimation className="my-class" />);
    expect(container.firstElementChild).toHaveClass("my-class");
  });
});
