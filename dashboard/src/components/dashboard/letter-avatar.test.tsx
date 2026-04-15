import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import { LetterAvatar, MindAvatar } from "./letter-avatar";

describe("LetterAvatar", () => {
  it("renders the first letter of the name, uppercased", () => {
    render(<LetterAvatar name="alice" />);
    expect(screen.getByText("A")).toBeInTheDocument();
  });

  it("uses a question mark when the name is empty", () => {
    render(<LetterAvatar name="" />);
    expect(screen.getByText("?")).toBeInTheDocument();
  });

  it("produces the same color for the same name (deterministic)", () => {
    const { container: a } = render(<LetterAvatar name="sovyx" />);
    const { container: b } = render(<LetterAvatar name="sovyx" />);
    const colorA = (a.firstChild as HTMLElement).style.backgroundColor;
    const colorB = (b.firstChild as HTMLElement).style.backgroundColor;
    expect(colorA).toBe(colorB);
    expect(colorA).not.toBe("");
  });
});

describe("MindAvatar", () => {
  it("exposes the Sovyx Mind aria-label", () => {
    render(<MindAvatar />);
    expect(screen.getByLabelText("Sovyx Mind")).toBeInTheDocument();
  });

  it("shows the S brand initial", () => {
    render(<MindAvatar />);
    expect(screen.getByText("S")).toBeInTheDocument();
  });
});
