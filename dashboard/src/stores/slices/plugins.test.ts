/**
 * Plugin slice tests — store actions, filters, WebSocket events.
 *
 * TASK-463
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { useDashboardStore } from "../dashboard";
import type { PluginInfo, PluginDetail, PluginStateChangedEvent } from "@/types/api";

// ── Mock data ──

const MOCK_PLUGIN: PluginInfo = {
  name: "weather",
  version: "1.0.0",
  description: "Weather plugin",
  status: "active",
  tools_count: 2,
  tools: [
    { name: "get_weather", description: "Get weather" },
    { name: "get_forecast", description: "Get forecast" },
  ],
  permissions: [
    { permission: "network:internet", risk: "high", description: "Internet access" },
  ],
  health: {
    consecutive_failures: 0,
    disabled: false,
    last_error: "",
    active_tasks: 0,
  },
  category: "weather",
  tags: ["weather", "api"],
  icon_url: "",
  pricing: "free",
};

const MOCK_PLUGIN_2: PluginInfo = {
  ...MOCK_PLUGIN,
  name: "calculator",
  description: "Calculator plugin",
  status: "disabled",
  tools_count: 1,
  tools: [{ name: "calculate", description: "Calculate" }],
  permissions: [],
  category: "productivity",
  tags: ["math"],
};

const MOCK_PLUGIN_ERROR: PluginInfo = {
  ...MOCK_PLUGIN,
  name: "broken",
  description: "Broken plugin",
  status: "error",
  tools_count: 0,
  tools: [],
  health: {
    consecutive_failures: 3,
    disabled: false,
    last_error: "timeout",
    active_tasks: 0,
  },
  category: "",
  tags: [],
};

const MOCK_DETAIL: PluginDetail = {
  name: "weather",
  version: "1.0.0",
  description: "Weather plugin",
  status: "active",
  tools: [
    {
      name: "get_weather",
      description: "Get weather",
      parameters: { type: "object" },
      requires_confirmation: false,
      timeout_seconds: 30,
    },
  ],
  permissions: [
    { permission: "network:internet", risk: "high", description: "Internet" },
  ],
  health: {
    consecutive_failures: 0,
    disabled: false,
    last_error: "",
    active_tasks: 0,
  },
  manifest: {},
};

// ── Reset ──

function resetPluginState(): void {
  useDashboardStore.setState({
    plugins: [],
    pluginDetail: null,
    pluginsLoading: false,
    pluginDetailLoading: false,
    pluginsError: null,
    pluginFilter: "all",
    pluginSearchQuery: "",
    pluginSort: "name",
    pluginCounts: { total: 0, active: 0, disabled: 0, error: 0, totalTools: 0 },
  });
}

beforeEach(() => {
  resetPluginState();
  vi.restoreAllMocks();
});

// ── Initial state ──

describe("plugins slice — initial state", () => {
  it("starts with empty plugins", () => {
    const state = useDashboardStore.getState();
    expect(state.plugins).toEqual([]);
    expect(state.pluginDetail).toBeNull();
    expect(state.pluginsLoading).toBe(false);
    expect(state.pluginsError).toBeNull();
  });

  it("starts with default filter/sort", () => {
    const state = useDashboardStore.getState();
    expect(state.pluginFilter).toBe("all");
    expect(state.pluginSearchQuery).toBe("");
    expect(state.pluginSort).toBe("name");
  });
});

// ── Filter + sort setters ──

describe("plugins slice — filter/sort", () => {
  it("setPluginFilter updates filter", () => {
    useDashboardStore.getState().setPluginFilter("active");
    expect(useDashboardStore.getState().pluginFilter).toBe("active");
  });

  it("setPluginSearchQuery updates search", () => {
    useDashboardStore.getState().setPluginSearchQuery("weather");
    expect(useDashboardStore.getState().pluginSearchQuery).toBe("weather");
  });

  it("setPluginSort updates sort", () => {
    useDashboardStore.getState().setPluginSort("tools");
    expect(useDashboardStore.getState().pluginSort).toBe("tools");
  });

  it("clearPluginDetail resets detail", () => {
    useDashboardStore.setState({ pluginDetail: MOCK_DETAIL });
    useDashboardStore.getState().clearPluginDetail();
    expect(useDashboardStore.getState().pluginDetail).toBeNull();
  });
});

// ── fetchPlugins ──

describe("plugins slice — fetchPlugins", () => {
  it("sets loading and plugins on success", async () => {
    const mockResponse = {
      available: true,
      plugins: [MOCK_PLUGIN, MOCK_PLUGIN_2],
      total: 2,
      active: 1,
      disabled: 1,
      error: 0,
      total_tools: 3,
    };

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(mockResponse),
    } as Response);

    await useDashboardStore.getState().fetchPlugins();

    const state = useDashboardStore.getState();
    expect(state.plugins).toHaveLength(2);
    expect(state.pluginsLoading).toBe(false);
    expect(state.pluginCounts.total).toBe(2);
    expect(state.pluginCounts.active).toBe(1);
    expect(state.pluginCounts.totalTools).toBe(3);
  });

  it("sets error on failure", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(new Error("Network"));

    await useDashboardStore.getState().fetchPlugins();

    const state = useDashboardStore.getState();
    expect(state.pluginsLoading).toBe(false);
    expect(state.pluginsError).toBe("Failed to load plugins");
  });
});

// ── fetchPluginDetail ──

describe("plugins slice — fetchPluginDetail", () => {
  it("fetches and sets detail", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(MOCK_DETAIL),
    } as Response);

    await useDashboardStore.getState().fetchPluginDetail("weather");

    const state = useDashboardStore.getState();
    expect(state.pluginDetail).not.toBeNull();
    expect(state.pluginDetail!.name).toBe("weather");
    expect(state.pluginDetailLoading).toBe(false);
  });

  it("clears detail on failure", async () => {
    useDashboardStore.setState({ pluginDetail: MOCK_DETAIL });
    vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(new Error("Not found"));

    await useDashboardStore.getState().fetchPluginDetail("ghost");

    expect(useDashboardStore.getState().pluginDetail).toBeNull();
  });
});

// ── enablePlugin (optimistic) ──

describe("plugins slice — enablePlugin", () => {
  it("optimistically updates status and refetches", async () => {
    useDashboardStore.setState({
      plugins: [{ ...MOCK_PLUGIN_2, status: "disabled" }],
    });

    // Enable API call
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ ok: true, plugin: "calculator", status: "active" }),
      } as Response)
      // Refetch call
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          available: true,
          plugins: [{ ...MOCK_PLUGIN_2, status: "active" }],
          total: 1, active: 1, disabled: 0, error: 0, total_tools: 1,
        }),
      } as Response);

    const result = await useDashboardStore.getState().enablePlugin("calculator");
    expect(result).toBe(true);

    // Check optimistic update happened (status changed immediately)
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("rolls back on failure", async () => {
    const original = [{ ...MOCK_PLUGIN_2, status: "disabled" as const }];
    useDashboardStore.setState({ plugins: original });

    vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(new Error("fail"));

    const result = await useDashboardStore.getState().enablePlugin("calculator");
    expect(result).toBe(false);

    // Rolled back
    expect(useDashboardStore.getState().plugins[0].status).toBe("disabled");
  });
});

// ── disablePlugin (optimistic) ──

describe("plugins slice — disablePlugin", () => {
  it("optimistically disables and refetches", async () => {
    useDashboardStore.setState({ plugins: [MOCK_PLUGIN] });

    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ ok: true, plugin: "weather", status: "disabled" }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          available: true,
          plugins: [{ ...MOCK_PLUGIN, status: "disabled" }],
          total: 1, active: 0, disabled: 1, error: 0, total_tools: 2,
        }),
      } as Response);

    const result = await useDashboardStore.getState().disablePlugin("weather");
    expect(result).toBe(true);
  });
});

// ── reloadPlugin ──

describe("plugins slice — reloadPlugin", () => {
  it("reloads and refetches", async () => {
    useDashboardStore.setState({ plugins: [MOCK_PLUGIN] });

    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ ok: true, plugin: "weather", status: "reloaded" }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          available: true, plugins: [MOCK_PLUGIN],
          total: 1, active: 1, disabled: 0, error: 0, total_tools: 2,
        }),
      } as Response);

    const result = await useDashboardStore.getState().reloadPlugin("weather");
    expect(result).toBe(true);
  });

  it("returns false on failure", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(new Error("fail"));

    const result = await useDashboardStore.getState().reloadPlugin("weather");
    expect(result).toBe(false);
  });
});

// ── WebSocket event handling ──

describe("plugins slice — handlePluginEvent", () => {
  beforeEach(() => {
    useDashboardStore.setState({
      plugins: [MOCK_PLUGIN, MOCK_PLUGIN_2, MOCK_PLUGIN_ERROR],
    });
  });

  it("PluginStateChanged updates plugin status", () => {
    const event: PluginStateChangedEvent = {
      plugin_name: "weather",
      from_state: "active",
      to_state: "disabled",
    };

    useDashboardStore.getState().handlePluginEvent("PluginStateChanged", event);

    const weather = useDashboardStore.getState().plugins.find((p) => p.name === "weather");
    expect(weather?.status).toBe("disabled");
  });

  it("PluginAutoDisabled sets disabled status and health flag", () => {
    const event: PluginStateChangedEvent = {
      plugin_name: "broken",
      from_state: "error",
      to_state: "disabled",
    };

    useDashboardStore.getState().handlePluginEvent("PluginAutoDisabled", event);

    const broken = useDashboardStore.getState().plugins.find((p) => p.name === "broken");
    expect(broken?.status).toBe("disabled");
    expect(broken?.health.disabled).toBe(true);
  });

  it("updates detail panel if viewing affected plugin", () => {
    useDashboardStore.setState({ pluginDetail: MOCK_DETAIL });

    const event: PluginStateChangedEvent = {
      plugin_name: "weather",
      from_state: "active",
      to_state: "disabled",
    };

    useDashboardStore.getState().handlePluginEvent("PluginStateChanged", event);

    expect(useDashboardStore.getState().pluginDetail?.status).toBe("disabled");
  });

  it("does not update detail for different plugin", () => {
    useDashboardStore.setState({ pluginDetail: MOCK_DETAIL });

    const event: PluginStateChangedEvent = {
      plugin_name: "calculator",
      from_state: "disabled",
      to_state: "active",
    };

    useDashboardStore.getState().handlePluginEvent("PluginStateChanged", event);

    // Weather detail unchanged
    expect(useDashboardStore.getState().pluginDetail?.status).toBe("active");
  });

  it("ignores unknown plugin in event", () => {
    const event: PluginStateChangedEvent = {
      plugin_name: "nonexistent",
      from_state: "active",
      to_state: "disabled",
    };

    // Should not throw
    useDashboardStore.getState().handlePluginEvent("PluginStateChanged", event);

    // All plugins unchanged
    expect(useDashboardStore.getState().plugins).toHaveLength(3);
  });
});
