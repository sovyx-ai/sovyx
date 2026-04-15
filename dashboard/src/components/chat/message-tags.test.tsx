/**
 * Tests for MessageTags — the inline pill row above assistant messages.
 *
 * Coverage: empty array is a no-op, known tags get their styled pill,
 * unknown tags fall through to a neutral pill with the raw name, i18n
 * labels come from the chat.tags namespace with raw-name fallback.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import "@/lib/i18n";

import { MessageTags } from "./message-tags";

describe("MessageTags", () => {
  it("renders nothing when the tags array is empty", () => {
    const { container } = render(<MessageTags tags={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders a pill for each tag using its translated label", () => {
    render(<MessageTags tags={["brain"]} />);
    // chat.json maps brain → "brain" (lower-case label).
    expect(screen.getByText("brain")).toBeInTheDocument();
  });

  it("emits a data-tag attribute matching the raw tag name", () => {
    render(<MessageTags tags={["brain"]} />);
    const pill = screen.getByText("brain");
    expect(pill).toHaveAttribute("data-tag", "brain");
  });

  it("translates the financial_math tag to the shorter 'financial' label", () => {
    render(<MessageTags tags={["financial_math"]} />);
    expect(screen.getByText("financial")).toBeInTheDocument();
    // The raw tag name is still available on data-tag for test hooks /
    // analytics — only the display label is shortened.
    expect(screen.getByText("financial")).toHaveAttribute(
      "data-tag",
      "financial_math",
    );
  });

  it("falls back to the raw tag name when no translation exists", () => {
    render(<MessageTags tags={["mystery_plugin"]} />);
    // Unknown tag: chat.tags.mystery_plugin isn't defined, so i18next
    // uses defaultValue (the raw name) and the UI still renders.
    const pill = screen.getByText("mystery_plugin");
    expect(pill).toBeInTheDocument();
    expect(pill).toHaveAttribute("data-tag", "mystery_plugin");
  });

  it("applies the neutral fallback classes to unknown tags", () => {
    render(<MessageTags tags={["mystery_plugin"]} />);
    const pill = screen.getByText("mystery_plugin");
    // Neutral pill uses the elevated-surface background token.
    expect(pill.className).toContain("bg-[var(--svx-color-bg-elevated)]");
  });

  it("applies the brand-primary classes to the brain tag", () => {
    render(<MessageTags tags={["brain"]} />);
    const pill = screen.getByText("brain");
    expect(pill.className).toContain(
      "text-[var(--svx-color-brand-primary)]",
    );
  });

  it("renders multiple tags in the order they were passed", () => {
    render(<MessageTags tags={["financial_math", "brain"]} />);
    const financial = screen.getByText("financial");
    const brain = screen.getByText("brain");
    // DOCUMENT_POSITION_FOLLOWING (0x04) means `brain` comes after
    // `financial` in tree order — exactly what the backend contract
    // specifies (plugins first, brain last).
    expect(
      financial.compareDocumentPosition(brain) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("exposes an aria-label on the container describing the row", () => {
    render(<MessageTags tags={["brain"]} />);
    expect(
      screen.getByLabelText("Modules that produced this response"),
    ).toBeInTheDocument();
  });
});
