import type { StateCreator } from "zustand";
import type { DashboardState } from "../dashboard";

export interface ConnectionSlice {
  connected: boolean;
  setConnected: (v: boolean) => void;
}

export const createConnectionSlice: StateCreator<
  DashboardState,
  [],
  [],
  ConnectionSlice
> = (set) => ({
  connected: false,
  setConnected: (v) => set({ connected: v }),
});
