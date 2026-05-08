/**
 * Settings page tests.
 *
 * Validates:
 * - Loading/render states
 * - Engine configuration display
 * - Removed placeholder cards (credibility sweep TASK-200)
 * - Export/Import placeholder retained for TASK-201
 * - Mind config sections render when mind is loaded
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@/test/test-utils";
import SettingsPage from "./settings";
// v0.32.0 BT.B.1: the page now reads /api/onboarding/state via the
// shared module-level singleton in ``useResolvedMindId``. Tests that
// don't care about specific mind_id resolution can pre-seed the
// singleton via ``__seedResolvedMindIdForTests`` so the hook's own
// ``api.get`` never fires (preserves the chained
// ``mockResolvedValueOnce`` ordering for the page's settings/config
// /safety calls). Tests that DO care about the resolution path
// reset the singleton + assert the hook fetched its own data.
import {
  __resetResolvedMindIdCacheForTests,
  __seedResolvedMindIdForTests,
} from "@/hooks/use-resolved-mind-id";
import type { OnboardingState } from "@/types/api";

vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn(),
    put: vi.fn(),
  },
  isAbortError: (err: unknown) =>
    err instanceof DOMException && (err as DOMException).name === "AbortError",
}));

import { api } from "@/lib/api";

const mockApi = api as unknown as { get: ReturnType<typeof vi.fn>; put: ReturnType<typeof vi.fn> };

const mockSettings = {
  log_level: "INFO",
  log_format: "text",
  log_file: null,
  data_dir: "/data",
  telemetry_enabled: false,
  relay_enabled: true,
  api_host: "0.0.0.0",
  api_port: 7777,
};

const mockMindConfig = {
  name: "TestMind",
  language: "en",
  timezone: "UTC",
  personality: {
    tone: "neutral",
    formality: 0.5,
    humor: 0.4,
    assertiveness: 0.6,
    curiosity: 0.7,
    empathy: 0.8,
    verbosity: 0.5,
  },
  ocean: {
    openness: 0.7,
    conscientiousness: 0.8,
    extraversion: 0.5,
    agreeableness: 0.6,
    neuroticism: 0.3,
  },
  safety: {
    content_filter: "standard",
    child_safe_mode: false,
    financial_confirmation: true,
  },
  llm: {
    temperature: 0.7,
    budget_daily_usd: 5.0,
    budget_per_conversation_usd: 0.5,
  },
  brain: {
    max_concepts: 10000,
    consolidation_interval_hours: 6,
  },
};

// v0.32.0 BT.B.1 — default seed for the singleton hook, so the
// hook's own ``api.get`` never fires during page render. This
// preserves the chained-mockResolvedValueOnce ordering for the page's
// settings/config/safety calls (which would otherwise be disrupted
// by the hook racing its own fetch through the shared queue).
const DEFAULT_ONBOARDING_STATE: OnboardingState = {
  complete: true,
  mind_name: "TestMind",
  mind_id: "test-mind",
  provider_configured: true,
  default_provider: "anthropic",
  default_model: "claude-3",
  ollama_available: false,
  ollama_models: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  __resetResolvedMindIdCacheForTests();
  // Pre-seed the singleton so the hook resolves synchronously to a
  // sane default. Tests that need to drive the hook's resolution
  // path explicitly call ``__resetResolvedMindIdCacheForTests`` then
  // stage the /api/onboarding/state response on the FIFO queue.
  __seedResolvedMindIdForTests(DEFAULT_ONBOARDING_STATE);
});

describe("SettingsPage", () => {
  it("shows loading state initially", () => {
    mockApi.get.mockImplementation(() => new Promise(() => {}));
    render(<SettingsPage />);
    expect(document.querySelector(".animate-spin")).toBeInTheDocument();
  });

  it("renders settings on successful load", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });
  });

  it("renders engine configuration section", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("Engine Configuration")).toBeInTheDocument();
    });
  });

  // ── TASK-200: Credibility sweep — removed placeholders ──

  it("does NOT render Channels placeholder card", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });
    expect(screen.queryByText("Channels")).not.toBeInTheDocument();
  });

  it("does NOT render API Keys placeholder card", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });
    expect(screen.queryByText("API Keys")).not.toBeInTheDocument();
  });

  it("does NOT render Plugins placeholder card", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });
    expect(screen.queryByText("Plugins")).not.toBeInTheDocument();
  });

  it("does NOT render Webhooks placeholder card", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });
    expect(screen.queryByText("Webhooks")).not.toBeInTheDocument();
  });

  it("renders functional Export / Import section with action buttons", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });
    // Functional section renders with export/import buttons (via i18n keys)
    expect(screen.getByText("Export / Import")).toBeInTheDocument();
    expect(screen.getByText("Export Mind")).toBeInTheDocument();
    expect(screen.getByText("Import Mind")).toBeInTheDocument();
  });

  // ── Log level controls ──

  it("renders all log level options", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      for (const level of ["DEBUG", "INFO", "WARNING", "ERROR"]) {
        expect(screen.getByText(level)).toBeInTheDocument();
      }
    });
  });

  // ── Mind config sections ──

  it("renders mind identity when mind config is loaded", async () => {
    mockApi.get
      .mockResolvedValueOnce(mockSettings)
      .mockResolvedValueOnce(mockMindConfig);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByDisplayValue("TestMind")).toBeInTheDocument();
    });
    expect(screen.getByText("Mind Identity")).toBeInTheDocument();
  });

  it("renders personality tone selector when mind config is loaded", async () => {
    mockApi.get
      .mockResolvedValueOnce(mockSettings)
      .mockResolvedValueOnce(mockMindConfig);
    render(<SettingsPage />);
    await waitFor(() => {
      // Tones rendered via i18n; some labels may appear in multiple contexts
      // (e.g. "Direct" as tone AND "Playful" as trait high label)
      for (const tone of ["warm", "neutral", "direct", "playful"]) {
        expect(screen.getAllByText(new RegExp(tone, "i")).length).toBeGreaterThanOrEqual(1);
      }
    });
  });

  it("renders safety guardrails when mind config is loaded", async () => {
    mockApi.get
      .mockResolvedValueOnce(mockSettings)
      .mockResolvedValueOnce(mockMindConfig);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("Child Safe Mode")).toBeInTheDocument();
      expect(screen.getByText("Financial Confirmation")).toBeInTheDocument();
    });
  });

  it("shows no-mind warning when mind config returns 503", async () => {
    const err503 = Object.assign(new Error("Service Unavailable"), { status: 503 });
    mockApi.get
      .mockResolvedValueOnce(mockSettings)
      .mockRejectedValueOnce(err503);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("No Mind Loaded")).toBeInTheDocument();
    });
  });

  // ── v0.31.7 T2.2 — anti-pattern #35 closure (5th occurrence) ──
  //
  // SettingsPage now fetches /api/onboarding/state to resolve the
  // active mind_id and threads it into <RecalibrateButton mindId={...} />.
  // Pre-v0.31.7 RecalibrateButton defaulted ``mindId="default"`` — a
  // sentinel value that on a real ``meu-mind`` daemon would land the
  // calibration profile at <data_dir>/default/ instead of
  // <data_dir>/meu-mind/. The backend resolver is the safety net but
  // should never need to fire.

  it("threads resolved mindId from /api/onboarding/state to RecalibrateButton", async () => {
    // v0.32.0 BT.B.1: pre-seed the singleton with a real mind_id so
    // the hook resolves to ``meu-mind`` synchronously. The page
    // itself no longer fetches /api/onboarding/state — the hook does.
    __resetResolvedMindIdCacheForTests();
    __seedResolvedMindIdForTests({
      complete: true,
      mind_name: "Real Mind",
      mind_id: "meu-mind",
      provider_configured: true,
      default_provider: "anthropic",
      default_model: "claude-3",
      ollama_available: false,
      ollama_models: [],
    });
    mockApi.get
      .mockResolvedValueOnce(mockSettings)
      .mockResolvedValueOnce(mockMindConfig)
      .mockResolvedValueOnce({
        confirmation_method: "inline",
        confirmation_channels: [],
        classification_fallback: "ask",
      });

    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });

    // The page no longer fetches /api/onboarding/state directly.
    // The shared singleton owns that path; this assertion guards
    // against a regression where someone re-adds the duplicate.
    const onboardingFetch = mockApi.get.mock.calls.find(
      (c) => (c[0] as string) === "/api/onboarding/state",
    );
    expect(onboardingFetch).toBeUndefined();
  });

  // v0.32.0 BT.B.1 — the warn breadcrumb is now fired by the shared
  // ``useResolvedMindId`` hook (singleton), not by the page itself.
  // Message format moved from ``[settings] RecalibrateButton: ...``
  // to ``[useResolvedMindId] resolved mind_id is unavailable ...``.
  // Tests drive the hook's resolution by calling
  // ``__resetResolvedMindIdCacheForTests`` (so the hook re-runs its
  // fetch) and ALSO staging the /api/onboarding/state response as
  // the FIRST item on the FIFO queue — the hook's fetch runs as a
  // side effect of mount + before the page's own settings fetch.

  it("warns once when /api/onboarding/state returns null mind_id", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      // Reset so the singleton actually fires the hook fetch + observes
      // the staged response. The mock queue order: hook reads first
      // (subscribe happens during render), then the page's effects
      // run (settings → config → safety).
      __resetResolvedMindIdCacheForTests();
      mockApi.get
        .mockResolvedValueOnce({
          complete: false,
          mind_name: "Sovyx",
          mind_id: null,
          provider_configured: false,
          default_provider: "",
          default_model: "",
          ollama_available: false,
          ollama_models: [],
        })
        .mockResolvedValueOnce(mockSettings)
        .mockResolvedValueOnce(mockMindConfig)
        .mockResolvedValueOnce({
          confirmation_method: "inline",
          confirmation_channels: [],
          classification_fallback: "ask",
        });

      render(<SettingsPage />);
      await waitFor(() => {
        expect(screen.getByText("INFO")).toBeInTheDocument();
      });

      // The single-fire warn breadcrumb should fire exactly once.
      await waitFor(() => {
        const matches = warnSpy.mock.calls.filter((args) =>
          (args[0] as string).includes("[useResolvedMindId]"),
        );
        expect(matches.length).toBe(1);
        expect(matches[0]![0]).toContain('"default"');
      });
    } finally {
      warnSpy.mockRestore();
    }
  });

  it("does NOT warn when /api/onboarding/state yields a real mind_id", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      // Reset + stage onboarding-state FIRST in the queue so the hook
      // fetch consumes the real-mind response.
      __resetResolvedMindIdCacheForTests();
      mockApi.get
        .mockResolvedValueOnce({
          complete: true,
          mind_name: "Real Mind",
          mind_id: "meu-mind",
          provider_configured: true,
          default_provider: "anthropic",
          default_model: "claude-3",
          ollama_available: false,
          ollama_models: [],
        })
        .mockResolvedValueOnce(mockSettings)
        .mockResolvedValueOnce(mockMindConfig)
        .mockResolvedValueOnce({
          confirmation_method: "inline",
          confirmation_channels: [],
          classification_fallback: "ask",
        });

      render(<SettingsPage />);
      await waitFor(() => {
        expect(screen.getByText("INFO")).toBeInTheDocument();
      });

      // Allow the singleton fetch to settle. With a real mind id
      // resolved, the warn breadcrumb must NOT fire.
      await new Promise((r) => setTimeout(r, 50));
      const matches = warnSpy.mock.calls.filter((args) =>
        (args[0] as string).includes("[useResolvedMindId]"),
      );
      expect(matches.length).toBe(0);
    } finally {
      warnSpy.mockRestore();
    }
  });

  // ── Zero "Coming in v1.0" on page ──

  it("does NOT contain any 'Coming in v1.0' text", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });
    expect(screen.queryByText(/Coming in v1\.0/)).not.toBeInTheDocument();
  });

  // ── Interaction tests — actual state + save flow ──

  it("selecting a log level toggles the active-state styling", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });

    // DEBUG is not the current level — clicking it should mark it active.
    const debugButton = screen.getByText("DEBUG").closest("button");
    expect(debugButton).not.toBeNull();
    fireEvent.click(debugButton!);
    // Active buttons use the brand-primary background token.
    expect(debugButton!.className).toContain("brand-primary");
  });

  it("clicking a tone preset flips the highlighted tone button", async () => {
    mockApi.get
      .mockResolvedValueOnce(mockSettings)
      .mockResolvedValueOnce(mockMindConfig);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByDisplayValue("TestMind")).toBeInTheDocument();
    });

    // Locate the tone row buttons. "warm" is a preset different from the
    // current "neutral" state — after the click it should carry the
    // active brand-primary style.
    const warmButton = screen
      .getAllByText(/warm/i)
      .map((n) => n.closest("button"))
      .find((b): b is HTMLButtonElement => b !== null);
    expect(warmButton).toBeDefined();
    fireEvent.click(warmButton!);
    expect(warmButton!.className).toContain("brand-primary");
  });

  it("save-settings button triggers PUT /api/settings", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    mockApi.put.mockResolvedValueOnce({ ok: true, changes: {} });
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });

    // Select a different log level to mark the form dirty, then click save.
    fireEvent.click(screen.getByText("DEBUG").closest("button")!);
    const saveButtons = screen.getAllByRole("button", { name: /save/i });
    expect(saveButtons.length).toBeGreaterThan(0);
    fireEvent.click(saveButtons[0]!);
    await waitFor(() => {
      expect(mockApi.put).toHaveBeenCalledWith(
        "/api/settings",
        expect.objectContaining({ log_level: "DEBUG" }),
      );
    });
  });

  // ── BT.B.3 (v0.32.0) — SigningKeyCard wired into Settings page ──
  //
  // Note: dedicated unit tests for the card itself live in
  // ``src/components/settings/signing-key-card.test.tsx``. We only
  // need a single smoke test here to confirm the card mounts inside
  // the page tree (alongside the other voice cards). Adding more page-
  // level tests that mount ``SettingsPage`` would consume mocks from
  // the queue and pollute the ``useResolvedMindId`` singleton state
  // across tests, breaking earlier ``threads resolved mindId`` cases.

  it("mounts the Voice signing key card alongside the rollback card", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });
    // Stable test ids — survive i18n copy edits + section reorders.
    expect(
      screen.getByTestId("settings-signing-key-card"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("settings-rollback-card"),
    ).toBeInTheDocument();
    // Status row renders (proves the card finished its initial render
    // without throwing despite the api.get mock being undefined).
    expect(
      screen.getByTestId("settings-signing-key-status"),
    ).toBeInTheDocument();
  });
});
