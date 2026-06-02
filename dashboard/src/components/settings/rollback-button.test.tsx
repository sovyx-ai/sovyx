/**
 * RollbackButton tests (rc.12) -- Settings -> Voice -> Restore previous.
 *
 * Covers:
 * * Renders disabled when calibrationBackupCount is null (load failed).
 * * Renders disabled when count is 0 (chain empty).
 * * Renders enabled when count > 0.
 * * Confirm flow POSTs /rollback + shows success toast.
 * * 409 chain-exhausted path surfaces an error toast (the slice
 *   already maps the 409 detail into calibrationError; the button
 *   surfaces the failed-toast).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@/test/test-utils";

import {
  __resetResolvedMindIdCacheForTests,
  __seedResolvedMindIdForTests,
} from "@/hooks/use-resolved-mind-id";
import { useDashboardStore } from "@/stores/dashboard";
import { RollbackButton } from "./rollback-button";

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

const mockToastSuccess = vi.fn();
const mockToastError = vi.fn();

vi.mock("sonner", () => ({
  toast: {
    success: (...args: unknown[]) => mockToastSuccess(...args),
    error: (...args: unknown[]) => mockToastError(...args),
  },
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockFetch.mockReset();
  mockToastSuccess.mockReset();
  mockToastError.mockReset();
  // RollbackButton subscribes to ``useResolvedMindId`` so every slice
  // call carries an explicit ``mind_id``. That hook is a module-level
  // singleton that, on first subscription, fires its OWN
  // ``/api/onboarding/state`` fetch through the shared ``mockFetch`` —
  // silently consuming a queued ``mockResolvedValueOnce`` and corrupting
  // the strict response ordering the tests rely on. It also transitions
  // ``mindId`` from the loading sentinel to the resolved value, which
  // re-fires the mount ``loadBackups`` effect (an extra fetch). Reset +
  // pre-seed the singleton so ``mindId`` is stable from first render and
  // the hook never touches ``mockFetch``. (Same proven pattern as
  // signing-key-card.test.tsx.)
  __resetResolvedMindIdCacheForTests();
  __seedResolvedMindIdForTests({
    complete: true,
    mind_name: "Default",
    mind_id: "default",
    provider_configured: true,
    default_provider: "ollama",
    default_model: "llama3.1:latest",
    ollama_available: true,
    ollama_models: ["llama3.1:latest"],
  });
  useDashboardStore.setState({
    calibrationBackupCount: null,
    calibrationError: null,
    currentCalibrationJob: null,
    calibrationFeatureFlag: null,
  });
});

afterEach(() => {
  __resetResolvedMindIdCacheForTests();
});

describe("RollbackButton", () => {
  it("renders disabled trigger while backup count is loading (null)", async () => {
    // Mount fires loadCalibrationBackups which the slice will try to
    // fetch. We don't stub fetch -> it rejects -> count stays null
    // -> button stays disabled. Conservative gate.
    mockFetch.mockRejectedValue(new Error("network down"));
    render(<RollbackButton />);
    const trigger = await screen.findByTestId("settings-rollback-toggle");
    expect(trigger).toBeInTheDocument();
    expect(trigger).toBeDisabled();
  });

  it("renders disabled trigger when backup chain is empty (count = 0)", async () => {
    // Backups endpoint returns empty generations.
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ mind_id: "default", generations: [] }),
    );
    render(<RollbackButton />);
    await waitFor(() => {
      const trigger = screen.getByTestId("settings-rollback-toggle");
      expect(trigger).toBeDisabled();
    });
  });

  it("renders enabled trigger when at least one backup exists", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ mind_id: "default", generations: [1, 2] }),
    );
    render(<RollbackButton />);
    await waitFor(() => {
      const trigger = screen.getByTestId("settings-rollback-toggle");
      expect(trigger).not.toBeDisabled();
    });
  });

  it("confirm flow POSTs /rollback and shows success toast", async () => {
    // 1. GET /backups -> count = 2 (enables button)
    // 2. POST /rollback -> success with 1 remaining
    mockFetch
      .mockResolvedValueOnce(
        jsonResponse({ mind_id: "default", generations: [1, 2] }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          restored_path: "/home/user/.sovyx/default/calibration.json",
          backup_generations_remaining: 1,
          resolved_mind_id: "default",
          resolved_mind_id_source: "fallback_default",
        }),
      );

    render(<RollbackButton />);
    await waitFor(() =>
      expect(screen.getByTestId("settings-rollback-toggle")).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByTestId("settings-rollback-toggle"));
    fireEvent.click(screen.getByTestId("settings-rollback-confirm"));

    await waitFor(() => expect(mockToastSuccess).toHaveBeenCalled());
    const postCall = mockFetch.mock.calls.find((c) => {
      const init = c[1] as RequestInit | undefined;
      return init?.method === "POST";
    });
    expect(postCall).toBeDefined();
  });

  it("409 chain-exhausted shows failure toast and keeps button visible", async () => {
    mockFetch
      .mockResolvedValueOnce(
        jsonResponse({ mind_id: "default", generations: [1] }),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          { detail: "no calibration backup at .bak.1 — chain exhausted" },
          409,
        ),
      );

    render(<RollbackButton />);
    await waitFor(() =>
      expect(screen.getByTestId("settings-rollback-toggle")).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByTestId("settings-rollback-toggle"));
    fireEvent.click(screen.getByTestId("settings-rollback-confirm"));

    await waitFor(() => expect(mockToastError).toHaveBeenCalled());
  });

  it("dismiss button hides the confirm flow", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ mind_id: "default", generations: [1] }),
    );
    render(<RollbackButton />);
    await waitFor(() =>
      expect(screen.getByTestId("settings-rollback-toggle")).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByTestId("settings-rollback-toggle"));
    expect(screen.getByTestId("settings-rollback-cancel")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("settings-rollback-cancel"));
    expect(
      screen.queryByTestId("settings-rollback-confirm"),
    ).not.toBeInTheDocument();
  });

  // ════════════════════════════════════════════════════════════════════
  // rc.15 polish bundle — auto-refresh + retry behaviour.
  // ════════════════════════════════════════════════════════════════════

  it("rc.15 LOW.1: re-fetches backups when calibration job reaches terminal", async () => {
    // Initial mount: 1 backup available.
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ mind_id: "default", generations: [1] }),
    );
    // Auto-refresh after terminal: 2 backups (just-completed save).
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ mind_id: "default", generations: [1, 2] }),
    );

    render(<RollbackButton />);
    await waitFor(() =>
      expect(useDashboardStore.getState().calibrationBackupCount).toBe(1),
    );

    // Simulate the calibration slice receiving a terminal snapshot
    // (DONE) — this is what subscribeToCalibrationJob does on the
    // last WS message before unsubscribing. Wrap in ``act`` so the
    // terminal-subscription effect (which fires the re-fetch) is
    // flushed synchronously before we assert, instead of racing the
    // assertion's poll loop.
    act(() => {
      useDashboardStore.setState({
        currentCalibrationJob: {
          job_id: "default",
          mind_id: "default",
          status: "done",
          progress: 1.0,
          current_stage_message: "complete",
          created_at_utc: "2026-05-07T00:00:00Z",
          updated_at_utc: "2026-05-07T00:08:00Z",
          profile_path: "/home/user/.sovyx/default/calibration.json",
          triage_winner_hid: "H10",
          error_summary: null,
          fallback_reason: null,
          extras: null,
        },
      });
    });

    await waitFor(() =>
      expect(useDashboardStore.getState().calibrationBackupCount).toBe(2),
    );
  });

  it("rc.15 LOW.4: retries loadBackups after initial-mount failure (fake timers, 1500ms delay)", async () => {
    // The component schedules a single ``setTimeout(_RETRY_DELAY_MS=1500)``
    // retry after the initial-mount load fails. Pre-hardening this test
    // used REAL timers + the real 1500ms production delay with a 3000ms
    // ``waitFor`` — under full-suite parallelism on a low-memory machine
    // the real wall-clock delay routinely exceeded the headroom and the
    // test flaked. Fix: drive the retry deterministically with fake
    // timers — advance exactly the retry delay and assert, with zero
    // dependence on wall-clock scheduling.
    vi.useFakeTimers();
    try {
      // Flake fix (known intermittent "expected 1 to be null"). Root cause:
      // under full-suite cross-file ordering the `useResolvedMindId` module
      // singleton can re-subscribe and trigger a VARIABLE NUMBER of mount
      // `loadBackups` calls. A count-based mock ("call #1 rejects, #2
      // resolves") then lets an EXTRA mount call resolve → count=1 before the
      // post-mount assertion. Fix: gate on a flag, not a call count — EVERY
      // mount load rejects (however many) until we explicitly allow success
      // before firing the retry timer. Immune to the mount-call count + order.
      let allowSuccess = false;
      mockFetch.mockImplementation((input: RequestInfo | URL) => {
        if (String(input).includes("/api/voice/calibration/backups")) {
          if (!allowSuccess) {
            return Promise.reject(new Error("network blip"));
          }
          return Promise.resolve(
            jsonResponse({ mind_id: "default", generations: [1] }),
          );
        }
        return Promise.resolve(jsonResponse({}));
      });

      render(<RollbackButton />);

      // Settle: EVERY mount load rejects, so count stays null and the retry
      // ``setTimeout`` is armed. Wrap in ``act`` to flush the re-render.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(useDashboardStore.getState().calibrationBackupCount).toBeNull();

      // Now allow success, fire the retry timer → its loadBackups resolves →
      // count becomes 1.
      allowSuccess = true;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1500);
      });

      expect(useDashboardStore.getState().calibrationBackupCount).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  // ====================================================================
  // v0.31.2 F4 — defense-in-depth platform_supported gate
  // ====================================================================

  it("disables button when platform_supported=false even with backups present", async () => {
    // Backend (post-v0.31.2) actually returns generations=[] on non-Linux,
    // but the frontend gate is defense-in-depth: if a future refactor
    // ever loosens the backend, the platform_supported flag still gates.
    // Here we simulate the "stale state" scenario: backups already
    // loaded BEFORE the platform_supported flag arrived.
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ mind_id: "default", generations: [1, 2] }),
    );
    useDashboardStore.setState({
      calibrationFeatureFlag: {
        enabled: true,
        runtime_override_active: false,
        platform_supported: false,
      },
    });
    render(<RollbackButton />);
    await waitFor(() => {
      const trigger = screen.getByTestId("settings-rollback-toggle");
      expect(trigger).toBeDisabled();
    });
    // Tooltip surfaces the platform-specific message, not the
    // empty-chain message.
    const trigger = screen.getByTestId("settings-rollback-toggle");
    expect(trigger.getAttribute("title")).toMatch(/Linux-only|Linux only/i);
  });

  it("enables button when platform_supported=true and backups exist", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ mind_id: "default", generations: [1] }),
    );
    useDashboardStore.setState({
      calibrationFeatureFlag: {
        enabled: true,
        runtime_override_active: false,
        platform_supported: true,
      },
    });
    render(<RollbackButton />);
    await waitFor(() => {
      const trigger = screen.getByTestId("settings-rollback-toggle");
      expect(trigger).not.toBeDisabled();
    });
  });

  it("falls through to legacy behaviour when platform_supported is undefined (pre-rc.11 zod schema)", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ mind_id: "default", generations: [1] }),
    );
    // Legacy flag without platform_supported field — schema's
    // ?? true default applies, button enables when count > 0.
    useDashboardStore.setState({
      calibrationFeatureFlag: {
        enabled: true,
        runtime_override_active: false,
      } as never,
    });
    render(<RollbackButton />);
    await waitFor(() => {
      const trigger = screen.getByTestId("settings-rollback-toggle");
      expect(trigger).not.toBeDisabled();
    });
  });
});
