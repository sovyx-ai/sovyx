/**
 * Settings page tests — POLISH-16.
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
});
