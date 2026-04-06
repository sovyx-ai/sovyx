/**
 * Conversations page tests — POLISH-16.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";
import ConversationsPage from "./conversations";

vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn(),
  },
  isAbortError: (err: unknown) =>
    err instanceof DOMException && (err as DOMException).name === "AbortError",
}));

import { api } from "@/lib/api";

const mockApi = api as unknown as { get: ReturnType<typeof vi.fn> };

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ConversationsPage", () => {
  it("shows empty state when no conversations", async () => {
    mockApi.get.mockResolvedValueOnce({ conversations: [] });
    render(<ConversationsPage />);
    await waitFor(() => {
      expect(screen.getByText(/empty|no conversations/i)).toBeInTheDocument();
    });
  });

  it("renders conversation list on success", async () => {
    mockApi.get.mockResolvedValueOnce({
      conversations: [
        {
          id: "conv-1",
          participant: "Alice",
          channel: "telegram",
          status: "active",
          message_count: 10,
          last_message_at: new Date().toISOString(),
        },
      ],
    });
    render(<ConversationsPage />);
    await waitFor(() => {
      expect(screen.getByText("Alice")).toBeInTheDocument();
    });
  });

  it("shows error state on fetch failure", async () => {
    mockApi.get.mockRejectedValueOnce(new Error("Network error"));
    render(<ConversationsPage />);
    await waitFor(() => {
      expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
    });
  });

  it("shows select prompt when no conversation active", async () => {
    mockApi.get.mockResolvedValueOnce({ conversations: [] });
    render(<ConversationsPage />);
    await waitFor(() => {
      // The right panel shows "Select a conversation"
      expect(screen.getByText(/select a conversation/i)).toBeInTheDocument();
    });
  });
});
