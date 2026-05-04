/**
 * Tests for PerMindRetentionCard — Mission v0.30.2 §T2.2.
 *
 * Covers the preview-then-apply UX contract:
 *   - Card collapsed by default.
 *   - Expanded card shows preview button (no error / report yet).
 *   - Preview fires slice with dry_run=true.
 *   - After preview lands, Apply button replaces Preview.
 *   - Apply fires slice with dry_run=false.
 *   - Effective horizons map renders inside the report panel.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { render } from "@/test/test-utils";
import { PerMindRetentionCard } from "./PerMindRetentionCard";
import { useDashboardStore } from "@/stores/dashboard";
import { api } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
    },
  };
});

const MIND_ID = "alpha";

const PREVIEW_REPORT = {
  mind_id: MIND_ID,
  cutoff_utc: "2026-02-04T00:00:00Z",
  episodes_purged: 100,
  conversations_purged: 50,
  conversation_turns_purged: 200,
  daily_stats_purged: 30,
  consolidation_log_purged: 0,
  consent_ledger_purged: 0,
  effective_horizons: { episodes: 90, conversations: 90, daily_stats: 365 },
  total_brain_rows_purged: 100,
  total_conversations_rows_purged: 250,
  total_system_rows_purged: 30,
  total_rows_purged: 380,
  dry_run: true,
};

const APPLY_REPORT = { ...PREVIEW_REPORT, dry_run: false };

beforeEach(() => {
  vi.mocked(api.post).mockReset();
  useDashboardStore.setState({
    retentionReports: {},
    retentionPending: {},
    retentionErrors: {},
  });
});

describe("PerMindRetentionCard — collapsed default", () => {
  it("shows only the open button by default", () => {
    render(<PerMindRetentionCard mindId={MIND_ID} />);
    expect(
      screen.getByRole("button", { name: /manage retention/i }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("mind-retention-report")).not.toBeInTheDocument();
  });

  it("expands to reveal preview button when opened", () => {
    render(<PerMindRetentionCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /manage retention/i }));
    expect(
      screen.getByRole("button", { name: /preview prune/i }),
    ).toBeInTheDocument();
    // Apply button is NOT rendered until a preview lands.
    expect(
      screen.queryByRole("button", { name: /apply prune/i }),
    ).not.toBeInTheDocument();
  });
});

describe("PerMindRetentionCard — preview-then-apply flow", () => {
  it("preview fires slice with dry_run=true", async () => {
    vi.mocked(api.post).mockResolvedValueOnce(PREVIEW_REPORT);
    render(<PerMindRetentionCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /manage retention/i }));
    fireEvent.click(screen.getByRole("button", { name: /preview prune/i }));

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        `/api/mind/${MIND_ID}/retention/prune`,
        { dry_run: true },
        expect.objectContaining({ schema: expect.anything() }),
      );
    });
  });

  it("apply button replaces preview after preview lands", async () => {
    vi.mocked(api.post).mockResolvedValueOnce(PREVIEW_REPORT);
    render(<PerMindRetentionCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /manage retention/i }));
    fireEvent.click(screen.getByRole("button", { name: /preview prune/i }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /apply prune/i }),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByRole("button", { name: /preview prune/i }),
    ).not.toBeInTheDocument();
  });

  it("apply fires slice with dry_run=false", async () => {
    vi.mocked(api.post)
      .mockResolvedValueOnce(PREVIEW_REPORT)
      .mockResolvedValueOnce(APPLY_REPORT);
    render(<PerMindRetentionCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /manage retention/i }));
    fireEvent.click(screen.getByRole("button", { name: /preview prune/i }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /apply prune/i }),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /apply prune/i }));

    await waitFor(() => {
      expect(api.post).toHaveBeenLastCalledWith(
        `/api/mind/${MIND_ID}/retention/prune`,
        { dry_run: false },
        expect.objectContaining({ schema: expect.anything() }),
      );
    });
  });
});

describe("PerMindRetentionCard — report rendering", () => {
  it("renders effective_horizons map after preview lands", async () => {
    vi.mocked(api.post).mockResolvedValueOnce(PREVIEW_REPORT);
    render(<PerMindRetentionCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /manage retention/i }));
    fireEvent.click(screen.getByRole("button", { name: /preview prune/i }));

    await waitFor(() => {
      expect(screen.getByTestId("mind-retention-report")).toBeInTheDocument();
    });
    // Cutoff timestamp surfaced.
    expect(screen.getByText(/2026-02-04T00:00:00Z/)).toBeInTheDocument();
    // Total surfaced.
    expect(screen.getByText(/total rows purged: 380/i)).toBeInTheDocument();
    // Horizons summary toggle present. The "365" days horizon is
    // unique to the daily_stats surface — proves the horizons map
    // rendered without colliding with shared values like "90" that
    // appear on multiple surfaces.
    fireEvent.click(screen.getByText(/effective horizons/i));
    expect(screen.getByText("365", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getAllByText("90", { selector: "dd" })).toHaveLength(2);
  });

  it("renders error banner when slice surfaces an error", () => {
    useDashboardStore.setState({
      retentionErrors: { [MIND_ID]: "EngineConfig not available" },
    });
    render(<PerMindRetentionCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /manage retention/i }));
    expect(
      screen.getByText(/engineconfig not available/i),
    ).toBeInTheDocument();
  });
});
