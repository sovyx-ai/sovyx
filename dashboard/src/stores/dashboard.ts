/**
 * Global dashboard store — zustand v5 slices pattern
 *
 * FE-00c: Refactored from monolithic to 6 slices:
 *   connection, status, conversations, brain, logs, settings
 *
 * Each slice is in stores/slices/ for separation of concerns.
 * Combined into one bounded store with devtools middleware.
 */
import { create } from "zustand";
import { devtools } from "zustand/middleware";

import {
  createConnectionSlice,
  type ConnectionSlice,
} from "./slices/connection";
import { createStatusSlice, type StatusSlice } from "./slices/status";
import {
  createConversationsSlice,
  type ConversationsSlice,
} from "./slices/conversations";
import { createBrainSlice, type BrainSlice } from "./slices/brain";
import { createLogsSlice, type LogsSlice } from "./slices/logs";
import { createSettingsSlice, type SettingsSlice } from "./slices/settings";

export type DashboardState = ConnectionSlice &
  StatusSlice &
  ConversationsSlice &
  BrainSlice &
  LogsSlice &
  SettingsSlice;

export const useDashboardStore = create<DashboardState>()(
  devtools(
    (...a) => ({
      ...createConnectionSlice(...a),
      ...createStatusSlice(...a),
      ...createConversationsSlice(...a),
      ...createBrainSlice(...a),
      ...createLogsSlice(...a),
      ...createSettingsSlice(...a),
    }),
    { name: "sovyx-dashboard" },
  ),
);
