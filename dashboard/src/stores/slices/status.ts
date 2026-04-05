import type { StateCreator } from "zustand";
import type { SystemStatus, HealthCheck } from "@/types/api";
import type { DashboardState } from "../dashboard";

export interface StatusSlice {
  status: SystemStatus | null;
  setStatus: (s: SystemStatus) => void;
  healthChecks: HealthCheck[];
  setHealthChecks: (checks: HealthCheck[]) => void;
}

export const createStatusSlice: StateCreator<
  DashboardState,
  [],
  [],
  StatusSlice
> = (set) => ({
  status: null,
  setStatus: (s) => set({ status: s }),
  healthChecks: [],
  setHealthChecks: (checks) => set({ healthChecks: checks }),
});
