/* Vitest unit tests for Mission H4 §4.8 ADR-D8 + v0.49.25 ThreadSnapshotViewer widget. */

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThreadSnapshotViewer } from "./ThreadSnapshotViewer";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => {
      if (options && Object.keys(options).length > 0) {
        return `${key}:${JSON.stringify(options)}`;
      }
      return key;
    },
  }),
}));

const mockApiFetch = vi.fn();

vi.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
}));

const SAMPLE_DUMP = `# Thread snapshot — cohort=thread_count
# observed_at_unix=1716143280
# cohort_observed=178
# cohort_budget=32

=== Thread 1 (name='MainThread', daemon=False) ===
  src/sovyx/main.py:42 in main
  src/sovyx/main.py:18 in run

=== Thread 2 (name='asyncio_0', daemon=True) ===
  src/sovyx/loop.py:99 in loop
`;

function mockResponse(status: number, body: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  } as unknown as Response;
}

describe("ThreadSnapshotViewer", () => {
  beforeEach(() => {
    mockApiFetch.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows loading state before the fetch resolves", () => {
    mockApiFetch.mockReturnValue(new Promise(() => undefined));
    render(<ThreadSnapshotViewer timestamp={1716143280} />);
    expect(screen.getByTestId("thread-snapshot-loading")).toBeInTheDocument();
  });

  it("renders the not-found state on 404", async () => {
    mockApiFetch.mockResolvedValue(mockResponse(404, {}));
    render(<ThreadSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("thread-snapshot-not-found")).toBeInTheDocument();
    });
  });

  it("renders the error state on 5xx", async () => {
    mockApiFetch.mockResolvedValue(mockResponse(503, {}));
    render(<ThreadSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("thread-snapshot-error")).toBeInTheDocument();
    });
  });

  it("renders the error state on network rejection", async () => {
    mockApiFetch.mockRejectedValue(new Error("network down"));
    render(<ThreadSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("thread-snapshot-error")).toBeInTheDocument();
    });
  });

  it("renders the dump and counts threads from the content", async () => {
    mockApiFetch.mockResolvedValue(
      mockResponse(200, { content: SAMPLE_DUMP, timestamp: "1716143280" }),
    );
    render(<ThreadSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(screen.getByTestId("thread-snapshot-viewer")).toBeInTheDocument();
    });
    expect(screen.getByTestId("thread-snapshot-dump").textContent).toContain(
      "MainThread",
    );
    // Subtitle interpolation receives count=2 (two "=== Thread " markers).
    expect(screen.getByText(/threadSnapshot\.subtitle/).textContent).toContain(
      '"count":2',
    );
  });

  it("re-fetches when the timestamp prop changes", async () => {
    mockApiFetch.mockResolvedValue(
      mockResponse(200, { content: SAMPLE_DUMP, timestamp: "1716143280" }),
    );
    const { rerender } = render(<ThreadSnapshotViewer timestamp={1716143280} />);
    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalledWith(
        "/api/engine/resources/thread-snapshot/1716143280",
      );
    });
    rerender(<ThreadSnapshotViewer timestamp={1716143400} />);
    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalledWith(
        "/api/engine/resources/thread-snapshot/1716143400",
      );
    });
  });
});
