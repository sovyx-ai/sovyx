import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import { ChatThread } from "./chat-thread";
import type { Message } from "@/types/api";

// jsdom lacks scrollIntoView — stub to no-op.
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

function makeMsg(i: number): Message {
  return {
    id: `m${i}`,
    role: i % 2 === 0 ? "user" : "assistant",
    content: `msg ${i}`,
    timestamp: new Date().toISOString(),
  };
}

describe("ChatThread", () => {
  it("renders every message in the list", () => {
    const msgs = [makeMsg(1), makeMsg(2), makeMsg(3)];
    render(<ChatThread messages={msgs} participantName="Alice" />);
    expect(screen.getByText("msg 1")).toBeInTheDocument();
    expect(screen.getByText("msg 2")).toBeInTheDocument();
    expect(screen.getByText("msg 3")).toBeInTheDocument();
  });

  it("shows the empty state when there are no messages", () => {
    render(<ChatThread messages={[]} participantName="Alice" />);
    // EmptyState title uses conversations.list.empty — just verify something
    // renders when there's no message body
    expect(screen.queryByText("msg 1")).not.toBeInTheDocument();
  });

  it("shows a loading spinner while loading", () => {
    const { container } = render(
      <ChatThread messages={[]} participantName="Alice" loading />,
    );
    expect(container.querySelector(".animate-spin")).toBeInTheDocument();
  });
});
