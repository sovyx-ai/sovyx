/**
 * NarrativePanel component tests — Phase 12.11.
 *
 * Pins compact ⇄ expanded mode toggle, structured-step preference,
 * story-string fallback, and the empty-state sentinel.
 */

import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@/test/test-utils";
import type { NarrativeResponse } from "@/types/api";

import { NarrativePanel } from "./narrative-panel";

function mk(overrides: Partial<NarrativeResponse> = {}): NarrativeResponse {
  return {
    saga_id: "saga-abc",
    locale: "en-US",
    story: "engine.boot\nvoice.frame\nllm.request",
    ...overrides,
  };
}

describe("NarrativePanel", () => {
  it("renders the empty-state copy when story is blank and no steps", () => {
    render(<NarrativePanel narrative={mk({ story: "" })} />);
    expect(
      screen.getByText("No narrative could be rendered for this saga."),
    ).toBeInTheDocument();
  });

  it("compact mode renders all story lines inside a single <pre>", () => {
    const { container } = render(<NarrativePanel narrative={mk()} />);
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toContain("engine.boot");
    expect(pre?.textContent).toContain("voice.frame");
    expect(pre?.textContent).toContain("llm.request");
  });

  it("compact mode does NOT render an ordered list", () => {
    const { container } = render(<NarrativePanel narrative={mk()} />);
    expect(container.querySelector("ol")).toBeNull();
  });

  it("expanded mode renders one <li> per line with a '#N' ordinal", () => {
    const { container } = render(
      <NarrativePanel narrative={mk()} defaultMode="expanded" />,
    );
    const items = container.querySelectorAll("ol > li");
    expect(items).toHaveLength(3);
    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("#2")).toBeInTheDocument();
    expect(screen.getByText("#3")).toBeInTheDocument();
  });

  it("expanded mode does NOT render the compact <pre>", () => {
    const { container } = render(
      <NarrativePanel narrative={mk()} defaultMode="expanded" />,
    );
    expect(container.querySelector("pre")).toBeNull();
  });

  it("the mode toggle switches from compact to expanded on click", () => {
    const { container } = render(<NarrativePanel narrative={mk()} />);
    expect(container.querySelector("pre")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Expanded" }));
    expect(container.querySelector("pre")).toBeNull();
    expect(container.querySelector("ol")).not.toBeNull();
  });

  it("the mode toggle switches back from expanded to compact on click", () => {
    const { container } = render(
      <NarrativePanel narrative={mk()} defaultMode="expanded" />,
    );
    expect(container.querySelector("ol")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Compact" }));
    expect(container.querySelector("ol")).toBeNull();
    expect(container.querySelector("pre")).not.toBeNull();
  });

  it("prefers the structured 'steps' array over the story string", () => {
    const narrative = mk({
      story: "should-be-ignored",
      steps: [
        { timestamp: "2026-04-20T12:00:00Z", text: "structured first" },
        { timestamp: "2026-04-20T12:00:01Z", text: "structured second" },
      ],
    });
    const { container } = render(<NarrativePanel narrative={narrative} />);
    const pre = container.querySelector("pre")!;
    expect(pre.textContent).toContain("structured first");
    expect(pre.textContent).toContain("structured second");
    expect(pre.textContent).not.toContain("should-be-ignored");
  });

  it("ignores blank lines in the fallback story string", () => {
    const { container } = render(
      <NarrativePanel
        narrative={mk({ story: "first\n\n\nsecond\n   \nthird" })}
        defaultMode="expanded"
      />,
    );
    expect(container.querySelectorAll("ol > li")).toHaveLength(3);
  });

  it("renders the locale label in the header", () => {
    render(<NarrativePanel narrative={mk({ locale: "pt-BR" })} />);
    expect(screen.getByText(/locale/)).toBeInTheDocument();
    expect(screen.getByText(/pt-BR/)).toBeInTheDocument();
  });

  it("expanded mode surfaces the step's timestamp when present", () => {
    const narrative = mk({
      steps: [
        { timestamp: "2026-04-20T12:00:00.000Z", text: "step-one" },
        { timestamp: "2026-04-20T12:00:00.250Z", text: "step-two" },
      ],
    });
    render(<NarrativePanel narrative={narrative} defaultMode="expanded" />);
    expect(screen.getByText("2026-04-20T12:00:00.000Z")).toBeInTheDocument();
    // Relative-ts annotation pinned for the second step.
    expect(screen.getByText("+250 ms")).toBeInTheDocument();
  });
});
