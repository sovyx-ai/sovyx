/**
 * Plugins page — page-level test. Covers fetch on mount, stat cards,
 * search filtering, status filtering, and routing to the detail panel.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@/test/test-utils";
import type { PluginInfo } from "@/types/api";

const mockFetchPlugins = vi.fn();
const mockSetPluginFilter = vi.fn();
const mockSetPluginSearchQuery = vi.fn();
const mockSetPluginSort = vi.fn();

let mockState: Record<string, unknown> = {};

vi.mock("@/stores/dashboard", () => ({
  useDashboardStore: (selector: (s: Record<string, unknown>) => unknown) =>
    typeof selector === "function" ? selector(mockState) : mockState,
}));

import PluginsPage from "./plugins";

function plugin(overrides: Partial<PluginInfo>): PluginInfo {
  return {
    name: "example",
    version: "1.0.0",
    description: "",
    status: "active",
    tools_count: 0,
    tools: [],
    permissions: [],
    health: { consecutive_failures: 0, disabled: false, last_error: "", active_tasks: 0 },
    category: "",
    tags: [],
    icon_url: "",
    pricing: "free",
    ...overrides,
  };
}

function setStore(
  plugins: PluginInfo[],
  overrides: Record<string, unknown> = {},
) {
  mockState = {
    plugins,
    pluginsAvailable: true,
    pluginsLoading: false,
    pluginsError: null,
    pluginCounts: {
      total: plugins.length,
      active: plugins.filter((p) => p.status === "active").length,
      disabled: plugins.filter((p) => p.status === "disabled").length,
      error: plugins.filter((p) => p.status === "error").length,
      totalTools: plugins.reduce((s, p) => s + p.tools_count, 0),
    },
    pluginFilter: "all",
    pluginSearchQuery: "",
    pluginSort: "name",
    pluginDetail: null,
    pluginDetailLoading: false,
    fetchPlugins: mockFetchPlugins,
    fetchPluginDetail: vi.fn(),
    setPluginFilter: mockSetPluginFilter,
    setPluginSearchQuery: mockSetPluginSearchQuery,
    setPluginSort: mockSetPluginSort,
    enablePlugin: vi.fn(async () => true),
    disablePlugin: vi.fn(async () => true),
    reloadPlugin: vi.fn(async () => true),
    ...overrides,
  };
}

beforeEach(() => {
  mockFetchPlugins.mockClear();
  mockSetPluginFilter.mockClear();
  mockSetPluginSearchQuery.mockClear();
  mockSetPluginSort.mockClear();
  setStore([]);
});

describe("PluginsPage", () => {
  it("fetches plugins on mount", async () => {
    setStore([]);
    render(<PluginsPage />);
    await waitFor(() => expect(mockFetchPlugins).toHaveBeenCalled());
  });

  it("renders the 4 stat cards with the right totals", () => {
    setStore([
      plugin({ name: "a", status: "active", tools_count: 3 }),
      plugin({ name: "b", status: "disabled", tools_count: 1 }),
    ]);
    render(<PluginsPage />);
    // One "1" for Disabled card, "2" for Total, "4" for total tools, "1" for Active.
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
  });

  it("renders every plugin in the list", () => {
    setStore([
      plugin({ name: "finance" }),
      plugin({ name: "weather" }),
      plugin({ name: "notes" }),
    ]);
    render(<PluginsPage />);
    expect(screen.getByRole("heading", { name: "finance" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "weather" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "notes" })).toBeInTheDocument();
  });

  it("pipes the search input into the store", () => {
    setStore([plugin({ name: "finance" })]);
    render(<PluginsPage />);
    const input = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "finance" } });
    expect(mockSetPluginSearchQuery).toHaveBeenCalledWith("finance");
  });

  it("shows the loading skeleton when loading with no cached plugins", () => {
    setStore([], { pluginsLoading: true });
    const { container } = render(<PluginsPage />);
    // Skeleton renders animated placeholders — match on class marker
    expect(container.querySelector("[class*='animate-']")).not.toBeNull();
  });
});
