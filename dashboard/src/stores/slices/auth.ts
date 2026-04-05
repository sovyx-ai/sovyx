import type { StateCreator } from "zustand";
import type { DashboardState } from "../dashboard";

export interface AuthSlice {
  /** Whether a valid token exists and has been verified */
  authenticated: boolean;
  setAuthenticated: (v: boolean) => void;
  /** Show the token entry modal */
  showTokenModal: boolean;
  setShowTokenModal: (v: boolean) => void;
}

export const createAuthSlice: StateCreator<
  DashboardState,
  [],
  [],
  AuthSlice
> = (set) => ({
  authenticated: false,
  setAuthenticated: (v) => set({ authenticated: v }),
  showTokenModal: false,
  setShowTokenModal: (v) => set({ showTokenModal: v }),
});
