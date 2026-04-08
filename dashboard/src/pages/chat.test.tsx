/**
 * Tests for Chat page (DASH-03).
 *
 * Coverage targets: chat.tsx ≥95%
 * Tests: render, input, send, loading, error, new chat, empty state.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";
import userEvent from "@testing-library/user-event";
import ChatPage from "./chat";
import { useDashboardStore } from "@/stores/dashboard";

vi.mock("@/lib/api", () => ({
  api: {
    post: vi.fn(),
    get: vi.fn(),
  },
  isAbortError: (err: unknown) =>
    err instanceof DOMException && (err as DOMException).name === "AbortError",
  BASE_URL: "",
  setToken: vi.fn(),
  clearToken: vi.fn(),
}));

import { api } from "@/lib/api";

const mockApi = api as unknown as { post: ReturnType<typeof vi.fn> };

beforeEach(() => {
  vi.clearAllMocks();
  useDashboardStore.getState().clearChat();
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
});
