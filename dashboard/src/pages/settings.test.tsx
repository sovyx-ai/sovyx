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
import { render, screen, waitFor } from "@/test/test-utils";
import SettingsPage from "./settings";

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

beforeEach(() => {
  vi.clearAllMocks();
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

  it("retains Export / Import placeholder for future implementation", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });
    expect(screen.getByText("Export / Import")).toBeInTheDocument();
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

  // ── Zero "Coming in v1.0" on page ──

  it("does NOT contain any 'Coming in v1.0' text", async () => {
    mockApi.get.mockResolvedValueOnce(mockSettings);
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByText("INFO")).toBeInTheDocument();
    });
    expect(screen.queryByText(/Coming in v1\.0/)).not.toBeInTheDocument();
  });
});
