/**
 * Tests for PerMindForgetCard — Mission v0.30.2 §T2.1.
 *
 * Covers the typed-confirm UX contract:
 *   - Card collapsed by default (just the open button visible).
 *   - Expanded card shows warning banner + confirm input + dry-run
 *     toggle + submit button.
 *   - Submit disabled until confirm input matches mind_id verbatim.
 *   - Submit fires the slice action with the typed value (the
 *     backend's defense-in-depth: confirm field MUST equal mind_id).
 *   - Report panel renders per-table counts after success.
 *   - Error banner renders the slice's error message.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { render } from "@/test/test-utils";
import { PerMindForgetCard } from "./PerMindForgetCard";
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

const FORGET_REPORT = {
  mind_id: MIND_ID,
  concepts_purged: 12,
  relations_purged: 4,
  episodes_purged: 7,
  concept_embeddings_purged: 12,
  episode_embeddings_purged: 7,
  conversation_imports_purged: 0,
  consolidation_log_purged: 0,
  conversations_purged: 3,
  conversation_turns_purged: 18,
  daily_stats_purged: 5,
  consent_ledger_purged: 1,
  total_brain_rows_purged: 42,
  total_conversations_rows_purged: 21,
  total_system_rows_purged: 5,
  total_rows_purged: 69,
  dry_run: false,
};

beforeEach(() => {
  vi.mocked(api.post).mockReset();
  // Reset slice state for each test — destructive ops are per-mind keyed.
  useDashboardStore.setState({
    forgetReports: {},
    forgetPending: {},
    forgetErrors: {},
  });
});

describe("PerMindForgetCard — collapsed default", () => {
  it("shows only the open button by default", () => {
    render(<PerMindForgetCard mindId={MIND_ID} />);
    expect(
      screen.getByRole("button", { name: /forget/i }),
    ).toBeInTheDocument();
    // The confirm input only appears after expansion.
    expect(
      screen.queryByLabelText(/mind id confirmation/i),
    ).not.toBeInTheDocument();
  });

  it("expands to reveal confirm input + warning when opened", () => {
    render(<PerMindForgetCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /forget…/i }));
    expect(
      screen.getByLabelText(/mind id confirmation/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/wipes data permanently/i),
    ).toBeInTheDocument();
  });
});

describe("PerMindForgetCard — typed-confirm gate", () => {
  it("disables submit until confirm input matches mind_id verbatim", () => {
    render(<PerMindForgetCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /forget…/i }));

    // Default dryRun=true → preview button label.
    const submitBtn = screen.getByRole("button", { name: /preview counts/i });
    expect(submitBtn).toBeDisabled();

    // Wrong value — still disabled.
    fireEvent.change(screen.getByLabelText(/mind id confirmation/i), {
      target: { value: "wrong" },
    });
    expect(submitBtn).toBeDisabled();

    // Exact match — enabled.
    fireEvent.change(screen.getByLabelText(/mind id confirmation/i), {
      target: { value: MIND_ID },
    });
    expect(submitBtn).toBeEnabled();
  });

  it("button label flips when dry_run is unchecked", () => {
    render(<PerMindForgetCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /forget…/i }));
    fireEvent.change(screen.getByLabelText(/mind id confirmation/i), {
      target: { value: MIND_ID },
    });
    expect(
      screen.getByRole("button", { name: /preview counts/i }),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("checkbox", { name: /preview only/i }),
    );
    expect(
      screen.getByRole("button", { name: /forget mind permanently/i }),
    ).toBeInTheDocument();
  });
});

describe("PerMindForgetCard — submit flow", () => {
  it("fires slice action with typed value + dry_run flag", async () => {
    vi.mocked(api.post).mockResolvedValueOnce(FORGET_REPORT);
    render(<PerMindForgetCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /forget…/i }));
    fireEvent.change(screen.getByLabelText(/mind id confirmation/i), {
      target: { value: MIND_ID },
    });
    fireEvent.click(
      screen.getByRole("checkbox", { name: /preview only/i }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /forget mind permanently/i }),
    );

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        `/api/mind/${MIND_ID}/forget`,
        { confirm: MIND_ID, dry_run: false },
        expect.objectContaining({ schema: expect.anything() }),
      );
    });
  });

  it("renders per-table report panel after success", async () => {
    vi.mocked(api.post).mockResolvedValueOnce(FORGET_REPORT);
    render(<PerMindForgetCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /forget…/i }));
    fireEvent.change(screen.getByLabelText(/mind id confirmation/i), {
      target: { value: MIND_ID },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /preview counts/i }),
    );

    await waitFor(() => {
      expect(screen.getByTestId("mind-forget-report")).toBeInTheDocument();
    });
    // The total row sums embed the count inline ("Total rows purged: 69").
    expect(screen.getByText(/total rows purged: 69/i)).toBeInTheDocument();
  });

  it("renders error banner when slice surfaces an error", async () => {
    useDashboardStore.setState({
      forgetErrors: { [MIND_ID]: "mind not found: alpha" },
    });
    render(<PerMindForgetCard mindId={MIND_ID} />);
    fireEvent.click(screen.getByRole("button", { name: /forget…/i }));
    expect(screen.getByText(/mind not found: alpha/i)).toBeInTheDocument();
  });
});
