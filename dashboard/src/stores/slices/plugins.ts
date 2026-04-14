/**
 * Plugin slice — zustand store for plugin management.
 *
 * State: plugin list, selected plugin, filters, loading/error.
 * Actions: fetch, enable, disable, reload, filter, search.
 * WebSocket: handlePluginEvent for real-time state updates.
 *
 * TASK-452
 */
import type { StateCreator } from "zustand";
import type {
  PluginInfo,
  PluginDetail,
  PluginStatus,
  PluginsResponse,
  PluginActionResponse,
  PluginStateChangedEvent,
} from "@/types/api";
import type { DashboardState } from "../dashboard";
import { api } from "@/lib/api";
import {
  PluginsResponseSchema,
  PluginDetailSchema,
  PluginActionResponseSchema,
} from "@/types/schemas";

/** Filter values for the plugin list */
export type PluginFilter = "all" | "active" | "disabled" | "error";

/** Sort options for the plugin list */
export type PluginSort = "name" | "status" | "tools";

export interface PluginsSlice {
  // ── State ──
  plugins: PluginInfo[];
  pluginsAvailable: boolean;
  pluginDetail: PluginDetail | null;
  pluginsLoading: boolean;
  pluginDetailLoading: boolean;
  pluginsError: string | null;
  pluginFilter: PluginFilter;
  pluginSearchQuery: string;
  pluginSort: PluginSort;
  pluginCounts: {
    total: number;
    active: number;
    disabled: number;
    error: number;
    totalTools: number;
  };

  // ── Actions ──
  fetchPlugins: () => Promise<void>;
  fetchPluginDetail: (name: string) => Promise<void>;
  enablePlugin: (name: string) => Promise<boolean>;
  disablePlugin: (name: string) => Promise<boolean>;
  reloadPlugin: (name: string) => Promise<boolean>;
  setPluginFilter: (filter: PluginFilter) => void;
  setPluginSearchQuery: (query: string) => void;
  setPluginSort: (sort: PluginSort) => void;
  clearPluginDetail: () => void;

  // ── WebSocket ──
  handlePluginEvent: (
    type: string,
    data: PluginStateChangedEvent,
  ) => void;
}

export const createPluginsSlice: StateCreator<
  DashboardState,
  [],
  [],
  PluginsSlice
> = (set, get) => ({
  // ── Initial State ──
  plugins: [],
  pluginsAvailable: false,
  pluginDetail: null,
  pluginsLoading: false,
  pluginDetailLoading: false,
  pluginsError: null,
  pluginFilter: "all",
  pluginSearchQuery: "",
  pluginSort: "name",
  pluginCounts: {
    total: 0,
    active: 0,
    disabled: 0,
    error: 0,
    totalTools: 0,
  },

  // ── Fetch all plugins ──
  fetchPlugins: async () => {
    set({ pluginsLoading: true, pluginsError: null });
    try {
      const data = await api.get<PluginsResponse>("/api/plugins", {
        schema: PluginsResponseSchema,
      });
      set({
        plugins: data.plugins,
        pluginsAvailable: data.available,
        pluginsLoading: false,
        pluginCounts: {
          total: data.total,
          active: data.active,
          disabled: data.disabled,
          error: data.error,
          totalTools: data.total_tools,
        },
      });
    } catch {
      set({ pluginsLoading: false, pluginsError: "Failed to load plugins" });
    }
  },

  // ── Fetch single plugin detail ──
  fetchPluginDetail: async (name: string) => {
    set({ pluginDetailLoading: true });
    try {
      const data = await api.get<PluginDetail>(`/api/plugins/${name}`, {
        schema: PluginDetailSchema,
      });
      set({ pluginDetail: data, pluginDetailLoading: false });
    } catch {
      set({ pluginDetailLoading: false, pluginDetail: null });
    }
  },

  // ── Enable plugin (optimistic) ──
  enablePlugin: async (name: string) => {
    const prev = get().plugins;
    // Optimistic update
    set({
      plugins: prev.map((p) =>
        p.name === name ? { ...p, status: "active" as PluginStatus } : p,
      ),
    });

    try {
      await api.post<PluginActionResponse>(`/api/plugins/${name}/enable`, undefined, {
        schema: PluginActionResponseSchema,
      });
      // Refetch for accurate counts
      void get().fetchPlugins();
      return true;
    } catch {
      // Rollback
      set({ plugins: prev });
      return false;
    }
  },

  // ── Disable plugin (optimistic) ──
  disablePlugin: async (name: string) => {
    const prev = get().plugins;
    set({
      plugins: prev.map((p) =>
        p.name === name
          ? { ...p, status: "disabled" as PluginStatus }
          : p,
      ),
    });

    try {
      await api.post<PluginActionResponse>(
        `/api/plugins/${name}/disable`,
        undefined,
        { schema: PluginActionResponseSchema },
      );
      void get().fetchPlugins();
      return true;
    } catch {
      set({ plugins: prev });
      return false;
    }
  },

  // ── Reload plugin ──
  reloadPlugin: async (name: string) => {
    try {
      await api.post<PluginActionResponse>(
        `/api/plugins/${name}/reload`,
        undefined,
        { schema: PluginActionResponseSchema },
      );
      void get().fetchPlugins();
      // Refresh detail if viewing this plugin
      const detail = get().pluginDetail;
      if (detail?.name === name) {
        void get().fetchPluginDetail(name);
      }
      return true;
    } catch {
      return false;
    }
  },

  // ── Filters ──
  setPluginFilter: (filter: PluginFilter) => set({ pluginFilter: filter }),
  setPluginSearchQuery: (query: string) =>
    set({ pluginSearchQuery: query }),
  setPluginSort: (sort: PluginSort) => set({ pluginSort: sort }),
  clearPluginDetail: () => set({ pluginDetail: null }),

  // ── WebSocket event handler ──
  handlePluginEvent: (type: string, data: PluginStateChangedEvent) => {
    const { plugins } = get();
    const pluginName = data.plugin_name;

    if (type === "PluginStateChanged") {
      const newStatus =
        data.to_state === "active"
          ? "active"
          : data.to_state === "disabled"
            ? "disabled"
            : "error";

      set({
        plugins: plugins.map((p) =>
          p.name === pluginName
            ? { ...p, status: newStatus as PluginStatus }
            : p,
        ),
      });

      // Update detail if open
      const detail = get().pluginDetail;
      if (detail?.name === pluginName) {
        set({
          pluginDetail: {
            ...detail,
            status: newStatus as PluginStatus,
          },
        });
      }
    }

    if (type === "PluginAutoDisabled") {
      set({
        plugins: plugins.map((p) =>
          p.name === pluginName
            ? {
                ...p,
                status: "disabled" as PluginStatus,
                health: { ...p.health, disabled: true },
              }
            : p,
        ),
      });
    }
  },
});
