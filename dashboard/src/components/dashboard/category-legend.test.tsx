import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import { CategoryLegend, RelationLegend } from "./category-legend";

describe("CategoryLegend", () => {
  it("renders all 7 concept categories", () => {
    const { container } = render(<CategoryLegend />);
    expect(container.querySelectorAll("span[aria-hidden='true']")).toHaveLength(7);
  });

  it("renders counts alongside categories when provided", () => {
    render(<CategoryLegend counts={{ fact: 12, skill: 4 }} />);
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
  });
});

describe("RelationLegend", () => {
  it("renders all relation types by default", () => {
    const { container } = render(<RelationLegend />);
    expect(container.querySelectorAll("span[aria-hidden='true']")).toHaveLength(7);
  });

  it("filters out relations with zero count when counts are provided", () => {
    const { container } = render(
      <RelationLegend counts={{ related_to: 5, part_of: 0 }} />,
    );
    // Only one relation has a count > 0
    expect(container.querySelectorAll("span[aria-hidden='true']")).toHaveLength(1);
  });

  it("returns null when all counts are zero", () => {
    const { container } = render(<RelationLegend counts={{ related_to: 0 }} />);
    expect(container.firstChild).toBeNull();
  });
});
