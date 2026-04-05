import type { StateCreator } from "zustand";
import type { Settings } from "@/types/api";
import type { DashboardState } from "../dashboard";

export interface SettingsSlice {
  settings: Settings | null;
  setSettings: (s: Settings) => void;
}

export const createSettingsSlice: StateCreator<
  DashboardState,
  [],
  [],
  SettingsSlice
> = (set) => ({
  settings: null,
  setSettings: (s) => set({ settings: s }),
});
