/**
 * Brain page tests — V05-P03 + V05-P09.
 *
 * Mocks API to avoid real fetches. Tests loading, error, success,
 * search, and search-result highlighting states.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@/test/test-utils";
import { useDashboardStore } from "@/stores/dashboard";
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

const MOCK_GRAPH = {
  nodes: [
    { id: "n1", name: "TypeScript", category: "fact", importance: 0.8, confidence: 0.9, access_count: 5 },
    { id: "n2", name: "React", category: "skill", importance: 0.7, confidence: 0.85, access_count: 3 },
  ],
  links: [
    { source: "n1", target: "n2", relation_type: "related_to", weight: 0.6 },
  ],
};

const MOCK_SEARCH = {
  results: [
    { id: "n1", name: "TypeScript", category: "fact", importance: 0.8, confidence: 0.9, access_count: 5, score: 0.92 },
  ],
  query: "typescript",
};

beforeEach(() => {
  vi.clearAllMocks();
  // Reset zustand store between tests to prevent state leakage
  useDashboardStore.setState({
    brainGraph: null,
    selectedBrainNode: null,
    brainSearchResults: [],
    brainSearchQuery: "",
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("BrainPage", () => {
  it("shows loading state initially", () => {
    mockApi.get.mockImplementation(() => new Promise(() => {})); // never resolves
    render(<BrainPage />);
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
    mockApi.get.mockResolvedValueOnce(MOCK_GRAPH);
    render(<BrainPage />);
    await waitFor(() => {
      expect(screen.getByTestId("force-graph")).toBeInTheDocument();
    });
  });

  it("shows empty state when no nodes", async () => {
    mockApi.get.mockResolvedValueOnce({ nodes: [], links: [] });
    render(<BrainPage />);
    await waitFor(() => {
      expect(screen.getByText(/no concepts yet/i)).toBeInTheDocument();
    });
  });

  it("renders search input", async () => {
    mockApi.get.mockResolvedValueOnce(MOCK_GRAPH);
    render(<BrainPage />);
    await waitFor(() => {
      expect(screen.getByTestId("force-graph")).toBeInTheDocument();
    });
    const searchInput = screen.getByRole("textbox", { name: /search concepts/i });
    expect(searchInput).toBeInTheDocument();
  });

  it("performs search on input with debounce", async () => {
    mockApi.get
      .mockResolvedValueOnce(MOCK_GRAPH) // graph fetch
      .mockResolvedValueOnce(MOCK_SEARCH); // search fetch

    render(<BrainPage />);

    // Wait for graph to load
    await waitFor(() => {
      expect(screen.getByTestId("force-graph")).toBeInTheDocument();
    });

    const searchInput = screen.getByRole("textbox", { name: /search concepts/i });
    fireEvent.change(searchInput, { target: { value: "typescript" } });

    // Wait for debounce (300ms) + search fetch to resolve — results appear
    await waitFor(() => {
      expect(screen.getByText("TypeScript")).toBeInTheDocument();
    });

    // Verify the search endpoint was called with correct URL
    const searchCall = mockApi.get.mock.calls.find(
      (c: unknown[]) => typeof c[0] === "string" && (c[0] as string).includes("/api/brain/search"),
    );
    expect(searchCall).toBeTruthy();
    expect((searchCall as string[])[0]).toContain("q=typescript");
  });

  it("shows search results as chips", async () => {
    mockApi.get
      .mockResolvedValueOnce(MOCK_GRAPH)
      .mockResolvedValueOnce(MOCK_SEARCH);

    render(<BrainPage />);
    await waitFor(() => {
      expect(screen.getByTestId("force-graph")).toBeInTheDocument();
    });

    const searchInput = screen.getByRole("textbox", { name: /search concepts/i });
    fireEvent.change(searchInput, { target: { value: "typescript" } });

    // Wait for debounce + fetch — chip shows result name
    await waitFor(() => {
      expect(screen.getByText("TypeScript")).toBeInTheDocument();
    });
    // Score badge
    expect(screen.getByText("92%")).toBeInTheDocument();
  });

  it("clears search when X button is clicked", async () => {
    mockApi.get
      .mockResolvedValueOnce(MOCK_GRAPH)
      .mockResolvedValueOnce(MOCK_SEARCH);

    render(<BrainPage />);
    await waitFor(() => {
      expect(screen.getByTestId("force-graph")).toBeInTheDocument();
    });

    const searchInput = screen.getByRole("textbox", { name: /search concepts/i });
    fireEvent.change(searchInput, { target: { value: "typescript" } });

    await waitFor(() => {
      expect(screen.getByText("TypeScript")).toBeInTheDocument();
    });

    // aria-label is t("common:clear", { defaultValue: "Clear" }) = "Clear"
    const clearBtn = screen.getByLabelText("Clear");
    fireEvent.click(clearBtn);

    expect(searchInput).toHaveValue("");
  });

  it("hides results panel when search results are empty", async () => {
    mockApi.get
      .mockResolvedValueOnce(MOCK_GRAPH)
      .mockResolvedValueOnce({ results: [], query: "xyzzy" });

    render(<BrainPage />);
    await waitFor(() => {
      expect(screen.getByTestId("force-graph")).toBeInTheDocument();
    });

    const searchInput = screen.getByRole("textbox", { name: /search concepts/i });
    fireEvent.change(searchInput, { target: { value: "xyzzy" } });

    // Wait for search to complete
    await waitFor(() => {
      expect(mockApi.get).toHaveBeenCalledTimes(2);
    });

    // No results panel should be visible (component renders nothing for empty results)
    expect(screen.queryByText(/\d+ results/)).not.toBeInTheDocument();
  });
});
