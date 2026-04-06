/**
 * Connection slice — tracks WebSocket connection state.
 *
 * POLISH-11: Added "reconnecting" state for visual feedback.
 *
 * States: connected (online) → disconnected → reconnecting → connected
 */

import type { StateCreator } from "zustand";
import type { DashboardState } from "../dashboard";

export type ConnectionState = "connected" | "disconnected" | "reconnecting";

export interface ConnectionSlice {
  connected: boolean;
  connectionState: ConnectionState;
  setConnected: (v: boolean) => void;
  setConnectionState: (state: ConnectionState) => void;
}

export const createConnectionSlice: StateCreator<
  DashboardState,
  [],
  [],
  ConnectionSlice
> = (set) => ({
  connected: false,
  connectionState: "disconnected",
  setConnected: (v) => set({ connected: v, connectionState: v ? "connected" : "disconnected" }),
  setConnectionState: (state) => set({ connectionState: state, connected: state === "connected" }),
});
