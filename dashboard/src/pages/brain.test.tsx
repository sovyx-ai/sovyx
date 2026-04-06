/**
 * Brain page tests — POLISH-16.
 *
 * Mocks API to avoid real fetches. Tests loading, error, and success states.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";
import BrainPage from "./brain";

// Mock the API module
vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn(),
  },
  isAbortError: (err: unknown) =>
    err instanceof DOMException && (err as DOMException).name === "AbortError",
}));

// Mock ForceGraph2D (heavy canvas component)
vi.mock("react-force-graph-2d", () => ({
  default: () => <div data-testid="force-graph">ForceGraph</div>,
}));

import { api } from "@/lib/api";

const mockApi = api as unknown as { get: ReturnType<typeof vi.fn> };

beforeEach(() => {
  vi.clearAllMocks();
});

describe("BrainPage", () => {
  it("shows loading state initially", () => {
    mockApi.get.mockImplementation(() => new Promise(() => {})); // never resolves
    render(<BrainPage />);
    // Should show the spinning loader
    expect(document.querySelector(".animate-spin")).toBeInTheDocument();
  });

  it("shows error state on fetch failure", async () => {
    mockApi.get.mockRejectedValueOnce(new Error("Network error"));
    render(<BrainPage />);
    await waitFor(() => {
      expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
    });
  });

  it("renders brain graph on successful fetch", async () => {
    mockApi.get.mockResolvedValueOnce({
      nodes: [
        { id: "n1", label: "Test", category: "fact", importance: 0.8 },
      ],
      edges: [],
    });
    render(<BrainPage />);
    await waitFor(() => {
      expect(screen.getByTestId("force-graph")).toBeInTheDocument();
    });
  });

  it("shows empty state when no nodes", async () => {
    mockApi.get.mockResolvedValueOnce({ nodes: [], edges: [] });
    render(<BrainPage />);
    await waitFor(() => {
      expect(screen.getByText(/empty/i)).toBeInTheDocument();
    });
  });
});
