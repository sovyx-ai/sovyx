import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import { PluginDetailPanel } from "./plugin-detail";
import type { PluginDetail } from "@/types/api";

const mockFetchPluginDetail = vi.fn(async () => {});
let mockState: Record<string, unknown> = {};

vi.mock("@/stores/dashboard", () => ({
  useDashboardStore: (selector: (s: Record<string, unknown>) => unknown) =>
    typeof selector === "function" ? selector(mockState) : mockState,
}));

function setState(detail: PluginDetail | null, loading = false) {
  mockState = {
    pluginDetail: detail,
    pluginDetailLoading: loading,
    fetchPluginDetail: mockFetchPluginDetail,
    enablePlugin: vi.fn(async () => true),
    disablePlugin: vi.fn(async () => true),
    reloadPlugin: vi.fn(async () => true),
  };
}

function makeDetail(overrides: Partial<PluginDetail> = {}): PluginDetail {
  return {
    name: "finance",
    version: "1.0.0",
    description: "Finance plugin",
    status: "active",
    tools: [],
    permissions: [],
    health: { consecutive_failures: 0, disabled: false, last_error: "", active_tasks: 0 },
    manifest: {},
    ...overrides,
  };
}

beforeEach(() => {
  mockFetchPluginDetail.mockClear();
  setState(null);
});

describe("PluginDetailPanel", () => {
  it("renders nothing visible when closed", () => {
    render(<PluginDetailPanel pluginName="finance" open={false} onClose={() => {}} />);
    expect(screen.queryByText("finance")).not.toBeInTheDocument();
  });

  it("fetches the plugin detail when opened", () => {
    render(<PluginDetailPanel pluginName="finance" open={true} onClose={() => {}} />);
    expect(mockFetchPluginDetail).toHaveBeenCalledWith("finance");
  });

  it("renders the plugin name and version when detail is loaded", () => {
    setState(makeDetail());
    render(<PluginDetailPanel pluginName="finance" open={true} onClose={() => {}} />);
    expect(screen.getByText("finance")).toBeInTheDocument();
    expect(screen.getByText("v1.0.0")).toBeInTheDocument();
  });

  it("shows spinner while loading without a cached detail", () => {
    setState(null, true);
    render(<PluginDetailPanel pluginName="finance" open={true} onClose={() => {}} />);
    // Sheet renders into a portal — query document, not the local container
    expect(document.querySelector(".animate-spin")).not.toBeNull();
  });
});
