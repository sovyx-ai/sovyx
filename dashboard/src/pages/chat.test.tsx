/**
 * Tests for Chat page (DASH-03).
 *
 * Coverage targets: chat.tsx ≥95%
 * Tests: render, input, send, loading, error, new chat, empty state.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";
import userEvent from "@testing-library/user-event";
import ChatPage from "./chat";
import { useDashboardStore } from "@/stores/dashboard";

vi.mock("@/lib/api", () => ({
  api: {
    post: vi.fn(),
    get: vi.fn().mockResolvedValue({ episode_count: 0, label: "", quadrant: "neutral" }),
  },
  isAbortError: (err: unknown) =>
    err instanceof DOMException && (err as DOMException).name === "AbortError",
  getToken: () => "test-token",
  BASE_URL: "",
  setToken: vi.fn(),
  clearToken: vi.fn(),
}));

import { api } from "@/lib/api";

const mockApi = api as unknown as { post: ReturnType<typeof vi.fn> };

// Mock fetch to reject (forces SSE fallback to batch endpoint)
const originalFetch = globalThis.fetch;
beforeEach(() => {
  vi.clearAllMocks();
  useDashboardStore.getState().clearChat();
  globalThis.fetch = vi.fn().mockRejectedValue(new Error("SSE not available in test"));
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("ChatPage", () => {
  it("renders page title", () => {
    render(<ChatPage />);
    expect(screen.getByText("Chat")).toBeInTheDocument();
  });

  it("renders subtitle", () => {
    render(<ChatPage />);
    expect(
      screen.getByText("Talk directly with your mind"),
    ).toBeInTheDocument();
  });

  it("renders empty state when no messages", () => {
    render(<ChatPage />);
    expect(
      screen.getByText("Start a conversation"),
    ).toBeInTheDocument();
  });

  it("renders chat input", () => {
    render(<ChatPage />);
    expect(screen.getByTestId("chat-input")).toBeInTheDocument();
  });

  it("renders send button", () => {
    render(<ChatPage />);
    expect(screen.getByTestId("chat-send")).toBeInTheDocument();
  });

  it("send button is disabled when input is empty", () => {
    render(<ChatPage />);
    const btn = screen.getByTestId("chat-send");
    expect(btn).toBeDisabled();
  });

  it("send button is enabled when input has text", async () => {
    const user = userEvent.setup();
    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "Hello");
    const btn = screen.getByTestId("chat-send");
    expect(btn).not.toBeDisabled();
  });

  it("shows user message optimistically after sending", async () => {
    const user = userEvent.setup();
    mockApi.post.mockResolvedValueOnce({
      response: "Hi there!",
      conversation_id: "conv-1",
      mind_id: "aria",
      timestamp: new Date().toISOString(),
    });

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "Hello");
    await user.click(screen.getByTestId("chat-send"));

    expect(screen.getByText("Hello")).toBeInTheDocument();
  });

  it("shows AI response after sending", async () => {
    const user = userEvent.setup();
    mockApi.post.mockResolvedValueOnce({
      response: "Hi from Aria!",
      conversation_id: "conv-1",
      mind_id: "aria",
      timestamp: new Date().toISOString(),
    });

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "Hello");
    await user.click(screen.getByTestId("chat-send"));

    await waitFor(() => {
      expect(screen.getByText("Hi from Aria!")).toBeInTheDocument();
    });
  });

  it("clears input after sending", async () => {
    const user = userEvent.setup();
    mockApi.post.mockResolvedValueOnce({
      response: "Response",
      conversation_id: "conv-1",
      mind_id: "aria",
      timestamp: new Date().toISOString(),
    });

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    await user.type(input, "Hello");
    await user.click(screen.getByTestId("chat-send"));

    expect(input.value).toBe("");
  });

  it("shows loading indicator while waiting for response", async () => {
    const user = userEvent.setup();
    mockApi.post.mockReturnValue(new Promise(() => {}));

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "Hello");
    await user.click(screen.getByTestId("chat-send"));

    expect(screen.getByTestId("chat-loading")).toBeInTheDocument();
    expect(screen.getByText("Thinking...")).toBeInTheDocument();
  });

  it("shows error message on API failure", async () => {
    const user = userEvent.setup();
    mockApi.post.mockRejectedValueOnce(new Error("Network error"));

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "Hello");
    await user.click(screen.getByTestId("chat-send"));

    await waitFor(() => {
      expect(
        screen.getByText("Something went wrong. Please try again."),
      ).toBeInTheDocument();
    });
  });

  it("sends Enter key to submit", async () => {
    const user = userEvent.setup();
    mockApi.post.mockResolvedValueOnce({
      response: "Response",
      conversation_id: "conv-1",
      mind_id: "aria",
      timestamp: new Date().toISOString(),
    });

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "Hello{enter}");

    expect(mockApi.post).toHaveBeenCalledOnce();
  });

  it("Shift+Enter does not submit", async () => {
    const user = userEvent.setup();

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "Hello{shift>}{enter}{/shift}");

    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it("shows New Chat button when messages exist", async () => {
    const user = userEvent.setup();
    mockApi.post.mockResolvedValueOnce({
      response: "Hi!",
      conversation_id: "conv-1",
      mind_id: "aria",
      timestamp: new Date().toISOString(),
    });

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "Hello");
    await user.click(screen.getByTestId("chat-send"));

    await waitFor(() => {
      expect(screen.getByText("New Chat")).toBeInTheDocument();
    });
  });

  it("New Chat clears messages and shows empty state", async () => {
    const user = userEvent.setup();
    mockApi.post.mockResolvedValueOnce({
      response: "Hi!",
      conversation_id: "conv-1",
      mind_id: "aria",
      timestamp: new Date().toISOString(),
    });

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "Hello");
    await user.click(screen.getByTestId("chat-send"));

    await waitFor(() => {
      expect(screen.getByText("New Chat")).toBeInTheDocument();
    });

    await user.click(screen.getByText("New Chat"));

    expect(
      screen.getByText("Start a conversation"),
    ).toBeInTheDocument();
  });

  it("does not show New Chat button when empty", () => {
    render(<ChatPage />);
    expect(screen.queryByText("New Chat")).not.toBeInTheDocument();
  });

  it("has correct data-testid on page container", () => {
    render(<ChatPage />);
    expect(screen.getByTestId("chat-page")).toBeInTheDocument();
  });

  it("passes conversation_id to API for continuity", async () => {
    const user = userEvent.setup();

    mockApi.post.mockResolvedValueOnce({
      response: "Hello!",
      conversation_id: "conv-new",
      mind_id: "aria",
      timestamp: new Date().toISOString(),
    });

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "First");
    await user.click(screen.getByTestId("chat-send"));

    await waitFor(() => {
      expect(screen.getByText("Hello!")).toBeInTheDocument();
    });

    mockApi.post.mockResolvedValueOnce({
      response: "World!",
      conversation_id: "conv-new",
      mind_id: "aria",
      timestamp: new Date().toISOString(),
    });

    await user.type(input, "Second");
    await user.click(screen.getByTestId("chat-send"));

    const secondCall = mockApi.post.mock.calls[1]!;
    expect(secondCall[1]).toEqual(
      expect.objectContaining({ conversation_id: "conv-new" }),
    );
  });

  // ── Tag rendering ─────────────────────────────────────────────────

  it("renders tags from the backend on the assistant message", async () => {
    const user = userEvent.setup();
    mockApi.post.mockResolvedValueOnce({
      response: "Rate calculated.",
      conversation_id: "conv-1",
      mind_id: "aria",
      timestamp: new Date().toISOString(),
      tags: ["financial_math", "brain"],
    });

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "5% on 1000?");
    await user.click(screen.getByTestId("chat-send"));

    await waitFor(() => {
      expect(screen.getByText("Rate calculated.")).toBeInTheDocument();
    });

    // Both pills present, with translated labels.
    expect(
      screen.getByText("financial", { selector: "[data-tag]" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("brain", { selector: "[data-tag]" }),
    ).toBeInTheDocument();
  });

  it("never renders tags on user messages", async () => {
    const user = userEvent.setup();
    mockApi.post.mockResolvedValueOnce({
      response: "Hi back.",
      conversation_id: "conv-1",
      mind_id: "aria",
      timestamp: new Date().toISOString(),
      tags: ["brain"],
    });

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "Hello");
    await user.click(screen.getByTestId("chat-send"));

    await waitFor(() => {
      expect(screen.getByText("Hi back.")).toBeInTheDocument();
    });

    // Exactly one brain pill (on the assistant message), never duplicated
    // beside the user's "Hello" bubble.
    expect(screen.getAllByText("brain")).toHaveLength(1);
  });

  it("renders no tags when the backend omits the tags field", async () => {
    const user = userEvent.setup();
    // Older backends (pre-0.11.2) may not send the tags field. The UI
    // must degrade gracefully rather than crashing.
    mockApi.post.mockResolvedValueOnce({
      response: "Legacy reply.",
      conversation_id: "conv-1",
      mind_id: "aria",
      timestamp: new Date().toISOString(),
    });

    render(<ChatPage />);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "hey");
    await user.click(screen.getByTestId("chat-send"));

    await waitFor(() => {
      expect(screen.getByText("Legacy reply.")).toBeInTheDocument();
    });

    // No tag pill rendered at all for legacy responses.
    expect(document.querySelector("[data-tag]")).toBeNull();
  });
});
