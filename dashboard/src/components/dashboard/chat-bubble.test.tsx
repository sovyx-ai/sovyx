import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ChatBubble } from "./chat-bubble";
import type { Message } from "@/types/api";

function makeMsg(overrides: Partial<Message> = {}): Message {
  return {
    id: "m1",
    role: "user",
    content: "hello",
    timestamp: new Date().toISOString(),
    ...overrides,
  };
}

describe("ChatBubble", () => {
  it("renders user message content as plain text", () => {
    render(<ChatBubble message={makeMsg({ content: "plain body" })} participantName="Alice" />);
    expect(screen.getByText("plain body")).toBeInTheDocument();
  });

  it("renders assistant message via markdown path", () => {
    render(
      <ChatBubble
        message={makeMsg({ role: "assistant", content: "**bold**" })}
        participantName="Alice"
      />,
    );
    expect(screen.getByText("bold")).toBeInTheDocument();
  });

  it("shows the user avatar (not the mind avatar) for user messages", () => {
    render(<ChatBubble message={makeMsg({ role: "user" })} participantName="Alice" />);
    expect(screen.queryByLabelText("Sovyx Mind")).not.toBeInTheDocument();
  });

  it("shows the mind avatar for assistant messages", () => {
    render(<ChatBubble message={makeMsg({ role: "assistant" })} participantName="Alice" />);
    expect(screen.getByLabelText("Sovyx Mind")).toBeInTheDocument();
  });
});
